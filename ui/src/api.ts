/**
 * The one place that talks to the orchestrator's REST API.
 *
 * Authentication is the session cookie set by POST /api/auth/login. It is HttpOnly, so the
 * API key never stays in JavaScript: every request is sent with the browser's credentials
 * and no key header. A 401 means the session is gone; the application then shows the login
 * screen (see `onUnauthorized`).
 */

// ── Errors ──────────────────────────────────────────────────────────────────

interface ValidationItem {
  loc?: (string | number)[];
  msg?: string;
}

/** An answer of the API that is not a success. `detail` is the text the API wrote, verbatim. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  /** Field name → message, from a 422 validation answer. */
  readonly fieldErrors: Record<string, string>;

  constructor(status: number, detail: string, fieldErrors: Record<string, string> = {}) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.fieldErrors = fieldErrors;
  }

  /** The refusal says that `allow_orphan` would override it. */
  get offersAllowOrphan(): boolean {
    return [400, 409, 502].includes(this.status) && /allow_orphan/.test(this.detail);
  }

  /** nginx -t refused the change: nothing was saved. */
  get nginxRejected(): boolean {
    return this.status === 422 && this.detail.startsWith("nginx rejected");
  }
}

const LOCATIONS = new Set(["body", "query", "path", "header", "cookie"]);

function cleanMessage(msg: string): string {
  return msg.replace(/^Value error, /, "");
}

/** Turn the body of an error answer into an ApiError. Exported for tests. */
export function toApiError(status: number, statusText: string, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return new ApiError(status, detail);
  if (Array.isArray(detail)) {
    const fieldErrors: Record<string, string> = {};
    const lines: string[] = [];
    for (const item of detail as ValidationItem[]) {
      const msg = cleanMessage(item.msg ?? "invalid value");
      const path = (item.loc ?? []).filter((part, i) => !(i === 0 && LOCATIONS.has(String(part))));
      const field = path.length ? String(path[0]) : "";
      if (field && !(field in fieldErrors)) fieldErrors[field] = msg;
      lines.push(path.length ? `${path.join(".")}: ${msg}` : msg);
    }
    return new ApiError(status, lines.join("\n"), fieldErrors);
  }
  return new ApiError(status, `The server answered ${status}${statusText ? ` ${statusText}` : ""} without an explanation.`);
}

let unauthorizedHandler: (() => void) | null = null;

/** Called whenever a data request is answered 401 (the session expired or was never there). */
export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

type Query = Record<string, string | number | boolean | null | undefined>;

function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}

export async function request<T>(method: string, path: string, options: { body?: unknown; query?: Query } = {}): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (options.body !== undefined) {
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(withQuery(path, options.query), init);
  } catch (cause) {
    throw new ApiError(0, `Cannot reach the orchestrator: ${cause instanceof Error ? cause.message : String(cause)}`);
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/api/auth/")) unauthorizedHandler?.();
    throw toApiError(response.status, response.statusText, body);
  }
  return body as T;
}

const get = <T>(path: string, query?: Query) => request<T>("GET", path, { query });
const post = <T>(path: string, body?: unknown, query?: Query) => request<T>("POST", path, { body, query });
const put = <T>(path: string, body?: unknown) => request<T>("PUT", path, { body });
const del = <T>(path: string, query?: Query) => request<T>("DELETE", path, { query });
const enc = encodeURIComponent;

// ── Types ───────────────────────────────────────────────────────────────────

export interface Session {
  authenticated: boolean;
  dev_mode: boolean;
  version: string;
}

export interface Health {
  status: string;
  version: string;
  dev_mode: boolean;
  db_ok: boolean;
  nginx_config_dir: string;
  drift_alerts: number;
  missing_buckets: number;
}

export type PoolType = "nanio" | "http";
export type LbMethod = "least_conn" | "round_robin" | "ip_hash";

export interface Pool {
  id: number;
  name: string;
  description: string | null;
  type: PoolType;
  lb_method: LbMethod;
  keepalive: number;
  source_nanio_pool_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface PoolInput {
  name: string;
  description: string | null;
  type: PoolType;
  lb_method: LbMethod;
  keepalive: number;
  source_nanio_pool_id: number | null;
}

export type MemberRole = "active" | "primary" | "replica";

export interface Member {
  id: number;
  pool_id: number;
  address: string;
  role: MemberRole;
  weight: number;
  max_fails: number;
  fail_timeout_s: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface MemberInput {
  address: string;
  role: MemberRole;
  weight: number;
  max_fails: number;
  fail_timeout_s: number;
  enabled: boolean;
}

export interface Credentials {
  pool_id: number;
  access_key_masked: string;
  endpoint_url: string | null;
  region: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface CredentialsInput {
  access_key: string;
  secret_key: string;
  region: string;
  endpoint_url: string | null;
}

export type NodeType = "nanio-only" | "nginx-nanio" | "nginx-only";

export interface NodeConfigInput {
  node_type: NodeType;
  data_dir: string;
  nanio_port: number | null;
  nanio_host: string;
  nanio_region: string;
  access_key: string | null;
  secret_key: string | null;
  nanio_bin: string;
}

export interface NodeConfig {
  node_type: string;
  member_address: string;
  files: { path: string; content: string }[];
  instructions: string;
  warnings: string[];
}

export type Zone = "top" | "ssl" | "proxy" | "end";

export interface ExtraBlock {
  zone: Zone;
  content: string;
}

export type IpRuleMode = "allow" | "deny";

export interface Vhost {
  id: number;
  server_name: string;
  listen_port: number;
  ssl: boolean;
  ssl_cert_path: string | null;
  ssl_key_path: string | null;
  extra_blocks: ExtraBlock[] | null;
  enabled: boolean;
  default_pool_id: number | null;
  ip_rule_mode: IpRuleMode | null;
  ip_rule_ips: string[] | null;
  created_at: string;
  updated_at: string;
}

export interface VhostInput {
  server_name: string;
  listen_port: number;
  ssl: boolean;
  ssl_cert_path: string | null;
  ssl_key_path: string | null;
  extra_blocks: ExtraBlock[];
  default_pool_id: number | null;
  ip_rule_mode: IpRuleMode | null;
  ip_rule_ips: string[] | null;
  /** false: the server block is kept on disk but nginx does not load it. */
  enabled?: boolean;
}

export interface Route {
  id: number;
  vhost_id: number;
  path_prefix: string;
  pool_id: number;
  pool_name: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  migration_id?: number | null;
}

export interface RouteCreated extends Route {
  bucket: string;
  bucket_provisioned: boolean;
  default_pool_id: number | null;
  warnings: string[];
}

export type BucketStatus = "unrouted" | "routed" | "migrating" | "ignored" | "deleted";

export interface Bucket {
  name: string;
  status: BucketStatus;
  pool_name: string | null;
  routed_pool_id: number | null;
  object_count: number | null;
  discovered_at: string | null;
  missing_since: string | null;
}

export interface BucketList {
  vhost_id: number;
  buckets: Bucket[];
  last_synced_at: string | null;
}

export interface PromoteResult {
  ok: boolean;
  output: string;
  bucket: string;
  pool: string;
  route: string;
  migration_started: boolean;
  migration_id?: number;
}

export interface OrphanList {
  vhost_id: number;
  orphans: { bucket: string; objects: number }[];
  checked: number;
}

export interface PurgeResult {
  ok: boolean;
  bucket: string;
  deleted: number;
  total: number;
  errors: string[];
}

export interface HttpBucketRoutes {
  vhost_id: number;
  server_name: string;
  source_nanio_pool_id: number;
  source_nanio_pool_name: string;
  routes: { id: number; path_prefix: string; pool_id: number; pool_name: string; bucket: string }[];
  linked_nanio_buckets: { bucket: string; has_route: boolean }[];
}

export type Phase = "pending" | "copying" | "write_routing" | "verifying" | "switching" | "done" | "error" | "cancelled";

export interface Migration {
  id: number;
  vhost_id: number;
  bucket: string;
  src_pool_id: number;
  dst_pool_id: number;
  phase: Phase;
  objects_total: number;
  objects_done: number;
  bytes_total: number;
  bytes_done: number;
  error_msg: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  orphaned_source_pool_id: number | null;
  orphaned_source_prefix: string | null;
  orphaned_at: string | null;
  split_routing: boolean;
  folder: string;
  cascade_warnings: string[] | null;
}

export interface MigrationInput {
  bucket: string;
  dst_pool_id: number;
  src_pool_id?: number;
  folder?: string;
  vhost_id?: number;
}

export interface MigrationLogEntry {
  id: number;
  migration_id: number;
  phase: string;
  message: string;
  created_at: string;
}

export interface OrphanedMigration {
  migration_id: number;
  bucket: string;
  src_pool_id: number;
  dst_pool_id: number;
  orphaned_source_pool_id: number;
  orphaned_source_prefix: string;
  orphaned_at: string;
  finished_at: string | null;
}

export interface StaleMigration {
  migration_id: number;
  bucket: string;
  src_pool_id: number;
  dst_pool_id: number;
  phase: string;
  reason: string;
  created_at: string;
}

export interface ConfigFile {
  path: string;
  sha256_disk: string | null;
  sha256_db: string | null;
  drifted: boolean;
  last_synced_at: string | null;
}

export interface PendingFile {
  path: string;
  change: "added" | "modified" | "removed";
}

export interface ConfigStatus {
  files: ConfigFile[];
  last_reload_ok: boolean | null;
  last_reload_at: string | null;
  /** null: no reload recorded yet, so it cannot be told whether nginx runs these files. */
  pending_reload: boolean | null;
  pending_files: PendingFile[];
  last_applied_at: string | null;
}

export interface NginxResult {
  ok: boolean;
  output: string;
}

export interface ConfigFileContent {
  path: string;
  content: string | null;
  recorded: string | null;
  drifted: boolean;
}

export interface SyncResult {
  ok: boolean;
  pools_new: number;
  pools_updated: number;
  members_new: number;
  vhosts_new: number;
  vhosts_updated: number;
  routes_synced: number;
  files_synced: number;
  warnings: string[];
}

export interface RebuildResult {
  ok: boolean;
  output: string;
  written: string[];
  removed: string[];
  errors: string[];
}

export type SettingValue = string | number | boolean | null;
export type Settings = Record<string, SettingValue | string[]> & {
  config_file: string;
  dev_mode: boolean;
  host_only: string[];
};

export interface AuditEntry {
  id: number;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: number | null;
  before_json: string | null;
  after_json: string | null;
  nginx_reload_ok: boolean | null;
  nginx_reload_output: string | null;
  created_at: string;
}

export interface PoolSummary {
  id: number;
  name: string;
  type: PoolType;
  members: number;
  enabled_members: number;
  has_credentials: boolean;
}

export interface MissingBucket {
  vhost_id: number;
  server_name: string;
  bucket: string;
  pool_name: string | null;
  missing_since: string;
}

export interface Overview {
  pools: PoolSummary[];
  vhosts: number;
  routes: number;
  unrouted_buckets: number;
  missing_buckets: MissingBucket[];
}

export interface PoolRef {
  id: number;
  name: string;
}

export interface ExplorerBuckets {
  pool: PoolRef;
  member: string;
  buckets: string[];
}

export interface ExplorerObject {
  key: string;
  size: number;
  last_modified: string | null;
  etag: string | null;
}

export interface ExplorerPage {
  pool: PoolRef;
  bucket: string;
  prefix: string;
  folders: { prefix: string; routed_pool: PoolRef | null }[];
  objects: ExplorerObject[];
  next_token: string | null;
}

export interface ExplorerObjectDetails {
  pool: PoolRef;
  bucket: string;
  key: string;
  size: number | null;
  last_modified: string | null;
  etag: string | null;
  content_type: string | null;
}

export interface Located {
  vhost: { id: number; server_name: string };
  bucket: string;
  prefix: string;
  route: string;
  /** The pool that answers. During a split it is the destination of the migration. */
  pool: PoolRef & { type: PoolType };
  /** Set while a migration has split routing active on that route. */
  split: { migration_id: number; source: PoolRef; destination: PoolRef } | null;
}

// ── Endpoints ───────────────────────────────────────────────────────────────

export const api = {
  session: () => get<Session>("/api/auth/session"),
  login: (key: string) => post<Session>("/api/auth/login", { key }),
  logout: () => post<Session>("/api/auth/logout"),
  health: () => get<Health>("/api/health"),
  overview: () => get<Overview>("/api/overview"),

  pools: {
    list: () => get<Pool[]>("/api/pools"),
    create: (body: PoolInput) => post<Pool>("/api/pools", body),
    update: (id: number, body: Partial<PoolInput>) => put<Pool>(`/api/pools/${id}`, body),
    remove: (id: number) => del<void>(`/api/pools/${id}`),
    preview: (id: number) => get<{ filepath: string; content: string | null }>(`/api/config/preview/pool/${id}`),
    members: (id: number) => get<Member[]>(`/api/pools/${id}/members`),
    addMember: (id: number, body: MemberInput) => post<Member>(`/api/pools/${id}/members`, body),
    updateMember: (id: number, memberId: number, body: Partial<MemberInput>) =>
      put<Member>(`/api/pools/${id}/members/${memberId}`, body),
    removeMember: (id: number, memberId: number) => del<void>(`/api/pools/${id}/members/${memberId}`),
    nodeConfig: (id: number, memberId: number, body: NodeConfigInput) =>
      post<NodeConfig>(`/api/pools/${id}/members/${memberId}/node-config`, body),
    credentials: (id: number) => get<Credentials>(`/api/pools/${id}/credentials`),
    setCredentials: (id: number, body: CredentialsInput) => put<Credentials>(`/api/pools/${id}/credentials`, body),
    removeCredentials: (id: number) => del<{ ok: boolean }>(`/api/pools/${id}/credentials`),
    bucketObjects: (id: number, bucket: string) =>
      get<{ pool_id: number; bucket: string; objects: string[]; count: number }>(
        `/api/pools/${id}/buckets/${enc(bucket)}/objects`,
      ),
    purgeBucket: (id: number, bucket: string) =>
      post<{ ok: boolean; deleted: number; total: number; errors: string[] }>(
        `/api/pools/${id}/buckets/${enc(bucket)}/purge`,
      ),
  },

  vhosts: {
    list: () => get<Vhost[]>("/api/vhosts"),
    create: (body: VhostInput) => post<Vhost>("/api/vhosts", body),
    update: (id: number, body: Partial<VhostInput>) => put<Vhost>(`/api/vhosts/${id}`, body),
    remove: (id: number) => del<void>(`/api/vhosts/${id}`),
    preview: (id: number) => get<{ filepath: string; content: string }>(`/api/vhosts/${id}/preview`),
    routes: (id: number) => get<Route[]>(`/api/vhosts/${id}/routes`),
    addRoute: (id: number, body: { path_prefix: string; pool_id: number; allow_orphan?: boolean }) =>
      post<RouteCreated>(`/api/vhosts/${id}/routes`, body),
    updateRoute: (id: number, routeId: number, body: { pool_id?: number; enabled?: boolean; allow_orphan?: boolean }) =>
      put<Route>(`/api/vhosts/${id}/routes/${routeId}`, body),
    removeRoute: (id: number, routeId: number, allowOrphan = false) =>
      del<void>(`/api/vhosts/${id}/routes/${routeId}`, allowOrphan ? { allow_orphan: true } : undefined),
  },

  buckets: {
    list: (vhostId: number) => get<BucketList>(`/api/vhosts/${vhostId}/buckets`),
    sync: (vhostId: number) => post<Record<string, unknown>>(`/api/vhosts/${vhostId}/buckets/sync`),
    promote: (vhostId: number, bucket: string, body: { pool_id: number; migrate: boolean; allow_orphan?: boolean }) =>
      post<PromoteResult>(`/api/vhosts/${vhostId}/buckets/${enc(bucket)}/promote`, body),
    ignore: (vhostId: number, bucket: string) =>
      post<{ ok: boolean }>(`/api/vhosts/${vhostId}/buckets/${enc(bucket)}/ignore`),
    removeRoute: (vhostId: number, bucket: string, allowOrphan = false) =>
      del<{ ok: boolean }>(
        `/api/vhosts/${vhostId}/buckets/${enc(bucket)}/route`,
        allowOrphan ? { allow_orphan: true } : undefined,
      ),
    orphans: (vhostId: number) => get<OrphanList>(`/api/vhosts/${vhostId}/buckets/orphans`),
    purgeOrphan: (vhostId: number, bucket: string) =>
      post<PurgeResult>(`/api/vhosts/${vhostId}/buckets/${enc(bucket)}/purge-orphan`),
    httpRoutes: (vhostId: number) => get<HttpBucketRoutes>(`/api/vhosts/${vhostId}/http-bucket-routes`),
    addHttpRoute: (vhostId: number, bucket: string) =>
      post<{ ok: boolean }>(`/api/vhosts/${vhostId}/http-bucket-routes/${enc(bucket)}`),
    removeHttpRoute: (vhostId: number, bucket: string) =>
      del<{ ok: boolean }>(`/api/vhosts/${vhostId}/http-bucket-routes/${enc(bucket)}`),
  },

  migrations: {
    list: () => get<Migration[]>("/api/migrations", { limit: 200 }),
    create: (body: MigrationInput) => post<Migration>("/api/migrations", body),
    cancel: (id: number) => post<{ ok: boolean }>(`/api/migrations/${id}/cancel`),
    log: (id: number) => get<MigrationLogEntry[]>(`/api/migrations/${id}/log`, { limit: 500 }),
    sourceBuckets: (poolId: number) =>
      get<{ pool_id: number; buckets: string[] }>("/api/migrations/source-buckets", { pool_id: poolId }),
    orphaned: () => get<OrphanedMigration[]>("/api/migrations/orphaned"),
    stale: () => get<StaleMigration[]>("/api/migrations/stale"),
  },

  config: {
    status: () => get<ConfigStatus>("/api/config/status"),
    validate: () => post<NginxResult>("/api/config/validate"),
    reload: () => post<NginxResult>("/api/config/reload"),
    rebuild: () => post<RebuildResult>("/api/config/rebuild"),
    sync: () => post<SyncResult>("/api/config/sync"),
    file: (path: string) => get<ConfigFileContent>("/api/config/file", { path }),
    absorb: (path: string) => post<{ ok: boolean }>("/api/config/absorb-file", { path }),
    rewrite: (path: string) => post<{ ok: boolean; output?: string; action?: string }>("/api/config/rewrite-file", { path }),
    rebuildFromDisk: (options: { dry_run?: boolean; force?: boolean }) =>
      post<Record<string, unknown>>("/api/config/rebuild-from-disk", undefined, options),
    backup: () => post<{ ok: boolean; backup_path?: string; detail?: string }>("/api/config/backup"),
    settings: () => get<Settings>("/api/config/settings"),
    setSetting: (key: string, value: string) =>
      put<{ ok: boolean; key: string; value: string | null; restart_required: boolean }>(
        `/api/config/settings/${enc(key)}`,
        { value },
      ),
    restart: () => post<{ ok: boolean; message: string }>("/api/config/settings/restart"),
  },

  audit: (query: { page: number; per_page: number; entity_type?: string; from?: string; to?: string }) =>
    get<AuditEntry[]>("/api/audit", query),

  explorer: {
    buckets: (poolId: number) => get<ExplorerBuckets>(`/api/explorer/pools/${poolId}/buckets`),
    objects: (poolId: number, bucket: string, query: { prefix: string; token?: string; vhost_id?: number }) =>
      get<ExplorerPage>(`/api/explorer/pools/${poolId}/buckets/${enc(bucket)}/objects`, query),
    object: (poolId: number, bucket: string, key: string) =>
      get<ExplorerObjectDetails>(`/api/explorer/pools/${poolId}/buckets/${enc(bucket)}/object`, { key }),
    /** Fetch one object (the server refuses above its cap) so that a refusal can be shown. */
    download: async (poolId: number, bucket: string, key: string): Promise<Blob> => {
      const url = `/api/explorer/pools/${poolId}/buckets/${enc(bucket)}/download?key=${enc(key)}`;
      let response: Response;
      try {
        response = await fetch(url, { credentials: "same-origin" });
      } catch (cause) {
        throw new ApiError(0, `Cannot reach the orchestrator: ${cause instanceof Error ? cause.message : String(cause)}`);
      }
      if (!response.ok) {
        if (response.status === 401) unauthorizedHandler?.();
        throw toApiError(response.status, response.statusText, await response.json().catch(() => null));
      }
      return response.blob();
    },
    locate: (vhostId: number, bucket: string, prefix: string) =>
      get<Located>(`/api/explorer/vhosts/${vhostId}/locate`, { bucket, prefix }),
  },
};
