import type { AuthSession, Capability } from "./api";

declare global {
  interface Window {
    __MUNINN_AUTH__?: { accessToken?: string; displayName?: string; capabilities?: Capability[] };
  }
}

export function browserSession(): AuthSession | null {
  const supplied = window.__MUNINN_AUTH__;
  if (!supplied) return null;
  return {
    accessToken: supplied.accessToken,
    displayName: supplied.displayName ?? "Test authenticated user",
    capabilities: new Set(supplied.capabilities ?? []),
  };
}
