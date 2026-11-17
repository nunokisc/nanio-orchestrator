import { useQuery } from "@tanstack/react-query";
import { Check, Circle } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, type ConfigStatus, type Migration, type Overview } from "../api";
import { Badge, Mono, Notice, PageHeader, Panel, Progress, QueryView, type Tone } from "../components/ui";
import {
  formatDateTime,
  isActive,
  migrationTarget,
  pendingState,
  PHASE_LABEL,
  plural,
  progressPercent,
  timeAgo,
  unresolvedSplits,
} from "../lib/format";
import { useConfigStatus, useHealth, useMigrations, useOverview } from "../queries";
import { poolProblems } from "./Pools";

function Tile({ label, value, tone, to, note }: { label: string; value: ReactNode; tone: Tone; to: string; note?: string }) {
  return (
    <Link to={to} className={`tile tile-${tone}`}>
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
      {note && <span className="tile-note">{note}</span>}
    </Link>
  );
}

interface Problem {
  key: string;
  tone: Tone;
  title: string;
  text: ReactNode;
  to: string;
  action: string;
}

function findProblems(
  overview: Overview,
  status: ConfigStatus | undefined,
  migrations: Migration[],
  nginx: { ok: boolean; output: string } | undefined,
  dbOk: boolean,
): Problem[] {
  const problems: Problem[] = [];
  if (!dbOk) {
    problems.push({ key: "db", tone: "bad", title: "The database cannot be read", text: "See the service log. If it is lost or corrupt it can be rebuilt from the files on disk.", to: "/configuration", action: "Configuration" });
  }
  if (nginx && !nginx.ok) {
    problems.push({ key: "nginx", tone: "bad", title: "nginx rejects the configuration on disk", text: <pre className="output">{nginx.output}</pre>, to: "/configuration", action: "Configuration" });
  }
  const pending = pendingState(status);
  if (pending.kind !== "applied") {
    problems.push({ key: "pending", tone: "warn", title: pending.kind === "pending" ? "Changes are waiting to be applied" : "No reload recorded yet", text: pending.message, to: "/configuration", action: "Review and reload" });
  }
  const drifted = status?.files.filter((file) => file.drifted) ?? [];
  if (drifted.length) {
    problems.push({
      key: "drift",
      tone: "warn",
      title: `${plural(drifted.length, "file")} edited by hand`,
      text: (
        <>
          {drifted.map((file) => file.path.split("/").slice(-2).join("/")).join(", ")}. Nothing is corrected automatically:
          keep the edit (absorb) or discard it (rewrite).
        </>
      ),
      to: "/configuration",
      action: "Resolve",
    });
  }
  for (const missing of overview.missing_buckets) {
    problems.push({
      key: `missing-${missing.vhost_id}-${missing.bucket}`,
      tone: "bad",
      title: `Bucket ${missing.bucket} is missing on ${missing.pool_name ?? "its pool"}`,
      text: `${missing.server_name} routes it there, but the pool says it does not exist (since ${formatDateTime(missing.missing_since)}). Either its owner deleted it, or the node's storage is not mounted.`,
      to: `/buckets?vhost=${missing.vhost_id}`,
      action: "Buckets",
    });
  }
  for (const migration of unresolvedSplits(migrations)) {
    problems.push({
      key: `split-${migration.id}`,
      tone: "warn",
      title: `Migration #${migration.id} of ${migrationTarget(migration)} stopped with split routing active`,
      text: "Clients keep working through the split. Start the same move again to finish it.",
      to: "/migrations",
      action: "Migrations",
    });
  }
  for (const pool of overview.pools) {
    const issues = poolProblems(pool);
    if (issues.length) {
      problems.push({ key: `pool-${pool.id}`, tone: "warn", title: `Pool ${pool.name} ${issues.join(" and ")}`, text: pool.type === "nanio" ? "Without members it is not written to nginx; without credentials its buckets cannot be listed or migrated." : "Without members it is not written to nginx.", to: "/pools", action: "Pools" });
    }
  }
  return problems;
}

function FirstRun({ overview, status }: { overview: Overview; status: ConfigStatus | undefined }) {
  const nanio = overview.pools.filter((pool) => pool.type === "nanio");
  const steps: { done: boolean; title: string; text: string; to: string }[] = [
    { done: overview.pools.length > 0, title: "Create a pool", text: "A pool is a group of machines that hold the same data.", to: "/pools" },
    { done: overview.pools.some((pool) => pool.members > 0), title: "Add its machines", text: "Each nanio machine is a member: host:port. Node setup generates what the machine itself needs.", to: "/pools" },
    { done: nanio.length > 0 && nanio.every((pool) => pool.has_credentials), title: "Store its S3 credentials", text: "The orchestrator needs them to list buckets and to run migrations. They are stored encrypted.", to: "/pools" },
    { done: overview.vhosts > 0, title: "Create a vhost", text: "The hostname clients connect to, with that pool as its default pool.", to: "/vhosts" },
    { done: Boolean(status?.last_applied_at) && status?.pending_reload === false, title: "Reload nginx", text: "Changes are checked by nginx when you make them and applied only when you reload.", to: "/configuration" },
  ];
  if (steps.every((step) => step.done)) return null;
  const next = steps.findIndex((step) => !step.done);
  return (
    <Panel title="Getting started" meta="Five steps to a working gateway.">
      <ol className="steps">
        {steps.map((step, index) => (
          <li key={step.title} className={step.done ? "done" : index === next ? "next" : undefined}>
            <span className="steps-mark" aria-hidden>
              {step.done ? <Check className="icon" /> : <Circle className="icon" />}
            </span>
            <div>
              <div className="steps-title">
                {index + 1}. {step.title} {step.done && <span className="sr-only">(done)</span>}
              </div>
              <div className="muted">{step.text}</div>
            </div>
            {!step.done && index === next && (
              <Link to={step.to} className="btn btn-primary btn-sm">
                {step.title}
              </Link>
            )}
          </li>
        ))}
      </ol>
    </Panel>
  );
}

export function OverviewPage() {
  const overview = useOverview();
  const status = useConfigStatus();
  const health = useHealth();
  const migrations = useMigrations();
  const nginx = useQuery({ queryKey: ["config", "validate"], queryFn: api.config.validate, refetchInterval: 60_000, staleTime: 30_000 });
  const audit = useQuery({ queryKey: ["audit", "recent"], queryFn: () => api.audit({ page: 1, per_page: 10 }), refetchInterval: 15_000 });

  return (
    <>
      <PageHeader title="Overview" description="The state of the gateway at a glance. Traffic never passes through the orchestrator: if it stops, nginx keeps serving as configured." />
      <QueryView query={overview}>
        {(data) => {
          const list = migrations.data ?? [];
          const running = list.filter(isActive);
          const splits = unresolvedSplits(list);
          const pending = pendingState(status.data);
          const drifted = status.data?.files.filter((file) => file.drifted).length ?? 0;
          const problems = findProblems(data, status.data, list, nginx.data, health.data?.db_ok ?? true);
          return (
            <div className="stack-lg">
              <FirstRun overview={data} status={status.data} />

              <div className="tiles">
                <Tile
                  label="nginx configuration"
                  to="/configuration"
                  tone={nginx.data ? (nginx.data.ok ? "ok" : "bad") : "neutral"}
                  value={nginx.data ? (nginx.data.ok ? "Valid" : "Rejected") : "…"}
                  note="nginx -t on the files on disk"
                />
                <Tile
                  label="Waiting for a reload"
                  to="/configuration"
                  tone={pending.kind === "applied" ? "ok" : "warn"}
                  value={pending.kind === "pending" ? pending.count : pending.kind === "unknown" ? "?" : 0}
                  note={status.data?.last_applied_at ? `last reload ${timeAgo(status.data.last_applied_at)}` : "no reload recorded yet"}
                />
                <Tile label="Drifted files" to="/configuration" tone={drifted ? "warn" : "ok"} value={drifted} note="edited by hand" />
                <Tile
                  label="Buckets missing on pool"
                  to="/buckets"
                  tone={data.missing_buckets.length ? "bad" : "ok"}
                  value={data.missing_buckets.length}
                  note="routed, but not on the pool"
                />
                <Tile label="Migrations running" to="/migrations" tone={running.length ? "info" : "neutral"} value={running.length} />
                <Tile label="Splits left active" to="/migrations" tone={splits.length ? "warn" : "ok"} value={splits.length} note="interrupted migrations" />
              </div>

              <Panel title="Needs attention">
                {problems.length === 0 ? (
                  <Notice tone="ok" title="Nothing needs attention" />
                ) : (
                  <div className="stack">
                    {problems.map((problem) => (
                      <Notice
                        key={problem.key}
                        tone={problem.tone}
                        title={problem.title}
                        actions={
                          <Link to={problem.to} className="btn btn-default btn-sm">
                            {problem.action}
                          </Link>
                        }
                      >
                        {problem.text}
                      </Notice>
                    ))}
                  </div>
                )}
              </Panel>

              <div className="columns">
                <Panel title="Cluster">
                  <dl className="kv">
                    <div>
                      <dt>Pools</dt>
                      <dd>
                        <Link to="/pools">{data.pools.length}</Link>{" "}
                        <span className="muted">
                          ({data.pools.filter((pool) => pool.type === "nanio").length} nanio, {data.pools.filter((pool) => pool.type === "http").length} http)
                        </span>
                      </dd>
                    </div>
                    <div>
                      <dt>Vhosts</dt>
                      <dd>
                        <Link to="/vhosts">{data.vhosts}</Link>
                      </dd>
                    </div>
                    <div>
                      <dt>Bucket and folder routes</dt>
                      <dd>
                        <Link to="/vhosts">{data.routes}</Link>
                      </dd>
                    </div>
                    <div>
                      <dt>Buckets on a default pool</dt>
                      <dd>
                        <Link to="/buckets">{data.unrouted_buckets}</Link>
                      </dd>
                    </div>
                    <div>
                      <dt>Last reload</dt>
                      <dd>{status.data?.last_applied_at ? `${formatDateTime(status.data.last_applied_at)} (${timeAgo(status.data.last_applied_at)})` : "not recorded yet"}</dd>
                    </div>
                    <div>
                      <dt>Managed config</dt>
                      <dd>
                        <Mono>{health.data?.nginx_config_dir ?? "…"}</Mono>
                      </dd>
                    </div>
                  </dl>
                </Panel>

                <Panel title="Migrations in progress">
                  {running.length === 0 ? (
                    <p className="muted">Nothing is being moved.</p>
                  ) : (
                    <div className="stack">
                      {running.map((migration) => (
                        <Link key={migration.id} to="/migrations" className="mini-migration">
                          <span>
                            <Mono>{migrationTarget(migration)}</Mono> <Badge tone="info">{PHASE_LABEL[migration.phase]}</Badge>
                          </span>
                          <Progress percent={progressPercent(migration)} label={`Progress of migration ${migration.id}`} />
                          <span className="muted">
                            {migration.objects_done} of {migration.objects_total || "?"} objects
                          </span>
                        </Link>
                      ))}
                    </div>
                  )}
                </Panel>
              </div>

              <Panel
                flush
                title="Recent activity"
                actions={
                  <Link to="/audit" className="btn btn-ghost btn-sm">
                    Audit log
                  </Link>
                }
              >
                <QueryView query={audit} isEmpty={(entries) => entries.length === 0} empty={<p className="muted table-note">Nothing has been changed yet.</p>}>
                  {(entries) => (
                    <table className="table">
                      <tbody>
                        {entries.map((entry) => (
                          <tr key={entry.id}>
                            <td title={formatDateTime(entry.created_at)}>{timeAgo(entry.created_at)}</td>
                            <td>
                              <Mono>{entry.action}</Mono>
                            </td>
                            <td>
                              {entry.entity_type.replace("_", " ")}
                              {entry.entity_id !== null && <span className="muted"> #{entry.entity_id}</span>}
                            </td>
                            <td>{entry.nginx_reload_ok === false && <Badge tone="bad">nginx refused</Badge>}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </QueryView>
              </Panel>
            </div>
          );
        }}
      </QueryView>
    </>
  );
}
