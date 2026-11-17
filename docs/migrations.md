# Migrations

A migration moves one bucket, or one folder of a bucket, from one `nanio` pool to another while
clients keep using it.
The copy is done by [rclone](https://rclone.org), talking directly to the storage nodes; the
orchestrator only drives rclone and changes the nginx routing at the right moments.

Requirements:

- `rclone` installed on the orchestrator machine (`NANIO_ORCHESTRATOR_RCLONE_PATH`)
- credentials stored for both pools
- both pools have at least one enabled member reachable from the orchestrator
- the storage nodes have synchronised clocks (NTP)
- the bucket has a route on the vhost (if it is still served by the default pool, route it and
  move it in one step: **Buckets** → *Route to a pool* → *Move them*)

---

## Starting one

Web panel: **Migrations** → *Start migration* → *A whole bucket* → the source pool, the bucket
and the destination pool. Or **Buckets** → *Move* on a routed bucket, which opens the same form
filled in.

```bash
curl -s -X POST $ORCH/api/migrations -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"bucket": "photos", "src_pool_id": 1, "dst_pool_id": 2}'
```

Follow it on the **Migrations** screen (the phases, the progress, whether split routing is
active, and *Log*), or with `GET /api/migrations/{id}` and `GET /api/migrations/{id}/log`.

To route a bucket and move it in one step, use promote with `"migrate": true`
(see [operations.md](operations.md)).

### Moving one folder of a bucket

Add `folder`. The source pool is worked out for you: it is the pool the folder lives on now.

```bash
curl -s -X POST $ORCH/api/migrations -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"bucket": "photos", "folder": "2025/", "dst_pool_id": 2}'
```

Web panel: **Migrations** → *Start migration* → *One folder of a bucket*. A folder that already
has its own route also has *Move* next to it on the **Vhosts** screen.

| The folder is… | Result |
|----------------|--------|
| Still part of its bucket | It is moved to the destination pool and gets its own route `/photos/2025/`. |
| Already routed to a pool of its own | It is moved to the destination pool; its route follows. |
| Moved back to the pool that holds the rest of the bucket | Its route is removed: the folder is part of the bucket again. |

Everything on this page applies to a folder exactly as to a bucket: the same phases, the same
split routing (for the folder only; the rest of the bucket is not touched), the same recovery.
Two rules are specific:

- **One move at a time per bucket.** While a bucket or any of its folders is being moved, or has
  a split left active by a failed move, another move of that bucket is refused (HTTP 409).
- `http` vhosts that serve the bucket get the folder's route and follow the move, provided an
  `http` pool linked to the destination pool exists.

What a folder on another pool means for clients is described in
[operations.md](operations.md#send-one-folder-of-a-bucket-to-another-pool).

---

## What happens, phase by phase

```
pending → copying → write_routing → verifying → switching → done
```

| Phase | Routing | What the engine does |
|-------|---------|----------------------|
| `copying` | The source serves everything | Creates the destination bucket if needed. Runs `rclone copy` in passes until the object counts match or the source stops growing. |
| `write_routing` | **Split**: the destination is authoritative | Reloads nginx with split routing (below). From here on the source receives no new data. |
| `verifying` | Split | Each pass runs `rclone copy --update`, then a one-way `rclone check`. Repeats while the number of unresolved objects goes down. |
| `switching` | The destination serves everything | Points the route at the destination, removes the split, reloads nginx. |
| `done` | Destination | Records the source copy as orphaned. Nothing is deleted. |

Refusals before anything is copied: source and destination are the same pool, the source bucket
(or folder) is empty, the bucket is marked deleted, another move of the same bucket is in
progress.

### Split routing

While the split is active nginx handles the bucket like this:

| Request | Goes to |
|---------|---------|
| Writes (`PUT`, `POST`) | The destination only |
| Reads (`GET`, `HEAD`) | The destination; if it answers 404, the source |
| `DELETE` | The destination, and mirrored to the source |
| Multi-object delete (`POST /bucket?delete`) | The destination, and mirrored to the source |
| Listings | The destination |

So during the migration a client always reads its own latest write, an object that has not been
copied yet is still served from the source, and a deleted object stays deleted.

The split is always used, even when the copy has already caught up: switching directly would
lose an object uploaded between the last check and the switch.

### Why `--update` and a one-way check

Once the split is active the destination holds objects written by clients. Two rules protect
them:

- `rclone copy --update` never overwrites a destination object that is newer than its source.
- The check only requires that every source object is present at the destination. An object that
  differs is accepted when the destination copy is newer (a client overwrote it); a missing or
  older object fails the pass.

### Limits to be aware of

| Situation | Effect |
|-----------|--------|
| Listing the bucket during the split | Shows what is already at the destination. An object not copied yet is readable by its key but missing from listings until the next verification pass copies it. |
| Multipart upload started before the split | Fails when the split begins; the client has to retry the upload. This is accepted: the engine does not wait for uploads in progress. |
| Clocks of the storage nodes out of sync | "Newer" is decided by `Last-Modified`. Keep NTP running on every node. |
| `http` vhosts linked to the source pool | Follow the migration, split included, provided an `http` pool linked to the destination pool exists. Without one they keep reading from the source until you repoint them. |

The listing and multipart rows cannot be removed without a component that processes S3
traffic. [clients.md](clients.md) describes them from the application's side.

---

## When it does not finish

A migration ends in `error` or `cancelled` when rclone fails, verification does not converge,
nginx rejects a config, the orchestrator restarts in the middle of a routing change, or an
operator cancels it. The log (`GET /api/migrations/{id}/log`) says which.

What matters is whether the split had already started. Check `split_routing` on the migration:

| `split_routing` | State of the bucket | What to do |
|-----------------|---------------------|------------|
| `false` | Still served entirely by the source. Nothing changed for clients. | Fix the cause and start the migration again. |
| `true` | Split routing is **still active**. Clients keep working: they write to the destination and read from it with the source as fallback. | Fix the cause and start a new migration **between the same two pools**. It takes over the split and finishes the move. |

The split is kept on purpose. The destination already holds objects that exist nowhere else;
routing the bucket back to the source alone would make them disappear.

In the panel such a migration is listed first on the **Migrations** screen (*Interrupted, with
split routing still active*), on the Overview and in the status bar, with the reason it stopped
and *Start the same move again*, which starts that new migration. Finished migrations without a
split have *Start again* in the history.

While a split is active for a bucket:

- a migration to any other pool is refused (HTTP 409),
- editing the vhost or regenerating its config keeps the split,
- a database rebuild from disk restores it.

### Restart of the orchestrator during a migration

| Phase at restart | On startup |
|------------------|-----------|
| `pending`, `copying`, `verifying` | Restarted automatically from the copy phase. rclone only transfers what is missing. |
| `write_routing`, `switching` | Marked `error`. Review the log, then start the migration again as described above. |

### Cancelling

*Cancel* on the Migrations screen, or `POST /api/migrations/{id}/cancel`, stops rclone and marks the migration `cancelled`. The same
table applies: if the split had started it stays active until a new migration between the same
pools completes.

---

## After `done`

The source bucket is left untouched. It is listed as orphaned:

```bash
curl -s $ORCH/api/migrations/orphaned -H "X-Orchestrator-Key: $KEY"
nanio-orchestrator orphaned list
```

Delete it yourself once you have verified the bucket on its new pool. The orchestrator never
deletes source data on its own. The Migrations screen lists these old copies with *Browse the
old copy*, which opens the Explorer on the source pool.

---

## Tuning

| Setting | Default | Effect |
|---------|---------|--------|
| `MIGRATION_MAX_PARALLEL` | `2` | Migrations running at the same time |
| `MIGRATION_TRANSFERS` | `4` | rclone `--transfers` (parallel object copies) |
| `MIGRATION_CHECKERS` | `8` | rclone `--checkers` |
| `MIGRATION_BANDWIDTH_LIMIT` | *(none)* | rclone `--bwlimit`, e.g. `50M` |
| `MIGRATION_MAX_COPY_PASSES` | `10` | Maximum passes in `copying`, and again in `verifying` |
| `S3_REQUEST_TIMEOUT` | `3600` | Socket timeout of the orchestrator's own S3 calls |

For a faster migration raise `MIGRATION_TRANSFERS`; to protect production traffic set
`MIGRATION_BANDWIDTH_LIMIT`. Settings are read at startup: restart the service after changing
them.
