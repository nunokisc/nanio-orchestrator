import { AlertTriangle } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../api";
import { Button, Checkbox } from "./ui";

/**
 * The API refused something because objects would be left where clients can no longer reach
 * them, and said that `allow_orphan` overrides the refusal. This offers that override as a
 * separate, explicit choice: it states the consequence, and its button stays disabled until
 * the box has been ticked by hand. It renders nothing for any other error.
 */
export function OrphanOverride({
  error,
  consequence,
  actionLabel,
  busy,
  onOverride,
}: {
  error: unknown;
  /** What happens to the data, e.g. "The objects stay on pool-a and will not be reachable…" */
  consequence: string;
  actionLabel: string;
  busy?: boolean;
  onOverride: () => void;
}) {
  const [accepted, setAccepted] = useState(false);
  if (!(error instanceof ApiError) || !error.offersAllowOrphan) return null;
  return (
    <div className="override" role="group" aria-label="Override the refusal">
      <div className="override-title">
        <AlertTriangle className="icon" aria-hidden />
        Do it anyway and leave the objects behind
      </div>
      <p>{consequence}</p>
      <p>Nothing is deleted and nothing is moved: the objects stay where they are, out of the clients' sight.</p>
      <Checkbox label="I understand that these objects will not be reachable" checked={accepted} onChange={setAccepted} />
      <div>
        <Button variant="danger" disabled={!accepted} busy={busy} onClick={onOverride}>
          {actionLabel}
        </Button>
      </div>
    </div>
  );
}
