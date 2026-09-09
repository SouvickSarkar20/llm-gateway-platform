"""Async Jobs API endpoint for status polling and SSE streaming."""

import asyncio
import json
import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.models.user import User
from app.api.deps import require_user_or_admin
from app.services.job_service import job_service

logger = logging.getLogger("chat_platform.jobs_api")

router = APIRouter(prefix="/jobs", tags=["Async LLM Jobs & SSE"])


@router.get(
    "/{job_id}",
    summary="Poll status and results for asynchronous LLM job",
    status_code=status.HTTP_200_OK,
)
async def get_job_status(
    job_id: str,
    current_user: User = Depends(require_user_or_admin),
) -> Dict[str, Any]:
    """Retrieve current state, progress, and result of an asynchronous LLM job."""
    job_data = await job_service.get_job(job_id)
    if not job_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    # Basic authorization check (ensure job belongs to current user or user is admin)
    user_tenant_id = getattr(current_user, "tenant_id", None)
    user_role = getattr(current_user, "role", None)
    role_str = user_role.value if hasattr(user_role, "value") else str(user_role)
    if job_data.get("tenant_id") and user_tenant_id and job_data["tenant_id"] != user_tenant_id and role_str != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Job belongs to another tenant.",
        )

    return {
        "job_id": job_data["job_id"],
        "status": job_data["status"],
        "tenant_id": job_data.get("tenant_id"),
        "tier": job_data.get("tier"),
        "model": job_data.get("model"),
        "created_at": job_data.get("created_at"),
        "updated_at": job_data.get("updated_at"),
        "result": job_data.get("result"),
        "error": job_data.get("error"),
    }


@router.get(
    "/{job_id}/stream",
    summary="Server-Sent Events (SSE) stream for real-time job progress",
)
async def stream_job_updates(
    job_id: str,
    request: Request,
    current_user: User = Depends(require_user_or_admin),
) -> StreamingResponse:
    """Establish SSE connection streaming job execution state transitions in real time."""
    job_data = await job_service.get_job(job_id)
    if not job_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    async def event_generator():
        last_status = None
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                logger.info("Client disconnected from SSE stream for job %s", job_id)
                break

            current_job = await job_service.get_job(job_id)
            if not current_job:
                yield f"data: {json.dumps({'error': 'Job disappeared'})}\n\n"
                break

            current_status = current_job.get("status")
            if current_status != last_status:
                last_status = current_status
                payload = {
                    "job_id": job_id,
                    "status": current_status,
                    "result": current_job.get("result"),
                    "error": current_job.get("error"),
                    "updated_at": current_job.get("updated_at"),
                }
                yield f"event: status_update\ndata: {json.dumps(payload)}\n\n"

            if current_status in ("completed", "failed"):
                yield f"event: done\ndata: {json.dumps({'job_id': job_id, 'status': current_status})}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
