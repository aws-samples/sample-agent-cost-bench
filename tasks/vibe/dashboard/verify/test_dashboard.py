"""
Harness tests for task-003-dashboard.

Checks: files exist · FastAPI CRUD API (TestClient) · GET / returns HTML
(live uvicorn) · index.html UI wiring (static analysis).

WORKSPACE is the model's output dir; everything that needs the model's code
imports through importlib (PYTHONPATH=workspace is set by the harness).
"""

from __future__ import annotations

import importlib
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

WS = Path(os.environ.get("WORKSPACE", "."))
EXCLUDE = {".venv-verify", ".venv", "venv", "__pycache__", "site-packages"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_html() -> Path | None:
    for p in (WS / "index.html", WS / "static" / "index.html", WS / "templates" / "index.html"):
        if p.exists():
            return p
    for p in WS.rglob("*.html"):
        if not any(part in EXCLUDE for part in p.parts) and p.stat().st_size > 50:
            return p
    return None


def _free_port() -> int:
    """Ask the OS for an available ephemeral port — no clashes in parallel runs."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_client():
    mod = importlib.import_module("main")
    from starlette.testclient import TestClient
    return TestClient(mod.app)


@pytest.fixture(scope="module")
def html() -> str:
    p = _find_html()
    if p is None:
        pytest.skip("index.html not found")
    return p.read_text(encoding="utf-8", errors="replace").lower()


# ---------------------------------------------------------------------------
# 1. Files exist
# ---------------------------------------------------------------------------

def test_main_py_exists():
    assert (WS / "main.py").exists(), "main.py not produced"


def test_index_html_exists():
    assert _find_html() is not None, "index.html not found"


# ---------------------------------------------------------------------------
# 2. CRUD API via TestClient
# ---------------------------------------------------------------------------

def test_list_initially_empty(app_client):
    r = app_client.get("/api/todos")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_create_todo(app_client):
    r = app_client.post("/api/todos", json={"title": "hello"})
    assert r.status_code in (200, 201)
    body = r.json()
    assert body["title"] == "hello" and body["done"] is False


def test_update_todo(app_client):
    tid = app_client.post("/api/todos", json={"title": "upd"}).json()["id"]
    r = app_client.put(f"/api/todos/{tid}", json={"title": "upd", "done": True})
    assert r.status_code == 200


def test_put_missing_returns_404(app_client):
    assert app_client.put("/api/todos/999999", json={"title": "x", "done": False}).status_code == 404


def test_delete_todo(app_client):
    tid = app_client.post("/api/todos", json={"title": "del"}).json()["id"]
    assert app_client.delete(f"/api/todos/{tid}").status_code in (200, 204)


def test_delete_missing_returns_404(app_client):
    assert app_client.delete("/api/todos/999999").status_code == 404


def test_deleted_item_gone_from_list(app_client):
    tid = app_client.post("/api/todos", json={"title": "gone"}).json()["id"]
    app_client.delete(f"/api/todos/{tid}")
    ids = [t["id"] for t in app_client.get("/api/todos").json()]
    assert tid not in ids


# ---------------------------------------------------------------------------
# 3. GET / serves HTML (live uvicorn)
# ---------------------------------------------------------------------------

def test_root_serves_html():
    PORT = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", f"--port={PORT}", "--log-level=error"],
        cwd=WS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(16):
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.5)
        import httpx
        r = httpx.get(f"http://127.0.0.1:{PORT}/", timeout=5)
        is_html = "html" in r.headers.get("content-type", "") or "<html" in r.text.lower()
        assert r.status_code == 200 and is_html, f"GET / → {r.status_code}, html={is_html}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except Exception:
            server.kill()


# ---------------------------------------------------------------------------
# 4. HTML UI wiring (static analysis)
# ---------------------------------------------------------------------------

def test_html_has_text_input(html):
    assert re.search(r'<input[^>]+type=["\']?text', html) \
        or re.search(r'<input(?![^>]+type=)[^>]*>', html), \
        "no text input found"


def test_html_has_submit_control(html):
    assert re.search(r'<button|<input[^>]+type=["\']?submit', html), \
        "no submit button found"


def test_html_calls_api(html):
    assert "/api/todos" in html, "HTML does not reference /api/todos"


def test_html_uses_post(html):
    assert '"post"' in html or "'post'" in html, "no POST fetch call found"


def test_html_uses_put(html):
    assert '"put"' in html or "'put'" in html, "no PUT fetch call found"


def test_html_uses_delete(html):
    assert '"delete"' in html or "'delete'" in html, "no DELETE fetch call found"


def test_html_has_checkbox(html):
    assert (
        re.search(r'<input[^>]+type=["\']?checkbox', html)
        or re.search(r'\.type\s*=\s*["\']checkbox["\']', html)
        or re.search(r'type:\s*["\']checkbox["\']', html)
        or ("checkbox" in html and re.search(r'createelement\(["\']input["\']', html))
    ), "no checkbox found for done toggle"


def test_html_has_done_style(html):
    assert any(kw in html for kw in [
        "line-through", "text-decoration", "textdecoration",
        "class=\"done\"", "class='done'", ".done {", ".done{",
        "classlist.add('done')", "classlist.add(\"done\")",
        "classlist.toggle('done')", "classlist.toggle(\"done\")",
    ]), "no visual done-state indicator found"
