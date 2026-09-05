"""Prometheus scrape endpoint for Kubernetes and monitoring scrapers."""

from fastapi import APIRouter
from app.services.metrics_service import metrics_service

router = APIRouter(tags=["Observability & Metrics"])


@router.get(
    "/metrics",
    summary="Prometheus metrics scrape target",
    include_in_schema=True,
)
async def metrics():
    """Expose application and LLM metrics formatted for Prometheus scraping."""
    return metrics_service.export_metrics()
