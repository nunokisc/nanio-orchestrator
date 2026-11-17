import { ArrowRightLeft, CornerDownRight, FileCode, FolderSearch, Lock, Pencil, Plus, Trash2 } from "lucide-react";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { api, type ExtraBlock, type IpRuleMode, type Migration, type Pool, type Route, type Vhost, type Zone } from "../api";
import { OrphanOverride } from "../components/OrphanOverride";
import { PreviewDialog } from "../components/PreviewDialog";
import { RouteBucketDialog } from "../components/RouteBucketDialog";
import { useToast } from "../components/toast";
import {
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  EmptyState,
  FormDialog,
  IconButton,
  Mono,
  Notice,
  PageHeader,
  Panel,
  QueryView,
  SelectField,
  Switch,
  TextArea,
  TextField,
} from "../components/ui";
import { buildRouteTree, isActive, plural, splitLeftActive } from "../lib/format";
import { describeRoutePath, hasErrors, normalizeRoutePath, parseIpList, validateVhost } from "../lib/validation";
import { useAction, useMigrations, usePools, useRoutes, useVhosts } from "../queries";

type Dialog =
  | { kind: "create" }
  | { kind: "edit"; vhost: Vhost }
  | { kind: "delete"; vhost: Vhost }
  | { kind: "preview"; vhost: Vhost }
  | { kind: "add-route"; vhost: Vhost }
  | { kind: "promote"; vhost: Vhost; bucket: string }
  | { kind: "change-pool"; vhost: Vhost; route: Route }
  | { kind: "disable-route"; vhost: Vhost; route: Route }
  | { kind: "delete-route"; vhost: Vhost; route: Route };

export function VhostsPage() {
  const vhosts = useVhosts();
  const pools = usePools();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const close = () => setDialog(null);
  const poolList = pools.data ?? [];

  return (
    <>
      <PageHeader
        title="Vhosts"
        description="A vhost is a hostname clients connect to. Its default pool serves everything that has no route of its own; a route sends one bucket, or one folder of a bucket, to another pool."
        actions={
          <Button variant="primary" icon={<Plus className="icon" aria-hidden />} onClick={() => setDialog({ kind: "create" })}>
            Create vhost
          </Button>
        }
      />
      <QueryView
        query={vhosts}
        isEmpty={(list) => list.length === 0}
        empty={
          <EmptyState
            title="No vhosts yet"
            action={
              <Button variant="primary" onClick={() => setDialog({ kind: "create" })}>
                Create the first vhost
              </Button>
            }
          >
            {poolList.length === 0 ? (
              <>
                A vhost needs a pool to send requests to. <Link to="/pools">Create a pool</Link> and add a member first.
              </>
            ) : (
              "Create a vhost with one of your pools as its default pool."
            )}
          </EmptyState>
        }
      >
        {(list) => (
          <div className="stack-lg">
            {list.map((vhost) => (
              <VhostPanel key={vhost.id} vhost={vhost} pools={poolList} open={setDialog} />
            ))}
          </div>
        )}
      </QueryView>

      {dialog?.kind === "create" && <VhostDialog pools={poolList} onClose={close} />}
      {dialog?.kind === "edit" && <VhostDialog vhost={dialog.vhost} pools={poolList} onClose={close} />}
      {dialog?.kind === "delete" && <DeleteVhostDialog vhost={dialog.vhost} onClose={close} />}
      {dialog?.kind === "preview" && (
        <PreviewDialog
          title={`Server block of ${dialog.vhost.server_name}`}
          queryKey={["vhost", dialog.vhost.id]}
          load={() => api.vhosts.preview(dialog.vhost.id)}
          onClose={close}
        />
      )}
      {dialog?.kind === "add-route" && (
        <AddRouteDialog
          vhost={dialog.vhost}
          pools={poolList}
          onClose={close}
          onBucketRoute={(bucket) => setDialog({ kind: "promote", vhost: dialog.vhost, bucket })}
        />
      )}
      {dialog?.kind === "promote" && (
        <RouteBucketDialog vhost={dialog.vhost} bucket={dialog.bucket} pools={poolList} onClose={close} />
      )}
      {dialog?.kind === "change-pool" && (
        <ChangePoolDialog vhost={dialog.vhost} route={dialog.route} pools={poolList} onClose={close} />
      )}
      {dialog?.kind === "disable-route" && <DisableRouteDialog vhost={dialog.vhost} route={dialog.route} onClose={close} />}
      {dialog?.kind === "delete-route" && <DeleteRouteDialog vhost={dialog.vhost} route={dialog.route} onClose={close} />}
    </>
  );
}

function poolTypeOf(vhost: Vhost, pools: Pool[]) {
  return pools.find((pool) => pool.id === vhost.default_pool_id)?.type ?? null;
}

function VhostPanel({ vhost, pools, open }: { vhost: Vhost; pools: Pool[]; open: (dialog: Dialog) => void }) {
  const routes = useRoutes(vhost.id);
  const migrations = useMigrations();
  const defaultPool = pools.find((pool) => pool.id === vhost.default_pool_id);
  const isS3 = defaultPool?.type === "nanio";
  const blocks = vhost.extra_blocks?.length ?? 0;

  return (
    <Panel
      flush
      title={<Mono>{vhost.server_name}</Mono>}
      meta={
        <>
          <span>
            port <Mono>{vhost.listen_port}</Mono>
          </span>
          {vhost.ssl ? (
            <Badge tone="ok">
              <Lock className="icon-xs" aria-hidden /> TLS
            </Badge>
          ) : (
            <Badge tone="neutral">plain HTTP</Badge>
          )}
          {defaultPool ? (
            <span>
              default pool <Mono>{defaultPool.name}</Mono> <Badge tone={isS3 ? "info" : "neutral"}>{defaultPool.type}</Badge>
            </span>
          ) : (
            <Badge tone="warn" title="Requests without a route of their own have nowhere to go">
              No default pool
            </Badge>
          )}
          {vhost.ip_rule_mode && (
            <Badge tone="neutral" title={(vhost.ip_rule_ips ?? []).join(", ")}>
              {vhost.ip_rule_mode === "allow" ? "Only" : "Blocks"} {plural(vhost.ip_rule_ips?.length ?? 0, "address", "addresses")}
            </Badge>
          )}
          {blocks > 0 && <Badge tone="neutral">{plural(blocks, "extra block")}</Badge>}
        </>
      }
      actions={
        <>
          <Button size="sm" icon={<FileCode className="icon" aria-hidden />} onClick={() => open({ kind: "preview", vhost })}>
            Config
          </Button>
          <Button size="sm" icon={<Pencil className="icon" aria-hidden />} onClick={() => open({ kind: "edit", vhost })}>
            Edit
          </Button>
          <Button size="sm" variant="danger" icon={<Trash2 className="icon" aria-hidden />} onClick={() => open({ kind: "delete", vhost })}>
            Delete
          </Button>
        </>
      }
    >
      <QueryView query={routes}>
        {(list) => <RouteTable vhost={vhost} routes={list} migrations={migrations.data ?? []} isS3={isS3} open={open} />}
      </QueryView>
    </Panel>
  );
}

function MigrationMark({ migrations, vhost, bucket, folder }: { migrations: Migration[]; vhost: Vhost; bucket: string; folder: string }) {
  const mine = migrations.filter((m) => m.vhost_id === vhost.id && m.bucket === bucket && m.folder === folder);
  const running = mine.find(isActive);
  if (running) {
    return (
      <Link to="/migrations" className="chip-link">
        <Badge tone="info">migrating · {running.phase.replace("_", " ")}</Badge>
      </Link>
    );
  }
  if (mine.some(splitLeftActive)) {
    return (
      <Link to="/migrations" className="chip-link">
        <Badge tone="warn" title="A migration stopped with split routing still active. Start the same move again to finish it.">
          split routing active
        </Badge>
      </Link>
    );
  }
  return null;
}

function RouteTable({
  vhost,
  routes,
  migrations,
  isS3,
  open,
}: {
  vhost: Vhost;
  routes: Route[];
  migrations: Migration[];
  isS3: boolean;
  open: (dialog: Dialog) => void;
}) {
  const toast = useToast();
  const tree = buildRouteTree(routes);
  const enable = useAction((route: Route) => api.vhosts.updateRoute(vhost.id, route.id, { enabled: true }));

  const browse = (bucket: string, prefix: string) =>
    isS3 && (
      <Link
        className="btn btn-ghost btn-icon"
        to={`/explorer?vhost=${vhost.id}&bucket=${encodeURIComponent(bucket)}&prefix=${encodeURIComponent(prefix)}`}
        aria-label={`Browse ${bucket}/${prefix}`}
        title="Browse: see which pool holds what"
      >
        <FolderSearch className="icon" aria-hidden />
      </Link>
    );

  const toggle = (route: Route) => (
    <Switch
      label={`${route.enabled ? "Disable" : "Enable"} route ${route.path_prefix}`}
      checked={route.enabled}
      disabled={enable.isPending}
      onChange={(enabled) =>
        enabled
          ? enable.mutate(route, { onError: (error) => toast.error(`Could not enable ${route.path_prefix}`, error) })
          : open({ kind: "disable-route", vhost, route })
      }
    />
  );

  return (
    <table className="table">
      <thead>
        <tr>
          <th>Route</th>
          <th>Pool</th>
          <th>Enabled</th>
          <th className="actions">
            <Button size="sm" icon={<Plus className="icon" aria-hidden />} onClick={() => open({ kind: "add-route", vhost })}>
              Add route
            </Button>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>
            <Mono>/</Mono> <span className="muted">everything else (default pool)</span>
          </td>
          <td>{tree.defaultRoute ? <Mono>{tree.defaultRoute.pool_name}</Mono> : <span className="muted">none</span>}</td>
          <td className="muted">always</td>
          <td className="actions">
            <Button size="sm" variant="ghost" onClick={() => open({ kind: "edit", vhost })}>
              Change on the vhost
            </Button>
          </td>
        </tr>
        {tree.buckets.map((node) => (
          <Fragment key={node.bucket}>
            <tr className={node.route && !node.route.enabled ? "row-off" : undefined}>
              <td>
                <Mono>/{node.bucket}/</Mono> <MigrationMark migrations={migrations} vhost={vhost} bucket={node.bucket} folder="" />
              </td>
              <td>
                {node.route ? <Mono>{node.route.pool_name}</Mono> : <span className="muted">default pool (no route of its own)</span>}
              </td>
              <td>{node.route ? toggle(node.route) : <span className="muted">—</span>}</td>
              <td className="actions">
                {browse(node.bucket, "")}
                {node.route && (
                  <>
                    <Button size="sm" onClick={() => open({ kind: "change-pool", vhost, route: node.route! })}>
                      Change pool
                    </Button>
                    <IconButton label={`Delete route ${node.route.path_prefix}`} onClick={() => open({ kind: "delete-route", vhost, route: node.route! })}>
                      <Trash2 className="icon" aria-hidden />
                    </IconButton>
                  </>
                )}
              </td>
            </tr>
            {node.folders.map((route) => {
              const folder = route.path_prefix.split("/").filter(Boolean).slice(1).join("/") + "/";
              return (
                <tr key={route.id} className={route.enabled ? "row-child" : "row-child row-off"}>
                  <td>
                    <CornerDownRight className="icon tree-branch" aria-hidden />
                    <Mono>{folder}</Mono> <span className="muted">folder</span>{" "}
                    <MigrationMark migrations={migrations} vhost={vhost} bucket={node.bucket} folder={folder} />
                  </td>
                  <td>
                    <Mono>{route.pool_name}</Mono>
                  </td>
                  <td>{toggle(route)}</td>
                  <td className="actions">
                    {browse(node.bucket, folder)}
                    {isS3 ? (
                      <Link
                        className="btn btn-default btn-sm"
                        to={`/migrations?start=folder&vhost=${vhost.id}&bucket=${encodeURIComponent(node.bucket)}&folder=${encodeURIComponent(folder)}`}
                        title="A folder that holds data is moved with a migration"
                      >
                        <ArrowRightLeft className="icon" aria-hidden />
                        <span>Move</span>
                      </Link>
                    ) : (
                      <Button size="sm" onClick={() => open({ kind: "change-pool", vhost, route })}>
                        Change pool
                      </Button>
                    )}
                    <IconButton label={`Delete route ${route.path_prefix}`} onClick={() => open({ kind: "delete-route", vhost, route })}>
                      <Trash2 className="icon" aria-hidden />
                    </IconButton>
                  </td>
                </tr>
              );
            })}
          </Fragment>
        ))}
        {tree.buckets.length === 0 && (
          <tr>
            <td colSpan={4} className="muted table-note">
              No bucket or folder routes: every request goes to the default pool.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

// ── Vhost form ──────────────────────────────────────────────────────────────

const ZONES: Record<Zone, string> = {
  top: "top — after server_name and the IP rules",
  ssl: "ssl — after the certificate lines",
  proxy: "proxy — after the proxy buffering lines",
  end: "end — before the closing brace",
};

function VhostDialog({ vhost, pools, onClose }: { vhost?: Vhost; pools: Pool[]; onClose: () => void }) {
  const [form, setForm] = useState({
    server_name: vhost?.server_name ?? "",
    listen_port: String(vhost?.listen_port ?? 443),
    ssl: vhost?.ssl ?? true,
    ssl_cert_path: vhost?.ssl_cert_path ?? "",
    ssl_key_path: vhost?.ssl_key_path ?? "",
    default_pool_id: vhost?.default_pool_id ? String(vhost.default_pool_id) : "",
    ip_rule_mode: (vhost?.ip_rule_mode ?? "") as "" | IpRuleMode,
    ip_rule_ips: (vhost?.ip_rule_ips ?? []).join("\n"),
    extra_blocks: (vhost?.extra_blocks ?? []) as ExtraBlock[],
  });
  const [submitted, setSubmitted] = useState(false);
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => setForm((current) => ({ ...current, [key]: value }));
  const setBlock = (index: number, patch: Partial<ExtraBlock>) =>
    set(
      "extra_blocks",
      form.extra_blocks.map((block, i) => (i === index ? { ...block, ...patch } : block)),
    );

  const save = useAction(
    () => {
      const ips = parseIpList(form.ip_rule_ips);
      const body = {
        server_name: form.server_name.trim(),
        listen_port: Number(form.listen_port),
        ssl: form.ssl,
        ssl_cert_path: form.ssl ? form.ssl_cert_path.trim() : null,
        ssl_key_path: form.ssl ? form.ssl_key_path.trim() : null,
        default_pool_id: form.default_pool_id ? Number(form.default_pool_id) : null,
        ip_rule_mode: form.ip_rule_mode || null,
        ip_rule_ips: form.ip_rule_mode ? ips : null,
        extra_blocks: form.extra_blocks,
      };
      return vhost ? api.vhosts.update(vhost.id, body) : api.vhosts.create(body);
    },
    { onSuccess: onClose },
  );
  const clientErrors = validateVhost(form);
  const errors = submitted ? { ...save.error?.fieldErrors, ...clientErrors } : {};

  return (
    <FormDialog
      title={vhost ? `Edit vhost ${vhost.server_name}` : "Create vhost"}
      submitLabel={vhost ? "Save" : "Create vhost"}
      size="lg"
      busy={save.isPending}
      error={save.error}
      onClose={onClose}
      onSubmit={() => {
        setSubmitted(true);
        if (!hasErrors(clientErrors)) save.mutate(undefined);
      }}
    >
      <div className="grid-2-1">
        <TextField
          label="Hostname"
          mono
          autoFocus
          placeholder="s3.example.com"
          value={form.server_name}
          onValue={(value) => set("server_name", value)}
          error={errors.server_name}
          hint="The server_name clients use. A wildcard such as *.example.com is accepted."
        />
        <TextField
          label="Port"
          mono
          inputMode="numeric"
          value={form.listen_port}
          onValue={(value) => set("listen_port", value)}
          error={errors.listen_port}
        />
      </div>
      <SelectField
        label="Default pool"
        value={form.default_pool_id}
        onValue={(value) => set("default_pool_id", value)}
        error={errors.default_pool_id}
        hint="Serves everything that has no route of its own, and new buckets. Its type (nanio or http) is the type every route of this vhost must use."
      >
        <option value="">None</option>
        {pools.map((pool) => (
          <option key={pool.id} value={pool.id}>
            {pool.name} ({pool.type})
          </option>
        ))}
      </SelectField>

      <fieldset className="group">
        <legend>TLS</legend>
        <Checkbox label="Serve this vhost over TLS" checked={form.ssl} onChange={(value) => set("ssl", value)} />
        {form.ssl && (
          <div className="grid-2">
            <TextField
              label="Certificate path"
              mono
              placeholder="/etc/ssl/s3.example.com/fullchain.pem"
              value={form.ssl_cert_path}
              onValue={(value) => set("ssl_cert_path", value)}
              error={errors.ssl_cert_path}
              hint="On the gateway machine. nginx must be able to read it."
            />
            <TextField
              label="Private key path"
              mono
              placeholder="/etc/ssl/s3.example.com/privkey.pem"
              value={form.ssl_key_path}
              onValue={(value) => set("ssl_key_path", value)}
              error={errors.ssl_key_path}
            />
          </div>
        )}
      </fieldset>

      <fieldset className="group">
        <legend>IP rules</legend>
        <SelectField label="Who may connect" value={form.ip_rule_mode} onValue={(value) => set("ip_rule_mode", value as "" | IpRuleMode)}>
          <option value="">Anyone (no restriction)</option>
          <option value="allow">Only the addresses listed (allowlist)</option>
          <option value="deny">Anyone except the addresses listed (denylist)</option>
        </SelectField>
        {form.ip_rule_mode && (
          <TextArea
            label="Addresses"
            mono
            rows={4}
            placeholder={"10.0.0.0/8\n192.168.1.5\n2001:db8::/32"}
            value={form.ip_rule_ips}
            onValue={(value) => set("ip_rule_ips", value)}
            error={errors.ip_rule_ips}
            hint="One per line: IPv4, IPv6 or CIDR."
          />
        )}
      </fieldset>

      <fieldset className="group">
        <legend>Extra nginx directives</legend>
        <p className="field-hint">
          Raw directives inserted into the server block. They must be valid nginx syntax: nginx checks them when you
          save, and a change it rejects is not saved.
        </p>
        {form.extra_blocks.map((block, index) => (
          <div key={index} className="block-editor">
            <div className="block-editor-head">
              <SelectField label={`Block ${index + 1}: where`} value={block.zone} onValue={(value) => setBlock(index, { zone: value as Zone })}>
                {(Object.keys(ZONES) as Zone[]).map((zone) => (
                  <option key={zone} value={zone}>
                    {ZONES[zone]}
                  </option>
                ))}
              </SelectField>
              <Button
                size="sm"
                icon={<Trash2 className="icon" aria-hidden />}
                onClick={() =>
                  set(
                    "extra_blocks",
                    form.extra_blocks.filter((_, i) => i !== index),
                  )
                }
              >
                Remove block
              </Button>
            </div>
            <TextArea
              label={`Block ${index + 1}: directives`}
              mono
              rows={5}
              placeholder="proxy_read_timeout 300s;"
              value={block.content}
              onValue={(value) => setBlock(index, { content: value })}
              error={errors[`extra_blocks.${index}`]}
            />
          </div>
        ))}
        <div>
          <Button
            size="sm"
            icon={<Plus className="icon" aria-hidden />}
            onClick={() => set("extra_blocks", [...form.extra_blocks, { zone: "proxy", content: "" }])}
          >
            Add a block
          </Button>
        </div>
      </fieldset>
    </FormDialog>
  );
}

function DeleteVhostDialog({ vhost, onClose }: { vhost: Vhost; onClose: () => void }) {
  const remove = useAction(() => api.vhosts.remove(vhost.id), { onSuccess: onClose });
  return (
    <ConfirmDialog
      title={`Delete vhost ${vhost.server_name}`}
      confirmLabel="Delete vhost"
      typeToConfirm={vhost.server_name}
      busy={remove.isPending}
      error={remove.error}
      onConfirm={() => remove.mutate(undefined)}
      onClose={onClose}
    >
      <p>
        This removes the vhost with all its routes, its bucket list and its migration history, and deletes its server
        block. After the next reload nginx no longer answers for <Mono>{vhost.server_name}</Mono>.
      </p>
      <p>No data is deleted on any pool. It is refused while a migration is moving one of its buckets.</p>
    </ConfirmDialog>
  );
}

// ── Routes ──────────────────────────────────────────────────────────────────

function AddRouteDialog({
  vhost,
  pools,
  onClose,
  onBucketRoute,
}: {
  vhost: Vhost;
  pools: Pool[];
  onClose: () => void;
  onBucketRoute: (bucket: string) => void;
}) {
  const toast = useToast();
  const type = poolTypeOf(vhost, pools);
  const candidates = pools.filter((pool) => (type ? pool.type === type : true));
  const [path, setPath] = useState("/");
  const [poolId, setPoolId] = useState(candidates[0] ? String(candidates[0].id) : "");
  const [submitted, setSubmitted] = useState(false);
  const normalized = normalizeRoutePath(path);
  const described = describeRoutePath(normalized);
  const pathError = "error" in described ? described.error : undefined;
  const target = candidates.find((pool) => String(pool.id) === poolId);
  const isS3 = type === "nanio";
  const viaPromote = isS3 && "kind" in described && described.kind === "bucket";

  const add = useAction(
    (allowOrphan: boolean) =>
      api.vhosts.addRoute(vhost.id, { path_prefix: normalized, pool_id: Number(poolId), allow_orphan: allowOrphan || undefined }),
    {
      onSuccess: (route) => {
        toast.success(`Route ${route.path_prefix} written`, `It points at ${route.pool_name}. Reload nginx to apply it.`);
        for (const warning of route.warnings) toast.warn("The route was created, with a warning", warning);
        onClose();
      },
    },
  );

  return (
    <FormDialog
      title={`Add a route to ${vhost.server_name}`}
      submitLabel={viaPromote ? "Continue" : "Add route"}
      busy={add.isPending && !add.variables}
      error={add.error ?? undefined}
      submitDisabled={!target}
      onClose={onClose}
      onSubmit={() => {
        setSubmitted(true);
        if (pathError) return;
        if (viaPromote && "bucket" in described) onBucketRoute(described.bucket);
        else add.mutate(false);
      }}
      afterError={
        <OrphanOverride
          key={add.error?.detail}
          error={add.error}
          consequence={`The objects already under ${normalized} stay on the bucket's current pool and will not be reachable through ${vhost.server_name} once nginx is reloaded.`}
          actionLabel={`Route ${normalized} to ${target?.name ?? "the pool"} anyway`}
          busy={add.isPending && add.variables === true}
          onOverride={() => add.mutate(true)}
        />
      }
    >
      <table className="table table-compact explain">
        <tbody>
          <tr>
            <td>
              <Mono>/bucket/</Mono>
            </td>
            <td>The whole bucket goes to the pool you choose.</td>
          </tr>
          <tr>
            <td>
              <Mono>/bucket/folder/</Mono>
            </td>
            <td>
              Only that folder goes to the pool; the rest of the bucket stays where it is. The most specific route wins.
            </td>
          </tr>
        </tbody>
      </table>
      <TextField
        label="Path"
        mono
        autoFocus
        placeholder="/photos/ or /photos/2025/"
        value={path}
        onValue={setPath}
        error={submitted || path.length > 1 ? pathError : undefined}
      />
      {!viaPromote && (
        <SelectField
          label="Pool"
          value={poolId}
          onValue={setPoolId}
          error={add.error?.fieldErrors.pool_id}
          hint={type ? `Only ${type} pools: every route of a vhost uses the type of its default pool.` : undefined}
        >
          {candidates.map((pool) => (
            <option key={pool.id} value={pool.id}>
              {pool.name}
            </option>
          ))}
        </SelectField>
      )}

      {"kind" in described && described.kind === "bucket" && isS3 && (
        <Notice tone="info" title={`Whole bucket ${described.bucket}`}>
          The next step asks for the target pool and whether to move the objects there (a live migration) or only to
          route the bucket (for a new, empty bucket).
        </Notice>
      )}
      {"kind" in described && described.kind === "bucket" && !isS3 && (
        <Notice tone="info" title={`Whole bucket ${described.bucket}`}>
          Requests under <Mono>/{described.bucket}/</Mono> are served by the pool you choose. Nothing is copied.
        </Notice>
      )}
      {"kind" in described && described.kind === "folder" && isS3 && (
        <Notice tone="info" title={`Folder ${described.folder} of bucket ${described.bucket}`}>
          <ul className="plain-list">
            <li>The folder must be empty on the bucket's current pool: objects already there would be hidden, so that is refused.</li>
            <li>The bucket is created on the target pool, and a hidden marker keeps the folder visible when the bucket is browsed.</li>
            <li>http vhosts that serve this bucket get the matching route.</li>
            <li>
              A recursive listing of the whole bucket will not include this folder, and a server-side copy between the
              folder and the rest of the bucket fails: tell the application owner.
            </li>
          </ul>
          To move a folder that already holds data, start a <Link to="/migrations?start=folder">folder migration</Link> instead.
        </Notice>
      )}
      {"kind" in described && described.kind === "folder" && !isS3 && (
        <Notice tone="info" title={`Folder ${described.folder} of bucket ${described.bucket}`}>
          Requests under <Mono>{normalized}</Mono> are served by the pool you choose; the rest of the bucket is not affected.
        </Notice>
      )}
    </FormDialog>
  );
}

function ChangePoolDialog({ vhost, route, pools, onClose }: { vhost: Vhost; route: Route; pools: Pool[]; onClose: () => void }) {
  const toast = useToast();
  const current = pools.find((pool) => pool.id === route.pool_id);
  const candidates = pools.filter((pool) => pool.type === current?.type && pool.id !== route.pool_id);
  const [poolId, setPoolId] = useState(candidates[0] ? String(candidates[0].id) : "");
  const target = candidates.find((pool) => String(pool.id) === poolId);
  const isS3 = current?.type === "nanio";

  const change = useAction(
    (allowOrphan: boolean) =>
      api.vhosts.updateRoute(vhost.id, route.id, { pool_id: Number(poolId), allow_orphan: allowOrphan || undefined }),
    {
      onSuccess: (updated) => {
        if (updated.migration_id) {
          toast.success(
            `Migration #${updated.migration_id} started`,
            `${route.path_prefix} holds objects, so they are being moved to ${target?.name}. The route switches when the move is verified.`,
          );
        } else {
          toast.success(`Route ${route.path_prefix} now points at ${updated.pool_name}`, "Reload nginx to apply it.");
        }
        onClose();
      },
    },
  );

  return (
    <FormDialog
      title={`Change the pool of ${route.path_prefix}`}
      submitLabel="Change pool"
      busy={change.isPending && !change.variables}
      error={change.error}
      submitDisabled={!target}
      onClose={onClose}
      onSubmit={() => change.mutate(false)}
      afterError={
        <OrphanOverride
          key={change.error?.detail}
          error={change.error}
          consequence={`Whatever ${route.path_prefix} holds on ${route.pool_name} stays there and will not be reachable through ${vhost.server_name} once nginx is reloaded.`}
          actionLabel={`Repoint ${route.path_prefix} to ${target?.name ?? "the pool"} anyway`}
          busy={change.isPending && change.variables === true}
          onOverride={() => change.mutate(true)}
        />
      }
    >
      <p className="muted">
        <Mono>{route.path_prefix}</Mono> on <Mono>{vhost.server_name}</Mono> is served by <Mono>{route.pool_name}</Mono>.
      </p>
      {candidates.length === 0 ? (
        <Notice tone="warn" title="There is no other pool of this type" />
      ) : (
        <SelectField label="New pool" value={poolId} onValue={setPoolId}>
          {candidates.map((pool) => (
            <option key={pool.id} value={pool.id}>
              {pool.name}
            </option>
          ))}
        </SelectField>
      )}
      {isS3 && (
        <Notice tone="info" title="This may start a migration">
          If the bucket has objects on <Mono>{route.pool_name}</Mono>, they are moved with a live migration and the route
          switches only when the copy is verified. If it is empty, the route is repointed at once. If the current pool
          cannot be asked, the change is refused.
        </Notice>
      )}
    </FormDialog>
  );
}

function DisableRouteDialog({ vhost, route, onClose }: { vhost: Vhost; route: Route; onClose: () => void }) {
  const disable = useAction(() => api.vhosts.updateRoute(vhost.id, route.id, { enabled: false }), { onSuccess: onClose });
  return (
    <ConfirmDialog
      title={`Disable route ${route.path_prefix}`}
      confirmLabel="Disable route"
      busy={disable.isPending}
      error={disable.error}
      onConfirm={() => disable.mutate(undefined)}
      onClose={onClose}
    >
      <p>
        A disabled route is left out of nginx. After the next reload, requests for <Mono>{route.path_prefix}</Mono> go to
        the next matching route or to the default pool, and what is stored on <Mono>{route.pool_name}</Mono> is not
        reachable through <Mono>{vhost.server_name}</Mono> until the route is enabled again.
      </p>
      <p>Nothing is deleted.</p>
    </ConfirmDialog>
  );
}

function DeleteRouteDialog({ vhost, route, onClose }: { vhost: Vhost; route: Route; onClose: () => void }) {
  const remove = useAction((allowOrphan: boolean) => api.vhosts.removeRoute(vhost.id, route.id, allowOrphan), {
    onSuccess: onClose,
  });
  const described = describeRoutePath(route.path_prefix);
  const isFolder = "kind" in described && described.kind === "folder";
  return (
    <ConfirmDialog
      title={`Delete route ${route.path_prefix}`}
      confirmLabel="Delete route"
      busy={remove.isPending && !remove.variables}
      error={remove.error}
      onConfirm={() => remove.mutate(false)}
      onClose={onClose}
      extra={
        <OrphanOverride
          key={remove.error?.detail}
          error={remove.error}
          consequence={`The objects under ${route.path_prefix} stay on ${route.pool_name} and will not be reachable through ${vhost.server_name} once nginx is reloaded.`}
          actionLabel="Delete the route anyway"
          busy={remove.isPending && remove.variables === true}
          onOverride={() => remove.mutate(true)}
        />
      }
    >
      {isFolder ? (
        <p>
          The folder becomes part of its bucket again: after the next reload its requests go to the bucket's own pool. It
          is refused while the folder still has objects on <Mono>{route.pool_name}</Mono>, because they would become
          unreachable. To bring a folder with data back, use a migration.
        </p>
      ) : (
        <p>
          After the next reload, requests for <Mono>{route.path_prefix}</Mono> go to the default pool. Objects stored on{" "}
          <Mono>{route.pool_name}</Mono> stay there and are no longer reachable through <Mono>{vhost.server_name}</Mono>.
          Nothing is deleted.
        </p>
      )}
    </ConfirmDialog>
  );
}
