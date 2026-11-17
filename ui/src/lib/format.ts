import type { ConfigStatus, Migration, Phase, Route } from "../api";

/** The API sends UTC, as "2026-01-02 03:04:05" (SQLite) or ISO 8601 with a Z. */
export function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function timeAgo(value: string | null | undefined, now: Date = new Date()): string {
  const date = parseDate(value);
  if (!date) return "never";
  const seconds = Math.max(0, Math.round((now.getTime() - date.getTime()) / 1000));
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

// ── Migrations ──────────────────────────────────────────────────────────────

export const STEPS: Phase[] = ["copying", "write_routing", "verifying", "switching", "done"];
const ACTIVE: Phase[] = ["pending", "copying", "write_routing", "verifying", "switching"];

export const PHASE_LABEL: Record<Phase, string> = {
  pending: "Pending",
  copying: "Copying",
  write_routing: "Split routing",
  verifying: "Verifying",
  switching: "Switching",
  done: "Done",
  error: "Error",
  cancelled: "Cancelled",
};

export function isActive(migration: Pick<Migration, "phase">): boolean {
  return ACTIVE.includes(migration.phase);
}

/** Ended without finishing while nginx still serves the bucket through the split. */
export function splitLeftActive(migration: Pick<Migration, "phase" | "split_routing">): boolean {
  return migration.split_routing && (migration.phase === "error" || migration.phase === "cancelled");
}

/** Splits left behind that nothing is finishing: no migration of the same bucket/folder is running now. */
export function unresolvedSplits(migrations: Migration[]): Migration[] {
  const running = new Set(migrations.filter(isActive).map((m) => `${m.vhost_id}/${m.bucket}/${m.folder}`));
  return migrations.filter((m) => splitLeftActive(m) && !running.has(`${m.vhost_id}/${m.bucket}/${m.folder}`));
}

export function migrationTarget(migration: Pick<Migration, "bucket" | "folder">): string {
  return migration.folder ? `${migration.bucket}/${migration.folder}` : migration.bucket;
}

export function progressPercent(migration: Pick<Migration, "objects_done" | "objects_total" | "phase">): number | null {
  if (migration.phase === "done") return 100;
  if (migration.objects_total <= 0) return null;
  return Math.min(100, Math.round((migration.objects_done / migration.objects_total) * 100));
}

// ── Changes waiting for a reload ────────────────────────────────────────────

export type PendingState =
  | { kind: "applied" }
  | { kind: "pending"; count: number; message: string }
  | { kind: "unknown"; message: string };

/** What the "not applied" indicator says for a config status. */
export function pendingState(status: Pick<ConfigStatus, "pending_reload" | "pending_files"> | undefined): PendingState {
  if (!status || status.pending_reload === false) return { kind: "applied" };
  if (status.pending_reload === null) {
    return {
      kind: "unknown",
      message: "Configuration files exist, but nginx has not been reloaded through the orchestrator yet: it cannot be told whether nginx is serving them.",
    };
  }
  const count = status.pending_files.length;
  return {
    kind: "pending",
    count,
    message: `${plural(count, "change")} not applied — nginx is still serving the previous configuration.`,
  };
}

// ── Routes ──────────────────────────────────────────────────────────────────

export interface BucketNode {
  bucket: string;
  /** The `/bucket/` route, when the bucket has one; otherwise it is served by the default pool. */
  route: Route | null;
  folders: Route[];
}

export interface RouteTree {
  defaultRoute: Route | null;
  buckets: BucketNode[];
}

/** Group a vhost's routes as bucket → folders. */
export function buildRouteTree(routes: Route[]): RouteTree {
  const nodes = new Map<string, BucketNode>();
  let defaultRoute: Route | null = null;
  for (const route of routes) {
    const segments = route.path_prefix.split("/").filter(Boolean);
    const bucket = segments[0];
    if (!bucket) {
      defaultRoute = route;
      continue;
    }
    const node = nodes.get(bucket) ?? { bucket, route: null, folders: [] };
    if (segments.length === 1) node.route = route;
    else node.folders.push(route);
    nodes.set(bucket, node);
  }
  const buckets = [...nodes.values()].sort((a, b) => a.bucket.localeCompare(b.bucket));
  for (const node of buckets) node.folders.sort((a, b) => a.path_prefix.localeCompare(b.path_prefix));
  return { defaultRoute, buckets };
}

export function basename(path: string): string {
  return path.split("/").pop() ?? path;
}
