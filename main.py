"""
Development entry point for the PDF export FastAPI service.

Mounts the pdf_export_service router under /api and provides
a stub project_manager for local testing with sample chapter data.
"""

import os

from fastapi import FastAPI

import pdf_export_service

app = FastAPI(title="AIHaiFeng PDF Export Service")


class _StubProjectManager:
    """Minimal project_manager for local development / testing."""

    def get_project_output_dir(self, project_id: str) -> str | None:
        base = os.path.join(os.path.dirname(__file__), "test_data", project_id)
        return base if os.path.isdir(base) else None


pdf_export_service.project_manager = _StubProjectManager()

app.include_router(pdf_export_service.router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
