"""Shared by the API modules."""

from __future__ import annotations

from fastapi import HTTPException


async def reject_if_refused(db, ok: bool, output: str) -> None:
    """Undo a change that nginx refused, and tell the caller.

    Endpoints make their changes on ``db`` without committing, generate the config from that
    uncommitted state and let nginx test it. Accepted: the write helper commits. Refused: the
    previous file is already back in place, and this rolls the database back too, so the
    database, the files and nginx keep describing the same thing.
    """
    if ok:
        return
    await db.rollback()
    raise HTTPException(422, f"nginx rejected this change, so nothing was saved. nginx said: {output}")
