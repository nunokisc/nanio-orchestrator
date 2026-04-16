"""Audit log API."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Query

from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import AuditEntry

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=List[AuditEntry])
async def list_audit(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    conditions = []
    params = []

    if entity_type:
        conditions.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        conditions.append("entity_id = ?")
        params.append(entity_id)
    if from_date:
        conditions.append("created_at >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("created_at <= ?")
        params.append(to_date)

    where = " AND ".join(conditions) if conditions else "1=1"
    offset = (page - 1) * per_page

    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            f"SELECT * FROM audit_log WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        )
        return [dict(r) for r in rows]
