/**
 * Form validation with the same rules as the API (nanio_orchestrator/models.py), so that a
 * mistake is shown next to the field before anything is sent.
 */
import type { PoolType } from "../api";

export type Errors = Record<string, string>;

export const POOL_NAME_RE = /^[a-zA-Z0-9._-]+$/;
export const SERVER_NAME_RE = /^(\*\.)?[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$/;
export const IP_ENTRY_RE = /^[\d:a-fA-F./]+$/;
export const PATH_PREFIX_RE = /^\/[A-Za-z0-9._@-]+(?:\/[A-Za-z0-9._@-]+)*\/?$/;
const NODE_PATH_RE = /^\/[A-Za-z0-9._/-]+$/;
// eslint-disable-next-line no-control-regex
const NODE_SECRET_RE = /^[^"\\\x00-\x1f\x7f]+$/;

/** A whole number within [min, max], or the message to show. */
export function intInRange(raw: string, min: number, max: number, label: string): number | string {
  const text = raw.trim();
  if (!/^-?\d+$/.test(text)) return `${label} must be a whole number`;
  const value = Number(text);
  if (value < min || value > max) return `${label} must be between ${min} and ${max}`;
  return value;
}

function checkInt(errors: Errors, field: string, raw: string, min: number, max: number, label: string): void {
  const result = intInRange(raw, min, max, label);
  if (typeof result === "string") errors[field] = result;
}

export interface PoolForm {
  name: string;
  type: PoolType;
  keepalive: string;
  source_nanio_pool_id: string;
}

export function validatePool(form: PoolForm): Errors {
  const errors: Errors = {};
  if (!form.name) errors.name = "A name is required";
  else if (form.name.length > 128) errors.name = "At most 128 characters";
  else if (!POOL_NAME_RE.test(form.name)) errors.name = "Only letters, digits, dot, underscore and hyphen (it becomes the nginx upstream name)";
  checkInt(errors, "keepalive", form.keepalive, 0, 1024, "Keepalive");
  if (form.source_nanio_pool_id && form.type !== "http") {
    errors.source_nanio_pool_id = "Only an http pool can be linked to a nanio pool";
  }
  return errors;
}

export interface MemberForm {
  address: string;
  role: string;
  weight: string;
  max_fails: string;
  fail_timeout_s: string;
}

export function validateMember(form: MemberForm, poolType: PoolType): Errors {
  const errors: Errors = {};
  const address = form.address.trim();
  if (!address) errors.address = "An address is required";
  else if (!address.startsWith("unix:") && !address.includes(":")) errors.address = "Use host:port, or unix:/path";
  else if (/[\s;{}]/.test(address)) errors.address = "No spaces, semicolons or braces";
  if (poolType === "nanio" && form.role !== "active") errors.role = "Every member of a nanio pool is active";
  if (poolType === "http" && !["primary", "replica"].includes(form.role)) errors.role = "Choose primary or replica";
  checkInt(errors, "weight", form.weight, 1, 100, "Weight");
  checkInt(errors, "max_fails", form.max_fails, 0, 100, "Max fails");
  checkInt(errors, "fail_timeout_s", form.fail_timeout_s, 0, 600, "Fail timeout");
  return errors;
}

/** One address per line or separated by commas/spaces. */
export function parseIpList(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

export interface VhostForm {
  server_name: string;
  listen_port: string;
  ssl: boolean;
  ssl_cert_path: string;
  ssl_key_path: string;
  ip_rule_mode: "" | "allow" | "deny";
  ip_rule_ips: string;
  extra_blocks: { zone: string; content: string }[];
}

export function validateVhost(form: VhostForm): Errors {
  const errors: Errors = {};
  const name = form.server_name.trim();
  if (!name) errors.server_name = "A hostname is required";
  else if (name.length > 253) errors.server_name = "At most 253 characters";
  else if (!SERVER_NAME_RE.test(name)) errors.server_name = "A hostname, or a wildcard such as *.example.com";
  checkInt(errors, "listen_port", form.listen_port, 1, 65535, "Port");
  if (form.ssl) {
    if (!form.ssl_cert_path.trim()) errors.ssl_cert_path = "Required when TLS is on";
    if (!form.ssl_key_path.trim()) errors.ssl_key_path = "Required when TLS is on";
  }
  const ips = parseIpList(form.ip_rule_ips);
  if (form.ip_rule_mode && ips.length === 0) errors.ip_rule_ips = "List at least one address";
  const bad = ips.find((ip) => !IP_ENTRY_RE.test(ip));
  if (bad) errors.ip_rule_ips = `Not an IP address or CIDR range: ${bad}`;
  form.extra_blocks.forEach((block, index) => {
    if (!block.content.trim()) errors[`extra_blocks.${index}`] = "Write the directives, or remove this block";
  });
  return errors;
}

export type RouteKind = "bucket" | "folder";

/** What a route path means, or why it is not valid. */
export function describeRoutePath(path: string): { kind: RouteKind; bucket: string; folder: string } | { error: string } {
  if (!path) return { error: "A path is required" };
  if (!path.startsWith("/")) return { error: "The path starts with /" };
  if (path === "/") return { error: "/ is the default pool: change it on the vhost" };
  if (!PATH_PREFIX_RE.test(path)) {
    return { error: "Letters, digits and . _ @ - in each segment; no empty segment, no spaces" };
  }
  const segments = path.split("/").filter(Boolean);
  const bucket = segments[0] ?? "";
  return segments.length === 1
    ? { kind: "bucket", bucket, folder: "" }
    : { kind: "folder", bucket, folder: `${segments.slice(1).join("/")}/` };
}

/** The API stores `/bucket/` and `/bucket/folder/`: add the trailing slash a user leaves out. */
export function normalizeRoutePath(path: string): string {
  const trimmed = path.trim();
  return trimmed && !trimmed.endsWith("/") ? `${trimmed}/` : trimmed;
}

/** "2025", "/2025/q1" → "2025/", "2025/q1/" as the API does; an error message otherwise. */
export function normalizeFolder(raw: string): { folder: string } | { error: string } {
  const stripped = raw.trim().replace(/^\/+|\/+$/g, "");
  if (!stripped) return { folder: "" };
  if (!PATH_PREFIX_RE.test(`/${stripped}/`)) return { error: "Letters, digits and . _ @ - separated by /" };
  return { folder: `${stripped}/` };
}

export function validateCredentials(form: { access_key: string; secret_key: string; region: string }): Errors {
  const errors: Errors = {};
  if (!form.access_key.trim()) errors.access_key = "Required";
  if (!form.secret_key) errors.secret_key = "Required";
  if (!form.region.trim()) errors.region = "Required";
  return errors;
}

export interface NodeForm {
  data_dir: string;
  nanio_port: string;
  nanio_host: string;
  nanio_region: string;
  access_key: string;
  secret_key: string;
  nanio_bin: string;
}

export function validateNodeConfig(form: NodeForm): Errors {
  const errors: Errors = {};
  if (!NODE_PATH_RE.test(form.data_dir)) errors.data_dir = "An absolute path of letters, digits and . _ - /";
  if (!NODE_PATH_RE.test(form.nanio_bin)) errors.nanio_bin = "An absolute path of letters, digits and . _ - /";
  if (form.nanio_port.trim()) checkInt(errors, "nanio_port", form.nanio_port, 1, 65535, "Port");
  if (!/^[A-Za-z0-9.:-]+$/.test(form.nanio_host)) errors.nanio_host = "An IP address or hostname";
  if (!/^[A-Za-z0-9-]+$/.test(form.nanio_region)) errors.nanio_region = "Letters, digits and hyphens";
  for (const field of ["access_key", "secret_key"] as const) {
    if (form[field] && !NODE_SECRET_RE.test(form[field])) errors[field] = "No quotes, backslashes or control characters";
  }
  return errors;
}

export function hasErrors(errors: Errors): boolean {
  return Object.keys(errors).length > 0;
}
