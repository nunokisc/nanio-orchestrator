import { describe, expect, it } from "vitest";
import type { Migration, Route } from "../api";
import { collapseUnchanged, lineDiff } from "../lib/diff";
import {
  buildRouteTree,
  formatBytes,
  parseDate,
  pendingState,
  progressPercent,
  splitLeftActive,
  timeAgo,
  unresolvedSplits,
} from "../lib/format";
import {
  describeRoutePath,
  intInRange,
  normalizeFolder,
  normalizeRoutePath,
  parseIpList,
  validateCredentials,
  validateMember,
  validateNodeConfig,
  validatePool,
  validateVhost,
  type VhostForm,
} from "../lib/validation";

describe("pool form", () => {
  const valid = { name: "pool-2026", type: "nanio" as const, keepalive: "32", source_nanio_pool_id: "" };

  it("accepts what the API accepts", () => {
    expect(validatePool(valid)).toEqual({});
    expect(validatePool({ ...valid, name: "a.b_c-D9", keepalive: "0" })).toEqual({});
    expect(validatePool({ ...valid, type: "http", source_nanio_pool_id: "3" })).toEqual({});
  });

  it.each(["", "pool 1", "pool;", "pool/1", "ção", "x".repeat(129)])("refuses the name %j", (name) => {
    expect(validatePool({ ...valid, name }).name).toBeTruthy();
  });

  it.each(["-1", "1025", "abc", "", "3.5"])("refuses keepalive %j", (keepalive) => {
    expect(validatePool({ ...valid, keepalive }).keepalive).toBeTruthy();
  });

  it("only an http pool can be linked to a nanio pool", () => {
    expect(validatePool({ ...valid, source_nanio_pool_id: "3" }).source_nanio_pool_id).toBeTruthy();
  });
});

describe("member form", () => {
  const valid = { address: "10.0.1.11:9000", role: "active", weight: "1", max_fails: "3", fail_timeout_s: "30" };

  it("accepts host:port and unix sockets", () => {
    expect(validateMember(valid, "nanio")).toEqual({});
    expect(validateMember({ ...valid, address: "unix:/run/nanio.sock" }, "nanio")).toEqual({});
    expect(validateMember({ ...valid, address: "[::1]:9000", role: "replica" }, "http")).toEqual({});
  });

  it.each(["", "10.0.1.11", "nanio-a", "10.0.0.1:9000; evil"])("refuses the address %j", (address) => {
    expect(validateMember({ ...valid, address }, "nanio").address).toBeTruthy();
  });

  it("enforces the roles of each pool type", () => {
    expect(validateMember({ ...valid, role: "replica" }, "nanio").role).toBeTruthy();
    expect(validateMember({ ...valid, role: "active" }, "http").role).toBeTruthy();
    expect(validateMember({ ...valid, role: "primary" }, "http")).toEqual({});
  });

  it("enforces the numeric ranges of the API", () => {
    expect(Object.keys(validateMember({ ...valid, weight: "0", max_fails: "101", fail_timeout_s: "601" }, "nanio")).sort()).toEqual([
      "fail_timeout_s",
      "max_fails",
      "weight",
    ]);
    expect(validateMember({ ...valid, weight: "100", max_fails: "0", fail_timeout_s: "600" }, "nanio")).toEqual({});
  });
});

describe("vhost form", () => {
  const valid: VhostForm = {
    server_name: "s3.example.com",
    listen_port: "443",
    ssl: true,
    ssl_cert_path: "/etc/ssl/fullchain.pem",
    ssl_key_path: "/etc/ssl/privkey.pem",
    ip_rule_mode: "",
    ip_rule_ips: "",
    extra_blocks: [],
  };

  it("accepts a hostname, a wildcard, and plain HTTP without certificates", () => {
    expect(validateVhost(valid)).toEqual({});
    expect(validateVhost({ ...valid, server_name: "*.example.com" })).toEqual({});
    expect(validateVhost({ ...valid, ssl: false, ssl_cert_path: "", ssl_key_path: "" })).toEqual({});
  });

  it.each(["", "s3 example.com", "s3.example.com;", "-bad.example.com", "{evil}", "x".repeat(254)])(
    "refuses the hostname %j",
    (server_name) => {
      expect(validateVhost({ ...valid, server_name }).server_name).toBeTruthy();
    },
  );

  it("needs both certificate paths when TLS is on", () => {
    const errors = validateVhost({ ...valid, ssl_cert_path: " ", ssl_key_path: "" });
    expect(errors.ssl_cert_path).toBeTruthy();
    expect(errors.ssl_key_path).toBeTruthy();
  });

  it.each(["0", "65536", "http"])("refuses the port %j", (listen_port) => {
    expect(validateVhost({ ...valid, listen_port }).listen_port).toBeTruthy();
  });

  it("an IP rule needs addresses, and each must look like one", () => {
    expect(validateVhost({ ...valid, ip_rule_mode: "allow" }).ip_rule_ips).toBeTruthy();
    expect(validateVhost({ ...valid, ip_rule_mode: "deny", ip_rule_ips: "10.0.0.0/8\n2001:db8::/32, 192.168.1.5" })).toEqual({});
    expect(validateVhost({ ...valid, ip_rule_mode: "allow", ip_rule_ips: "10.0.0.1\nexample.com" }).ip_rule_ips).toContain("example.com");
  });

  it("an empty extra block is pointed at by its position", () => {
    const errors = validateVhost({ ...valid, extra_blocks: [{ zone: "proxy", content: "proxy_read_timeout 300s;" }, { zone: "end", content: "  " }] });
    expect(Object.keys(errors)).toEqual(["extra_blocks.1"]);
  });

  it("splits an address list on lines, commas and spaces", () => {
    expect(parseIpList(" 10.0.0.1,10.0.0.2\n\n 10.0.0.0/8  ")).toEqual(["10.0.0.1", "10.0.0.2", "10.0.0.0/8"]);
  });
});

describe("route paths", () => {
  it("tells a bucket route from a folder route", () => {
    expect(describeRoutePath("/photos/")).toEqual({ kind: "bucket", bucket: "photos", folder: "" });
    expect(describeRoutePath("/photos")).toEqual({ kind: "bucket", bucket: "photos", folder: "" });
    expect(describeRoutePath("/photos/2025/")).toEqual({ kind: "folder", bucket: "photos", folder: "2025/" });
    expect(describeRoutePath("/my.bucket_1/a@b/c-d/")).toEqual({ kind: "folder", bucket: "my.bucket_1", folder: "a@b/c-d/" });
  });

  it.each(["", "photos/", "/", "//", "/photos//2025/", "/pho tos/", "/photos/;", "/photos/{x}/", "/photos/2025/\n"])(
    "refuses %j",
    (path) => {
      expect(describeRoutePath(path)).toHaveProperty("error");
    },
  );

  it("adds the trailing slash a user leaves out", () => {
    expect(normalizeRoutePath(" /photos/2025 ")).toBe("/photos/2025/");
    expect(normalizeRoutePath("/photos/")).toBe("/photos/");
    expect(normalizeRoutePath("")).toBe("");
  });

  it("normalises a folder as the API does", () => {
    expect(normalizeFolder("2025")).toEqual({ folder: "2025/" });
    expect(normalizeFolder("/2025/q1/")).toEqual({ folder: "2025/q1/" });
    expect(normalizeFolder("  ")).toEqual({ folder: "" });
    expect(normalizeFolder("20 25")).toHaveProperty("error");
    expect(normalizeFolder("a//b")).toHaveProperty("error");
  });
});

describe("other forms", () => {
  it("credentials need all three values", () => {
    expect(validateCredentials({ access_key: "AK", secret_key: "s", region: "us-east-1" })).toEqual({});
    expect(Object.keys(validateCredentials({ access_key: " ", secret_key: "", region: "" })).sort()).toEqual(["access_key", "region", "secret_key"]);
  });

  it("node setup refuses what would break the generated files", () => {
    const valid = { data_dir: "/data", nanio_port: "", nanio_host: "0.0.0.0", nanio_region: "us-east-1", access_key: "", secret_key: "", nanio_bin: "/opt/nanio/bin/nanio" };
    expect(validateNodeConfig(valid)).toEqual({});
    expect(validateNodeConfig({ ...valid, nanio_port: "9000", access_key: "AK", secret_key: "s3cret/+=" })).toEqual({});
    const errors = validateNodeConfig({ ...valid, data_dir: "data", nanio_bin: "/opt/na nio", nanio_port: "70000", nanio_host: "a b", nanio_region: "us east", secret_key: 'a"b' });
    expect(Object.keys(errors).sort()).toEqual(["data_dir", "nanio_bin", "nanio_host", "nanio_port", "nanio_region", "secret_key"]);
  });

  it("intInRange returns the number or the reason", () => {
    expect(intInRange(" 42 ", 1, 100, "Weight")).toBe(42);
    expect(intInRange("101", 1, 100, "Weight")).toBe("Weight must be between 1 and 100");
    expect(intInRange("4.2", 1, 100, "Weight")).toBe("Weight must be a whole number");
  });
});

describe("changes waiting for a reload", () => {
  it("nothing to say before the status is known, or when everything is applied", () => {
    expect(pendingState(undefined)).toEqual({ kind: "applied" });
    expect(pendingState({ pending_reload: false, pending_files: [] })).toEqual({ kind: "applied" });
  });

  it("counts the files that differ from what nginx loaded", () => {
    const state = pendingState({
      pending_reload: true,
      pending_files: [
        { path: "/etc/nginx/nanio/pools/a.conf", change: "modified" },
        { path: "/etc/nginx/nanio/vhosts/s3.conf", change: "added" },
        { path: "/etc/nginx/nanio/vhosts/old.conf", change: "removed" },
      ],
    });
    expect(state).toEqual({ kind: "pending", count: 3, message: "3 changes not applied — nginx is still serving the previous configuration." });
    expect(pendingState({ pending_reload: true, pending_files: [{ path: "x", change: "added" }] })).toMatchObject({
      message: "1 change not applied — nginx is still serving the previous configuration.",
    });
  });

  it("does not claim to know when no reload was ever recorded", () => {
    expect(pendingState({ pending_reload: null, pending_files: [] }).kind).toBe("unknown");
  });
});

describe("migrations", () => {
  const base: Migration = {
    id: 1, vhost_id: 1, bucket: "photos", src_pool_id: 1, dst_pool_id: 2, phase: "error", objects_total: 10, objects_done: 4,
    bytes_total: 0, bytes_done: 0, error_msg: null, started_at: null, finished_at: null, created_at: "2026-01-01 00:00:00",
    orphaned_source_pool_id: null, orphaned_source_prefix: null, orphaned_at: null, split_routing: true, folder: "", cascade_warnings: null,
  };

  it("a split is left active only by a migration that ended without finishing", () => {
    expect(splitLeftActive(base)).toBe(true);
    expect(splitLeftActive({ ...base, phase: "cancelled" })).toBe(true);
    expect(splitLeftActive({ ...base, split_routing: false })).toBe(false);
    expect(splitLeftActive({ ...base, phase: "verifying" })).toBe(false);
    expect(splitLeftActive({ ...base, phase: "done" })).toBe(false);
  });

  it("a split that a running migration is finishing needs no attention", () => {
    const retry = { ...base, id: 2, phase: "copying" as const };
    const otherFolder = { ...base, id: 3, folder: "2025/" };
    expect(unresolvedSplits([base]).map((m) => m.id)).toEqual([1]);
    expect(unresolvedSplits([retry, base, otherFolder]).map((m) => m.id)).toEqual([3]);
  });

  it("progress is unknown until rclone reports a total", () => {
    expect(progressPercent({ ...base, phase: "copying" })).toBe(40);
    expect(progressPercent({ ...base, phase: "copying", objects_total: 0 })).toBeNull();
    expect(progressPercent({ ...base, phase: "done", objects_total: 0 })).toBe(100);
    expect(progressPercent({ ...base, phase: "copying", objects_done: 12 })).toBe(100);
  });
});

describe("route tree", () => {
  const route = (id: number, path_prefix: string, pool_name: string): Route => ({
    id, vhost_id: 1, path_prefix, pool_id: id, pool_name, enabled: true, created_at: "", updated_at: "",
  });

  it("groups folder routes under their bucket, even without a bucket route", () => {
    const tree = buildRouteTree([
      route(1, "/photos/2025/q1/", "pool-c"),
      route(2, "/photos/2025/", "pool-b"),
      route(3, "/photos/", "pool-b"),
      route(4, "/logs/app/", "pool-b"),
      route(5, "/", "pool-a"),
      route(6, "/archive/", "pool-c"),
    ]);
    expect(tree.defaultRoute?.pool_name).toBe("pool-a");
    expect(tree.buckets.map((node) => [node.bucket, node.route?.path_prefix ?? null, node.folders.map((f) => f.path_prefix)])).toEqual([
      ["archive", "/archive/", []],
      ["logs", null, ["/logs/app/"]],
      ["photos", "/photos/", ["/photos/2025/", "/photos/2025/q1/"]],
    ]);
  });

  it("a vhost without a default pool has no default route", () => {
    expect(buildRouteTree([])).toEqual({ defaultRoute: null, buckets: [] });
  });
});

describe("formatting", () => {
  it("reads the API's two date formats as UTC", () => {
    expect(parseDate("2026-01-02 03:04:05")?.toISOString()).toBe("2026-01-02T03:04:05.000Z");
    expect(parseDate("2026-01-02T03:04:05Z")?.toISOString()).toBe("2026-01-02T03:04:05.000Z");
    expect(parseDate("2026-01-02T03:04:05.000Z")?.toISOString()).toBe("2026-01-02T03:04:05.000Z");
    expect(parseDate(null)).toBeNull();
    expect(parseDate("garbage")).toBeNull();
  });

  it("says how long ago", () => {
    const now = new Date("2026-01-02T12:00:00Z");
    expect(timeAgo("2026-01-02 11:59:40", now)).toBe("just now");
    expect(timeAgo("2026-01-02 11:30:00", now)).toBe("30 min ago");
    expect(timeAgo("2026-01-02 06:00:00", now)).toBe("6 h ago");
    expect(timeAgo("2025-12-30 12:00:00", now)).toBe("3 d ago");
    expect(timeAgo(null, now)).toBe("never");
  });

  it("formats sizes in binary units", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(64 * 1024 * 1024)).toBe("64.0 MiB");
    expect(formatBytes(null)).toBe("—");
  });
});

describe("line diff", () => {
  it("marks what a hand edit removed and added", () => {
    const before = "upstream a {\n    server 10.0.0.1:9000;\n    keepalive 32;\n}\n";
    const after = "upstream a {\n    server 10.0.0.1:9000;\n    server 10.0.0.9:9000;\n    keepalive 64;\n}\n";
    expect(lineDiff(before, after).filter((line) => line.type !== "same")).toEqual([
      { type: "del", text: "    keepalive 32;" },
      { type: "add", text: "    server 10.0.0.9:9000;" },
      { type: "add", text: "    keepalive 64;" },
    ]);
  });

  it("identical texts have no differences", () => {
    expect(lineDiff("a\nb", "a\nb").every((line) => line.type === "same")).toBe(true);
  });

  it("long unchanged stretches are folded, keeping a few lines around each change", () => {
    const before = Array.from({ length: 20 }, (_, i) => `line ${i}`);
    const after = [...before];
    after[10] = "changed";
    const rows = collapseUnchanged(lineDiff(before.join("\n"), after.join("\n")), 2);
    expect(rows.map((row) => (row.type === "skip" ? `skip ${row.count}` : `${row.type} ${row.text}`))).toEqual([
      "skip 8",
      "same line 8",
      "same line 9",
      "del line 10",
      "add changed",
      "same line 11",
      "same line 12",
      "skip 7",
    ]);
    expect(collapseUnchanged(lineDiff("a\nb", "a\nb"))).toEqual([{ type: "skip", count: 2 }]);
  });

  it("lines appended at the end are additions", () => {
    expect(lineDiff("a", "a\nb\nc")).toEqual([
      { type: "same", text: "a" },
      { type: "add", text: "b" },
      { type: "add", text: "c" },
    ]);
  });
});
