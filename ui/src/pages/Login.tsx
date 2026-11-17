import { useState } from "react";
import { api, ApiError, type Session } from "../api";
import { Badge, Button, Notice, TextField } from "../components/ui";

export function LoginPage({ session, onSignedIn }: { session: Session; onSignedIn: (session: Session) => void }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!key) {
      setError("Enter the API key.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await api.login(key));
    } catch (cause) {
      const apiError = cause instanceof ApiError ? cause : null;
      setError(apiError?.status === 401 ? "That is not the API key of this orchestrator." : (apiError?.detail ?? String(cause)));
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form
        className="login-card"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="brand brand-large">
          <span className="brand-mark" aria-hidden />
          <span>
            nanio<span className="brand-dim">-orchestrator</span>
          </span>
        </div>
        <h1>Sign in</h1>
        <TextField
          label="API key"
          type="password"
          autoComplete="current-password"
          autoFocus
          mono
          value={key}
          onValue={setKey}
          hint={
            session.dev_mode ? (
              "Development mode: the key is the one in dev.env (dev by default)."
            ) : (
              <>
                The value of <span className="mono">NANIO_ORCHESTRATOR_API_KEY</span> in{" "}
                <span className="mono">/etc/nanio-orchestrator/config.env</span>.
              </>
            )
          }
        />
        {error && <Notice tone="bad">{error}</Notice>}
        <Button variant="primary" type="submit" busy={busy}>
          Sign in
        </Button>
        <div className="login-foot">
          <span className="mono">v{session.version}</span>
          {session.dev_mode && <Badge tone="warn">dev mode</Badge>}
        </div>
      </form>
    </div>
  );
}
