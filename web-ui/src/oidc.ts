import type { AuthSession, Capability } from "./api";

const issuer = import.meta.env.VITE_OIDC_ISSUER?.trim() ?? "";
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID?.trim() ?? "";
const redirectUri = import.meta.env.VITE_OIDC_REDIRECT_URI?.trim()
  || (typeof window === "undefined" ? "" : `${window.location.origin}/callback`);

export function oidcConfigurationError(
  issuerValue: string | undefined,
  clientIdValue: string | undefined,
  redirectUriValue: string | undefined,
): string | null {
  const missing = [
    !issuerValue && "VITE_OIDC_ISSUER",
    !clientIdValue && "VITE_OIDC_CLIENT_ID",
    !redirectUriValue && "VITE_OIDC_REDIRECT_URI",
  ].filter(Boolean);
  return missing.length ? `Sign-in configuration is incomplete. Set ${missing.join(", ")}.` : null;
}

export function getOidcConfigurationError(): string | null {
  if (import.meta.env.MODE === "test") return null;
  return oidcConfigurationError(issuer, clientId, redirectUri);
}

function oidcConfiguration() {
  const error = oidcConfigurationError(issuer, clientId, redirectUri);
  if (error) throw new Error(error);
  return { issuer: issuer!, clientId: clientId!, redirectUri: redirectUri! };
}
const pendingStateKey = "muninn.oidc.state";
const pendingVerifierKey = "muninn.oidc.verifier";
const tokenKeys = [
  "muninn.oidc.access_token",
  "muninn.oidc.refresh_token",
  "muninn.oidc.id_token",
  "muninn.oidc.expires_at",
];
type SessionStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const supportedCapabilities = new Set<Capability>([
  "context:read",
  "metadata:write",
  "processing:admin",
  "deletion:write",
  "notes:human:create",
  "notes:human:edit",
  "notes:human:delete",
]);

interface OidcMetadata {
  issuer: string;
  authorization_endpoint: string;
  token_endpoint: string;
  end_session_endpoint?: string;
}

interface TokenResponse {
  access_token: string;
  expires_in?: number;
  refresh_token?: string;
  id_token?: string;
  token_type: string;
}

export interface OidcSession {
  session: AuthSession;
  refreshToken?: string;
  idToken?: string;
  expiresAt: number;
}

export function generateCodeVerifier(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return encodeBase64Url(bytes);
}

export async function createCodeChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return encodeBase64Url(new Uint8Array(digest));
}

export function validateCallbackState(params: URLSearchParams, storage: Pick<Storage, "getItem" | "removeItem">): string {
  const expectedState = storage.getItem(pendingStateKey);
  const verifier = storage.getItem(pendingVerifierKey);
  storage.removeItem(pendingStateKey);
  storage.removeItem(pendingVerifierKey);
  const receivedState = params.get("state");
  if (!expectedState || !verifier || !receivedState || expectedState !== receivedState) {
    throw new Error("The sign-in response could not be verified. Please sign in again.");
  }
  return verifier;
}

export async function startLogin(): Promise<void> {
  const verifier = generateCodeVerifier();
  const state = generateCodeVerifier();
  sessionStorage.setItem(pendingStateKey, state);
  sessionStorage.setItem(pendingVerifierKey, verifier);
  try {
    const metadata = await discoverProvider();
    const challenge = await createCodeChallenge(verifier);
    const authorizationUrl = new URL(metadata.authorization_endpoint);
    authorizationUrl.search = new URLSearchParams({
      client_id: clientId,
      redirect_uri: redirectUri,
      response_type: "code",
      scope: "openid profile email",
      state,
      code_challenge: challenge,
      code_challenge_method: "S256",
    }).toString();
    window.location.assign(authorizationUrl);
  } catch (error) {
    clearPendingLogin();
    throw error;
  }
}

export async function completeLogin(callbackUrl: URL): Promise<OidcSession> {
  const params = callbackUrl.searchParams;
  const verifier = validateCallbackState(params, sessionStorage);
  const error = params.get("error_description") ?? params.get("error");
  if (error) throw new Error(error);
  const code = params.get("code");
  if (!code) throw new Error("The sign-in response did not contain an authorization code.");
  const metadata = await discoverProvider();
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: clientId,
    code,
    redirect_uri: redirectUri,
    code_verifier: verifier,
  });
  return createSession(await exchangeToken(metadata.token_endpoint, body));
}

export async function refreshLogin(refreshToken: string): Promise<OidcSession> {
  const metadata = await discoverProvider();
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: clientId,
    refresh_token: refreshToken,
  });
  const session = createSession(await exchangeToken(metadata.token_endpoint, body));
  return { ...session, refreshToken: session.refreshToken ?? refreshToken };
}

export async function restoreLogin(
  storage: SessionStorage = sessionStorage,
  refresh: typeof refreshLogin = refreshLogin,
  now = Date.now(),
): Promise<OidcSession | null> {
  const accessToken = storage.getItem("muninn.oidc.access_token");
  const refreshToken = storage.getItem("muninn.oidc.refresh_token");
  if (accessToken) {
    try {
      const expirationValue = storage.getItem("muninn.oidc.expires_at");
      const storedExpiration = expirationValue ? Number(expirationValue) : Number.NaN;
      const restored = createSession({
        access_token: accessToken,
        expires_in: Number.isFinite(storedExpiration) ? (storedExpiration - now) / 1000 : 0,
        refresh_token: refreshToken ?? undefined,
        id_token: storage.getItem("muninn.oidc.id_token") ?? undefined,
        token_type: "Bearer",
      }, now);
      const expiresAt = Number.isFinite(storedExpiration)
        ? Math.min(restored.expiresAt, storedExpiration)
        : restored.expiresAt;
      if (expiresAt > now + (refreshToken ? 30_000 : 0)) {
        return { ...restored, expiresAt };
      }
    } catch {
      storage.removeItem("muninn.oidc.access_token");
      storage.removeItem("muninn.oidc.expires_at");
    }
  }
  if (refreshToken) {
    try {
      const restored = await refresh(refreshToken);
      saveSession(restored, storage);
      return restored;
    } catch (error) {
      clearStoredTokens(storage);
      throw error;
    }
  }
  clearStoredTokens(storage);
  return null;
}

export function saveSession(session: OidcSession, storage: SessionStorage = sessionStorage): void {
  storage.setItem("muninn.oidc.access_token", session.session.accessToken ?? "");
  if (session.refreshToken) storage.setItem("muninn.oidc.refresh_token", session.refreshToken);
  else storage.removeItem("muninn.oidc.refresh_token");
  if (session.idToken) storage.setItem("muninn.oidc.id_token", session.idToken);
  else storage.removeItem("muninn.oidc.id_token");
  storage.setItem("muninn.oidc.expires_at", String(session.expiresAt));
  if (typeof localStorage !== "undefined") {
    for (const key of tokenKeys) localStorage.removeItem(key);
  }
}

export function millisecondsUntilRefresh(expiresAt: number, now = Date.now(), buffer = 30_000): number {
  return Math.max(0, expiresAt - now - buffer);
}

export async function endLogin(idToken?: string): Promise<void> {
  clearAuthState();
  const metadata = await discoverProvider();
  if (!metadata.end_session_endpoint) return;
  const logoutUrl = new URL(metadata.end_session_endpoint);
  logoutUrl.searchParams.set("client_id", clientId);
  logoutUrl.searchParams.set("post_logout_redirect_uri", window.location.origin);
  if (idToken) logoutUrl.searchParams.set("id_token_hint", idToken);
  window.location.assign(logoutUrl);
}

export function clearPendingLogin(): void {
  sessionStorage.removeItem(pendingStateKey);
  sessionStorage.removeItem(pendingVerifierKey);
}

export function clearAuthState(): void {
  clearPendingLogin();
  for (const storage of [sessionStorage, localStorage]) clearStoredTokens(storage);
}

function clearStoredTokens(storage: SessionStorage): void {
  for (const key of tokenKeys) storage.removeItem(key);
}

async function discoverProvider(): Promise<OidcMetadata> {
  const configuration = oidcConfiguration();
  const response = await fetch(`${configuration.issuer.replace(/\/$/, "")}/.well-known/openid-configuration`, { cache: "no-store" });
  if (!response.ok) throw new Error("The identity provider could not be reached.");
  const metadata = await response.json() as OidcMetadata;
  if (metadata.issuer !== configuration.issuer || !metadata.authorization_endpoint || !metadata.token_endpoint) {
    throw new Error("The identity provider configuration is invalid.");
  }
  return metadata;
}

async function exchangeToken(endpoint: string, body: URLSearchParams): Promise<TokenResponse> {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
    cache: "no-store",
  });
  if (!response.ok) throw new Error("The identity provider rejected the sign-in request.");
  const tokens = await response.json() as TokenResponse;
  if (!tokens.access_token || tokens.token_type.toLowerCase() !== "bearer") {
    throw new Error("The identity provider returned an invalid token response.");
  }
  return tokens;
}

function createSession(tokens: TokenResponse, now = Date.now()): OidcSession {
  const claims = decodeTokenClaims(tokens.access_token);
  const expiration = typeof claims.exp === "number"
    ? claims.exp * 1000
    : now + (tokens.expires_in ?? 0) * 1000;
  if (!Number.isFinite(expiration) || expiration <= now) {
    throw new Error("The identity provider returned an expired access token.");
  }
  const claimCapabilities = Array.isArray(claims.capabilities) ? claims.capabilities : [];
  const capabilities = claimCapabilities.filter((value): value is Capability =>
    typeof value === "string" && supportedCapabilities.has(value as Capability),
  );
  const displayName = [claims.name, claims.preferred_username, claims.email].find(
    (value): value is string => typeof value === "string" && value.length > 0,
  ) ?? "Authenticated user";
  return {
    session: {
      accessToken: tokens.access_token,
      displayName,
      capabilities: new Set(capabilities),
    },
    refreshToken: tokens.refresh_token,
    idToken: tokens.id_token,
    expiresAt: expiration,
  };
}

function decodeTokenClaims(token: string): Record<string, unknown> {
  const payload = token.split(".")[1];
  if (!payload) throw new Error("The identity provider returned an invalid access token.");
  try {
    const encoded = payload.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "="));
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
  } catch {
    throw new Error("The identity provider returned an invalid access token.");
  }
}

function encodeBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
