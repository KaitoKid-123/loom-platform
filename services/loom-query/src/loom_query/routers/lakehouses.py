"""`GET /api/v1/lakehouses/{lakehouse_id}/schema` — xem module docstring
`loom_query.lakehouse_schema` cho cổng quyền và quyết định `?depth=`.

Đứng sau `security.require_shared_secret` (`dependencies=` ở cấp `APIRouter`,
cùng khuôn `routers/query.py`) — 401 trước khi chạm bất kỳ dòng nào ở đây nếu
thiếu/sai header bí mật chia sẻ với `loom-api`.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, Request

from loom_query.authz import AuthzPort
from loom_query.config import Settings
from loom_query.lakehouse_schema import Depth, build_schema_tree, run_schema_gate
from loom_query.schemas import LakehouseSchemaOut, SchemaRequest
from loom_query.security import require_shared_secret

router = APIRouter(tags=["lakehouses"], dependencies=[Depends(require_shared_secret)])


@router.get(
    "/lakehouses/{lakehouse_id}/schema",
    response_model=LakehouseSchemaOut,
    response_model_exclude_none=True,
)
async def lakehouse_schema(
    lakehouse_id: uuid.UUID,
    body: SchemaRequest,
    request: Request,
    depth: Depth = "tables",
) -> LakehouseSchemaOut:
    authz: AuthzPort = request.app.state.authz
    await run_schema_gate(lakehouse_id=lakehouse_id, principal=body.principal, authz=authz)

    settings: Settings = request.app.state.settings
    # `asyncio.to_thread`: `build_schema_tree` gọi PyIceberg (đồng bộ) — cùng
    # lý do `runner._run_sync` chạy trong thread riêng, xem docstring ở đó.
    return await asyncio.to_thread(build_schema_tree, lakehouse_id, settings=settings, depth=depth)
