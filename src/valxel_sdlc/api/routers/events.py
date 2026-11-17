"""Live updates over server-sent events."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...web.auth import (
    User,
)



def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    eng = ctx.eng
    current_user = ctx.current_user

    # --- live updates ---
    @api.get("/events")
    async def events(request: Request, user: User = Depends(current_user)) -> StreamingResponse:
        q = eng().bus.subscribe()

        async def stream():
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                eng().bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return api
