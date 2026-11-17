import { QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { api, ApiError, onUnauthorized, type Session } from "./api";
import { Layout } from "./components/Layout";
import { ErrorNotice, Spinner } from "./components/ui";
import { AuditPage } from "./pages/Audit";
import { BucketsPage } from "./pages/Buckets";
import { ConfigurationPage } from "./pages/Configuration";
import { ExplorerPage } from "./pages/Explorer";
import { LoginPage } from "./pages/Login";
import { MigrationsPage } from "./pages/Migrations";
import { OverviewPage } from "./pages/Overview";
import { PoolsPage } from "./pages/Pools";
import { SettingsPage } from "./pages/Settings";
import { VhostsPage } from "./pages/Vhosts";
import { keys, useSession } from "./queries";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // an answer of the API (4xx) will not change by asking again; a network blip might
        retry: (failures, error) => failures < 2 && !(error instanceof ApiError && error.status >= 400),
        refetchOnWindowFocus: true,
        staleTime: 2_000,
      },
    },
  });
}

export function App() {
  const client = useQueryClient();
  const session = useSession();

  // Any data request answered 401 means the session is over: show the login screen.
  useEffect(() => {
    onUnauthorized(() => {
      client.setQueryData<Session>(keys.session, (current) => (current ? { ...current, authenticated: false } : current));
    });
    return () => onUnauthorized(null);
  }, [client]);

  if (session.isPending) return <div className="boot"><Spinner label="Connecting…" /></div>;
  if (session.isError || !session.data) {
    return (
      <div className="boot">
        <ErrorNotice error={session.error} title="The orchestrator is not answering" />
      </div>
    );
  }
  if (!session.data.authenticated) {
    return <LoginPage session={session.data} onSignedIn={(next) => client.setQueryData(keys.session, next)} />;
  }

  const logout = async () => {
    const next = await api.logout().catch(() => ({ ...session.data, authenticated: false }));
    client.setQueryData(keys.session, next);
    client.removeQueries({ predicate: (query) => query.queryKey[0] !== "session" });
  };

  return (
    <Routes>
      <Route element={<Layout session={session.data} onLogout={() => void logout()} />}>
        <Route index element={<OverviewPage />} />
        <Route path="pools" element={<PoolsPage />} />
        <Route path="vhosts" element={<VhostsPage />} />
        <Route path="buckets" element={<BucketsPage />} />
        <Route path="explorer" element={<ExplorerPage />} />
        <Route path="migrations" element={<MigrationsPage />} />
        <Route path="configuration" element={<ConfigurationPage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
