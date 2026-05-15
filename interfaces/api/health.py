"""Liveness/readiness endpoints."""
from __future__ import annotations

from fastapi import APIRouter


def build_health_router() -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return router
