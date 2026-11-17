import {
  ArrowRightLeft,
  Boxes,
  FileCog,
  FolderSearch,
  Globe,
  History,
  LayoutDashboard,
  LogOut,
  RefreshCw,
  Server,
  Settings as SettingsIcon,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { api, type Session } from "../api";
import { isActive, pendingState, plural, unresolvedSplits } from "../lib/format";
import { keys, useAction, useConfigStatus, useHealth, useMigrations } from "../queries";
import { useToast } from "./toast";
import { Badge, Button, type Tone } from "./ui";

const NAV: { to: string; label: string; icon: ReactNode; end?: boolean }[] = [
  { to: "/", label: "Overview", icon: <LayoutDashboard className="icon" aria-hidden />, end: true },
  { to: "/pools", label: "Pools", icon: <Server className="icon" aria-hidden /> },
  { to: "/vhosts", label: "Vhosts", icon: <Globe className="icon" aria-hidden /> },
  { to: "/buckets", label: "Buckets", icon: <Boxes className="icon" aria-hidden /> },
  { to: "/explorer", label: "Explorer", icon: <FolderSearch className="icon" aria-hidden /> },
  { to: "/migrations", label: "Migrations", icon: <ArrowRightLeft className="icon" aria-hidden /> },
  { to: "/configuration", label: "Configuration", icon: <FileCog className="icon" aria-hidden /> },
  { to: "/audit", label: "Audit log", icon: <History className="icon" aria-hidden /> },
  { to: "/settings", label: "Settings", icon: <SettingsIcon className="icon" aria-hidden /> },
];

/** Reload nginx and say what it answered. Used by the pending bar and the Configuration screen. */
export function useReloadNginx() {
  const toast = useToast();
  return useAction(() => api.config.reload(), {
    onSuccess: (result) => {
      if (result.ok) toast.success("nginx reloaded", "The configuration on disk is now what nginx serves.");
      else toast.error("nginx did not reload", new Error(result.output));
    },
  });
}

/** Unmissable while changes are written but nginx has not been told to load them. */
export function PendingBar() {
  const status = useConfigStatus();
  const reload = useReloadNginx();
  const toast = useToast();
  const state = pendingState(status.data);
  if (state.kind === "applied") return null;
  return (
    <div className={`pending-bar pending-${state.kind}`} role="status" aria-live="polite">
      <RefreshCw className="icon" aria-hidden />
      <span className="pending-text">{state.message}</span>
      <Link to="/configuration" className="pending-link">
        Review
      </Link>
      <Button
        variant="primary"
        size="sm"
        busy={reload.isPending}
        onClick={() => reload.mutate(undefined, { onError: (error) => toast.error("Could not reload nginx", error) })}
      >
        Reload nginx
      </Button>
    </div>
  );
}

function StatusChip({ tone, to, children }: { tone: Tone; to: string; children: ReactNode }) {
  return (
    <Link to={to} className="chip-link">
      <Badge tone={tone}>{children}</Badge>
    </Link>
  );
}

function ClusterStatus() {
  const health = useHealth();
  const migrations = useMigrations();
  const client = useQueryClient();
  // A migration reloads nginx at its routing steps: when one changes phase, what is waiting
  // for a reload may have changed too, so ask again at once instead of at the next poll.
  const phases = (migrations.data ?? []).map((m) => `${m.id}:${m.phase}`).join(",");
  useEffect(() => {
    void client.invalidateQueries({ queryKey: keys.configStatus });
    void client.invalidateQueries({ queryKey: keys.vhosts });
  }, [client, phases]);
  if (health.isError && !health.data) return <Badge tone="bad">Orchestrator unreachable</Badge>;
  if (!health.data) return null;
  const running = migrations.data?.filter(isActive).length ?? 0;
  const splits = migrations.data ? unresolvedSplits(migrations.data).length : 0;
  const { db_ok, drift_alerts, missing_buckets } = health.data;
  const healthy = db_ok && !drift_alerts && !missing_buckets && !splits;
  return (
    <div className="cluster-status" aria-label="Cluster status">
      {!db_ok && <Badge tone="bad">Database unreadable</Badge>}
      {healthy && <Badge tone="ok">No problems</Badge>}
      {drift_alerts > 0 && (
        <StatusChip tone="warn" to="/configuration">
          {plural(drift_alerts, "drifted file")}
        </StatusChip>
      )}
      {missing_buckets > 0 && (
        <StatusChip tone="bad" to="/buckets">
          {plural(missing_buckets, "bucket")} missing on pool
        </StatusChip>
      )}
      {splits > 0 && (
        <StatusChip tone="warn" to="/migrations">
          {plural(splits, "split")} left active
        </StatusChip>
      )}
      {running > 0 && (
        <StatusChip tone="info" to="/migrations">
          {plural(running, "migration")} running
        </StatusChip>
      )}
    </div>
  );
}

export function Layout({ session, onLogout }: { session: Session; onLogout: () => void }) {
  return (
    <div className="app">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden />
          <span>
            nanio<span className="brand-dim">-orchestrator</span>
          </span>
        </div>
        <nav aria-label="Main">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className="nav-item">
              {item.icon}
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="mono">v{session.version}</span>
          {session.dev_mode && <Badge tone="warn" title="nginx commands are printed, not executed">dev mode</Badge>}
        </div>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <ClusterStatus />
          <div className="topbar-right">
            <a href="/api/docs" target="_blank" rel="noreferrer" className="topbar-link">
              API docs
            </a>
            <Button size="sm" variant="ghost" icon={<LogOut className="icon" aria-hidden />} onClick={onLogout}>
              Sign out
            </Button>
          </div>
        </header>
        <PendingBar />
        <main id="main" className="content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
