# Migrations

A migration moves one bucket from one `nanio` pool to another while clients keep using it.
The copy is done by [rclone](https://rclone.org), talking directly to the storage nodes; the
orchestrator only drives rclone and changes the nginx routing at the right moments.

Requirements:

- `rclone` installed on the orchestrator machine (`NANIO_ORCHESTRATOR_RCLONE_PATH`)
- credentials stored for both pools
- both pools have at least one enabled member reachable from the orchestrator
- the storage nodes have synchronised clocks (NTP)
- the bucket has a route on the vhost (promote it first if it is still served by the default pool)

---

## Starting one

Web UI: **Migrations** → choose the source pool, the bucket and the destination pool.

```bash
curl -s -X POST $ORCH/api/migrations -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"bucket": "photos", "src_pool_id": 1, "dst_pool_id": 2}'
```

Follow it with `GET /api/migrations/{id}` and `GET /api/migrations/{id}/log`.

To route a bucket and move it in one step, use promote with `"migrate": true`
(see [operations.md](operations.md)).

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
is empty, the bucket is marked deleted.

### Split routing

While the split is active nginx handles the bucket like this:

| Request | Goes to |
|---------|---------|
| Writes (`PUT`, `POST`) | The destination only |
| Reads (`GET`, `HEAD`) | The destination; if it answers 404, the source |
| `DELETE` | The destination, and mirrored to the source |

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
| Multi-object delete (`POST /bucket?delete`) during the split | Not mirrored to the source. Those objects can reappear until the migration finishes. Single `DELETE` requests are not affected. |
| Multipart upload started before the split | Fails when the split begins; the client has to retry the upload. |
| Clocks of the storage nodes out of sync | "Newer" is decided by `Last-Modified`. Keep NTP running on every node. |
| `http` vhosts linked to the source pool | Their route follows the migration, but their split is applied only by the migration itself. |

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

`POST /api/migrations/{id}/cancel` stops rclone and marks the migration `cancelled`. The same
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
deletes source data on its own.

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
