import { useQuery } from "@tanstack/react-query";
import { ArrowRightLeft, EyeOff, FolderSearch, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { Fragment, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Bucket, type BucketStatus, type Pool, type Vhost } from "../api";
import { OrphanOverride } from "../components/OrphanOverride";
import { RouteBucketDialog } from "../components/RouteBucketDialog";
import { useToast } from "../components/toast";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  Mono,
  Notice,
  PageHeader,
  Panel,
  QueryView,
  Spinner,
  type Tone,
} from "../components/ui";
import { formatDateTime, plural, timeAgo } from "../lib/format";
import { keys, useAction, useBuckets, usePools, useVhosts } from "../queries";

const STATUS: Record<BucketStatus, { tone: Tone; text: string }> = {
  unrouted: { tone: "neutral", text: "Served by the default pool" },
  routed: { tone: "ok", text: "Has its own route" },
  migrating: { tone: "info", text: "A migration is moving it" },
  ignored: { tone: "neutral", text: "Hidden by an operator" },
  deleted: { tone: "warn", text: "No longer returned by ListBuckets" },
};

type Dialog =
  | { kind: "route"; bucket: string }
  | { kind: "remove-route"; bucket: Bucket }
  | { kind: "purge"; bucket: string; objects: number };

export function BucketsPage() {
  const vhosts = useVhosts();
  const pools = usePools();
  const [params, setParams] = useSearchParams();

  return (
    <>
      <PageHeader
        title="Buckets"
        description="The buckets discovered on each vhost's default pool, and where each one is served from. Routing a bucket to another pool is how data is sharded."
      />
      <QueryView
        query={vhosts}
        isEmpty={(list) => list.filter((vhost) => vhost.default_pool_id).length === 0}
        empty={
          <EmptyState title="No vhost with a default pool">
            Buckets are discovered on the default pool of a vhost. <Link to="/vhosts">Create a vhost</Link> first.
          </EmptyState>
        }
      >
        {(list) => {
          const usable = list.filter((vhost) => vhost.default_pool_id);
          const selected = usable.find((vhost) => String(vhost.id) === params.get("vhost")) ?? usable[0]!;
          const poolList = pools.data ?? [];
          const defaultPool = poolList.find((pool) => pool.id === selected.default_pool_id);
          return (
            <>
              <div className="tabs" role="tablist" aria-label="Vhost">
                {usable.map((vhost) => (
                  <button
                    key={vhost.id}
                    type="button"
                    role="tab"
                    aria-selected={vhost.id === selected.id}
                    className="tab mono"
                    onClick={() => setParams({ vhost: String(vhost.id) })}
                  >
                    {vhost.server_name}
                  </button>
                ))}
              </div>
              {!defaultPool ? (
                <Spinner />
              ) : defaultPool.type === "nanio" ? (
                <S3Buckets key={selected.id} vhost={selected} pools={poolList} defaultPool={defaultPool} />
              ) : (
                <HttpBuckets key={selected.id} vhost={selected} defaultPool={defaultPool} />
              )}
            </>
          );
        }}
      </QueryView>
    </>
  );
}

// ── S3 vhosts ───────────────────────────────────────────────────────────────

const FILTERS: ("all" | BucketStatus)[] = ["all", "unrouted", "routed", "migrating", "ignored", "deleted"];

function S3Buckets({ vhost, pools, defaultPool }: { vhost: Vhost; pools: Pool[]; defaultPool: Pool }) {
  const buckets = useBuckets(vhost.id);
  const toast = useToast();
  const [filter, setFilter] = useState<"all" | BucketStatus>("all");
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const close = () => setDialog(null);

  const sync = useAction(() => api.buckets.sync(vhost.id), {
    onSuccess: () => toast.success("Bucket list refreshed", `Asked ${defaultPool.name} for its buckets.`),
  });
  const ignore = useAction((bucket: string) => api.buckets.ignore(vhost.id, bucket));

  return (
    <div className="stack-lg">
      <Panel
        flush
        title={
          <>
            Buckets of <Mono>{vhost.server_name}</Mono>
          </>
        }
        meta={
          <>
            <span>
              default pool <Mono>{defaultPool.name}</Mono>
            </span>
            {buckets.data && <span>last discovery {timeAgo(buckets.data.last_synced_at)}</span>}
          </>
        }
        actions={
          <Button
            size="sm"
            icon={<RefreshCw className="icon" aria-hidden />}
            busy={sync.isPending}
            onClick={() => sync.mutate(undefined, { onError: (error) => toast.error("Could not refresh the bucket list", error) })}
          >
            Sync now
          </Button>
        }
      >
        <QueryView query={buckets}>
          {(data) => {
            const counts = new Map<string, number>();
            for (const bucket of data.buckets) counts.set(bucket.status, (counts.get(bucket.status) ?? 0) + 1);
            const shown = data.buckets.filter((bucket) => filter === "all" || bucket.status === filter);
            return (
              <>
                <div className="filter-bar" role="group" aria-label="Filter by state">
                  {FILTERS.map((name) => (
                    <button
                      key={name}
                      type="button"
                      className="filter"
                      aria-pressed={filter === name}
                      onClick={() => setFilter(name)}
                    >
                      {name === "all" ? "All" : name} <span className="count">{name === "all" ? data.buckets.length : (counts.get(name) ?? 0)}</span>
                    </button>
                  ))}
                </div>
                {shown.length === 0 ? (
                  <EmptyState title={data.buckets.length === 0 ? "No buckets discovered yet" : `No ${filter} buckets`}>
                    {data.buckets.length === 0 &&
                      "Buckets appear here after the next discovery (every few minutes), or right away with Sync now. A bucket is created by its owner through the gateway."}
                  </EmptyState>
                ) : (
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Bucket</th>
                        <th>State</th>
                        <th>Served by</th>
                        <th>Discovered</th>
                        <th className="actions">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {shown.map((bucket) => (
                        <Fragment key={bucket.name}>
                          <tr>
                            <td>
                              <Mono>{bucket.name}</Mono>
                            </td>
                            <td>
                              <Badge tone={STATUS[bucket.status].tone} title={STATUS[bucket.status].text}>
                                {bucket.status}
                              </Badge>{" "}
                              {bucket.missing_since && <Badge tone="bad">missing on pool</Badge>}
                            </td>
                            <td>
                              {bucket.pool_name ? <Mono>{bucket.pool_name}</Mono> : <span className="muted">default pool</span>}
                            </td>
                            <td title={formatDateTime(bucket.discovered_at)}>{timeAgo(bucket.discovered_at)}</td>
                            <td className="actions">
                              {bucket.status !== "deleted" && (
                                <Link
                                  className="btn btn-ghost btn-icon"
                                  to={`/explorer?vhost=${vhost.id}&bucket=${encodeURIComponent(bucket.name)}`}
                                  aria-label={`Browse ${bucket.name}`}
                                  title="Browse: see which pool holds what"
                                >
                                  <FolderSearch className="icon" aria-hidden />
                                </Link>
                              )}
                              {(bucket.status === "unrouted" || bucket.status === "ignored") && (
                                <Button size="sm" icon={<Plus className="icon" aria-hidden />} onClick={() => setDialog({ kind: "route", bucket: bucket.name })}>
                                  Route to a pool
                                </Button>
                              )}
                              {bucket.status === "unrouted" && (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  icon={<EyeOff className="icon" aria-hidden />}
                                  title="Hide it from the unrouted list. Routing is not changed."
                                  onClick={() =>
                                    ignore.mutate(bucket.name, { onError: (error) => toast.error(`Could not ignore ${bucket.name}`, error) })
                                  }
                                >
                                  Ignore
                                </Button>
                              )}
                              {bucket.status === "routed" && bucket.routed_pool_id && (
                                <Link
                                  className="btn btn-default btn-sm"
                                  to={`/migrations?start=bucket&bucket=${encodeURIComponent(bucket.name)}&src=${bucket.routed_pool_id}`}
                                >
                                  <ArrowRightLeft className="icon" aria-hidden />
                                  <span>Move</span>
                                </Link>
                              )}
                              {bucket.status === "migrating" && (
                                <Link className="btn btn-default btn-sm" to="/migrations">
                                  View migration
                                </Link>
                              )}
                              {(bucket.status === "routed" || (bucket.status === "deleted" && bucket.routed_pool_id)) && (
                                <Button size="sm" onClick={() => setDialog({ kind: "remove-route", bucket })}>
                                  Remove route
                                </Button>
                              )}
                            </td>
                          </tr>
                          {bucket.missing_since && (
                            <tr className="row-detail">
                              <td colSpan={5}>
                                <Notice tone="bad" title={`${bucket.pool_name ?? "Its pool"} says this bucket does not exist (since ${formatDateTime(bucket.missing_since)})`}>
                                  The route is kept on purpose and nginx was not reloaded: a node whose storage is not mounted
                                  reports every bucket as missing, and removing the route would send clients to the default
                                  pool, where an older copy may still exist. Decide which case it is:
                                  <ul className="plain-list">
                                    <li>
                                      <strong>The owner deleted the bucket</strong> — use <em>Remove route</em>, then reload nginx.
                                    </li>
                                    <li>
                                      <strong>The pool's storage is not mounted, or the node points at an empty data directory</strong>{" "}
                                      — fix the node. This mark clears by itself at the next discovery.
                                    </li>
                                  </ul>
                                </Notice>
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            );
          }}
        </QueryView>
      </Panel>

      <Leftovers vhost={vhost} defaultPool={defaultPool} onPurge={(bucket, objects) => setDialog({ kind: "purge", bucket, objects })} />

      {dialog?.kind === "route" && <RouteBucketDialog vhost={vhost} bucket={dialog.bucket} pools={pools} onClose={close} />}
      {dialog?.kind === "remove-route" && <RemoveBucketRouteDialog vhost={vhost} bucket={dialog.bucket} defaultPool={defaultPool} onClose={close} />}
      {dialog?.kind === "purge" && (
        <PurgeDialog vhost={vhost} bucket={dialog.bucket} objects={dialog.objects} defaultPool={defaultPool} onClose={close} />
      )}
    </div>
  );
}

function RemoveBucketRouteDialog({
  vhost,
  bucket,
  defaultPool,
  onClose,
}: {
  vhost: Vhost;
  bucket: Bucket;
  defaultPool: Pool;
  onClose: () => void;
}) {
  const remove = useAction((allowOrphan: boolean) => api.buckets.removeRoute(vhost.id, bucket.name, allowOrphan), {
    onSuccess: onClose,
  });
  return (
    <ConfirmDialog
      title={`Remove the route of ${bucket.name}`}
      confirmLabel="Remove route"
      busy={remove.isPending && !remove.variables}
      error={remove.error}
      onConfirm={() => remove.mutate(false)}
      onClose={onClose}
      extra={
        <OrphanOverride
          key={remove.error?.detail}
          error={remove.error}
          consequence={`The objects of ${bucket.name} stay on ${bucket.pool_name ?? "its pool"} and will not be reachable through ${vhost.server_name} once nginx is reloaded.`}
          actionLabel="Remove the route anyway"
          busy={remove.isPending && remove.variables === true}
          onOverride={() => remove.mutate(true)}
        />
      }
    >
      <p>
        After the next reload, requests for <Mono>/{bucket.name}/</Mono> go back to the default pool{" "}
        <Mono>{defaultPool.name}</Mono>.
      </p>
      {bucket.status === "deleted" ? (
        <p>The bucket no longer exists, so this only cleans up its route.</p>
      ) : (
        <p>
          It is refused while the bucket has objects on <Mono>{bucket.pool_name}</Mono>: they would stay there, no
          longer reachable through <Mono>{vhost.server_name}</Mono>. To bring the data back as well, move the bucket
          with a migration instead. Nothing is deleted.
        </p>
      )}
    </ConfirmDialog>
  );
}

function Leftovers({ vhost, defaultPool, onPurge }: { vhost: Vhost; defaultPool: Pool; onPurge: (bucket: string, objects: number) => void }) {
  const [asked, setAsked] = useState(false);
  const orphans = useQuery({
    queryKey: [...keys.buckets(vhost.id), "orphans"],
    queryFn: () => api.buckets.orphans(vhost.id),
    enabled: asked,
    staleTime: 0,
  });
  return (
    <Panel
      title="Leftover copies on the default pool"
      meta="Routed buckets that still have objects on the default pool: the old copy after a move. Nothing is deleted for you."
      actions={
        <Button
          size="sm"
          icon={<Search className="icon" aria-hidden />}
          busy={orphans.isFetching}
          onClick={() => (asked ? void orphans.refetch() : setAsked(true))}
        >
          {asked ? "Check again" : "Check now"}
        </Button>
      }
    >
      {!asked ? (
        <p className="muted">
          Checking counts the objects of every routed bucket on <Mono>{defaultPool.name}</Mono>, which can take a while on
          large buckets. Run it when you want to clean up.
        </p>
      ) : (
        <QueryView
          query={orphans}
          isEmpty={(data) => data.orphans.length === 0}
          empty={<p className="muted">None found among {plural(orphans.data?.checked ?? 0, "routed bucket")}.</p>}
        >
          {(data) => (
            <table className="table">
              <thead>
                <tr>
                  <th>Bucket</th>
                  <th className="num">Objects left on {defaultPool.name}</th>
                  <th className="actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.orphans.map((orphan) => (
                  <tr key={orphan.bucket}>
                    <td>
                      <Mono>{orphan.bucket}</Mono>
                    </td>
                    <td className="num">{orphan.objects}</td>
                    <td className="actions">
                      <Link
                        className="btn btn-default btn-sm"
                        to={`/explorer?pool=${defaultPool.id}&bucket=${encodeURIComponent(orphan.bucket)}`}
                      >
                        <FolderSearch className="icon" aria-hidden />
                        <span>Browse</span>
                      </Link>
                      <Button size="sm" variant="danger" icon={<Trash2 className="icon" aria-hidden />} onClick={() => onPurge(orphan.bucket, orphan.objects)}>
                        Purge
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </QueryView>
      )}
    </Panel>
  );
}

function PurgeDialog({
  vhost,
  bucket,
  objects,
  defaultPool,
  onClose,
}: {
  vhost: Vhost;
  bucket: string;
  objects: number;
  defaultPool: Pool;
  onClose: () => void;
}) {
  const toast = useToast();
  const purge = useAction(() => api.buckets.purgeOrphan(vhost.id, bucket), {
    onSuccess: (result) => {
      if (result.ok) toast.success(`Purged ${bucket} on ${defaultPool.name}`, `${result.deleted} of ${result.total} objects deleted.`);
      else toast.warn(`Purge of ${bucket} was incomplete`, `${result.deleted} of ${result.total} objects deleted. Not deleted: ${result.errors.join(", ")}`);
      onClose();
    },
  });
  return (
    <ConfirmDialog
      title={`Purge the leftover copy of ${bucket}`}
      confirmLabel={`Delete ${plural(objects, "object")}`}
      typeToConfirm={bucket}
      busy={purge.isPending}
      error={purge.error}
      onConfirm={() => purge.mutate(undefined)}
      onClose={onClose}
    >
      <p>
        This <strong>deletes {plural(objects, "object")}</strong> of bucket <Mono>{bucket}</Mono> on the default pool{" "}
        <Mono>{defaultPool.name}</Mono>. It cannot be undone.
      </p>
      <p>
        Clients are served from the pool the bucket is routed to, so they do not see this copy. Verify the bucket on
        that pool first: after the purge it is the only copy.
      </p>
    </ConfirmDialog>
  );
}

// ── http vhosts ─────────────────────────────────────────────────────────────

function HttpBuckets({ vhost, defaultPool }: { vhost: Vhost; defaultPool: Pool }) {
  const toast = useToast();
  const linked = defaultPool.source_nanio_pool_id !== null;
  const routes = useQuery({
    queryKey: keys.httpRoutes(vhost.id),
    queryFn: () => api.buckets.httpRoutes(vhost.id),
    enabled: linked,
    refetchInterval: 10_000,
  });
  const add = useAction((bucket: string) => api.buckets.addHttpRoute(vhost.id, bucket));
  const remove = useAction((bucket: string) => api.buckets.removeHttpRoute(vhost.id, bucket));

  if (!linked) {
    return (
      <Notice tone="info" title={`${defaultPool.name} is not linked to a nanio pool`}>
        <Mono>{vhost.server_name}</Mono> serves files over plain HTTP from the http pool <Mono>{defaultPool.name}</Mono>.
        To expose individual buckets here and have them follow migrations, link that pool to the nanio pool whose files it
        serves: <Link to="/pools">Pools</Link> → Edit → <em>Serves the files of</em>.
      </Notice>
    );
  }

  return (
    <div className="stack-lg">
      <Notice tone="warn" title="Requests to an http pool are not authenticated">
        They do not go through nanio. Use the vhost's IP rules if the content is not public.
      </Notice>
      <QueryView query={routes}>
        {(data) => (
          <Panel
            flush
            title={
              <>
                Buckets served by <Mono>{vhost.server_name}</Mono>
              </>
            }
            meta={
              <span>
                files of nanio pool <Mono>{data.source_nanio_pool_name}</Mono>, through http pool <Mono>{defaultPool.name}</Mono>
              </span>
            }
          >
            {data.linked_nanio_buckets.length === 0 && data.routes.length === 0 ? (
              <EmptyState title="No buckets known on the linked nanio pool">
                Buckets are listed once a vhost with <Mono>{data.source_nanio_pool_name}</Mono> as its default pool has
                discovered them.
              </EmptyState>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Bucket</th>
                    <th>Route on this vhost</th>
                    <th className="actions">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {data.routes.map((route) => (
                    <tr key={route.id}>
                      <td>
                        <Mono>{route.bucket}</Mono>
                      </td>
                      <td>
                        <Mono>{route.path_prefix}</Mono> → <Mono>{route.pool_name}</Mono>
                      </td>
                      <td className="actions">
                        {!route.bucket.includes("/") && (
                          <Button
                            size="sm"
                            busy={remove.isPending && remove.variables === route.bucket}
                            onClick={() =>
                              remove.mutate(route.bucket, { onError: (error) => toast.error(`Could not remove ${route.path_prefix}`, error) })
                            }
                          >
                            Remove route
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                  {data.linked_nanio_buckets
                    .filter((bucket) => !bucket.has_route)
                    .map((bucket) => (
                      <tr key={bucket.bucket}>
                        <td>
                          <Mono>{bucket.bucket}</Mono>
                        </td>
                        <td className="muted">none</td>
                        <td className="actions">
                          <Button
                            size="sm"
                            icon={<Plus className="icon" aria-hidden />}
                            busy={add.isPending && add.variables === bucket.bucket}
                            onClick={() =>
                              add.mutate(bucket.bucket, { onError: (error) => toast.error(`Could not add a route for ${bucket.bucket}`, error) })
                            }
                          >
                            Add route
                          </Button>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            )}
          </Panel>
        )}
      </QueryView>
    </div>
  );
}
