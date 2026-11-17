import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Check, FolderSearch, Plus, RotateCcw, ScrollText, Split, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Migration, type MigrationInput, type Phase, type Pool, type Vhost } from "../api";
import { useToast } from "../components/toast";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  FormDialog,
  Modal,
  Mono,
  Notice,
  PageHeader,
  Panel,
  Progress,
  QueryView,
  SelectField,
  TextField,
  type Tone,
} from "../components/ui";
import {
  formatBytes,
  formatDateTime,
  isActive,
  migrationTarget,
  PHASE_LABEL,
  progressPercent,
  splitLeftActive,
  STEPS,
  timeAgo,
  unresolvedSplits,
} from "../lib/format";
import { normalizeFolder } from "../lib/validation";
import { useAction, useMigrations, usePools, useVhosts } from "../queries";

const PHASE_TONE: Record<Phase, Tone> = {
  pending: "neutral",
  copying: "info",
  write_routing: "info",
  verifying: "info",
  switching: "info",
  done: "ok",
  error: "bad",
  cancelled: "warn",
};

const STEP_HELP: Record<string, string> = {
  copying: "rclone copies the objects; the source serves everything",
  write_routing: "nginx switches to split routing: the destination becomes authoritative",
  verifying: "copy with --update and a one-way check, until nothing is missing",
  switching: "the route points at the destination and the split is removed",
  done: "the destination serves everything; the old copy is kept",
};

const STALE_REASON: Record<string, string> = {
  src_no_members: "its source pool has no enabled member",
  dst_no_members: "its destination pool has no enabled member",
  src_bucket_missing: "the source bucket no longer exists on the source pool",
};

type Prefill = { mode: "bucket" | "folder"; bucket: string; folder: string; src: string; vhost: string };
type Dialog = { kind: "start"; prefill: Prefill } | { kind: "log"; migration: Migration } | { kind: "cancel"; migration: Migration } | { kind: "retry"; migration: Migration };

export function MigrationsPage() {
  const migrations = useMigrations();
  const pools = usePools();
  const vhosts = useVhosts();
  const [params, setParams] = useSearchParams();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const stale = useQuery({ queryKey: ["migrations", "stale"], queryFn: api.migrations.stale, refetchInterval: 15_000 });
  const orphaned = useQuery({ queryKey: ["migrations", "orphaned"], queryFn: api.migrations.orphaned, refetchInterval: 30_000 });

  // Other screens link here with ?start=… to open the form already filled in.
  useEffect(() => {
    const start = params.get("start");
    if (start !== "bucket" && start !== "folder") return;
    setDialog({
      kind: "start",
      prefill: {
        mode: start,
        bucket: params.get("bucket") ?? "",
        folder: params.get("folder") ?? "",
        src: params.get("src") ?? "",
        vhost: params.get("vhost") ?? "",
      },
    });
    setParams({}, { replace: true });
  }, [params, setParams]);

  const poolName = (id: number) => pools.data?.find((pool) => pool.id === id)?.name ?? `pool #${id}`;
  const close = () => setDialog(null);
  const blank: Prefill = { mode: "bucket", bucket: "", folder: "", src: "", vhost: "" };

  return (
    <>
      <PageHeader
        title="Migrations"
        description="A migration moves one bucket, or one folder of a bucket, from one nanio pool to another while clients keep using it. Nothing is deleted: the old copy stays and is listed below."
        actions={
          <Button variant="primary" icon={<Plus className="icon" aria-hidden />} onClick={() => setDialog({ kind: "start", prefill: blank })}>
            Start migration
          </Button>
        }
      />

      <QueryView query={migrations}>
        {(list) => {
          const active = list.filter(isActive);
          const stuck = unresolvedSplits(list);
          const history = list.filter((m) => !isActive(m) && !stuck.includes(m));
          return (
            <div className="stack-lg">
              {(stale.data ?? []).map((item) => (
                <Notice key={item.migration_id} tone="warn" title={`Migration #${item.migration_id} (${item.bucket}) cannot proceed`}>
                  It is in phase {item.phase}, but {STALE_REASON[item.reason] ?? item.reason}. Fix that, or cancel the
                  migration.
                </Notice>
              ))}

              {stuck.length > 0 && (
                <section className="stack" aria-label="Interrupted with split routing active">
                  <h2 className="section-title">Interrupted, with split routing still active</h2>
                  {stuck.map((migration) => (
                    <MigrationCard key={migration.id} migration={migration} poolName={poolName} open={setDialog} />
                  ))}
                </section>
              )}

              <section className="stack" aria-label="In progress">
                <h2 className="section-title">In progress</h2>
                {active.length === 0 ? (
                  <EmptyState title="Nothing is being moved">
                    Start a migration to move a bucket that already has a route, or one folder of a bucket. To route a bucket
                    that is still on the default pool and move it in one step, use <Link to="/buckets">Buckets</Link>.
                  </EmptyState>
                ) : (
                  active.map((migration) => <MigrationCard key={migration.id} migration={migration} poolName={poolName} open={setDialog} />)
                )}
              </section>

              <Panel flush title="History">
                {history.length === 0 ? (
                  <EmptyState title="No finished migrations" />
                ) : (
                  <table className="table">
                    <thead>
                      <tr>
                        <th className="num">#</th>
                        <th>Bucket / folder</th>
                        <th>From → to</th>
                        <th>Result</th>
                        <th>Finished</th>
                        <th className="actions">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {history.map((migration) => (
                        <tr key={migration.id}>
                          <td className="num">{migration.id}</td>
                          <td>
                            <Mono>{migrationTarget(migration)}</Mono>
                          </td>
                          <td>
                            <Mono>{poolName(migration.src_pool_id)}</Mono> → <Mono>{poolName(migration.dst_pool_id)}</Mono>
                          </td>
                          <td>
                            <Badge tone={PHASE_TONE[migration.phase]}>{PHASE_LABEL[migration.phase]}</Badge>
                            {migration.error_msg && <div className="cell-note error-detail">{migration.error_msg}</div>}
                            {migration.phase !== "done" && !migration.split_routing && (
                              <div className="cell-note">
                                The split had not started: the bucket is still served entirely by the source. Fix the cause
                                and start it again.
                              </div>
                            )}
                          </td>
                          <td title={formatDateTime(migration.finished_at)}>{timeAgo(migration.finished_at)}</td>
                          <td className="actions">
                            <Button size="sm" icon={<ScrollText className="icon" aria-hidden />} onClick={() => setDialog({ kind: "log", migration })}>
                              Log
                            </Button>
                            {migration.phase !== "done" && (
                              <Button size="sm" icon={<RotateCcw className="icon" aria-hidden />} onClick={() => setDialog({ kind: "retry", migration })}>
                                Start again
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Panel>

              <Panel
                flush
                title="Old copies left by completed migrations"
                meta="The source is never deleted for you. Delete it yourself, directly on the pool, once you have verified the data on its new pool."
              >
                <QueryView
                  query={orphaned}
                  isEmpty={(items) => items.length === 0}
                  empty={<EmptyState title="No old copies are tracked" />}
                >
                  {(items) => (
                    <table className="table">
                      <thead>
                        <tr>
                          <th>What</th>
                          <th>Old copy on</th>
                          <th>Now served by</th>
                          <th>Since</th>
                          <th className="actions">Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {items.map((item) => {
                          const segments = item.orphaned_source_prefix.split("/").filter(Boolean);
                          const prefix = segments.length > 1 ? `${segments.slice(1).join("/")}/` : "";
                          return (
                            <tr key={item.migration_id}>
                              <td>
                                <Mono>{item.orphaned_source_prefix}</Mono> <span className="muted">migration #{item.migration_id}</span>
                              </td>
                              <td>
                                <Mono>{poolName(item.orphaned_source_pool_id)}</Mono>
                              </td>
                              <td>
                                <Mono>{poolName(item.dst_pool_id)}</Mono>
                              </td>
                              <td title={formatDateTime(item.orphaned_at)}>{timeAgo(item.orphaned_at)}</td>
                              <td className="actions">
                                <Link
                                  className="btn btn-default btn-sm"
                                  to={`/explorer?pool=${item.orphaned_source_pool_id}&bucket=${encodeURIComponent(item.bucket)}&prefix=${encodeURIComponent(prefix)}`}
                                >
                                  <FolderSearch className="icon" aria-hidden />
                                  <span>Browse the old copy</span>
                                </Link>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  )}
                </QueryView>
              </Panel>
            </div>
          );
        }}
      </QueryView>

      {dialog?.kind === "start" && (
        <StartDialog prefill={dialog.prefill} pools={pools.data ?? []} vhosts={vhosts.data ?? []} onClose={close} />
      )}
      {dialog?.kind === "log" && <LogDialog migration={dialog.migration} onClose={close} />}
      {dialog?.kind === "cancel" && <CancelDialog migration={dialog.migration} poolName={poolName} onClose={close} />}
      {dialog?.kind === "retry" && <RetryDialog migration={dialog.migration} poolName={poolName} onClose={close} />}
    </>
  );
}

function Stepper({ migration }: { migration: Migration }) {
  const current = STEPS.indexOf(migration.phase);
  return (
    <ol className="stepper" aria-label="Phases">
      {STEPS.map((step, index) => {
        const state = !isActive(migration) && migration.phase !== "done" ? "idle" : index < current || migration.phase === "done" ? "done" : index === current ? "current" : "todo";
        return (
          <li key={step} className={`step step-${state}`} aria-current={state === "current" ? "step" : undefined} title={STEP_HELP[step]}>
            <span className="step-dot">{state === "done" && <Check className="icon-xs" aria-hidden />}</span>
            <span className="step-label">{PHASE_LABEL[step]}</span>
          </li>
        );
      })}
    </ol>
  );
}

function MigrationCard({ migration, poolName, open }: { migration: Migration; poolName: (id: number) => string; open: (dialog: Dialog) => void }) {
  const src = poolName(migration.src_pool_id);
  const dst = poolName(migration.dst_pool_id);
  const percent = progressPercent(migration);
  const left = splitLeftActive(migration);
  return (
    <article className={`migration${left ? " migration-attention" : ""}`}>
      <div className="migration-head">
        <div className="migration-title">
          <span className="muted">#{migration.id}</span> <Mono>{migrationTarget(migration)}</Mono>
          <span className="migration-pools">
            <Mono>{src}</Mono> <ArrowRight className="icon" aria-label="to" /> <Mono>{dst}</Mono>
          </span>
          <Badge tone={PHASE_TONE[migration.phase]}>{PHASE_LABEL[migration.phase]}</Badge>
          {migration.split_routing && (
            <Badge tone={left ? "warn" : "info"}>
              <Split className="icon-xs" aria-hidden /> split routing active
            </Badge>
          )}
        </div>
        <div className="migration-actions">
          <Button size="sm" icon={<ScrollText className="icon" aria-hidden />} onClick={() => open({ kind: "log", migration })}>
            Log
          </Button>
          {isActive(migration) && (
            <Button size="sm" variant="danger" icon={<X className="icon" aria-hidden />} onClick={() => open({ kind: "cancel", migration })}>
              Cancel
            </Button>
          )}
        </div>
      </div>

      <Stepper migration={migration} />

      {isActive(migration) && (
        <div className="migration-progress">
          <Progress percent={percent} label={`Progress of migration ${migration.id}`} />
          <span className="muted">
            {migration.objects_done} of {migration.objects_total || "?"} objects · {formatBytes(migration.bytes_done)} of{" "}
            {migration.bytes_total ? formatBytes(migration.bytes_total) : "?"} · started {timeAgo(migration.started_at ?? migration.created_at)}
          </span>
        </div>
      )}

      {migration.split_routing && !left && (
        <p className="migration-note">
          <strong>Split routing:</strong> writes go to <Mono>{dst}</Mono> only; reads go to <Mono>{dst}</Mono> and fall back
          to <Mono>{src}</Mono> for objects not copied yet; deletes go to both. Clients keep working. A listing shows what
          is already on <Mono>{dst}</Mono>.
        </p>
      )}

      {left && (
        <Notice
          tone="warn"
          title={`This migration ended (${PHASE_LABEL[migration.phase].toLowerCase()}) with split routing still active`}
          actions={
            <Button variant="primary" size="sm" icon={<RotateCcw className="icon" aria-hidden />} onClick={() => open({ kind: "retry", migration })}>
              Start the same move again
            </Button>
          }
        >
          {migration.error_msg && <div className="error-detail">{migration.error_msg}</div>}
          Clients keep working: they write to <Mono>{dst}</Mono> and read from it, with <Mono>{src}</Mono> as fallback. The
          split is kept on purpose, because <Mono>{dst}</Mono> already holds objects that exist nowhere else. Fix the cause
          (see the log), then <strong>start the same move again to finish it</strong>: a new migration between the same two
          pools takes over the split. A migration to any other pool is refused until then.
        </Notice>
      )}

      {(migration.cascade_warnings ?? []).map((warning) => (
        <Notice key={warning} tone="warn">
          {warning}
        </Notice>
      ))}
    </article>
  );
}

// ── Dialogs ─────────────────────────────────────────────────────────────────

function StartDialog({ prefill, pools, vhosts, onClose }: { prefill: Prefill; pools: Pool[]; vhosts: Vhost[]; onClose: () => void }) {
  const toast = useToast();
  const nanio = pools.filter((pool) => pool.type === "nanio");
  const [mode, setMode] = useState(prefill.mode);
  const [src, setSrc] = useState(prefill.src || (nanio[0] ? String(nanio[0].id) : ""));
  const [bucket, setBucket] = useState(prefill.bucket);
  const [folder, setFolder] = useState(prefill.folder);
  const [vhost, setVhost] = useState(prefill.vhost);
  const [dst, setDst] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const sourceBuckets = useQuery({
    queryKey: ["migrations", "source-buckets", src],
    queryFn: () => api.migrations.sourceBuckets(Number(src)),
    enabled: mode === "bucket" && src !== "",
  });
  const start = useAction((body: MigrationInput) => api.migrations.create(body), {
    onSuccess: (migration) => {
      toast.success(`Migration #${migration.id} started`, `${migrationTarget(migration)} is being moved.`);
      onClose();
    },
  });

  const normalized = normalizeFolder(folder);
  const errors: Record<string, string> = { ...start.error?.fieldErrors };
  if (submitted) {
    if (!bucket.trim()) errors.bucket = "Choose the bucket";
    if (!dst) errors.dst_pool_id = "Choose the destination pool";
    if (mode === "bucket" && !src) errors.src_pool_id = "Choose the pool that serves the bucket now";
    if (mode === "bucket" && src && src === dst) errors.dst_pool_id = "The destination must be another pool";
    if (mode === "folder" && "error" in normalized) errors.folder = normalized.error;
    if (mode === "folder" && "folder" in normalized && !normalized.folder) errors.folder = "Name the folder to move";
  }
  const destinations = nanio.filter((pool) => mode === "folder" || String(pool.id) !== src);
  const bucketOptions = sourceBuckets.data?.buckets ?? [];

  const submit = () => {
    setSubmitted(true);
    if (!bucket.trim() || !dst) return;
    if (mode === "bucket") {
      if (!src || src === dst) return;
      start.mutate({ bucket: bucket.trim(), src_pool_id: Number(src), dst_pool_id: Number(dst) });
    } else {
      if (!("folder" in normalized) || !normalized.folder) return;
      start.mutate({
        bucket: bucket.trim(),
        folder: normalized.folder,
        dst_pool_id: Number(dst),
        vhost_id: vhost ? Number(vhost) : undefined,
      });
    }
  };

  return (
    <FormDialog title="Start a migration" submitLabel="Start migration" busy={start.isPending} error={start.error} onClose={onClose} onSubmit={submit}>
      <fieldset className="choices">
        <legend>What to move</legend>
        <label className="choice">
          <input type="radio" name="migration-mode" checked={mode === "bucket"} onChange={() => setMode("bucket")} />
          <span>
            <strong>A whole bucket</strong>
            <span className="field-hint">The bucket must already have a route. Its route follows the move.</span>
          </span>
        </label>
        <label className="choice">
          <input type="radio" name="migration-mode" checked={mode === "folder"} onChange={() => setMode("folder")} />
          <span>
            <strong>One folder of a bucket</strong>
            <span className="field-hint">
              The folder gets its own route on the destination pool; moved back to the bucket's own pool, its route is
              removed. The rest of the bucket is not touched.
            </span>
          </span>
        </label>
      </fieldset>

      {mode === "bucket" ? (
        <>
          <SelectField
            label="Source pool"
            value={src}
            onValue={(value) => {
              setSrc(value);
              setBucket("");
            }}
            error={errors.src_pool_id}
            hint="The pool the bucket's route points at now."
          >
            {nanio.map((pool) => (
              <option key={pool.id} value={pool.id}>
                {pool.name}
              </option>
            ))}
          </SelectField>
          <SelectField
            label="Bucket"
            value={bucket}
            onValue={setBucket}
            error={errors.bucket}
            hint={
              sourceBuckets.isSuccess && bucketOptions.length === 0
                ? "No routed bucket is served by this pool. A bucket still on the default pool is routed and moved from the Buckets screen."
                : "Buckets with a route to the source pool."
            }
          >
            <option value="">{sourceBuckets.isLoading ? "Loading…" : "Choose a bucket"}</option>
            {[...new Set([...bucketOptions, ...(bucket ? [bucket] : [])])].map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </SelectField>
        </>
      ) : (
        <>
          <div className="grid-2">
            <TextField label="Bucket" mono placeholder="photos" value={bucket} onValue={setBucket} error={errors.bucket} />
            <TextField
              label="Folder"
              mono
              placeholder="2025/"
              value={folder}
              onValue={setFolder}
              error={errors.folder}
              hint={"folder" in normalized && normalized.folder ? `Moves everything under ${normalized.folder}` : "For example 2025/ or 2025/q1/"}
            />
          </div>
          <Notice tone="info">
            The source is worked out for you: it is the pool the folder lives on now (its own route, the bucket's route,
            or the default pool).
          </Notice>
          <SelectField
            label="Vhost"
            value={vhost}
            onValue={setVhost}
            hint="Only needed when the bucket's vhost cannot be worked out: several vhosts, or a bucket not discovered yet."
          >
            <option value="">Work it out</option>
            {vhosts.map((item) => (
              <option key={item.id} value={item.id}>
                {item.server_name}
              </option>
            ))}
          </SelectField>
        </>
      )}

      <SelectField label="Destination pool" value={dst} onValue={setDst} error={errors.dst_pool_id}>
        <option value="">Choose a pool</option>
        {destinations.map((pool) => (
          <option key={pool.id} value={pool.id}>
            {pool.name}
          </option>
        ))}
      </SelectField>
      <p className="field-hint">
        Needs rclone on the orchestrator machine, stored credentials and an enabled member on both pools, and synchronised
        clocks on the storage nodes. Only one move at a time per bucket.
      </p>
    </FormDialog>
  );
}

function LogDialog({ migration, onClose }: { migration: Migration; onClose: () => void }) {
  const live = useMigrations().data?.find((m) => m.id === migration.id) ?? migration;
  const log = useQuery({
    queryKey: ["migrations", migration.id, "log"],
    queryFn: () => api.migrations.log(migration.id),
    refetchInterval: isActive(live) ? 2_000 : false,
  });
  return (
    <Modal title={`Log of migration #${migration.id} — ${migrationTarget(migration)}`} onClose={onClose} size="lg" footer={<Button onClick={onClose}>Close</Button>}>
      <div className="stack">
        <div className="log-status">
          <Badge tone={PHASE_TONE[live.phase]}>{PHASE_LABEL[live.phase]}</Badge>
          {isActive(live) ? <span className="muted">updating every 2 seconds, newest first</span> : <span className="muted">newest first</span>}
        </div>
        <QueryView query={log} isEmpty={(entries) => entries.length === 0} empty={<EmptyState title="Nothing logged yet" />}>
          {(entries) => (
            <div className="log" tabIndex={0} role="log" aria-label="Migration log">
              {entries.map((entry) => (
                <div key={entry.id} className="log-line">
                  <span className="log-time">{formatDateTime(entry.created_at)}</span>
                  <span className="log-phase">{entry.phase}</span>
                  <span className="log-message">{entry.message}</span>
                </div>
              ))}
            </div>
          )}
        </QueryView>
      </div>
    </Modal>
  );
}

function CancelDialog({ migration, poolName, onClose }: { migration: Migration; poolName: (id: number) => string; onClose: () => void }) {
  const cancel = useAction(() => api.migrations.cancel(migration.id), { onSuccess: onClose });
  return (
    <ConfirmDialog
      title={`Cancel migration #${migration.id}`}
      confirmLabel="Cancel the migration"
      busy={cancel.isPending}
      error={cancel.error}
      onConfirm={() => cancel.mutate(undefined)}
      onClose={onClose}
    >
      <p>
        rclone is stopped and the migration of <Mono>{migrationTarget(migration)}</Mono> is marked cancelled. Nothing
        already copied is deleted.
      </p>
      {migration.split_routing ? (
        <p>
          <strong>Split routing has started and stays active:</strong> clients keep writing to{" "}
          <Mono>{poolName(migration.dst_pool_id)}</Mono> and reading from it with <Mono>{poolName(migration.src_pool_id)}</Mono>{" "}
          as fallback, until a new migration between the same two pools finishes the move.
        </p>
      ) : (
        <p>
          Split routing has not started: the bucket is still served entirely by <Mono>{poolName(migration.src_pool_id)}</Mono>,
          and nothing changes for clients.
        </p>
      )}
    </ConfirmDialog>
  );
}

function RetryDialog({ migration, poolName, onClose }: { migration: Migration; poolName: (id: number) => string; onClose: () => void }) {
  const toast = useToast();
  const retry = useAction(
    () =>
      api.migrations.create(
        migration.folder
          ? { bucket: migration.bucket, folder: migration.folder, dst_pool_id: migration.dst_pool_id, vhost_id: migration.vhost_id }
          : { bucket: migration.bucket, src_pool_id: migration.src_pool_id, dst_pool_id: migration.dst_pool_id },
      ),
    {
      onSuccess: (started) => {
        toast.success(`Migration #${started.id} started`, `It continues the move of ${migrationTarget(migration)}.`);
        onClose();
      },
    },
  );
  return (
    <ConfirmDialog
      title={`Start the move of ${migrationTarget(migration)} again`}
      confirmLabel="Start migration"
      danger={false}
      busy={retry.isPending}
      error={retry.error}
      onConfirm={() => retry.mutate(undefined)}
      onClose={onClose}
    >
      <p>
        A new migration from <Mono>{poolName(migration.src_pool_id)}</Mono> to <Mono>{poolName(migration.dst_pool_id)}</Mono>{" "}
        is started. rclone only transfers what is missing.
      </p>
      {migration.split_routing && <p>It takes over the split routing that is still active and finishes the move.</p>}
      <p>Make sure the cause of the earlier stop is fixed: the log of migration #{migration.id} says what it was.</p>
    </ConfirmDialog>
  );
}
