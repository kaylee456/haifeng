"""Basic tests for pdf_export_service module."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_export_full_pdf_success():
    r = client.get("/api/demo/export-full-pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert len(r.content) > 1000


def test_export_nonexistent_project():
    r = client.get("/api/nonexistent/export-full-pdf")
    assert r.status_code == 404


def test_natural_sort_key():
    from pdf_export_service import natural_sort_key
    files = ["Chapter10_result.html", "Chapter2_result.html", "Chapter1_result.html"]
    assert sorted(files, key=natural_sort_key) == [
        "Chapter1_result.html",
        "Chapter2_result.html",
        "Chapter10_result.html",
    ]


def test_extract_body_content():
    from pdf_export_service import _extract_body_content
    html = "<html><head></head><body><p>hello</p></body></html>"
    assert _extract_body_content(html) == "<p>hello</p>"


def test_top_chapter_key():
    from pdf_export_service import _top_chapter_key
    assert _top_chapter_key("Chapter10-2-1_result.html") == "Chapter10"
    assert _top_chapter_key("Chapter3_result.html") == "Chapter3"


def test_determine_heading_level():
    from pdf_export_service import _determine_heading_level
    assert _determine_heading_level("Chapter10_result.html") == 1
    assert _determine_heading_level("Chapter10-2_result.html") == 2
    assert _determine_heading_level("Chapter10-2-1_result.html") == 3


def test_build_section_title():
    from pdf_export_service import _build_section_title
    assert _build_section_title("Chapter1_result.html", "<h1>工程概况</h1>") == "工程概况"
    assert _build_section_title("Chapter5_result.html", "<p>no heading</p>") == "第5章"
