"""Centralised audit logging: DB write + structured log file entry.

Every write operation on the platform calls log_audit(), which:
  1. Inserts a row into the audit_log table (durable, queryable via API).
  2. Emits a structured INFO line to the "nanio.audit" logger so the event
     appears in the application log file alongside the rest of the app logs.

Format in the log file (example):
  2026-04-28T10:00:00 INFO  nanio.audit: action=create entity=pool id=5 ...
  2026-04-28T10:00:01 INFO  nanio.audit: action=set_credentials entity=pool id=5
  2026-04-28T10:00:02 INFO  nanio.audit: action=purge_orphan entity=bucket after={...}
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

audit_logger = logging.getLogger("nanio.audit")


async def log_audit(
    db,
    action: str,
    entity_type: str,
    entity_id: Optional[int],
    *,
    before: Any = None,
    after: Any = None,
    reload_ok: Optional[bool] = None,
    reload_output: Optional[str] = None,
) -> None:
    """Write one audit entry to the DB table and to the structured audit log."""
    before_json = json.dumps(before) if before is not None else None
    after_json = json.dumps(after) if after is not None else None
    reload_int = 1 if reload_ok is True else (0 if reload_ok is False else None)

    await db.execute(
        """INSERT INTO audit_log
             (action, entity_type, entity_id, before_json, after_json,
              nginx_reload_ok, nginx_reload_output)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (action, entity_type, entity_id, before_json, after_json, reload_int, reload_output),
    )

    # Structured line for the log file — key=value pairs, compact JSON for data fields
    parts = [f"action={action}", f"entity={entity_type}"]
    if entity_id is not None:
        parts.append(f"id={entity_id}")
    if reload_ok is not None:
        parts.append(f"nginx_ok={reload_ok}")
    if after is not None:
        s = json.dumps(after, separators=(",", ":"))
        parts.append("after=" + (s[:300] + "..." if len(s) > 300 else s))
    elif before is not None:
        s = json.dumps(before, separators=(",", ":"))
        parts.append("before=" + (s[:300] + "..." if len(s) > 300 else s))
    audit_logger.info(" ".join(parts))
