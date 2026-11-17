import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, useState } from "react";
import { api, type AuditEntry } from "../api";
import { Badge, Button, EmptyState, Mono, PageHeader, Panel, QueryView, SelectField, TextField } from "../components/ui";
import { formatDateTime } from "../lib/format";

const ENTITY_TYPES = ["pool", "pool_member", "vhost", "route", "bucket", "migration", "config", "object"];
const PER_PAGE = 50;

function parse(json: string | null): Record<string, unknown> | null {
  if (!json) return null;
  try {
    const value: unknown = JSON.parse(json);
    return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : { value };
  } catch {
    return { value: json };
  }
}

function show(value: unknown): string {
  if (value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** Before and after, field by field; the fields that changed stand out. */
export function ChangeTable({ before, after }: { before: Record<string, unknown> | null; after: Record<string, unknown> | null }) {
  const fields = [...new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})])];
  if (fields.length === 0) return <p className="muted">No values were recorded for this entry.</p>;
  return (
    <table className="table table-compact change-table">
      <thead>
        <tr>
          <th>Field</th>
          {before && <th>Before</th>}
          {after && <th>After</th>}
        </tr>
      </thead>
      <tbody>
        {fields.map((field) => {
          const changed = before !== null && after !== null && show(before[field]) !== show(after[field]);
          return (
            <tr key={field} className={changed ? "changed" : undefined}>
              <td>
                <Mono>{field}</Mono>
              </td>
              {before && (
                <td>
                  <Mono>{show(before[field])}</Mono>
                </td>
              )}
              {after && (
                <td>
                  <Mono>{show(after[field])}</Mono>
                </td>
              )}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function AuditPage() {
  const [entityType, setEntityType] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<number | null>(null);

  const entries = useQuery({
    queryKey: ["audit", entityType, from, to, page],
    queryFn: () =>
      api.audit({
        page,
        per_page: PER_PAGE,
        entity_type: entityType || undefined,
        from: from || undefined,
        to: to ? `${to} 23:59:59` : undefined,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: page === 1 ? 15_000 : false,
  });
  const filter = (apply: () => void) => {
    apply();
    setPage(1);
    setOpen(null);
  };

  return (
    <>
      <PageHeader
        title="Audit log"
        description="Every change, newest first, with the values before and after and what nginx answered. It lives only in the database: a rebuild from disk does not recover it."
      />
      <Panel flush>
        <div className="filter-form">
          <SelectField label="Entity" value={entityType} onValue={(value) => filter(() => setEntityType(value))}>
            <option value="">All</option>
            {ENTITY_TYPES.map((type) => (
              <option key={type} value={type}>
                {type.replace("_", " ")}
              </option>
            ))}
          </SelectField>
          <TextField label="From" type="date" value={from} onValue={(value) => filter(() => setFrom(value))} />
          <TextField label="To" type="date" value={to} onValue={(value) => filter(() => setTo(value))} />
          {(entityType || from || to) && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                filter(() => {
                  setEntityType("");
                  setFrom("");
                  setTo("");
                })
              }
            >
              Clear filters
            </Button>
          )}
        </div>
        <QueryView
          query={entries}
          isEmpty={(list) => list.length === 0}
          empty={
            <EmptyState title={page > 1 ? "No more entries" : entityType || from || to ? "No entries match these filters" : "Nothing has been changed yet"} />
          }
        >
          {(list) => (
            <table className="table">
              <thead>
                <tr>
                  <th aria-label="Details" />
                  <th>Time</th>
                  <th>Action</th>
                  <th>Entity</th>
                  <th>nginx</th>
                </tr>
              </thead>
              <tbody>
                {list.map((entry) => (
                  <AuditRow key={entry.id} entry={entry} open={open === entry.id} onToggle={() => setOpen(open === entry.id ? null : entry.id)} />
                ))}
              </tbody>
            </table>
          )}
        </QueryView>
        <div className="pager">
          <Button size="sm" disabled={page === 1} onClick={() => setPage(page - 1)}>
            Newer
          </Button>
          <span className="muted">Page {page}</span>
          <Button size="sm" disabled={(entries.data?.length ?? 0) < PER_PAGE} onClick={() => setPage(page + 1)}>
            Older
          </Button>
        </div>
      </Panel>
    </>
  );
}

function AuditRow({ entry, open, onToggle }: { entry: AuditEntry; open: boolean; onToggle: () => void }) {
  return (
    <Fragment>
      <tr className={open ? "row-open" : undefined}>
        <td className="cell-toggle">
          <button type="button" className="btn btn-ghost btn-icon" aria-expanded={open} aria-label={`Details of entry ${entry.id}`} onClick={onToggle}>
            {open ? <ChevronDown className="icon" aria-hidden /> : <ChevronRight className="icon" aria-hidden />}
          </button>
        </td>
        <td>{formatDateTime(entry.created_at)}</td>
        <td>
          <Mono>{entry.action}</Mono>
        </td>
        <td>
          {entry.entity_type.replace("_", " ")}
          {entry.entity_id !== null && <span className="muted"> #{entry.entity_id}</span>}
        </td>
        <td>
          {entry.nginx_reload_ok === null ? (
            <span className="muted">—</span>
          ) : entry.nginx_reload_ok ? (
            <Badge tone="ok">accepted</Badge>
          ) : (
            <Badge tone="bad">refused</Badge>
          )}
        </td>
      </tr>
      {open && (
        <tr className="row-detail">
          <td />
          <td colSpan={4}>
            <div className="stack">
              <ChangeTable before={parse(entry.before_json)} after={parse(entry.after_json)} />
              {entry.nginx_reload_output && (
                <div>
                  <h4>nginx output</h4>
                  <pre className="output">{entry.nginx_reload_output}</pre>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  );
}
