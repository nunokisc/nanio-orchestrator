import { useInfiniteQuery, useQuery, type UseQueryResult } from "@tanstack/react-query";
import { ChevronRight, CornerUpRight, Download, File, Folder, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type ExplorerObject, type Located, type Pool, type Vhost } from "../api";
import {
  Badge,
  Button,
  EmptyState,
  ErrorNotice,
  Modal,
  Mono,
  Notice,
  PageHeader,
  Panel,
  QueryView,
  SelectField,
  Spinner,
} from "../components/ui";
import { basename, formatBytes, formatDateTime, plural } from "../lib/format";
import { usePools, useVhosts } from "../queries";

/** The server refuses larger downloads; the panel says so before asking. */
export const DOWNLOAD_LIMIT = 64 * 1024 * 1024;

interface Location {
  pool: number | null;
  vhost: number | null;
  bucket: string;
  prefix: string;
}

function toNumber(value: string | null): number | null {
  return value && /^\d+$/.test(value) ? Number(value) : null;
}

export function ExplorerPage() {
  const pools = usePools();
  const vhosts = useVhosts();
  const [params, setParams] = useSearchParams();
  const at: Location = {
    pool: toNumber(params.get("pool")),
    vhost: toNumber(params.get("vhost")),
    bucket: params.get("bucket") ?? "",
    prefix: params.get("prefix") ?? "",
  };

  const go = (next: Partial<Location>, replace = false) => {
    const target = { ...at, ...next };
    const query: Record<string, string> = {};
    if (target.pool !== null) query.pool = String(target.pool);
    if (target.vhost !== null) query.vhost = String(target.vhost);
    if (target.bucket) query.bucket = target.bucket;
    if (target.bucket && target.prefix) query.prefix = target.prefix;
    setParams(query, { replace });
  };

  // Seen through a vhost, the routes decide which pool holds a bucket or folder.
  const located = useQuery({
    queryKey: ["explorer", "locate", at.vhost, at.bucket, at.prefix],
    queryFn: () => api.explorer.locate(at.vhost!, at.bucket, at.prefix),
    enabled: at.vhost !== null && at.bucket !== "",
  });

  const nanio = (pools.data ?? []).filter((pool) => pool.type === "nanio");
  const vhost = (vhosts.data ?? []).find((item) => item.id === at.vhost);
  const fallbackPool =
    at.vhost !== null && at.bucket
      ? located.data?.pool.type === "nanio"
        ? located.data.pool.id
        : null
      : (nanio.find((pool) => pool.id === vhost?.default_pool_id)?.id ?? nanio[0]?.id ?? null);

  // No pool in the address yet: open the one the routes (or the vhost, or the first pool) point at.
  useEffect(() => {
    if (at.pool === null && fallbackPool !== null) go({ pool: fallbackPool }, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [at.pool, fallbackPool]);

  /** Move inside a bucket. Seen through a vhost, the pool is worked out again from the routes. */
  const browse = (bucket: string, prefix: string) => go({ bucket, prefix, pool: at.vhost !== null && bucket ? null : at.pool });
  const pool = nanio.find((item) => item.id === at.pool);

  return (
    <>
      <PageHeader
        title="Explorer"
        description={
          <>
            Look inside a bucket and see which pool really holds what. <Badge tone="neutral">Read-only</Badge> Nothing can
            be uploaded, deleted or renamed here; these requests are made by the orchestrator on your behalf and are not
            client traffic.
          </>
        }
      />
      <QueryView
        query={pools}
        isEmpty={() => nanio.length === 0}
        empty={
          <EmptyState title="No nanio pool to browse">
            The explorer talks S3 to a nanio pool with its stored credentials. <Link to="/pools">Create a pool</Link> first.
          </EmptyState>
        }
      >
        {() => (
          <div className="stack-lg">
            <Panel>
              <div className="explorer-controls">
                <SelectField
                  label="Pool"
                  value={at.pool === null ? "" : String(at.pool)}
                  onValue={(value) => go({ pool: Number(value) })}
                  hint="What you see below is what this pool holds."
                >
                  {at.pool === null && <option value="">…</option>}
                  {nanio.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </SelectField>
                <SelectField
                  label="As served by vhost"
                  value={at.vhost === null ? "" : String(at.vhost)}
                  onValue={(value) => go({ vhost: value ? Number(value) : null, pool: value && at.bucket ? null : at.pool })}
                  hint="Follows the vhost's routes: a folder routed to another pool is marked, and opening it switches to that pool."
                >
                  <option value="">None — just this pool</option>
                  {(vhosts.data ?? []).map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.server_name}
                    </option>
                  ))}
                </SelectField>
              </div>
            </Panel>

            {at.vhost !== null && at.bucket && <Served located={located} pool={pool} vhost={vhost} onSwitch={(id) => go({ pool: id })} />}

            {at.pool === null || !pool ? (
              located.isError || located.data ? null : <Spinner />
            ) : at.bucket ? (
              <Objects key={`${pool.id}/${at.bucket}/${at.prefix}`} pool={pool} at={at} browse={browse} />
            ) : (
              <Buckets pool={pool} onOpen={(bucket) => browse(bucket, "")} />
            )}
          </div>
        )}
      </QueryView>
    </>
  );
}

function Served({
  located,
  pool,
  vhost,
  onSwitch,
}: {
  located: UseQueryResult<Located>;
  pool: Pool | undefined;
  vhost: Vhost | undefined;
  onSwitch: (poolId: number) => void;
}) {
  if (located.isError) return <ErrorNotice error={located.error} title="Cannot tell which pool serves this" />;
  if (!located.data) return null;
  const { pool: serving, route, bucket, prefix, split } = located.data;
  const what = (
    <Mono>
      {bucket}/{prefix}
    </Mono>
  );
  const by = route === "/" ? "its default pool" : <>route <Mono>{route}</Mono></>;
  if (serving.type !== "nanio") {
    return (
      <Notice tone="info" title={`${vhost?.server_name ?? "This vhost"} serves this from an http pool`}>
        {what} goes to <Mono>{serving.name}</Mono> ({by}), which has no S3 API to browse.
      </Notice>
    );
  }
  if (split) {
    const looking = !pool
      ? ""
      : pool.id === split.destination.id
        ? `You are looking at ${pool.name}, the destination: what clients get first.`
        : pool.id === split.source.id
          ? `You are looking at ${pool.name}, the source: the fallback copy.`
          : `You are looking at ${pool.name}, which is neither of them: clients do not get this from here.`;
    return (
      <Notice
        tone="warn"
        title={`Being moved from ${split.source.name} to ${split.destination.name} (migration #${split.migration_id})`}
        actions={
          pool &&
          pool.id !== split.destination.id && (
            <Button size="sm" variant="primary" onClick={() => onSwitch(split.destination.id)}>
              Switch to {split.destination.name}
            </Button>
          )
        }
      >
        Split routing is active for {what} on <Mono>{vhost?.server_name}</Mono>: clients read{" "}
        <Mono>{split.destination.name}</Mono> first and fall back to <Mono>{split.source.name}</Mono> for objects not
        copied yet; they write to <Mono>{split.destination.name}</Mono> only. An object can be on either pool. {looking}
      </Notice>
    );
  }
  if (pool && pool.id !== serving.id) {
    return (
      <Notice
        tone="warn"
        title={`Clients do not get this from ${pool.name}`}
        actions={
          <Button size="sm" variant="primary" onClick={() => onSwitch(serving.id)}>
            Switch to {serving.name}
          </Button>
        }
      >
        <Mono>{vhost?.server_name}</Mono> serves {what} from <Mono>{serving.name}</Mono> ({by}). What <Mono>{pool.name}</Mono>{" "}
        holds here is not what clients see: an old copy, or nothing.
      </Notice>
    );
  }
  return (
    <Notice tone="ok" title={`Served by ${serving.name}`}>
      <Mono>{vhost?.server_name}</Mono> serves {what} from <Mono>{serving.name}</Mono> ({by}). This is what clients see.
    </Notice>
  );
}

function Buckets({ pool, onOpen }: { pool: Pool; onOpen: (bucket: string) => void }) {
  const buckets = useQuery({ queryKey: ["explorer", "buckets", pool.id], queryFn: () => api.explorer.buckets(pool.id) });
  return (
    <Panel
      flush
      title={
        <>
          Buckets on <Mono>{pool.name}</Mono>
        </>
      }
      meta={buckets.data && <span>asked member <Mono>{buckets.data.member}</Mono></span>}
    >
      <QueryView query={buckets} isEmpty={(data) => data.buckets.length === 0} empty={<EmptyState title="This pool has no buckets" />}>
        {(data) => (
          <table className="table">
            <thead>
              <tr>
                <th>Bucket</th>
              </tr>
            </thead>
            <tbody>
              {data.buckets.map((bucket) => (
                <tr key={bucket}>
                  <td>
                    <button type="button" className="link-button" onClick={() => onOpen(bucket)}>
                      <Folder className="icon" aria-hidden />
                      <Mono>{bucket}</Mono>
                    </button>
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

function Objects({ pool, at, browse }: { pool: Pool; at: Location; browse: (bucket: string, prefix: string) => void }) {
  const [typed, setTyped] = useState("");
  const [filter, setFilter] = useState("");
  const [details, setDetails] = useState<ExplorerObject | null>(null);
  const listing = useInfiniteQuery({
    queryKey: ["explorer", "objects", pool.id, at.bucket, at.prefix, filter, at.vhost],
    queryFn: ({ pageParam }) =>
      api.explorer.objects(pool.id, at.bucket, { prefix: at.prefix + filter, token: pageParam || undefined, vhost_id: at.vhost ?? undefined }),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_token ?? undefined,
  });

  const segments = at.prefix.split("/").filter(Boolean);
  const pages = listing.data?.pages ?? [];
  // A server may list a folder again on the page after the one that first showed it.
  const folders = [...new Map(pages.flatMap((page) => page.folders).map((folder) => [folder.prefix, folder])).values()];
  const objects = pages.flatMap((page) => page.objects);
  const short = (key: string) => key.slice(at.prefix.length);

  return (
    <Panel
      flush
      title={
        <nav className="breadcrumb" aria-label="Location">
          <button type="button" className="link-button" onClick={() => browse("", "")}>
            <Mono>{pool.name}</Mono>
          </button>
          <ChevronRight className="icon" aria-hidden />
          <button type="button" className="link-button" onClick={() => browse(at.bucket, "")} aria-current={segments.length === 0 ? "page" : undefined}>
            <Mono>{at.bucket}</Mono>
          </button>
          {segments.map((segment, index) => (
            <span key={index} className="breadcrumb-part">
              <ChevronRight className="icon" aria-hidden />
              <button
                type="button"
                className="link-button"
                aria-current={index === segments.length - 1 ? "page" : undefined}
                onClick={() => browse(at.bucket, `${segments.slice(0, index + 1).join("/")}/`)}
              >
                <Mono>{segment}</Mono>
              </button>
            </span>
          ))}
        </nav>
      }
      actions={
        <form
          className="inline-form"
          role="search"
          onSubmit={(event) => {
            event.preventDefault();
            setFilter(typed);
          }}
        >
          <label className="sr-only" htmlFor="explorer-filter">
            Names in this folder starting with
          </label>
          <input
            id="explorer-filter"
            className="input mono"
            placeholder="Starts with…"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
          />
          <Button size="sm" type="submit" icon={<Search className="icon" aria-hidden />}>
            Filter
          </Button>
        </form>
      }
    >
      {listing.isPending ? (
        <Spinner />
      ) : listing.isError && pages.length === 0 ? (
        <ErrorNotice error={listing.error} title={`Could not list this on ${pool.name}`} />
      ) : folders.length === 0 && objects.length === 0 ? (
        <EmptyState title={filter ? `Nothing here starts with “${filter}”` : `${pool.name} holds nothing here`}>
          {!filter && at.vhost !== null && "If a route sends this folder to another pool, the notice above says which one."}
        </EmptyState>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th className="num">Size</th>
                <th>Last modified</th>
              </tr>
            </thead>
            <tbody>
              {folders.map((folder) => (
                <tr key={folder.prefix}>
                  <td>
                    <button type="button" className="link-button" onClick={() => browse(at.bucket, folder.prefix)}>
                      {folder.routed_pool ? <CornerUpRight className="icon" aria-hidden /> : <Folder className="icon" aria-hidden />}
                      <Mono>{short(folder.prefix)}</Mono>
                    </button>{" "}
                    {folder.routed_pool && (
                      <Badge tone="info" title={`A route sends this folder to ${folder.routed_pool.name}: opening it switches to that pool`}>
                        served by {folder.routed_pool.name}
                      </Badge>
                    )}
                  </td>
                  <td className="num muted">—</td>
                  <td className="muted">{folder.routed_pool ? `on ${folder.routed_pool.name}, not on ${pool.name}` : "folder"}</td>
                </tr>
              ))}
              {objects.map((object) => (
                <tr key={object.key}>
                  <td>
                    <button type="button" className="link-button" onClick={() => setDetails(object)}>
                      <File className="icon" aria-hidden />
                      <Mono>{short(object.key)}</Mono>
                    </button>
                  </td>
                  <td className="num">{formatBytes(object.size)}</td>
                  <td>{formatDateTime(object.last_modified)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="pager">
            <span className="muted">
              {plural(folders.length, "folder")}, {plural(objects.length, "object")} shown
              {listing.hasNextPage ? " — there are more" : ""}
            </span>
            {listing.hasNextPage && (
              <Button size="sm" busy={listing.isFetchingNextPage} onClick={() => void listing.fetchNextPage()}>
                Load more
              </Button>
            )}
          </div>
          {listing.isError && <ErrorNotice error={listing.error} title="Could not load more" />}
        </>
      )}
      {details && <ObjectDialog pool={pool} bucket={at.bucket} object={details} onClose={() => setDetails(null)} />}
    </Panel>
  );
}

function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function ObjectDialog({ pool, bucket, object, onClose }: { pool: Pool; bucket: string; object: ExplorerObject; onClose: () => void }) {
  const details = useQuery({
    queryKey: ["explorer", "object", pool.id, bucket, object.key],
    queryFn: () => api.explorer.object(pool.id, bucket, object.key),
    staleTime: 0,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const size = details.data?.size ?? object.size;
  const tooLarge = size > DOWNLOAD_LIMIT;

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      saveBlob(await api.explorer.download(pool.id, bucket, object.key), basename(object.key));
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={basename(object.key)}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Close</Button>
          <Button variant="primary" icon={<Download className="icon" aria-hidden />} busy={busy} disabled={tooLarge} onClick={() => void download()}>
            Download
          </Button>
        </>
      }
    >
      <div className="stack">
        <QueryView query={details}>
          {(data) => (
            <dl className="kv">
              <div>
                <dt>Pool</dt>
                <dd>
                  <Mono>{data.pool.name}</Mono>
                </dd>
              </div>
              <div>
                <dt>Bucket</dt>
                <dd>
                  <Mono>{data.bucket}</Mono>
                </dd>
              </div>
              <div>
                <dt>Key</dt>
                <dd>
                  <Mono>{data.key}</Mono>
                </dd>
              </div>
              <div>
                <dt>Size</dt>
                <dd>
                  {formatBytes(data.size)} {data.size !== null && <span className="muted">({data.size} bytes)</span>}
                </dd>
              </div>
              <div>
                <dt>Last modified</dt>
                <dd>{formatDateTime(data.last_modified)}</dd>
              </div>
              <div>
                <dt>ETag</dt>
                <dd>
                  <Mono>{data.etag ?? "—"}</Mono>
                </dd>
              </div>
              <div>
                <dt>Content type</dt>
                <dd>
                  <Mono>{data.content_type ?? "—"}</Mono>
                </dd>
              </div>
            </dl>
          )}
        </QueryView>
        {tooLarge && (
          <Notice tone="info" title="Too large to download from the panel">
            The panel downloads objects up to {formatBytes(DOWNLOAD_LIMIT)}. Fetch this one with an S3 client through the
            gateway.
          </Notice>
        )}
        <ErrorNotice error={error} title="Could not download" />
        <p className="field-hint">Downloads are recorded in the audit log.</p>
      </div>
    </Modal>
  );
}
