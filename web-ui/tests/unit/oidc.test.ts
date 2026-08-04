import { describe, expect, it } from "vitest";

import { clearAuthState, createCodeChallenge, generateCodeVerifier, millisecondsUntilRefresh, oidcConfigurationError, restoreLogin, saveSession, validateCallbackState, type OidcSession } from "../../src/oidc";

class MemoryStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

describe("OIDC configuration", () => {
  it("reports missing issuer, client ID, and redirect URI settings", () => {
    expect(oidcConfigurationError(undefined, undefined, undefined)).toBe(
      "Sign-in configuration is incomplete. Set VITE_OIDC_ISSUER, VITE_OIDC_CLIENT_ID, VITE_OIDC_REDIRECT_URI.",
    );
  });

  it("accepts a complete environment configuration", () => {
    expect(oidcConfigurationError("https://issuer.example", "muninn-web", "https://app.example/callback")).toBeNull();
  });
});

describe("OIDC PKCE", () => {
  it("generates a URL-safe verifier with the required entropy length", () => {
    expect(generateCodeVerifier()).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("creates the RFC 7636 S256 code challenge", async () => {
    expect(await createCodeChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"))
      .toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });
});

describe("OIDC callback state", () => {
  it("returns the verifier only when the callback state matches and consumes the stored values", () => {
    const storage = new MemoryStorage();
    storage.setItem("muninn.oidc.state", "expected-state");
    storage.setItem("muninn.oidc.verifier", "stored-verifier");

    expect(validateCallbackState(new URLSearchParams("state=expected-state"), storage)).toBe("stored-verifier");
    expect(storage.getItem("muninn.oidc.state")).toBeNull();
    expect(storage.getItem("muninn.oidc.verifier")).toBeNull();
  });

  it("rejects missing or mismatched state and consumes the pending values", () => {
    const storage = new MemoryStorage();
    storage.setItem("muninn.oidc.state", "expected-state");
    storage.setItem("muninn.oidc.verifier", "stored-verifier");

    expect(() => validateCallbackState(new URLSearchParams("state=attacker-state"), storage)).toThrow(/could not be verified/);
    expect(storage.getItem("muninn.oidc.state")).toBeNull();
    expect(storage.getItem("muninn.oidc.verifier")).toBeNull();
    expect(() => validateCallbackState(new URLSearchParams("state=expected-state"), storage)).toThrow(/could not be verified/);
  });
});

describe("OIDC session restoration", () => {
  function token(expiration: number, name = "Restored user"): string {
    const payload = btoa(JSON.stringify({ exp: expiration, name })).replace(/=/g, "");
    return `header.${payload}.signature`;
  }

  function session(accessToken: string, expiresAt: number, refreshToken?: string): OidcSession {
    return {
      session: { accessToken, displayName: "Renewed user", capabilities: new Set() },
      refreshToken,
      expiresAt,
    };
  }

  it("restores an unexpired access token from session storage without using the id token", async () => {
    const now = 1_800_000_000_000;
    const accessToken = token(now / 1000 + 300, "Access-token user");
    const storage = new MemoryStorage();
    storage.setItem("muninn.oidc.access_token", accessToken);
    storage.setItem("muninn.oidc.id_token", token(now / 1000 + 600, "ID-token user"));
    storage.setItem("muninn.oidc.refresh_token", "refresh-token");
    storage.setItem("muninn.oidc.expires_at", String(now + 300_000));
    let refreshCalls = 0;

    const restored = await restoreLogin(storage, async () => {
      refreshCalls += 1;
      throw new Error("Unexpected refresh");
    }, now);

    expect(restored?.session.accessToken).toBe(accessToken);
    expect(restored?.session.displayName).toBe("Access-token user");
    expect(restored?.expiresAt).toBe(now + 300_000);
    expect(refreshCalls).toBe(0);
  });

  it("renews an expired access token and persists the rotated refresh token", async () => {
    const now = 1_800_000_000_000;
    const storage = new MemoryStorage();
    storage.setItem("muninn.oidc.access_token", token(now / 1000 - 1));
    storage.setItem("muninn.oidc.refresh_token", "old-refresh-token");
    storage.setItem("muninn.oidc.expires_at", String(now - 1_000));
    const freshToken = token(now / 1000 + 300);
    let receivedRefreshToken = "";

    const restored = await restoreLogin(storage, async (refreshToken) => {
      receivedRefreshToken = refreshToken;
      return session(freshToken, now + 300_000, "rotated-refresh-token");
    }, now);

    expect(receivedRefreshToken).toBe("old-refresh-token");
    expect(restored?.session.accessToken).toBe(freshToken);
    expect(storage.getItem("muninn.oidc.access_token")).toBe(freshToken);
    expect(storage.getItem("muninn.oidc.refresh_token")).toBe("rotated-refresh-token");
    expect(storage.getItem("muninn.oidc.expires_at")).toBe(String(now + 300_000));
  });

  it("clears an invalid expired token when no refresh token exists", async () => {
    const now = 1_800_000_000_000;
    const storage = new MemoryStorage();
    storage.setItem("muninn.oidc.access_token", token(now / 1000 - 1));
    storage.setItem("muninn.oidc.expires_at", String(now - 1_000));

    expect(await restoreLogin(storage, undefined, now)).toBeNull();
    expect(storage.getItem("muninn.oidc.access_token")).toBeNull();
    expect(storage.getItem("muninn.oidc.expires_at")).toBeNull();
  });

  it("clears stored credentials after a rejected refresh instead of retrying restoration", async () => {
    const now = 1_800_000_000_000;
    const storage = new MemoryStorage();
    storage.setItem("muninn.oidc.refresh_token", "rejected-refresh-token");
    let refreshCalls = 0;

    await expect(restoreLogin(storage, async () => {
      refreshCalls += 1;
      throw new Error("Refresh rejected");
    }, now)).rejects.toThrow("Refresh rejected");

    expect(refreshCalls).toBe(1);
    expect(storage.getItem("muninn.oidc.refresh_token")).toBeNull();
  });

  it("schedules refresh before token expiry and never schedules a negative delay", () => {
    expect(millisecondsUntilRefresh(100_000, 10_000)).toBe(60_000);
    expect(millisecondsUntilRefresh(20_000, 10_000)).toBe(0);
  });
});

describe("OIDC token persistence", () => {
  it("saves rotated tokens to session storage and removes stale refresh and id tokens", () => {
    const storage = new MemoryStorage();
    storage.setItem("muninn.oidc.refresh_token", "old-refresh");
    storage.setItem("muninn.oidc.id_token", "old-id");
    const session: OidcSession = {
      session: { accessToken: "new-access", displayName: "User", capabilities: new Set() },
      expiresAt: 1234,
    };

    saveSession(session, storage);

    expect(storage.getItem("muninn.oidc.access_token")).toBe("new-access");
    expect(storage.getItem("muninn.oidc.refresh_token")).toBeNull();
    expect(storage.getItem("muninn.oidc.id_token")).toBeNull();
    expect(storage.getItem("muninn.oidc.expires_at")).toBe("1234");
  });

  it("clears every token key and pending login value on sign-out", () => {
    const local = new MemoryStorage();
    const session = new MemoryStorage();
    for (const storage of [local, session]) {
      storage.setItem("muninn.oidc.access_token", "access");
      storage.setItem("muninn.oidc.refresh_token", "refresh");
      storage.setItem("muninn.oidc.id_token", "id");
      storage.setItem("muninn.oidc.expires_at", "1");
    }
    session.setItem("muninn.oidc.state", "state");
    session.setItem("muninn.oidc.verifier", "verifier");
    const originalSessionStorage = globalThis.sessionStorage;
    const originalLocalStorage = globalThis.localStorage;
    Object.defineProperty(globalThis, "sessionStorage", { value: session, configurable: true });
    Object.defineProperty(globalThis, "localStorage", { value: local, configurable: true });
    try {
      clearAuthState();
    } finally {
      Object.defineProperty(globalThis, "sessionStorage", { value: originalSessionStorage, configurable: true });
      Object.defineProperty(globalThis, "localStorage", { value: originalLocalStorage, configurable: true });
    }

    for (const storage of [local, session]) {
      expect(storage.getItem("muninn.oidc.access_token")).toBeNull();
      expect(storage.getItem("muninn.oidc.refresh_token")).toBeNull();
      expect(storage.getItem("muninn.oidc.id_token")).toBeNull();
      expect(storage.getItem("muninn.oidc.expires_at")).toBeNull();
    }
    expect(session.getItem("muninn.oidc.state")).toBeNull();
    expect(session.getItem("muninn.oidc.verifier")).toBeNull();
  });
});
