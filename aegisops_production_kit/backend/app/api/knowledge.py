"""Knowledge / RAG endpoints — semantic search + document ingestion."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import repositories as repo
from ..db.session import session_scope
from ..rag import ingest, retriever
from ..schemas.auth import User
from ..security.deps import get_current_user, require_initiator
from ..settings import Settings, get_settings

router = APIRouter(tags=["knowledge"])


class IngestRequest(BaseModel):
    title: str
    content: str
    source: str | None = None
    kind: str = "runbook"
    uri: str | None = None


@router.get("/knowledge/search")
async def search(q: str, user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)) -> dict:
    async with session_scope() as s:
        org = await repo.get_default_org(s)
        results = await retriever.retrieve(s, org_id=org.id, query=q, settings=settings, k=8)
    return {"query": q, "results": results}


@router.post("/knowledge/ingest")
async def ingest_doc(body: IngestRequest, user: User = Depends(require_initiator),
                     settings: Settings = Depends(get_settings)) -> dict:
    async with session_scope() as s:
        org = await repo.get_default_org(s)
        doc = await ingest.ingest_document(
            s, org_id=org.id, settings=settings, title=body.title, content=body.content,
            source=body.source, kind=body.kind, uri=body.uri,
        )
        return {"id": str(doc.id), "title": doc.title, "status": "ingested"}
