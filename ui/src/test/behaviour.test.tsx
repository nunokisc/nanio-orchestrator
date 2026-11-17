/** The behaviours that are easy to get wrong: what is waiting for a reload, the allow_orphan
 * override, errors shown as the API wrote them, and the session. */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ConfigStatus, type Pool, type Vhost } from "../api";
import { App } from "../App";
import { PendingBar } from "../components/Layout";
import { OrphanOverride } from "../components/OrphanOverride";
import { RouteBucketDialog } from "../components/RouteBucketDialog";
import { ConfirmDialog, ErrorNotice } from "../components/ui";
import { mockApi, renderApp } from "./server";

afterEach(() => vi.unstubAllGlobals());

const applied: ConfigStatus = {
  files: [],
  last_reload_ok: true,
  last_reload_at: "2026-01-01 00:00:00",
  pending_reload: false,
  pending_files: [],
  last_applied_at: "2026-01-01T00:00:00Z",
};
const pending: ConfigStatus = {
  ...applied,
  pending_reload: true,
  pending_files: [
    { path: "/etc/nginx/nanio/pools/pool-a.conf", change: "modified" },
    { path: "/etc/nginx/nanio/vhosts/s3.dev.conf", change: "added" },
    { path: "/etc/nginx/nanio/vhosts/old.conf", change: "removed" },
  ],
};

describe("changes not applied", () => {
  it("says nothing while everything on disk has been applied", async () => {
    const server = mockApi({ "GET /api/config/status": { body: applied } });
    renderApp(<PendingBar />);
    await waitFor(() => expect(server.to("GET /api/config/status")).toHaveLength(1));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reload nginx" })).not.toBeInTheDocument();
  });

  it("counts the changes and offers the reload", async () => {
    mockApi({ "GET /api/config/status": { body: pending } });
    renderApp(<PendingBar />);
    expect(await screen.findByText("3 changes not applied — nginx is still serving the previous configuration.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload nginx" })).toBeEnabled();
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute("href", "/configuration");
  });

  it("reloads nginx and goes away once the status says it is applied", async () => {
    const server = mockApi({
      "GET /api/config/status": { body: pending },
      "POST /api/config/reload": () => {
        server.set("GET /api/config/status", { body: applied });
        return { body: { ok: true, output: "signal process started" } };
      },
    });
    renderApp(<PendingBar />);
    await userEvent.click(await screen.findByRole("button", { name: "Reload nginx" }));
    await waitFor(() => expect(screen.queryByText(/changes not applied/)).not.toBeInTheDocument());
    expect(server.to("POST /api/config/reload")).toHaveLength(1);
    expect(await screen.findByText("nginx reloaded")).toBeInTheDocument();
  });

  it("stays, with what nginx said, when the reload fails", async () => {
    mockApi({
      "GET /api/config/status": { body: pending },
      "POST /api/config/reload": { body: { ok: false, output: "nginx: [error] open() \"/run/nginx.pid\" failed" } },
    });
    renderApp(<PendingBar />);
    await userEvent.click(await screen.findByRole("button", { name: "Reload nginx" }));
    expect(await screen.findByText('nginx: [error] open() "/run/nginx.pid" failed')).toBeInTheDocument();
    expect(screen.getByText(/3 changes not applied/)).toBeInTheDocument();
  });

  it("does not claim to know before a reload was ever recorded", async () => {
    mockApi({ "GET /api/config/status": { body: { ...applied, pending_reload: null, last_applied_at: null } } });
    renderApp(<PendingBar />);
    expect(await screen.findByText(/it cannot be told whether nginx is serving them/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload nginx" })).toBeEnabled();
  });
});

describe("errors are shown as the API wrote them", () => {
  it.each([
    [422, "nginx rejected this change, so nothing was saved. nginx said: nginx: [emerg] unknown directive \"proxy_rd_timeout\" in /etc/nginx/nanio/vhosts/s3.conf:14", "nginx rejected this change"],
    [409, "Cannot delete pool: it is the default pool for vhost(s): s3.example.com. Change the vhost default_pool_id first.", "Refused"],
    [502, "Cannot tell whether bucket 'photos' has objects: its current pool has no enabled member. Repointing the route would leave them behind.", "A pool could not be asked"],
    [429, "Maximum parallel migrations (2) reached", "No migration slot is free"],
  ])("%i", (status, detail, title) => {
    renderApp(<ErrorNotice error={new ApiError(status, detail)} />);
    const alert = screen.getByRole("alert");
    expect(within(alert).getByText(detail)).toBeInTheDocument();
    expect(within(alert).getByText(title)).toBeInTheDocument();
  });

  it("keeps the line breaks of a multi-line detail", () => {
    renderApp(<ErrorNotice error={new ApiError(422, "name: too long\nkeepalive: too large")} />);
    expect(screen.getByRole("alert").querySelector(".error-detail")?.textContent).toBe("name: too long\nkeepalive: too large");
  });

  it("renders nothing without an error", () => {
    renderApp(<ErrorNotice error={null} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("the allow_orphan override", () => {
  const refusal = new ApiError(
    409,
    "Bucket 'photos' already has objects on the source pool. Routing to a different pool without migration will leave existing data inaccessible via this route. Either enable 'Migrate existing objects' or re-submit with allow_orphan=true to acknowledge data will remain on the source pool.",
  );
  const consequence = "The objects of photos stay on pool-a and will not be reachable through s3.dev once nginx is reloaded.";

  it("is not offered for an error that does not mention it", () => {
    const onOverride = vi.fn();
    renderApp(
      <OrphanOverride error={new ApiError(409, "Route /photos/ already exists for this vhost")} consequence={consequence} actionLabel="Route anyway" onOverride={onOverride} />,
    );
    expect(screen.queryByRole("button", { name: "Route anyway" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("states the consequence and stays disabled until the box is ticked by hand", async () => {
    const onOverride = vi.fn();
    renderApp(<OrphanOverride error={refusal} consequence={consequence} actionLabel="Route anyway" onOverride={onOverride} />);
    expect(screen.getByText(consequence)).toBeInTheDocument();
    const box = screen.getByRole("checkbox", { name: "I understand that these objects will not be reachable" });
    const button = screen.getByRole("button", { name: "Route anyway" });
    expect(box).not.toBeChecked();
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(onOverride).not.toHaveBeenCalled();

    await userEvent.click(box);
    await userEvent.click(button);
    expect(onOverride).toHaveBeenCalledTimes(1);
  });

  const pools = [
    { id: 1, name: "pool-a", type: "nanio" },
    { id: 2, name: "pool-b", type: "nanio" },
    { id: 3, name: "cdn", type: "http" },
  ] as Pool[];
  const vhost = { id: 7, server_name: "s3.dev", default_pool_id: 1 } as Vhost;

  it("routing a bucket: the first request never carries it, the refusal is shown, the second is explicit", async () => {
    const server = mockApi({
      "POST /api/vhosts/7/buckets/photos/promote": (call) =>
        (call.body as { allow_orphan?: boolean }).allow_orphan
          ? { body: { ok: true, output: "", bucket: "photos", pool: "pool-b", route: "/photos/", migration_started: false } }
          : { status: 409, body: { detail: refusal.detail } },
    });
    const onClose = vi.fn();
    renderApp(<RouteBucketDialog vhost={vhost} bucket="photos" pools={pools} onClose={onClose} />);

    // only nanio pools other than the default one can be chosen
    expect(within(screen.getByLabelText("Target pool")).getAllByRole("option").map((o) => o.textContent)).toEqual(["pool-b"]);
    // moving the objects is the default; the override does not exist yet
    expect(screen.getByRole("radio", { name: /Move them/ })).toBeChecked();
    expect(screen.queryByRole("group", { name: "Override the refusal" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: /Route only/ }));
    await userEvent.click(screen.getByRole("button", { name: "Route only" }));

    expect(await screen.findByText(refusal.detail)).toBeInTheDocument();
    expect(server.calls[0]!.body).toEqual({ pool_id: 2, migrate: false });
    expect(onClose).not.toHaveBeenCalled();

    const override = screen.getByRole("group", { name: "Override the refusal" });
    expect(within(override).getByText(consequence)).toBeInTheDocument();
    const anyway = within(override).getByRole("button", { name: "Route photos to pool-b anyway" });
    expect(anyway).toBeDisabled();
    expect(within(override).getByRole("checkbox")).not.toBeChecked();

    await userEvent.click(within(override).getByRole("checkbox"));
    await userEvent.click(anyway);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(server.calls).toHaveLength(2);
    expect(server.calls[1]!.body).toEqual({ pool_id: 2, migrate: false, allow_orphan: true });
  });

  it("moving the objects starts a migration and never offers the override", async () => {
    const server = mockApi({
      "POST /api/vhosts/7/buckets/photos/promote": { status: 429, body: { detail: "Maximum parallel migrations (2) reached" } },
    });
    renderApp(<RouteBucketDialog vhost={vhost} bucket="photos" pools={pools} onClose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Route and move the objects" }));
    expect(await screen.findByText("Maximum parallel migrations (2) reached")).toBeInTheDocument();
    expect(server.calls[0]!.body).toEqual({ pool_id: 2, migrate: true });
    expect(screen.queryByRole("group", { name: "Override the refusal" })).not.toBeInTheDocument();
  });
});

describe("destructive confirmations", () => {
  it("an irreversible action needs the name typed exactly", async () => {
    const onConfirm = vi.fn();
    renderApp(
      <ConfirmDialog title="Delete pool pool-a" confirmLabel="Delete pool" typeToConfirm="pool-a" onConfirm={onConfirm} onClose={vi.fn()}>
        This removes the pool.
      </ConfirmDialog>,
    );
    const confirm = screen.getByRole("button", { name: "Delete pool" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Type pool-a to confirm"), "pool-");
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Type pool-a to confirm"), "a");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("Escape closes the dialog without confirming", async () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    renderApp(
      <ConfirmDialog title="Remove member" confirmLabel="Remove" onConfirm={onConfirm} onClose={onClose}>
        text
      </ConfirmDialog>,
    );
    expect(screen.getByRole("dialog", { name: "Remove member" })).toHaveAttribute("aria-modal", "true");
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});

describe("session", () => {
  const anonymous = { authenticated: false, dev_mode: false, version: "0.5.0" };
  const overviewRoutes = {
    "GET /api/health": { body: { status: "ok", version: "0.5.0", dev_mode: false, db_ok: true, nginx_config_dir: "/etc/nginx/nanio", drift_alerts: 0, missing_buckets: 0 } },
    "GET /api/config/status": { body: applied },
    "GET /api/migrations": { body: [] },
    "GET /api/audit": { body: [] },
    "POST /api/config/validate": { body: { ok: true, output: "ok" } },
  };
  const emptyOverview = { pools: [], vhosts: 0, routes: 0, unrouted_buckets: 0, missing_buckets: [] };

  it("shows the login screen to a browser without a session, and no data is requested", async () => {
    const server = mockApi({ "GET /api/auth/session": { body: anonymous } });
    renderApp(<App />);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(server.calls.map((call) => call.path)).toEqual(["/api/auth/session"]);
  });

  it("a wrong key is explained and the form stays", async () => {
    mockApi({ "GET /api/auth/session": { body: anonymous }, "POST /api/auth/login": { status: 401, body: { detail: "Invalid API key" } } });
    renderApp(<App />);
    await userEvent.type(await screen.findByLabelText("API key"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("That is not the API key of this orchestrator.")).toBeInTheDocument();
    expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
  });

  it("signs in, shows the first-run guide, and returns to the login when the session ends", async () => {
    const server = mockApi({
      ...overviewRoutes,
      "GET /api/auth/session": { body: anonymous },
      "POST /api/auth/login": { body: { ...anonymous, authenticated: true } },
      "GET /api/overview": { body: emptyOverview },
    });
    renderApp(<App />);
    await userEvent.type(await screen.findByLabelText("API key"), "the-key");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Getting started" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a pool" })).toHaveAttribute("href", "/pools");
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(server.to("POST /api/auth/login")[0]!.body).toEqual({ key: "the-key" });

    // the session expires: the next data request is answered 401
    server.set("GET /api/pools", { status: 401, body: { detail: "Invalid or missing X-Orchestrator-Key" } });
    await userEvent.click(screen.getByRole("link", { name: "Pools" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });

  it("the overview links each problem to where it is fixed", async () => {
    mockApi({
      ...overviewRoutes,
      "GET /api/auth/session": { body: { ...anonymous, authenticated: true } },
      "GET /api/config/status": { body: { ...pending, files: [{ path: "/etc/nginx/nanio/pools/pool-a.conf", sha256_disk: "a", sha256_db: "b", drifted: true, last_synced_at: null }] } },
      "GET /api/overview": {
        body: {
          pools: [{ id: 1, name: "pool-a", type: "nanio", members: 1, enabled_members: 1, has_credentials: false }],
          vhosts: 1,
          routes: 1,
          unrouted_buckets: 0,
          missing_buckets: [{ vhost_id: 4, server_name: "s3.dev", bucket: "photos", pool_name: "pool-b", missing_since: "2026-01-02T00:00:00Z" }],
        },
      },
    });
    renderApp(<App />);
    const attention = (await screen.findByRole("heading", { name: "Needs attention" })).closest("section")!;
    expect(await within(attention).findByText("Changes are waiting to be applied")).toBeInTheDocument();
    expect(within(attention).getByText("1 file edited by hand")).toBeInTheDocument();
    expect(within(attention).getByText("Bucket photos is missing on pool-b")).toBeInTheDocument();
    expect(within(attention).getByText("Pool pool-a has no stored credentials")).toBeInTheDocument();
    expect(within(attention).getByRole("link", { name: "Buckets" })).toHaveAttribute("href", "/buckets?vhost=4");
    expect(within(attention).getByRole("link", { name: "Review and reload" })).toHaveAttribute("href", "/configuration");
    // and the bar above every screen says the same
    expect(document.querySelector(".pending-bar")).toHaveTextContent("3 changes not applied");
  });
});
