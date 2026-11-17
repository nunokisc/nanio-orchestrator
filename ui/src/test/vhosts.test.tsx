/** Vhosts screen: taking a hostname out of service, and switching a route off over data. */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExplorerPage } from "../pages/Explorer";
import { VhostsPage } from "../pages/Vhosts";
import { mockApi, renderApp } from "./server";

afterEach(() => vi.unstubAllGlobals());

const pools = [
  { id: 1, name: "pool-a", type: "nanio", lb_method: "least_conn", keepalive: 32, description: null, source_nanio_pool_id: null },
  { id: 2, name: "pool-b", type: "nanio", lb_method: "least_conn", keepalive: 32, description: null, source_nanio_pool_id: null },
];
const vhost = {
  id: 7,
  server_name: "s3.dev",
  listen_port: 8081,
  ssl: false,
  ssl_cert_path: null,
  ssl_key_path: null,
  extra_blocks: null,
  enabled: true,
  default_pool_id: 1,
  ip_rule_mode: null,
  ip_rule_ips: null,
};
const routes = [
  { id: 1, vhost_id: 7, path_prefix: "/", pool_id: 1, pool_name: "pool-a", enabled: true },
  { id: 2, vhost_id: 7, path_prefix: "/docs/", pool_id: 2, pool_name: "pool-b", enabled: true },
];

function server(extra: Parameters<typeof mockApi>[0] = {}) {
  return mockApi({
    "GET /api/pools": { body: pools },
    "GET /api/vhosts": { body: [vhost] },
    "GET /api/vhosts/7/routes": { body: routes },
    "GET /api/migrations": { body: [] },
    ...extra,
  });
}

describe("disabling a vhost", () => {
  it("is an action with a confirmation that names the hostname and what stops", async () => {
    const api = server({ "PUT /api/vhosts/7": { body: { ...vhost, enabled: false } } });
    renderApp(<VhostsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Disable" }));

    const dialog = screen.getByRole("dialog", { name: "Disable vhost s3.dev" });
    expect(dialog).toHaveTextContent("nginx stops answering for s3.dev on port 8081");
    expect(dialog).toHaveTextContent("s3.dev.conf.disabled");
    expect(api.to("PUT /api/vhosts/7")).toHaveLength(0);

    await userEvent.click(within(dialog).getByRole("button", { name: "Stop serving s3.dev" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.to("PUT /api/vhosts/7")[0]!.body).toEqual({ enabled: false });
  });

  it("shows the refusal while a migration is moving one of its buckets", async () => {
    const detail = "Migration 4 is still moving bucket 'photos' of this vhost. Finish or cancel it first.";
    server({ "PUT /api/vhosts/7": { status: 409, body: { detail } } });
    renderApp(<VhostsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Disable" }));
    await userEvent.click(screen.getByRole("button", { name: "Stop serving s3.dev" }));
    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("a disabled vhost says it is not served and offers to enable it", async () => {
    const api = mockApi({
      "GET /api/pools": { body: pools },
      "GET /api/vhosts": { body: [{ ...vhost, enabled: false }] },
      "GET /api/vhosts/7/routes": { body: routes },
      "GET /api/migrations": { body: [] },
      "PUT /api/vhosts/7": { body: vhost },
    });
    renderApp(<VhostsPage />);
    expect(await screen.findByText("disabled — not served")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Enable" }));
    await userEvent.click(screen.getByRole("button", { name: "Enable s3.dev" }));
    await waitFor(() => expect(api.to("PUT /api/vhosts/7")).toHaveLength(1));
    expect(api.to("PUT /api/vhosts/7")[0]!.body).toEqual({ enabled: true });
  });
});

describe("switching a bucket route off", () => {
  const detail =
    "Bucket 'docs' has objects on pool 'pool-b'. Disabling its route would leave them there, no longer reachable through this vhost. Move the bucket with a migration instead, or send allow_orphan=true to leave them behind.";

  it("asks first, shows the refusal as written, and overrides only after the box is ticked", async () => {
    const api = server({
      "PUT /api/vhosts/7/routes/2": (call) =>
        (call.body as { allow_orphan?: boolean }).allow_orphan
          ? { body: { ...routes[1], enabled: false } }
          : { status: 409, body: { detail } },
    });
    renderApp(<VhostsPage />);
    await userEvent.click(await screen.findByRole("switch", { name: "Disable route /docs/" }));
    expect(api.to("PUT /api/vhosts/7/routes/2")).toHaveLength(0);

    const dialog = screen.getByRole("dialog", { name: "Disable route /docs/" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Disable route" }));
    expect(await within(dialog).findByText(detail)).toBeInTheDocument();
    expect(api.to("PUT /api/vhosts/7/routes/2")[0]!.body).toEqual({ enabled: false });

    const override = within(dialog).getByRole("group", { name: "Override the refusal" });
    expect(override).toHaveTextContent("What /docs/ holds on pool-b stays there and will not be reachable through s3.dev");
    const anyway = within(override).getByRole("button", { name: "Disable the route anyway" });
    expect(anyway).toBeDisabled();
    await userEvent.click(within(override).getByRole("checkbox"));
    await userEvent.click(anyway);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.to("PUT /api/vhosts/7/routes/2")[1]!.body).toEqual({ enabled: false, allow_orphan: true });
  });
});

describe("explorer during a migration", () => {
  it("says the path is being moved, and which pool clients read first", async () => {
    mockApi({
      "GET /api/pools": { body: pools },
      "GET /api/vhosts": { body: [vhost] },
      "GET /api/explorer/vhosts/7/locate": {
        body: {
          vhost: { id: 7, server_name: "s3.dev" },
          bucket: "photos",
          prefix: "2025/",
          route: "/photos/2025/",
          pool: { id: 2, name: "pool-b", type: "nanio" },
          split: { migration_id: 9, source: { id: 1, name: "pool-a" }, destination: { id: 2, name: "pool-b" } },
        },
      },
      "GET /api/explorer/pools/1/buckets/photos/objects": {
        body: { pool: { id: 1, name: "pool-a" }, bucket: "photos", prefix: "2025/", folders: [], objects: [], next_token: null },
      },
    });
    renderApp(<ExplorerPage />, { route: "/explorer?vhost=7&pool=1&bucket=photos&prefix=2025/" });
    const notice = (await screen.findByText("Being moved from pool-a to pool-b (migration #9)")).closest(".notice")!;
    expect(notice).toHaveTextContent("clients read pool-b first and fall back to pool-a for objects not copied yet");
    expect(notice).toHaveTextContent("You are looking at pool-a, the source: the fallback copy.");
    expect(within(notice as HTMLElement).getByRole("button", { name: "Switch to pool-b" })).toBeInTheDocument();
  });
});
