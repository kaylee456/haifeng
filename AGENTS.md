# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This repository contains the **AIHaiFeng PDF Export Service** — a FastAPI module that generates professional PDF reports (with cover page, table of contents, and merged chapter content) from pre-generated HTML chapter files using `wkhtmltopdf`.

### System dependencies

- **Python 3.10+** (uses `str | None` union syntax)
- **wkhtmltopdf** system binary — install via `sudo apt-get install -y wkhtmltopdf`
- The binary is found at `/usr/bin/wkhtmltopdf` or via `$WKHTMLTOPDF_BIN` env var

### Running the dev server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The `main.py` entry point provides a stub `project_manager` that resolves project IDs to directories under `test_data/<project_id>/`.

### Testing

```bash
pytest test_pdf_export.py -v
```

Tests use `fastapi.testclient.TestClient` and require `wkhtmltopdf` to be installed.

### Linting

```bash
ruff check *.py
```

### Key gotchas

- **Cover images required**: The export endpoint requires cover images at `/opt/AIHaiFeng_task6/develop/agent/face/封面大图.png` and `封面角标.png`. These must be placed there manually for local dev (the setup process creates placeholder PNGs).
- **Not a standalone app**: `pdf_export_service.py` is designed as a plugin module. The `main.py` file provides a development harness with a stub `project_manager`.
- **Test data**: Sample chapter HTML files live in `test_data/demo/`. The export endpoint can be tested at `GET /api/demo/export-full-pdf`.
- **wkhtmltopdf uses Qt WebKit**: CSS support is limited (e.g., `display: table-cell` doesn't work). The codebase works around this with real `<table>` tags for TOC layout.
- **Formatting**: `ruff format` flags style issues in the existing `pdf_export_service.py`; this is pre-existing and not blocking.
