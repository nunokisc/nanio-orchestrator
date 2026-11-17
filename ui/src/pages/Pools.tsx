import { FileCode, KeyRound, Pencil, Plus, Terminal, Trash2 } from "lucide-react";
import { useState } from "react";
import {
  api,
  type LbMethod,
  type Member,
  type MemberRole,
  type NodeConfig,
  type NodeType,
  type Pool,
  type PoolSummary,
  type PoolType,
} from "../api";
import { PreviewDialog } from "../components/PreviewDialog";
import { useToast } from "../components/toast";
import {
  Badge,
  Button,
  Checkbox,
  CodeBlock,
  ConfirmDialog,
  EmptyState,
  FormDialog,
  IconButton,
  Modal,
  Mono,
  Notice,
  PageHeader,
  Panel,
  QueryView,
  SelectField,
  Switch,
  TextField,
} from "../components/ui";
import { formatDateTime, plural } from "../lib/format";
import { hasErrors, validateCredentials, validateMember, validateNodeConfig, validatePool } from "../lib/validation";
import { useAction, useCredentials, useMembers, useOverview, usePools } from "../queries";

const LB_LABEL: Record<LbMethod, string> = {
  least_conn: "Least connections",
  round_robin: "Round robin",
  ip_hash: "IP hash",
};

type Dialog =
  | { kind: "create" }
  | { kind: "edit"; pool: Pool }
  | { kind: "delete"; pool: Pool }
  | { kind: "preview"; pool: Pool }
  | { kind: "credentials"; pool: Pool }
  | { kind: "member"; pool: Pool; member: Member | null }
  | { kind: "remove-member"; pool: Pool; member: Member }
  | { kind: "node"; pool: Pool; member: Member };

export function PoolsPage() {
  const pools = usePools();
  const overview = useOverview();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const close = () => setDialog(null);
  const summaries = new Map((overview.data?.pools ?? []).map((summary) => [summary.id, summary]));

  return (
    <>
      <PageHeader
        title="Pools"
        description="A pool is a group of machines that hold the same data. nginx writes it as an upstream."
        actions={
          <Button variant="primary" icon={<Plus className="icon" aria-hidden />} onClick={() => setDialog({ kind: "create" })}>
            Create pool
          </Button>
        }
      />
      <QueryView
        query={pools}
        isEmpty={(list) => list.length === 0}
        empty={
          <EmptyState
            title="No pools yet"
            action={
              <Button variant="primary" onClick={() => setDialog({ kind: "create" })}>
                Create the first pool
              </Button>
            }
          >
            Start with a pool for your nanio machines. Then add each machine as a member and store the pool's S3
            credentials.
          </EmptyState>
        }
      >
        {(list) => (
          <div className="stack-lg">
            {list.map((pool) => (
              <PoolPanel key={pool.id} pool={pool} pools={list} summary={summaries.get(pool.id)} open={setDialog} />
            ))}
          </div>
        )}
      </QueryView>

      {dialog?.kind === "create" && <PoolDialog pools={pools.data ?? []} onClose={close} />}
      {dialog?.kind === "edit" && <PoolDialog pool={dialog.pool} pools={pools.data ?? []} onClose={close} />}
      {dialog?.kind === "delete" && <DeletePoolDialog pool={dialog.pool} onClose={close} />}
      {dialog?.kind === "preview" && (
        <PreviewDialog
          title={`Upstream of ${dialog.pool.name}`}
          queryKey={["pool", dialog.pool.id]}
          load={() => api.pools.preview(dialog.pool.id)}
          onClose={close}
        />
      )}
      {dialog?.kind === "credentials" && <CredentialsDialog pool={dialog.pool} onClose={close} />}
      {dialog?.kind === "member" && <MemberDialog pool={dialog.pool} member={dialog.member} onClose={close} />}
      {dialog?.kind === "remove-member" && <RemoveMemberDialog pool={dialog.pool} member={dialog.member} onClose={close} />}
      {dialog?.kind === "node" && <NodeSetupDialog pool={dialog.pool} member={dialog.member} onClose={close} />}
    </>
  );
}

function PoolPanel({
  pool,
  pools,
  summary,
  open,
}: {
  pool: Pool;
  pools: Pool[];
  summary: PoolSummary | undefined;
  open: (dialog: Dialog) => void;
}) {
  const members = useMembers(pool.id);
  const toast = useToast();
  const toggle = useAction(({ member, enabled }: { member: Member; enabled: boolean }) =>
    api.pools.updateMember(pool.id, member.id, { enabled }),
  );
  const source = pools.find((candidate) => candidate.id === pool.source_nanio_pool_id);

  return (
    <Panel
      flush
      title={<Mono>{pool.name}</Mono>}
      meta={
        <>
          <Badge tone={pool.type === "nanio" ? "info" : "neutral"}>{pool.type}</Badge>
          <span>{LB_LABEL[pool.lb_method]}</span>
          <span>keepalive {pool.keepalive}</span>
          {pool.type === "nanio" &&
            summary &&
            (summary.has_credentials ? (
              <Badge tone="ok">Credentials stored</Badge>
            ) : (
              <Badge tone="warn" title="Without credentials the orchestrator cannot list buckets or migrate">
                No credentials
              </Badge>
            ))}
          {pool.type === "http" &&
            (source ? (
              <span>
                serves the files of <Mono>{source.name}</Mono>
              </span>
            ) : (
              <Badge tone="neutral" title="Edit the pool to link it to the nanio pool whose files it serves">
                Not linked to a nanio pool
              </Badge>
            ))}
          {pool.description && <span className="muted">{pool.description}</span>}
        </>
      }
      actions={
        <>
          {pool.type === "nanio" && (
            <Button size="sm" icon={<KeyRound className="icon" aria-hidden />} onClick={() => open({ kind: "credentials", pool })}>
              Credentials
            </Button>
          )}
          <Button size="sm" icon={<FileCode className="icon" aria-hidden />} onClick={() => open({ kind: "preview", pool })}>
            Config
          </Button>
          <Button size="sm" icon={<Pencil className="icon" aria-hidden />} onClick={() => open({ kind: "edit", pool })}>
            Edit
          </Button>
          <Button size="sm" variant="danger" icon={<Trash2 className="icon" aria-hidden />} onClick={() => open({ kind: "delete", pool })}>
            Delete
          </Button>
        </>
      }
    >
      <QueryView
        query={members}
        isEmpty={(list) => list.length === 0}
        empty={
          <EmptyState
            title="No members"
            action={
              <Button size="sm" onClick={() => open({ kind: "member", pool, member: null })}>
                Add a member
              </Button>
            }
          >
            A pool without members is not written to nginx, and a vhost cannot use it yet.
          </EmptyState>
        }
      >
        {(list) => (
          <table className="table">
            <thead>
              <tr>
                <th>Address</th>
                <th>Role</th>
                <th className="num">Weight</th>
                <th className="num">Max fails</th>
                <th className="num">Fail timeout</th>
                <th>Enabled</th>
                <th className="actions">
                  <Button size="sm" icon={<Plus className="icon" aria-hidden />} onClick={() => open({ kind: "member", pool, member: null })}>
                    Add member
                  </Button>
                </th>
              </tr>
            </thead>
            <tbody>
              {list.map((member) => (
                <tr key={member.id} className={member.enabled ? undefined : "row-off"}>
                  <td>
                    <Mono>{member.address}</Mono>
                  </td>
                  <td>
                    {member.role === "replica" ? (
                      <Badge tone="neutral" title="Written as a backup server: used only when every primary is down">
                        replica
                      </Badge>
                    ) : (
                      member.role
                    )}
                  </td>
                  <td className="num">{member.weight}</td>
                  <td className="num">{member.max_fails}</td>
                  <td className="num">{member.fail_timeout_s} s</td>
                  <td>
                    <Switch
                      label={`${member.enabled ? "Disable" : "Enable"} ${member.address}`}
                      checked={member.enabled}
                      disabled={toggle.isPending}
                      onChange={(enabled) =>
                        toggle.mutate(
                          { member, enabled },
                          { onError: (error) => toast.error(`Could not ${enabled ? "enable" : "disable"} ${member.address}`, error) },
                        )
                      }
                    />
                  </td>
                  <td className="actions">
                    <Button size="sm" icon={<Terminal className="icon" aria-hidden />} onClick={() => open({ kind: "node", pool, member })}>
                      Node setup
                    </Button>
                    <IconButton label={`Edit ${member.address}`} onClick={() => open({ kind: "member", pool, member })}>
                      <Pencil className="icon" aria-hidden />
                    </IconButton>
                    <IconButton label={`Remove ${member.address}`} onClick={() => open({ kind: "remove-member", pool, member })}>
                      <Trash2 className="icon" aria-hidden />
                    </IconButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryView>
    </Panel>
  );
}

// ── Pool ────────────────────────────────────────────────────────────────────

function PoolDialog({ pool, pools, onClose }: { pool?: Pool; pools: Pool[]; onClose: () => void }) {
  const [form, setForm] = useState({
    name: pool?.name ?? "",
    description: pool?.description ?? "",
    type: (pool?.type ?? "nanio") as PoolType,
    lb_method: (pool?.lb_method ?? "least_conn") as LbMethod,
    keepalive: String(pool?.keepalive ?? 32),
    source_nanio_pool_id: pool?.source_nanio_pool_id ? String(pool.source_nanio_pool_id) : "",
  });
  const [submitted, setSubmitted] = useState(false);
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => setForm((current) => ({ ...current, [key]: value }));

  const save = useAction(
    () => {
      const body = {
        name: form.name,
        description: form.description.trim() || null,
        type: form.type,
        lb_method: form.lb_method,
        keepalive: Number(form.keepalive),
        source_nanio_pool_id: form.type === "http" && form.source_nanio_pool_id ? Number(form.source_nanio_pool_id) : null,
      };
      return pool ? api.pools.update(pool.id, body) : api.pools.create(body);
    },
    { onSuccess: onClose },
  );
  const clientErrors = validatePool(form);
  const errors = submitted ? { ...save.error?.fieldErrors, ...clientErrors } : {};
  const nanioPools = pools.filter((candidate) => candidate.type === "nanio" && candidate.id !== pool?.id);

  return (
    <FormDialog
      title={pool ? `Edit pool ${pool.name}` : "Create pool"}
      submitLabel={pool ? "Save" : "Create pool"}
      busy={save.isPending}
      error={save.error}
      onClose={onClose}
      onSubmit={() => {
        setSubmitted(true);
        if (!hasErrors(clientErrors)) save.mutate(undefined);
      }}
    >
      <TextField
        label="Name"
        mono
        autoFocus
        value={form.name}
        onValue={(value) => set("name", value)}
        error={errors.name}
        hint={
          pool
            ? "Renaming rewrites the upstream file and every vhost that uses this pool, as one change."
            : "Becomes the nginx upstream name, e.g. pool-2026."
        }
      />
      <SelectField
        label="Type"
        value={form.type}
        onValue={(value) => set("type", value as PoolType)}
        error={errors.type}
        hint={
          form.type === "nanio"
            ? "nanio instances sharing one data directory. Has S3 credentials, bucket discovery and migrations."
            : "Plain nginx instances serving the same files read-only, with replicas as backups. No S3 API, no authentication."
        }
      >
        <option value="nanio">nanio — S3 storage</option>
        <option value="http">http — read-only file serving</option>
      </SelectField>
      {form.type === "http" && (
        <SelectField
          label="Serves the files of"
          value={form.source_nanio_pool_id}
          onValue={(value) => set("source_nanio_pool_id", value)}
          error={errors.source_nanio_pool_id}
          hint="The nanio pool whose data directory these machines serve. Vhosts on this pool can then expose its buckets and follow their migrations."
        >
          <option value="">Not linked</option>
          {nanioPools.map((candidate) => (
            <option key={candidate.id} value={candidate.id}>
              {candidate.name}
            </option>
          ))}
        </SelectField>
      )}
      <div className="grid-2">
        <SelectField label="Load balancing" value={form.lb_method} onValue={(value) => set("lb_method", value as LbMethod)}>
          <option value="least_conn">Least connections</option>
          <option value="round_robin">Round robin</option>
          <option value="ip_hash">IP hash</option>
        </SelectField>
        <TextField
          label="Keepalive connections"
          inputMode="numeric"
          value={form.keepalive}
          onValue={(value) => set("keepalive", value)}
          error={errors.keepalive}
          hint="Idle connections kept open to the pool (0–1024)."
        />
      </div>
      <TextField label="Description" value={form.description} onValue={(value) => set("description", value)} hint="Optional." />
    </FormDialog>
  );
}

function DeletePoolDialog({ pool, onClose }: { pool: Pool; onClose: () => void }) {
  const remove = useAction(() => api.pools.remove(pool.id), { onSuccess: onClose });
  return (
    <ConfirmDialog
      title={`Delete pool ${pool.name}`}
      confirmLabel="Delete pool"
      typeToConfirm={pool.name}
      busy={remove.isPending}
      error={remove.error}
      onConfirm={() => remove.mutate(undefined)}
      onClose={onClose}
    >
      <p>
        This removes the pool, its members, its stored credentials and its upstream file, along with the records of
        finished migrations that involved it. It is refused while a vhost or a route still uses the pool.
      </p>
      <p>The data on the machines is not touched.</p>
    </ConfirmDialog>
  );
}

// ── Members ─────────────────────────────────────────────────────────────────

function MemberDialog({ pool, member, onClose }: { pool: Pool; member: Member | null; onClose: () => void }) {
  const [form, setForm] = useState({
    address: member?.address ?? "",
    role: (member?.role ?? (pool.type === "nanio" ? "active" : "primary")) as MemberRole,
    weight: String(member?.weight ?? 1),
    max_fails: String(member?.max_fails ?? 3),
    fail_timeout_s: String(member?.fail_timeout_s ?? 30),
    enabled: member?.enabled ?? true,
  });
  const [submitted, setSubmitted] = useState(false);
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => setForm((current) => ({ ...current, [key]: value }));

  const save = useAction(
    () => {
      const body = {
        address: form.address.trim(),
        role: form.role,
        weight: Number(form.weight),
        max_fails: Number(form.max_fails),
        fail_timeout_s: Number(form.fail_timeout_s),
        enabled: form.enabled,
      };
      return member ? api.pools.updateMember(pool.id, member.id, body) : api.pools.addMember(pool.id, body);
    },
    { onSuccess: onClose },
  );
  const clientErrors = validateMember(form, pool.type);
  const errors = submitted ? { ...save.error?.fieldErrors, ...clientErrors } : {};

  return (
    <FormDialog
      title={member ? `Edit member ${member.address}` : `Add a member to ${pool.name}`}
      submitLabel={member ? "Save" : "Add member"}
      busy={save.isPending}
      error={save.error}
      onClose={onClose}
      onSubmit={() => {
        setSubmitted(true);
        if (!hasErrors(clientErrors)) save.mutate(undefined);
      }}
    >
      <TextField
        label="Address"
        mono
        autoFocus
        placeholder="10.0.1.11:9000"
        value={form.address}
        onValue={(value) => set("address", value)}
        error={errors.address}
        hint="host:port of the machine, or unix:/path for a socket."
      />
      {pool.type === "http" ? (
        <SelectField
          label="Role"
          value={form.role}
          onValue={(value) => set("role", value as MemberRole)}
          error={errors.role}
          hint="A replica is written as a backup server: nginx uses it only when every primary is down. Keeping it in sync is done outside the orchestrator."
        >
          <option value="primary">primary</option>
          <option value="replica">replica</option>
        </SelectField>
      ) : (
        <Notice tone="info">
          Every member of a nanio pool is <strong>active</strong>: nanio expects all instances of a pool to see the same
          data directory, so any member can serve any request.
        </Notice>
      )}
      <div className="grid-3">
        <TextField label="Weight" inputMode="numeric" value={form.weight} onValue={(value) => set("weight", value)} error={errors.weight} />
        <TextField
          label="Max fails"
          inputMode="numeric"
          value={form.max_fails}
          onValue={(value) => set("max_fails", value)}
          error={errors.max_fails}
        />
        <TextField
          label="Fail timeout (s)"
          inputMode="numeric"
          value={form.fail_timeout_s}
          onValue={(value) => set("fail_timeout_s", value)}
          error={errors.fail_timeout_s}
        />
      </div>
      <Checkbox
        label="Enabled"
        hint="A disabled member stays in the pool but is left out of the upstream."
        checked={form.enabled}
        onChange={(value) => set("enabled", value)}
      />
    </FormDialog>
  );
}

function RemoveMemberDialog({ pool, member, onClose }: { pool: Pool; member: Member; onClose: () => void }) {
  const remove = useAction(() => api.pools.removeMember(pool.id, member.id), { onSuccess: onClose });
  return (
    <ConfirmDialog
      title={`Remove ${member.address}`}
      confirmLabel="Remove member"
      busy={remove.isPending}
      error={remove.error}
      onConfirm={() => remove.mutate(undefined)}
      onClose={onClose}
    >
      <p>
        <Mono>{member.address}</Mono> is taken out of pool <Mono>{pool.name}</Mono>. nginx stops sending it requests
        once you reload. The machine and its data are not touched.
      </p>
      <p>Removing the last member is refused while a vhost still uses the pool.</p>
    </ConfirmDialog>
  );
}

// ── Credentials ─────────────────────────────────────────────────────────────

function CredentialsDialog({ pool, onClose }: { pool: Pool; onClose: () => void }) {
  const stored = useCredentials(pool.id);
  const [form, setForm] = useState({ access_key: "", secret_key: "", region: "us-east-1", endpoint_url: "" });
  const [submitted, setSubmitted] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const set = (key: keyof typeof form, value: string) => setForm((current) => ({ ...current, [key]: value }));

  const save = useAction(
    () =>
      api.pools.setCredentials(pool.id, {
        access_key: form.access_key.trim(),
        secret_key: form.secret_key,
        region: form.region.trim(),
        endpoint_url: form.endpoint_url.trim() || null,
      }),
    { onSuccess: onClose },
  );
  const remove = useAction(() => api.pools.removeCredentials(pool.id), { onSuccess: onClose });
  const clientErrors = validateCredentials(form);
  const errors = submitted ? { ...save.error?.fieldErrors, ...clientErrors } : {};

  if (confirmRemove) {
    return (
      <ConfirmDialog
        title={`Remove the credentials of ${pool.name}`}
        confirmLabel="Remove credentials"
        busy={remove.isPending}
        error={remove.error}
        onConfirm={() => remove.mutate(undefined)}
        onClose={() => setConfirmRemove(false)}
      >
        <p>
          Without credentials the orchestrator can no longer list the buckets of this pool, browse it, or migrate to or
          from it. Client traffic is not affected: clients sign their own requests.
        </p>
      </ConfirmDialog>
    );
  }

  return (
    <FormDialog
      title={`S3 credentials of ${pool.name}`}
      submitLabel={stored.data ? "Replace credentials" : "Store credentials"}
      busy={save.isPending}
      error={save.error}
      onClose={onClose}
      onSubmit={() => {
        setSubmitted(true);
        if (!hasErrors(clientErrors)) save.mutate(undefined);
      }}
      footerExtra={
        stored.data ? (
          <Button variant="danger" className="push-left" onClick={() => setConfirmRemove(true)}>
            Remove
          </Button>
        ) : undefined
      }
    >
      <QueryView query={stored}>
        {(credentials) =>
          credentials ? (
            <Notice tone="ok" title="Credentials are stored">
              Access key <Mono>{credentials.access_key_masked}</Mono>, region <Mono>{credentials.region}</Mono>
              {credentials.endpoint_url && (
                <>
                  , endpoint <Mono>{credentials.endpoint_url}</Mono>
                </>
              )}
              . Last changed {formatDateTime(credentials.updated_at)}. The secret key is never shown; enter a new pair
              below to replace it.
            </Notice>
          ) : (
            <Notice tone="warn" title="No credentials stored">
              The orchestrator needs them to list buckets, to browse the pool and to run migrations.
            </Notice>
          )
        }
      </QueryView>
      <TextField
        label="Access key"
        mono
        value={form.access_key}
        onValue={(value) => set("access_key", value)}
        error={errors.access_key}
        hint="The same keys nanio has in its options file on the machines of this pool."
      />
      <TextField
        label="Secret key"
        type="password"
        autoComplete="new-password"
        mono
        value={form.secret_key}
        onValue={(value) => set("secret_key", value)}
        error={errors.secret_key}
        hint="Stored encrypted. It is never returned by the API."
      />
      <div className="grid-2">
        <TextField label="Region" mono value={form.region} onValue={(value) => set("region", value)} error={errors.region} />
        <TextField
          label="Endpoint URL"
          mono
          value={form.endpoint_url}
          onValue={(value) => set("endpoint_url", value)}
          hint="Optional. Leave empty to use the pool's members."
        />
      </div>
    </FormDialog>
  );
}

// ── Node setup ──────────────────────────────────────────────────────────────

const NODE_TYPES: Record<NodeType, { label: string; text: string }> = {
  "nanio-only": {
    label: "nanio only",
    text: "nanio listens on the member's port. Generates the nanio options file and its systemd unit.",
  },
  "nginx-nanio": {
    label: "nginx in front of nanio",
    text: "nginx listens on the member's port and forwards to nanio on loopback. Generates both.",
  },
  "nginx-only": {
    label: "nginx only",
    text: "nginx serves the data directory read-only. nanio's internal metadata directories are never served.",
  },
};

function NodeSetupDialog({ pool, member, onClose }: { pool: Pool; member: Member; onClose: () => void }) {
  const choices: NodeType[] = pool.type === "nanio" ? ["nanio-only", "nginx-nanio"] : ["nginx-only"];
  const [nodeType, setNodeType] = useState<NodeType>(choices[0]!);
  const [form, setForm] = useState({
    data_dir: "/data",
    nanio_port: "",
    nanio_host: "0.0.0.0",
    nanio_region: "us-east-1",
    access_key: "",
    secret_key: "",
    nanio_bin: "/opt/nanio/bin/nanio",
  });
  const [submitted, setSubmitted] = useState(false);
  const [result, setResult] = useState<NodeConfig | null>(null);
  const set = (key: keyof typeof form, value: string) => setForm((current) => ({ ...current, [key]: value }));

  const generate = useAction(
    () =>
      api.pools.nodeConfig(pool.id, member.id, {
        node_type: nodeType,
        data_dir: form.data_dir,
        nanio_port: form.nanio_port.trim() ? Number(form.nanio_port) : null,
        nanio_host: form.nanio_host,
        nanio_region: form.nanio_region,
        access_key: form.access_key || null,
        secret_key: form.secret_key || null,
        nanio_bin: form.nanio_bin,
      }),
    { onSuccess: setResult },
  );
  const clientErrors = validateNodeConfig(form);
  const errors = submitted ? { ...generate.error?.fieldErrors, ...clientErrors } : {};
  const usesNanio = nodeType !== "nginx-only";

  if (result) {
    return (
      <Modal
        title={`Node setup for ${member.address}`}
        onClose={onClose}
        size="lg"
        footer={
          <>
            <Button onClick={() => setResult(null)}>Back to the form</Button>
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          </>
        }
      >
        <div className="stack">
          <Notice tone="info">
            Nothing was deployed and nothing is kept: copy what you need now. Run the commands on the node, in this
            order.
          </Notice>
          {result.warnings.map((warning) => (
            <Notice key={warning} tone="warn" title="Check this before you continue">
              {warning}
            </Notice>
          ))}
          <h3>Files</h3>
          {result.files.map((file) => (
            <CodeBlock key={file.path} title={file.path} code={file.content} />
          ))}
          <h3>Commands, in order</h3>
          <CodeBlock title="Run on the node" code={result.instructions} />
        </div>
      </Modal>
    );
  }

  return (
    <FormDialog
      title={`Node setup for ${member.address}`}
      submitLabel="Generate"
      size="lg"
      busy={generate.isPending}
      error={generate.error}
      onClose={onClose}
      onSubmit={() => {
        setSubmitted(true);
        if (!hasErrors(clientErrors)) generate.mutate(undefined);
      }}
    >
      <p className="muted">
        The orchestrator does not log in to storage machines. This generates what the machine behind{" "}
        <Mono>{member.address}</Mono> needs so that it matches what the gateway expects: the config files and the
        commands to run.
      </p>
      <fieldset className="choices">
        <legend>How the node is set up</legend>
        {choices.map((choice) => (
          <label key={choice} className="choice">
            <input type="radio" name="node-type" checked={nodeType === choice} onChange={() => setNodeType(choice)} />
            <span>
              <strong>{NODE_TYPES[choice].label}</strong>
              <span className="field-hint">{NODE_TYPES[choice].text}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <div className="grid-2">
        <TextField
          label="Data directory"
          mono
          value={form.data_dir}
          onValue={(value) => set("data_dir", value)}
          error={errors.data_dir}
          hint="Where the objects live on the node. The unit refuses to start nanio while it is not mounted."
        />
        {usesNanio && (
          <TextField
            label="nanio binary"
            mono
            value={form.nanio_bin}
            onValue={(value) => set("nanio_bin", value)}
            error={errors.nanio_bin}
          />
        )}
      </div>
      {usesNanio && (
        <>
          <div className="grid-3">
            <TextField
              label="nanio port"
              mono
              inputMode="numeric"
              placeholder="from the member address"
              value={form.nanio_port}
              onValue={(value) => set("nanio_port", value)}
              error={errors.nanio_port}
              hint={nodeType === "nginx-nanio" ? "Loopback port behind nginx (default 9000)." : "Leave empty to use the member's port."}
            />
            <TextField
              label="Bind address"
              mono
              value={nodeType === "nginx-nanio" ? "127.0.0.1" : form.nanio_host}
              disabled={nodeType === "nginx-nanio"}
              onValue={(value) => set("nanio_host", value)}
              error={errors.nanio_host}
              hint={nodeType === "nginx-nanio" ? "Always loopback behind nginx." : undefined}
            />
            <TextField
              label="Region"
              mono
              value={form.nanio_region}
              onValue={(value) => set("nanio_region", value)}
              error={errors.nanio_region}
            />
          </div>
          <div className="grid-2">
            <TextField
              label="Access key"
              mono
              value={form.access_key}
              onValue={(value) => set("access_key", value)}
              error={errors.access_key}
              hint="Use the pool's credentials: nanio verifies requests with the keys in its options file."
            />
            <TextField
              label="Secret key"
              type="password"
              autoComplete="new-password"
              mono
              value={form.secret_key}
              onValue={(value) => set("secret_key", value)}
              error={errors.secret_key}
              hint="Written into the generated options file only. Not stored."
            />
          </div>
        </>
      )}
    </FormDialog>
  );
}

/** Used by the overview: where a pool still needs something. */
export function poolProblems(summary: PoolSummary): string[] {
  const problems: string[] = [];
  if (summary.members === 0) problems.push("has no members");
  else if (summary.enabled_members === 0) problems.push(`has ${plural(summary.members, "member")}, none enabled`);
  if (summary.type === "nanio" && !summary.has_credentials) problems.push("has no stored credentials");
  return problems;
}
