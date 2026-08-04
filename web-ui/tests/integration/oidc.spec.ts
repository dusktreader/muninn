import { Buffer } from "node:buffer";
import { expect, test } from "@playwright/test";

interface OidcMetadata {
  authorization_endpoint: string;
  token_endpoint: string;
}

const issuer = process.env.VITE_OIDC_ISSUER ?? "https://auth.dusktreader.dev/realms/master";
const discoveryUrl = `${issuer.replace(/\/$/, "")}/.well-known/openid-configuration`;
let metadata: OidcMetadata | undefined;

test.beforeAll(async () => {
  console.log(`OIDC integration issuer: ${issuer}`);
  try {
    const response = await fetch(discoveryUrl, { signal: AbortSignal.timeout(5_000) });
    if (response.ok) metadata = await response.json() as OidcMetadata;
  } catch {
    metadata = undefined;
  }
  if (!metadata) {
    const reason = `Keycloak discovery is unreachable at ${discoveryUrl}; start the dev k8s cluster or set VITE_OIDC_ISSUER.`;
    console.log(`OIDC integration skipped: ${reason}`);
    test.skip(true, reason);
  }
  if (!process.env.OIDC_TEST_USERNAME || !process.env.OIDC_TEST_PASSWORD) {
    const reason = "Set OIDC_TEST_USERNAME and OIDC_TEST_PASSWORD to run the real Keycloak login.";
    console.log(`OIDC integration skipped: ${reason}`);
    test.skip(true, reason);
  }
});

test("signs in through Keycloak with PKCE and uses the returned access token", async ({ page }) => {
  const username = process.env.OIDC_TEST_USERNAME!;
  const password = process.env.OIDC_TEST_PASSWORD!;
  if (!metadata) throw new Error("Keycloak discovery metadata was not loaded.");

  await page.goto("/");
  const authorizationRequestPromise = page.waitForRequest((request) =>
    request.url().startsWith(metadata!.authorization_endpoint) && request.isNavigationRequest(),
  );
  await page.getByRole("button", { name: "Sign in" }).click();
  const authorizationRequest = new URL((await authorizationRequestPromise).url());
  expect(authorizationRequest.searchParams.get("response_type")).toBe("code");
  expect(authorizationRequest.searchParams.get("redirect_uri")).toContain("/callback");
  expect(authorizationRequest.searchParams.get("code_challenge_method")).toBe("S256");
  expect(authorizationRequest.searchParams.get("code_challenge")).toBeTruthy();
  expect(authorizationRequest.searchParams.get("state")).toBeTruthy();

  const tokenRequestPromise = page.waitForRequest((request) => request.url() === metadata!.token_endpoint);
  const apiRequestPromise = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/v1/conversations" && Boolean(request.headers().authorization),
  );
  await page.locator("input[name='username']").fill(username!);
  await page.locator("input[name='password']").fill(password!);
  await page.locator("#kc-login").click();

  const tokenRequest = await tokenRequestPromise;
  const tokenForm = new URLSearchParams(tokenRequest.postData() ?? "");
  expect(tokenForm.get("grant_type")).toBe("authorization_code");
  expect(tokenForm.get("code")).toBeTruthy();
  expect(tokenForm.get("code_verifier")).toBeTruthy();

  const apiRequest = await apiRequestPromise;
  const accessToken = apiRequest.headers().authorization?.replace(/^Bearer\s+/i, "");
  expect(accessToken).toBeTruthy();
  const encodedClaims = accessToken!.split(".")[1];
  expect(encodedClaims).toBeTruthy();
  const claims = JSON.parse(Buffer.from(encodedClaims!, "base64url").toString("utf8")) as {
    iss: string;
    exp: number;
    aud: string | string[];
  };
  expect(claims.iss).toBe(issuer);
  expect(claims.exp * 1_000).toBeGreaterThan(Date.now());
  expect(Array.isArray(claims.aud) ? claims.aud : [claims.aud]).toContain("muninn-api");

  const restoredApiRequestPromise = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/v1/conversations" && Boolean(request.headers().authorization),
  );
  await page.reload();
  await expect(page.getByRole("heading", { name: "Sign in to continue" })).toHaveCount(0);
  expect((await restoredApiRequestPromise).headers().authorization).toMatch(/^Bearer\s+\S+/i);
});
