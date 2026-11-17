import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, DatabaseBackup, Download, FileDiff, FolderInput, RefreshCw, Upload } from "lucide-react";
import { type ReactNode, useState } from "react";
import { api, type ConfigFile, type ConfigStatus, type NginxResult, type PendingFile } from "../api";
import { useReloadNginx } from "../components/Layout";
import { useToast } from "../components/toast";
import {
  Badge,
  Button,
  CodeBlock,
  ConfirmDialog,
  EmptyState,
  ErrorNotice,
  Modal,
  Mono,
  Notice,
  PageHeader,
  Panel,
  QueryView,
} from "../components/ui";
import { collapseUnchanged, lineDiff } from "../lib/diff";
import { basename, formatDateTime, pendingState, plural, timeAgo } from "../lib/format";
import { useAction, useConfigStatus } from "../queries";

type Dialog =
  | { kind: "file"; path: string }
  | { kind: "rebuild" }
  | { kind: "rewrite"; path: string }
  | { kind: "absorb"; path: string }
  | { kind: "rebuild-db" };

const CHANGE_LABEL: Record<PendingFile["change"], string> = {
  added: "new file",
  modified: "changed",
  removed: "removed",
};

export function ConfigurationPage() {
  const status = useConfigStatus();
  const toast = useToast();
  const reload = useReloadNginx();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [result, setResult] = useState<{ title: string; tone: "ok" | "bad" | "warn"; body: ReactNode } | null>(null);
  const close = () => setDialog(null);

  const nginxResult = (title: string) => (answer: NginxResult) =>
    setResult({
      title: answer.ok ? title : `${title}: failed`,
      tone: answer.ok ? "ok" : "bad",
      body: <pre className="output">{answer.output || "(no output)"}</pre>,
    });
  const fail = (title: string) => (error: unknown) => toast.error(title, error);

  const validate = useAction(() => api.config.validate(), { onSuccess: nginxResult("nginx -t") });
  const sync = useAction(() => api.config.sync(), {
    onSuccess: (answer) =>
      setResult({
        title: "Imported from disk",
        tone: answer.warnings.length ? "warn" : "ok",
        body: (
          <>
            <p>
              {plural(answer.pools_new, "new pool")}, {plural(answer.pools_updated, "pool")} updated,{" "}
              {plural(answer.members_new, "new member")}, {plural(answer.vhosts_new, "new vhost")},{" "}
              {plural(answer.vhosts_updated, "vhost")} updated, {plural(answer.routes_synced, "route")} and{" "}
              {plural(answer.files_synced, "file")} read. Nothing was deleted.
            </p>
            <Warnings items={answer.warnings} />
          </>
        ),
      }),
  });
  const backup = useAction(() => api.config.backup(), {
    onSuccess: (answer) =>
      setResult(
        answer.ok
          ? { title: "Database backed up", tone: "ok", body: <Mono>{answer.backup_path}</Mono> }
          : { title: "The backup failed", tone: "bad", body: answer.detail ?? "See the service log." },
      ),
  });

  return (
    <>
      <PageHeader
        title="Configuration"
        description="The nginx files the orchestrator manages. A change is written and checked by nginx when you make it, and applied only when nginx is reloaded."
      />
      <div className="stack-lg">
        <Panel title="Apply and check">
          <div className="stack">
            <QueryView query={status}>{(data) => <PendingSummary status={data} />}</QueryView>
            <div className="action-row">
              <Button
                variant="primary"
                icon={<RefreshCw className="icon" aria-hidden />}
                busy={reload.isPending}
                onClick={() => reload.mutate(undefined, { onSuccess: nginxResult("nginx reloaded"), onError: fail("Could not reload nginx") })}
              >
                Reload nginx
              </Button>
              <Button
                icon={<CheckCircle2 className="icon" aria-hidden />}
                busy={validate.isPending}
                onClick={() => validate.mutate(undefined, { onError: fail("Could not run nginx -t") })}
              >
                Validate (nginx -t)
              </Button>
              <Button
                icon={<DatabaseBackup className="icon" aria-hidden />}
                busy={backup.isPending}
                onClick={() => backup.mutate(undefined, { onError: fail("Could not back up the database") })}
              >
                Back up the database now
              </Button>
            </div>
            {result && (
              <Notice tone={result.tone} title={result.title}>
                {result.body}
              </Notice>
            )}
          </div>
        </Panel>

        <Panel flush title="Managed files" meta="Compared with what the orchestrator wrote. A file edited by hand is drifted; nothing is corrected automatically.">
          <QueryView
            query={status}
            isEmpty={(data) => data.files.length === 0}
            empty={
              <EmptyState title="No managed files yet">
                A file appears when a pool gets its first member or a vhost gets a default pool.
              </EmptyState>
            }
          >
            {(data) => <FileTable status={data} open={setDialog} />}
          </QueryView>
        </Panel>

        <Panel title="Database and files" meta="The database and the files describe the same state. Either can be rebuilt from the other.">
          <div className="direction-grid">
            <div className="direction">
              <h3>
                <Upload className="icon" aria-hidden /> Database → files
              </h3>
              <p>
                Regenerate every config file and sidecar from the database, validate and <strong>reload nginx</strong>. Use it
                when the files on disk are wrong or missing. Hand edits are overwritten.
              </p>
              <Button onClick={() => setDialog({ kind: "rebuild" })}>Rebuild all from the database</Button>
            </div>
            <div className="direction">
              <h3>
                <FolderInput className="icon" aria-hidden /> Files → database, adding
              </h3>
              <p>
                Import the pools, members, vhosts and routes found on disk. Adds and updates; never deletes. Use it after
                adding something to a file by hand.
              </p>
              <Button busy={sync.isPending} onClick={() => sync.mutate(undefined, { onError: fail("Could not import from disk") })}>
                Sync from disk
              </Button>
            </div>
            <div className="direction direction-danger">
              <h3>
                <Download className="icon" aria-hidden /> Files → database, from scratch
              </h3>
              <p>
                Rebuild the whole database from the files on disk. For a lost or corrupt database. You see what would be
                imported before anything is replaced.
              </p>
              <Button variant="danger" onClick={() => setDialog({ kind: "rebuild-db" })}>
                Rebuild the database from disk
              </Button>
            </div>
          </div>
        </Panel>
      </div>

      {dialog?.kind === "file" && <FileDialog path={dialog.path} open={setDialog} onClose={close} />}
      {dialog?.kind === "rebuild" && (
        <RunDialog
          title="Rebuild all files from the database"
          confirmLabel="Rebuild and reload"
          run={api.config.rebuild}
          onDone={(answer) =>
            setResult({
              title: answer.ok ? "Rebuilt from the database and reloaded" : "Rebuild failed: the previous files are back in place",
              tone: answer.ok ? "ok" : "bad",
              body: (
                <>
                  <p>
                    {plural(answer.written.length, "file")} written, {answer.removed.length} removed.
                  </p>
                  <pre className="output">{answer.output}</pre>
                </>
              ),
            })
          }
          onClose={close}
        >
          <p>
            Every pool and vhost file and every sidecar is regenerated from the database. If nginx accepts the result it is{" "}
            <strong>reloaded at once</strong>; if not, the previous files are restored.
          </p>
          <p>Files edited by hand are overwritten. To keep a manual edit, absorb that file first.</p>
        </RunDialog>
      )}
      {dialog?.kind === "rewrite" && (
        <RunDialog
          title={`Rewrite ${basename(dialog.path)} from the database`}
          confirmLabel="Rewrite and reload"
          run={() => api.config.rewrite(dialog.path)}
          onDone={(answer) =>
            setResult({
              title: answer.ok ? `${basename(dialog.path)} rewritten from the database` : `Could not rewrite ${basename(dialog.path)}`,
              tone: answer.ok ? "ok" : "bad",
              body: <pre className="output">{answer.output ?? answer.action ?? ""}</pre>,
            })
          }
          onClose={close}
        >
          <p>
            The manual edit of <Mono>{dialog.path}</Mono> is discarded: the file is written again from the database,
            validated, and nginx is <strong>reloaded at once</strong>.
          </p>
        </RunDialog>
      )}
      {dialog?.kind === "absorb" && (
        <RunDialog
          title={`Absorb ${basename(dialog.path)}`}
          confirmLabel="Keep the manual edit"
          danger={false}
          run={() => api.config.absorb(dialog.path)}
          onDone={() => setResult({ title: `${basename(dialog.path)} accepted as it is on disk`, tone: "ok", body: null })}
          onClose={close}
        >
          <p>
            The file is recorded as accepted and no longer counts as drifted. It is <strong>not</strong> parsed into pools
            and routes: if you added a member or a route by hand, use <em>Sync from disk</em> instead.
          </p>
          <p>The next change the orchestrator makes to this pool or vhost regenerates the file and loses the edit.</p>
        </RunDialog>
      )}
      {dialog?.kind === "rebuild-db" && <RebuildDatabaseDialog onClose={close} onDone={setResult} />}
    </>
  );
}

function Warnings({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="plain-list">
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

function PendingSummary({ status }: { status: ConfigStatus }) {
  const state = pendingState(status);
  const last = status.last_applied_at ? `Last reload through the orchestrator: ${formatDateTime(status.last_applied_at)} (${timeAgo(status.last_applied_at)}).` : "No reload through the orchestrator has been recorded yet.";
  if (state.kind === "applied") {
    return (
      <Notice tone="ok" title="Everything on disk has been applied">
        {last}
      </Notice>
    );
  }
  if (state.kind === "unknown") {
    return (
      <Notice tone="warn" title="Not known whether nginx is serving these files">
        {state.message} Reload nginx to apply them and to start tracking.
      </Notice>
    );
  }
  return (
    <Notice tone="warn" title={state.message}>
      <ul className="plain-list">
        {status.pending_files.map((file) => (
          <li key={file.path}>
            <Mono>{file.path}</Mono> — {CHANGE_LABEL[file.change]}
          </li>
        ))}
      </ul>
      {last} A reload done outside the orchestrator (for example <Mono>systemctl reload nginx</Mono>) is not seen here.
    </Notice>
  );
}

function FileTable({ status, open }: { status: ConfigStatus; open: (dialog: Dialog) => void }) {
  const pending = new Map(status.pending_files.map((file) => [file.path, file.change]));
  return (
    <table className="table">
      <thead>
        <tr>
          <th>File</th>
          <th>State</th>
          <th>Written</th>
          <th className="actions">Actions</th>
        </tr>
      </thead>
      <tbody>
        {status.files.map((file: ConfigFile) => {
          const change = pending.get(file.path);
          return (
            <tr key={file.path}>
              <td>
                <Mono>{file.path}</Mono>
              </td>
              <td>
                {file.sha256_disk === null ? (
                  <Badge tone="bad" title="The orchestrator wrote this file but it is no longer on disk">
                    missing on disk
                  </Badge>
                ) : file.drifted ? (
                  <Badge tone="warn" title="Differs from what the orchestrator wrote">
                    drifted
                  </Badge>
                ) : (
                  <Badge tone="ok">in sync</Badge>
                )}{" "}
                {file.path.endsWith(".conf.disabled") && (
                  <Badge tone="neutral" title="The file of a disabled vhost: kept on disk, not loaded by nginx">
                    disabled vhost
                  </Badge>
                )}{" "}
                {change && <Badge tone="warn">not applied · {CHANGE_LABEL[change]}</Badge>}
              </td>
              <td title={formatDateTime(file.last_synced_at)}>{timeAgo(file.last_synced_at)}</td>
              <td className="actions">
                <Button size="sm" icon={<FileDiff className="icon" aria-hidden />} onClick={() => open({ kind: "file", path: file.path })}>
                  {file.drifted ? "Compare" : "View"}
                </Button>
                {(file.drifted || file.sha256_disk === null) && (
                  <Button size="sm" onClick={() => open({ kind: "rewrite", path: file.path })}>
                    Rewrite from the database
                  </Button>
                )}
                {file.drifted && (
                  <Button size="sm" onClick={() => open({ kind: "absorb", path: file.path })}>
                    Absorb
                  </Button>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function FileDialog({ path, open, onClose }: { path: string; open: (dialog: Dialog) => void; onClose: () => void }) {
  const file = useQuery({ queryKey: ["config", "file", path], queryFn: () => api.config.file(path), staleTime: 0 });
  return (
    <Modal
      title={basename(path)}
      onClose={onClose}
      size="lg"
      footer={
        <>
          {file.data?.drifted && (
            <>
              <Button onClick={() => open({ kind: "absorb", path })}>Absorb (keep the edit)</Button>
              <Button onClick={() => open({ kind: "rewrite", path })}>Rewrite from the database</Button>
            </>
          )}
          <Button variant="primary" onClick={onClose}>
            Close
          </Button>
        </>
      }
    >
      <QueryView query={file}>
        {(data) => {
          if (data.content === null) {
            return (
              <div className="stack">
                <Notice tone="bad" title="This file is not on disk">
                  Below is what the orchestrator last wrote. Rewrite it from the database to put it back.
                </Notice>
                <CodeBlock title={data.path} code={data.recorded ?? ""} />
              </div>
            );
          }
          if (data.drifted && data.recorded !== null) {
            const diff = collapseUnchanged(lineDiff(data.recorded, data.content));
            return (
              <div className="stack">
                <Notice tone="warn" title="Edited by hand">
                  <span className="diff-legend diff-del">−</span> lines the orchestrator wrote that are gone ·{" "}
                  <span className="diff-legend diff-add">+</span> lines that are on disk now.
                </Notice>
                <div className="code">
                  <div className="code-header">
                    <span className="mono">{data.path}</span>
                  </div>
                  <pre className="diff" tabIndex={0}>
                    {diff.map((line, index) =>
                      line.type === "skip" ? (
                        <span key={index} className="diff-line diff-skip">
                          {`   … ${plural(line.count, "unchanged line")}\n`}
                        </span>
                      ) : (
                      <span key={index} className={`diff-line diff-${line.type}`}>
                        <span className="diff-mark" aria-hidden>
                          {line.type === "add" ? "+" : line.type === "del" ? "−" : " "}
                        </span>
                        <span className="sr-only">{line.type === "add" ? "added: " : line.type === "del" ? "removed: " : ""}</span>
                        {line.text}
                        {"\n"}
                      </span>
                      ),
                    )}
                  </pre>
                </div>
              </div>
            );
          }
          return (
            <div className="stack">
              {data.recorded === null && (
                <Notice tone="info">This file was not written by the orchestrator, so there is nothing to compare it with.</Notice>
              )}
              <CodeBlock title={data.path} code={data.content} />
            </div>
          );
        }}
      </QueryView>
    </Modal>
  );
}

function RunDialog<T>({
  title,
  confirmLabel,
  danger = true,
  run,
  onDone,
  onClose,
  children,
}: {
  title: string;
  confirmLabel: string;
  danger?: boolean;
  run: () => Promise<T>;
  onDone: (answer: T) => void;
  onClose: () => void;
  children: ReactNode;
}) {
  const action = useAction(run, {
    onSuccess: (answer) => {
      onDone(answer);
      onClose();
    },
  });
  return (
    <ConfirmDialog
      title={title}
      confirmLabel={confirmLabel}
      danger={danger}
      busy={action.isPending}
      error={action.error}
      onConfirm={() => action.mutate(undefined)}
      onClose={onClose}
    >
      {children}
    </ConfirmDialog>
  );
}

function Report({ data }: { data: Record<string, unknown> }) {
  const simple = Object.entries(data).filter(([, value]) => typeof value === "number" || typeof value === "string" || typeof value === "boolean");
  const lists = Object.entries(data).filter(([, value]) => Array.isArray(value) && value.length > 0);
  return (
    <div className="stack">
      <dl className="kv">
        {simple.map(([key, value]) => (
          <div key={key}>
            <dt>{key.replace(/_/g, " ")}</dt>
            <dd>{String(value)}</dd>
          </div>
        ))}
      </dl>
      {lists.map(([key, value]) => (
        <div key={key}>
          <h4>{key.replace(/_/g, " ")}</h4>
          {(value as unknown[]).every((item) => typeof item === "string") ? (
            <Warnings items={value as string[]} />
          ) : (
            <pre className="output">{JSON.stringify(value, null, 2)}</pre>
          )}
        </div>
      ))}
    </div>
  );
}

function RebuildDatabaseDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: (result: { title: string; tone: "ok" | "warn"; body: ReactNode }) => void;
}) {
  const preview = useQuery({
    queryKey: ["config", "rebuild-dry-run"],
    queryFn: () => api.config.rebuildFromDisk({ dry_run: true }),
    staleTime: 0,
    gcTime: 0,
  });
  const rebuild = useAction(() => api.config.rebuildFromDisk({ force: true }), {
    onSuccess: (answer) => {
      const warnings = Array.isArray(answer.warnings) ? answer.warnings.length : 0;
      onDone({ title: "Database rebuilt from the files on disk", tone: warnings ? "warn" : "ok", body: <Report data={answer} /> });
      onClose();
    },
  });
  return (
    <ConfirmDialog
      title="Rebuild the database from disk"
      confirmLabel="Replace the database"
      typeToConfirm="rebuild"
      busy={rebuild.isPending}
      error={rebuild.error}
      onConfirm={() => rebuild.mutate(undefined)}
      onClose={onClose}
    >
      <p>
        <strong>Step 1 — dry run.</strong> This is what the files on disk would give. Nothing has been changed yet.
      </p>
      {preview.data ? <Report data={preview.data} /> : preview.isError ? <ErrorNotice error={preview.error} /> : <p className="muted">Reading the files…</p>}
      <p>
        <strong>Step 2 — replace.</strong> Every pool, member, vhost, route, credential and migration in the database is
        deleted and imported again from the files and sidecars. The <strong>audit log is lost</strong>: it lives only in
        the database. nginx is not touched.
      </p>
    </ConfirmDialog>
  );
}
