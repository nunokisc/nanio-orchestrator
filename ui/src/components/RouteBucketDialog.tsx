import { useState } from "react";
import { api, type Pool, type Vhost } from "../api";
import { useAction } from "../queries";
import { OrphanOverride } from "./OrphanOverride";
import { useToast } from "./toast";
import { FormDialog, Mono, Notice, SelectField } from "./ui";

/**
 * Send a whole bucket of an S3 vhost to another pool: with its objects (a migration) or
 * without them (routing only). Routing without moving is refused by the API when the bucket
 * has objects, or when that cannot be told; the refusal is shown and the override offered
 * as its own, separately confirmed step.
 */
export function RouteBucketDialog({
  vhost,
  bucket,
  pools,
  onClose,
}: {
  vhost: Vhost;
  bucket: string;
  pools: Pool[];
  onClose: () => void;
}) {
  const toast = useToast();
  const defaultPool = pools.find((pool) => pool.id === vhost.default_pool_id);
  const targets = pools.filter((pool) => pool.type === "nanio" && pool.id !== vhost.default_pool_id);
  const [poolId, setPoolId] = useState(targets[0] ? String(targets[0].id) : "");
  const [migrate, setMigrate] = useState(true);
  const target = targets.find((pool) => String(pool.id) === poolId);

  const promote = useAction(
    (allowOrphan: boolean) =>
      api.buckets.promote(vhost.id, bucket, { pool_id: Number(poolId), migrate, allow_orphan: allowOrphan || undefined }),
    {
      onSuccess: (result) => {
        if (result.migration_started) {
          toast.success(`Migration #${result.migration_id} started`, `${bucket} is being moved to ${result.pool}. Follow it under Migrations.`);
        } else {
          toast.success(`Route ${result.route} written`, `It points at ${result.pool}. Reload nginx to apply it.`);
        }
        onClose();
      },
    },
  );

  return (
    <FormDialog
      title={`Route bucket ${bucket} to a pool`}
      submitLabel={migrate ? "Route and move the objects" : "Route only"}
      busy={promote.isPending && !promote.variables}
      error={promote.error}
      submitDisabled={!target}
      onClose={onClose}
      onSubmit={() => promote.mutate(false)}
      afterError={
        !migrate && (
          <OrphanOverride
            key={promote.error?.detail}
            error={promote.error}
            consequence={`The objects of ${bucket} stay on ${defaultPool?.name ?? "the default pool"} and will not be reachable through ${vhost.server_name} once nginx is reloaded.`}
            actionLabel={`Route ${bucket} to ${target?.name ?? "the pool"} anyway`}
            busy={promote.isPending && promote.variables === true}
            onOverride={() => promote.mutate(true)}
          />
        )
      }
    >
      <p className="muted">
        Adds <Mono>location /{bucket}/</Mono> to <Mono>{vhost.server_name}</Mono>. Today the bucket is served by the
        default pool{defaultPool && <> (<Mono>{defaultPool.name}</Mono>)</>}.
      </p>
      {targets.length === 0 ? (
        <Notice tone="warn" title="There is no other nanio pool">
          Create a second nanio pool first: a bucket can only be routed to a pool other than the default one.
        </Notice>
      ) : (
        <SelectField label="Target pool" value={poolId} onValue={setPoolId}>
          {targets.map((pool) => (
            <option key={pool.id} value={pool.id}>
              {pool.name}
            </option>
          ))}
        </SelectField>
      )}
      <fieldset className="choices">
        <legend>What happens to the objects</legend>
        <label className="choice">
          <input type="radio" name="route-mode" checked={migrate} onChange={() => setMigrate(true)} />
          <span>
            <strong>Move them (live migration)</strong>
            <span className="field-hint">
              rclone copies the bucket to the target pool while clients keep reading and writing it; the route switches
              when the copy is verified. Needs credentials on both pools. The old copy is kept and tracked.
            </span>
          </span>
        </label>
        <label className="choice">
          <input type="radio" name="route-mode" checked={!migrate} onChange={() => setMigrate(false)} />
          <span>
            <strong>Route only</strong>
            <span className="field-hint">
              For a new, empty bucket: its owner then creates it on the target pool through the gateway. Refused if the
              bucket already has objects, because they would stay behind.
            </span>
          </span>
        </label>
      </fieldset>
    </FormDialog>
  );
}
