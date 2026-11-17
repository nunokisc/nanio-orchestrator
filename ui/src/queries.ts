/** Server state. Live data is polled; a mutation refreshes everything it could have changed. */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "./api";
import { isActive } from "./lib/format";

const FAST = 3_000;
const NORMAL = 10_000;
const SLOW = 30_000;

export const keys = {
  session: ["session"] as const,
  health: ["health"] as const,
  overview: ["overview"] as const,
  configStatus: ["config", "status"] as const,
  pools: ["pools"] as const,
  members: (poolId: number) => ["pools", poolId, "members"] as const,
  credentials: (poolId: number) => ["pools", poolId, "credentials"] as const,
  vhosts: ["vhosts"] as const,
  routes: (vhostId: number) => ["vhosts", vhostId, "routes"] as const,
  buckets: (vhostId: number) => ["vhosts", vhostId, "buckets"] as const,
  httpRoutes: (vhostId: number) => ["vhosts", vhostId, "http-routes"] as const,
  migrations: ["migrations"] as const,
};

export const useSession = () => useQuery({ queryKey: keys.session, queryFn: api.session, staleTime: Infinity, retry: false });
export const useHealth = () => useQuery({ queryKey: keys.health, queryFn: api.health, refetchInterval: NORMAL });
export const useOverview = () => useQuery({ queryKey: keys.overview, queryFn: api.overview, refetchInterval: NORMAL });
export const useConfigStatus = () =>
  useQuery({ queryKey: keys.configStatus, queryFn: api.config.status, refetchInterval: 5_000 });
export const usePools = () => useQuery({ queryKey: keys.pools, queryFn: api.pools.list, refetchInterval: SLOW });
export const useMembers = (poolId: number) =>
  useQuery({ queryKey: keys.members(poolId), queryFn: () => api.pools.members(poolId), refetchInterval: SLOW });
export const useVhosts = () => useQuery({ queryKey: keys.vhosts, queryFn: api.vhosts.list, refetchInterval: SLOW });
export const useRoutes = (vhostId: number) =>
  useQuery({ queryKey: keys.routes(vhostId), queryFn: () => api.vhosts.routes(vhostId), refetchInterval: NORMAL });
export const useBuckets = (vhostId: number | null) =>
  useQuery({
    queryKey: keys.buckets(vhostId ?? 0),
    queryFn: () => api.buckets.list(vhostId!),
    enabled: vhostId !== null,
    refetchInterval: NORMAL,
    placeholderData: keepPreviousData,
  });
export const useMigrations = () =>
  useQuery({
    queryKey: keys.migrations,
    queryFn: api.migrations.list,
    // quick while something is moving
    refetchInterval: (query) => (query.state.data?.some(isActive) ? FAST : NORMAL),
  });

/** Stored credentials of a pool, or null when none are stored (the API answers 404). */
export const useCredentials = (poolId: number, enabled = true) =>
  useQuery({
    queryKey: keys.credentials(poolId),
    enabled,
    queryFn: async () => {
      try {
        return await api.pools.credentials(poolId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
  });

/**
 * A change. Whatever it did, every list may be different afterwards (a route changes a config
 * file, a member changes the pending indicator…), so everything is refetched.
 */
export function useAction<TInput, TResult>(
  action: (input: TInput) => Promise<TResult>,
  options: { onSuccess?: (result: TResult, input: TInput) => void } = {},
) {
  const client = useQueryClient();
  return useMutation<TResult, ApiError, TInput>({
    mutationFn: action,
    onSettled: () => client.invalidateQueries({ predicate: (query) => query.queryKey[0] !== "session" }),
    onSuccess: options.onSuccess,
  });
}
