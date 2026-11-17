import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, onUnauthorized, request, toApiError } from "../api";
import { mockApi } from "./server";

afterEach(() => {
  onUnauthorized(null);
  vi.unstubAllGlobals();
});

describe("request", () => {
  it("authenticates with the session cookie and never sends a key header", async () => {
    const server = mockApi({ "GET /api/pools": { body: [] } });
    await api.pools.list();
    const { init } = server.calls[0]!;
    expect(init.credentials).toBe("same-origin");
    const headers = new Headers(init.headers);
    expect(headers.has("X-Orchestrator-Key")).toBe(false);
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("sends a body as JSON", async () => {
    const server = mockApi({ "POST /api/pools/3/members": { status: 201, body: { id: 1 } } });
    await request("POST", "/api/pools/3/members", { body: { address: "10.0.0.1:9000" } });
    const call = server.calls[0]!;
    expect(new Headers(call.init.headers).get("Content-Type")).toBe("application/json");
    expect(call.body).toEqual({ address: "10.0.0.1:9000" });
  });

  it("the login sends the key in the body, once, and nothing keeps it", async () => {
    const server = mockApi({ "POST /api/auth/login": { body: { authenticated: true, dev_mode: false, version: "1" } } });
    await api.login("s3cr3t");
    await api.session().catch(() => undefined);
    expect(server.calls[0]!.body).toEqual({ key: "s3cr3t" });
    expect(JSON.stringify(server.calls[1]!.init)).not.toContain("s3cr3t");
    expect(window.localStorage.length + window.sessionStorage.length).toBe(0);
  });

  it("encodes query values and leaves out the empty ones", async () => {
    const server = mockApi({ "GET /api/explorer/pools/1/buckets/photos/objects": { body: {} } });
    await api.explorer.objects(1, "photos", { prefix: "relatórios 2025/a+b&c/", token: undefined, vhost_id: 2 });
    const call = server.calls[0]!;
    expect(call.query.get("prefix")).toBe("relatórios 2025/a+b&c/");
    expect(call.query.get("vhost_id")).toBe("2");
    expect(call.query.has("token")).toBe(false);
  });

  it("encodes a bucket name in the path", async () => {
    const server = mockApi({});
    await api.buckets.ignore(1, "a b/c").catch(() => undefined);
    expect(server.calls[0]!.path).toBe("/api/vhosts/1/buckets/a%20b%2Fc/ignore");
  });

  it("returns nothing for 204", async () => {
    mockApi({ "DELETE /api/pools/1": { status: 204 } });
    await expect(api.pools.remove(1)).resolves.toBeUndefined();
  });

  it("asks for allow_orphan only when told to", async () => {
    const server = mockApi({ "DELETE /api/vhosts/1/routes/2": { status: 204 } });
    await api.vhosts.removeRoute(1, 2);
    await api.vhosts.removeRoute(1, 2, true);
    expect(server.calls[0]!.query.has("allow_orphan")).toBe(false);
    expect(server.calls[1]!.query.get("allow_orphan")).toBe("true");
  });
});

describe("errors", () => {
  it("keeps the detail of the API word for word", async () => {
    const detail = "nginx rejected this change, so nothing was saved. nginx said: nginx: [emerg] host not found in upstream";
    mockApi({ "POST /api/vhosts": { status: 422, body: { detail } } });
    const error = await api.vhosts.create({} as never).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(422);
    expect((error as ApiError).detail).toBe(detail);
    expect((error as ApiError).nginxRejected).toBe(true);
    expect((error as ApiError).fieldErrors).toEqual({});
  });

  it("maps a validation answer to its fields", () => {
    const error = toApiError(422, "Unprocessable Entity", {
      detail: [
        { loc: ["body", "name"], msg: "String should match pattern '^[a-zA-Z0-9._-]+$'", type: "string_pattern_mismatch" },
        { loc: ["body", "keepalive"], msg: "Input should be less than or equal to 1024", type: "less_than_equal" },
        { loc: ["body"], msg: "Value error, ssl_cert_path and ssl_key_path are required when ssl is enabled", type: "value_error" },
        { loc: ["query", "limit"], msg: "Input should be greater than or equal to 1" },
      ],
    });
    expect(error.fieldErrors).toEqual({
      name: "String should match pattern '^[a-zA-Z0-9._-]+$'",
      keepalive: "Input should be less than or equal to 1024",
      limit: "Input should be greater than or equal to 1",
    });
    expect(error.detail.split("\n")).toEqual([
      "name: String should match pattern '^[a-zA-Z0-9._-]+$'",
      "keepalive: Input should be less than or equal to 1024",
      "ssl_cert_path and ssl_key_path are required when ssl is enabled",
      "limit: Input should be greater than or equal to 1",
    ]);
    expect(error.nginxRejected).toBe(false);
  });

  it("says so when the server gives no explanation", () => {
    expect(toApiError(500, "Internal Server Error", null).detail).toBe(
      "The server answered 500 Internal Server Error without an explanation.",
    );
    expect(toApiError(502, "", { unexpected: true }).detail).toBe("The server answered 502 without an explanation.");
  });

  it("reports an unreachable server as an error with status 0", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const error = (await api.health().catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(0);
    expect(error.detail).toBe("Cannot reach the orchestrator: Failed to fetch");
  });

  it.each([
    [409, "Bucket 'photos' already has objects on the source pool. … re-submit with allow_orphan=true to acknowledge …", true],
    [502, "Cannot tell whether bucket 'photos' has objects: … or send allow_orphan=true to repoint anyway.", true],
    [400, "The pool … has no enabled member … Send ?allow_orphan=true to delete the route anyway.", true],
    [409, "Route /photos/ already exists for this vhost", false],
    [502, "Could not check the pools before creating the route: timed out", false],
    [422, "allow_orphan: Input should be a valid boolean", false],
  ])("%i %s → offers the override: %s", (status, detail, offered) => {
    expect(new ApiError(status, detail).offersAllowOrphan).toBe(offered);
  });
});

describe("401", () => {
  it("tells the application that the session is over", async () => {
    mockApi({ "GET /api/pools": { status: 401, body: { detail: "Invalid or missing X-Orchestrator-Key" } } });
    const handler = vi.fn();
    onUnauthorized(handler);
    await expect(api.pools.list()).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("a wrong key at the login is not a lost session", async () => {
    mockApi({ "POST /api/auth/login": { status: 401, body: { detail: "Invalid API key" } } });
    const handler = vi.fn();
    onUnauthorized(handler);
    await expect(api.login("wrong")).rejects.toMatchObject({ status: 401, detail: "Invalid API key" });
    expect(handler).not.toHaveBeenCalled();
  });

  it("other errors do not end the session", async () => {
    mockApi({ "GET /api/pools": { status: 500, body: { detail: "boom" } } });
    const handler = vi.fn();
    onUnauthorized(handler);
    await expect(api.pools.list()).rejects.toMatchObject({ status: 500 });
    expect(handler).not.toHaveBeenCalled();
  });
});

describe("download", () => {
  it("returns the bytes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("hello", { status: 200 })));
    const blob = await api.explorer.download(1, "photos", "a b/c+d.txt");
    expect(blob.size).toBe(5);
    const [url, init] = vi.mocked(fetch).mock.calls[0]!;
    expect(url).toBe("/api/explorer/pools/1/buckets/photos/download?key=a%20b%2Fc%2Bd.txt");
    expect(init).toEqual({ credentials: "same-origin" });
  });

  it("shows the refusal of an object above the cap", async () => {
    const detail = "'photos/big.iso' is larger than the panel's download limit (64 MiB). Fetch it with an S3 client through the gateway.";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail }), { status: 413 })));
    await expect(api.explorer.download(1, "photos", "big.iso")).rejects.toMatchObject({ status: 413, detail });
  });
});
