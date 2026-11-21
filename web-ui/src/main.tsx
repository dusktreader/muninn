import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";

import { App } from "./App";
import { browserSession } from "./auth-session";
import { clearAuthState, completeLogin, endLogin, getOidcConfigurationError, millisecondsUntilRefresh, OidcSession, refreshLogin, restoreLogin, saveSession, startLogin } from "./oidc";
import "./styles.css";

function AuthenticationGate() {
  const [testSession] = useState(browserSession);
  const [oidcSession, setOidcSession] = useState<OidcSession | null>(null);
  const [status, setStatus] = useState(testSession ? "authenticated" : "loading");
  const [error, setError] = useState("");
  const configurationError = getOidcConfigurationError();

  useEffect(() => {
    if (testSession) return;
    let active = true;
    const restore = async () => {
      if (configurationError) {
        setError(configurationError);
        setStatus("signed-out");
        return;
      }
      if (window.location.pathname === "/callback") {
        try {
          const result = await completeLogin(new URL(window.location.href));
          if (!active) return;
          saveSession(result);
          window.history.replaceState(null, "", "/");
          setOidcSession(result);
          setStatus("authenticated");
        } catch (reason) {
          if (!active) return;
          clearAuthState();
          window.history.replaceState(null, "", "/");
          setError(reason instanceof Error ? reason.message : "Sign-in failed. Please try again.");
          setStatus("signed-out");
        }
        return;
      }
      try {
        const result = await restoreLogin();
        if (!active) return;
        if (result) {
          setOidcSession(result);
          setStatus("authenticated");
        } else {
          setStatus("signed-out");
        }
      } catch {
        if (!active) return;
        clearAuthState();
        setError("Your session could not be restored. Sign in again.");
        setStatus("signed-out");
      }
    };
    void restore();
    return () => { active = false; };
  }, [configurationError, testSession]);

  useEffect(() => {
    if (!oidcSession) return;
    const timeout = window.setTimeout(() => {
      if (!oidcSession.refreshToken) {
        clearAuthState();
        setOidcSession(null);
        setError("Your session expired. Sign in again.");
        setStatus("signed-out");
        return;
      }
      void refreshLogin(oidcSession.refreshToken).then((session) => {
        saveSession(session);
        setOidcSession(session);
      }).catch(() => {
        clearAuthState();
        setOidcSession(null);
        setError("Your session expired. Sign in again.");
        setStatus("signed-out");
      });
    }, millisecondsUntilRefresh(oidcSession.expiresAt, Date.now(), oidcSession.refreshToken ? 30_000 : 0));
    return () => window.clearTimeout(timeout);
  }, [oidcSession]);

  function expireSession() {
    setOidcSession(null);
    setStatus("signed-out");
    setError("Your session expired. Sign in again.");
    clearAuthState();
  }

  function signOut(idToken?: string) {
    setOidcSession(null);
    setStatus("signed-out");
    setError("");
    clearAuthState();
    void endLogin(idToken).catch(() => clearAuthState());
  }

  if (status === "loading") {
    return <main className="auth-screen"><p role="status">Completing sign-in…</p></main>;
  }
  if (testSession) return <App session={testSession} />;
  if (oidcSession) {
    const session = {
      ...oidcSession.session,
      onLogout: () => signOut(oidcSession.idToken),
      onSessionExpired: () => expireSession(),
    };
    return <App session={session} />;
  }
  return <main className="auth-screen">
    <section className="auth-card">
      <p className="eyebrow">Muninn</p>
      <h1>Sign in to continue</h1>
      <p>Your recordings are available after you sign in.</p>
      {error && <p className="auth-error" role="alert">{error}</p>}
      <button disabled={Boolean(configurationError)} onClick={() => {
        setError("");
        void startLogin().catch((reason: unknown) => {
          setError(reason instanceof Error ? reason.message : "Sign-in could not be started.");
        });
      }} type="button">Sign in</button>
      <button className="sign-out" onClick={() => signOut()} type="button">Sign out</button>
    </section>
  </main>;
}

createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <AuthenticationGate />
  </BrowserRouter>,
);
