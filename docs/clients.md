# For applications using the gateway

What an S3 client needs to know when it talks to a gateway managed by nanio-orchestrator,
compared with a single nanio instance or a conventional S3 service.

The gateway is nginx. It forwards each request, unchanged, to the pool that holds the bucket
(or the folder) named in the request path. Nothing reads, merges or rewrites S3 traffic, so a
request that needs data from two pools at once cannot be answered. Every limit below comes from
that one fact.

---

## Client settings

| Setting | Value | Why |
|---------|-------|-----|
| Addressing | **Path style** (`https://host/bucket/key`), not `bucket.host` | Requests are routed by their path. |
| Signature | SigV4 | The only scheme nanio accepts. |
| Credentials | One set, valid on every pool behind the hostname | nanio verifies the signature itself; the gateway cannot re-sign. |

---

## What works as on a single server

Reading, writing, deleting and listing objects, multipart uploads, multi-object delete,
creating and deleting buckets, and `ListBuckets`, for buckets and folders on any pool.

- A new bucket is created on the default pool. An operator can route it elsewhere before data is
  written, or move it later without downtime.
- A bucket that lives on another pool appears in `ListBuckets` a few minutes after it is created
  (the orchestrator lists it at its next bucket sync, every 5 minutes by default).

---

## Limits that cannot be removed

These need a component that processes S3 traffic. nanio-orchestrator is deliberately not one.

| Operation | Behaviour | What to do in the client |
|-----------|-----------|--------------------------|
| `CopyObject` (server-side copy) between two buckets on different pools, or between a folder routed to another pool and the rest of its bucket | Fails with `404 NoSuchKey`: the source is on other machines. | Copy through the client: download and upload. Many tools have a switch for it (rclone: `--disable copy`). Copies within the same pool work. |
| Listing a whole bucket recursively (no delimiter, from the root) when one of its folders is routed to another pool | The folder's objects are missing from the result. | List folder by folder (with a delimiter), or start the recursive listing inside the folder. Both return everything. |
| Listing a bucket while it is being migrated | Shows what is already on the destination pool. An object not copied yet can be read by its key but is missing from listings for a short time. | Nothing; it resolves as the migration proceeds. Avoid treating a listing as an inventory during a migration. |
| A multipart upload that was started before a migration switched the bucket to split routing | Fails; the parts are on the old pool. | Retry the upload. Clients with automatic retry recover by themselves. |
| Different credentials per pool | Not possible behind one hostname. | Use the same access key and secret key on every pool. |

Whether these matter depends on the layout. With whole buckets routed to pools and no copies
between buckets, an application only meets the migration rows, and only while a migration runs.
The first two rows apply once a bucket is split by folder or data is copied between buckets on
different pools.

---

## Things that differ from a conventional S3 service

These come from nanio, a minimal S3 server, not from the gateway:

- TLS is terminated by nginx; nanio itself speaks plain HTTP on the private network.
- If the application relies on an S3 feature beyond objects, buckets and multipart uploads
  (versioning, lifecycle rules, bucket policies, and so on), test it against nanio first.

---

## Plain HTTP access (`http` pools)

A hostname backed by an `http` pool serves the same files read-only over plain HTTP, like a CDN
origin: `GET` and `HEAD` only, **no authentication**, no S3 API. Access is restricted, if at
all, by the IP rules of the vhost.
