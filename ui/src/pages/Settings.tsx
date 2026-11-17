import { useQuery } from "@tanstack/react-query";
import { Lock, Pencil, Power } from "lucide-react";
import { useState } from "react";
import { api, type Settings } from "../api";
import { useToast } from "../components/toast";
import { Badge, Button, ConfirmDialog, FormDialog, Mono, Notice, PageHeader, Panel, QueryView, TextField } from "../components/ui";
import { plural } from "../lib/format";
import { useAction } from "../queries";

interface Meta {
  key: string;
  text: string;
  secret?: boolean;
  /** Not accepted by the API at all: only the config file sets it. */
  fileOnly?: boolean;
  warning?: string;
}

const GROUPS: { title: string; settings: Meta[] }[] = [
  {
    title: "Core",
    settings: [
      { key: "host", text: "Address the panel and API listen on" },
      { key: "port", text: "Port the panel and API listen on" },
      {
        key: "api_key",
        text: "Key for this panel and the X-Orchestrator-Key header",
        secret: true,
        warning: "After the restart you sign in with the new key, and every API client must send it.",
      },
      { key: "log_level", text: "debug, info, warning or error" },
      { key: "session_ttl", text: "How long a panel session lasts, in seconds" },
      { key: "log_file", text: "Rotating log file; empty logs to stdout only" },
    ],
  },
  {
    title: "Database",
    settings: [
      { key: "db_path", text: "SQLite database file" },
      { key: "db_backup_path", text: "Where backups are written" },
      { key: "db_backup_interval", text: "Seconds between timed backups" },
      { key: "db_backup_rotate", text: "Number of backup copies kept" },
    ],
  },
  {
    title: "Security",
    settings: [
      {
        key: "secret",
        text: "Fernet key that encrypts the stored pool credentials",
        secret: true,
        warning: "Credentials stored with the current key cannot be read with another one: you will have to store the credentials of every pool again.",
      },
    ],
  },
  {
    title: "S3",
    settings: [
      { key: "bucket_sync_interval", text: "Seconds between bucket discoveries" },
      { key: "s3_request_timeout", text: "Socket timeout of the orchestrator's own S3 calls, in seconds" },
    ],
  },
  {
    title: "nginx",
    settings: [
      { key: "nginx_config_dir", text: "Directory the managed config is written to" },
      { key: "drift_interval", text: "Seconds between drift checks" },
    ],
  },
  {
    title: "Migrations",
    settings: [
      { key: "rclone_path", text: "The rclone binary" },
      { key: "migration_max_parallel", text: "Migrations running at the same time" },
      { key: "migration_transfers", text: "rclone --transfers: parallel object copies" },
      { key: "migration_checkers", text: "rclone --checkers" },
      { key: "migration_bandwidth_limit", text: "rclone --bwlimit, e.g. 50M; empty for no limit" },
      { key: "migration_max_copy_passes", text: "Maximum passes while copying, and again while verifying" },
    ],
  },
];

const KNOWN = new Set(GROUPS.flatMap((group) => group.settings.map((setting) => setting.key)));
const NOT_SETTINGS = new Set(["config_file", "dev_mode", "host_only", "dev"]);

function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  return String(value);
}

export function SettingsPage() {
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.config.settings });
  const toast = useToast();
  const [editing, setEditing] = useState<Meta | null>(null);
  const [confirmRestart, setConfirmRestart] = useState(false);
  // What was written to the config file in this session and is waiting for a restart.
  const [saved, setSaved] = useState<Record<string, string>>({});
  const restart = useAction(() => api.config.restart(), {
    onSuccess: (answer) => {
      toast.success("Restart scheduled", `${answer.message}. The panel reconnects by itself in a few seconds.`);
      setSaved({});
      setConfirmRestart(false);
    },
  });
  const waiting = Object.keys(saved).length;

  return (
    <>
      <PageHeader
        title="Settings"
        description="Settings are read when the service starts. A change made here is written to the config file and takes effect after a restart."
      />
      <QueryView query={settings}>
        {(data) => {
          const hostOnly = new Set(data.host_only);
          const others = Object.keys(data).filter((key) => !KNOWN.has(key) && !NOT_SETTINGS.has(key));
          const groups = others.length
            ? [...GROUPS, { title: "Other", settings: others.map((key) => ({ key, text: "", fileOnly: true })) }]
            : GROUPS;
          return (
            <div className="stack-lg">
              {waiting > 0 && (
                <Notice
                  tone="warn"
                  title={`Restart required: ${plural(waiting, "setting")} saved but not in effect`}
                  actions={
                    <Button variant="primary" size="sm" icon={<Power className="icon" aria-hidden />} onClick={() => setConfirmRestart(true)}>
                      Restart the service
                    </Button>
                  }
                >
                  The running service still uses the previous values of <Mono>{Object.keys(saved).join(", ")}</Mono>.
                </Notice>
              )}
              <Notice tone="info">
                Config file: <Mono>{data.config_file}</Mono>
                {data.dev_mode && " — development mode."} Secrets are shown masked.
              </Notice>
              {groups.map((group) => (
                <Panel key={group.title} flush title={group.title}>
                  <table className="table settings-table">
                    <thead>
                      <tr>
                        <th>Setting</th>
                        <th>Value in use</th>
                        <th className="actions">Change</th>
                      </tr>
                    </thead>
                    <tbody>
                      {group.settings
                        .filter((setting) => setting.key in data)
                        .map((setting) => (
                          <SettingRow
                            key={setting.key}
                            meta={setting}
                            data={data}
                            hostOnly={hostOnly.has(setting.key)}
                            saved={saved[setting.key]}
                            onEdit={() => setEditing(setting)}
                          />
                        ))}
                    </tbody>
                  </table>
                </Panel>
              ))}
            </div>
          );
        }}
      </QueryView>

      {editing && settings.data && (
        <EditDialog
          meta={editing}
          current={display(settings.data[editing.key])}
          onSaved={(value) => setSaved((map) => ({ ...map, [editing.key]: value }))}
          onClose={() => setEditing(null)}
        />
      )}
      {confirmRestart && (
        <ConfirmDialog
          title="Restart the orchestrator service"
          confirmLabel="Restart"
          danger={false}
          busy={restart.isPending}
          error={restart.error}
          onConfirm={() => restart.mutate(undefined)}
          onClose={() => setConfirmRestart(false)}
        >
          <p>
            The service restarts about a second after you confirm. nginx keeps serving: client traffic never passes
            through the orchestrator.
          </p>
          <p>
            A migration that is copying or verifying is picked up again after the restart. One that is in the middle of
            a routing change is marked as failed and has to be started again: avoid restarting while a migration is
            switching.
          </p>
        </ConfirmDialog>
      )}
    </>
  );
}

function SettingRow({
  meta,
  data,
  hostOnly,
  saved,
  onEdit,
}: {
  meta: Meta;
  data: Settings;
  hostOnly: boolean;
  saved: string | undefined;
  onEdit: () => void;
}) {
  const value = display(data[meta.key]);
  return (
    <tr>
      <td>
        <Mono>{meta.key}</Mono>
        <div className="cell-note">{meta.text}</div>
      </td>
      <td>
        {value ? <Mono>{value}</Mono> : <span className="muted">not set</span>}
        {meta.secret && value && <span className="muted"> (masked)</span>}
        {saved !== undefined && (
          <div className="cell-note">
            <Badge tone="warn">restart required</Badge> saved: <Mono>{saved || "(empty)"}</Mono>
          </div>
        )}
      </td>
      <td className="actions">
        {hostOnly ? (
          <span className="readonly" title="Read-only in the panel">
            <Lock className="icon" aria-hidden />
            <span>
              Host only. It decides which program or files the service uses, so the panel key cannot change it. On the
              host: <Mono>nanio-orchestrator config set {meta.key} &lt;value&gt;</Mono>
            </span>
          </span>
        ) : meta.fileOnly ? (
          <span className="readonly" title="Read-only in the panel">
            <Lock className="icon" aria-hidden />
            <span>
              Set <Mono>NANIO_ORCHESTRATOR_{meta.key.toUpperCase()}</Mono> in the config file.
            </span>
          </span>
        ) : (
          <Button size="sm" icon={<Pencil className="icon" aria-hidden />} onClick={onEdit}>
            {meta.secret ? "Replace" : "Change"}
          </Button>
        )}
      </td>
    </tr>
  );
}

function EditDialog({ meta, current, onSaved, onClose }: { meta: Meta; current: string; onSaved: (value: string) => void; onClose: () => void }) {
  const [value, setValue] = useState(meta.secret ? "" : current);
  const save = useAction(() => api.config.setSetting(meta.key, value), {
    onSuccess: (answer) => {
      onSaved(answer.value ?? "");
      onClose();
    },
  });
  // eslint-disable-next-line no-control-regex
  const invalid = /[\x00-\x1f\x7f]/.test(value) ? "No line breaks or control characters" : meta.secret && !value ? "Enter the new value" : undefined;
  return (
    <FormDialog
      title={`${meta.secret ? "Replace" : "Change"} ${meta.key}`}
      submitLabel="Save to the config file"
      busy={save.isPending}
      error={save.error}
      submitDisabled={Boolean(invalid)}
      onClose={onClose}
      onSubmit={() => save.mutate(undefined)}
    >
      <p className="muted">{meta.text}.</p>
      <TextField
        label={meta.secret ? "New value" : "Value"}
        mono
        autoFocus
        type={meta.secret ? "password" : "text"}
        autoComplete={meta.secret ? "new-password" : "off"}
        value={value}
        onValue={setValue}
        error={value ? invalid : undefined}
        hint={meta.secret ? `In use now: ${current || "not set"} (masked).` : undefined}
      />
      {meta.warning && <Notice tone="warn">{meta.warning}</Notice>}
      <Notice tone="info">The value is written to the config file now and takes effect when the service restarts.</Notice>
    </FormDialog>
  );
}
