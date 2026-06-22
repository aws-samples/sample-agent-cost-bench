"""
Harness tests for task-007-dockerize-flask.

Static analysis only — no Docker daemon required. Checks the Dockerfile and
docker-compose.yml the model added to the existing Flask app.

WORKSPACE is set to the model's output directory by the harness.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

WS = Path(os.environ.get("WORKSPACE", "."))
_EXCLUDE = {".venv-verify", ".venv", "venv", "__pycache__", "site-packages", ".git"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find(names: list[str]) -> Path | None:
    name_set = {n.lower() for n in names}
    for base in (WS / "src", WS):
        for n in names:
            p = base / n
            if p.exists():
                return p
    for p in WS.rglob("*"):
        if p.is_file() and p.name.lower() in name_set and not any(part in _EXCLUDE for part in p.parts):
            return p
    return None


@pytest.fixture(scope="module")
def df_text() -> str:
    f = _find(["Dockerfile", "dockerfile"])
    if f is None:
        pytest.skip("Dockerfile not found")
    return f.read_text(encoding="utf-8").lower()


@pytest.fixture(scope="module")
def compose() -> dict:
    f = _find(["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"])
    if f is None:
        pytest.skip("docker-compose.yml not found")
    parsed = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "docker-compose file did not parse as a dict"
    return parsed


@pytest.fixture(scope="module")
def compose_service(compose) -> dict:
    services = compose.get("services", {})
    assert services, "no services defined in docker-compose.yml"
    return next(iter(services.values())) or {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dockerfile_exists():
    assert _find(["Dockerfile", "dockerfile"]) is not None, "Dockerfile not produced"


def test_compose_exists():
    assert _find(["docker-compose.yml", "docker-compose.yaml",
                  "compose.yml", "compose.yaml"]) is not None, \
        "docker-compose.yml not produced"


def test_dockerfile_slim_base(df_text):
    assert re.search(r'^\s*from\s+python:[\w.\-]*(slim|alpine)', df_text, re.M), \
        "Dockerfile does not use a slim/alpine Python base image"


def test_dockerfile_installs_requirements(df_text):
    assert "requirements.txt" in df_text and re.search(r'pip\s+install', df_text), \
        "Dockerfile does not install requirements.txt"


def test_dockerfile_non_root_user(df_text):
    users = re.findall(r'^\s*user\s+(\S+)', df_text, re.M)
    assert any(u not in ("root", "0", "0:0") for u in users), \
        "Dockerfile does not switch to a non-root USER"


def test_dockerfile_exposes_5000(df_text):
    assert re.search(r'^\s*expose\s+5000', df_text, re.M), \
        "Dockerfile does not EXPOSE 5000"


def test_dockerfile_gunicorn_bind(df_text):
    assert "gunicorn" in df_text and re.search(r'0\.0\.0\.0:5000|:5000', df_text), \
        "Dockerfile does not start gunicorn bound to 0.0.0.0:5000"


def test_compose_has_service(compose):
    assert isinstance(compose.get("services"), dict) and compose["services"], \
        "docker-compose.yml defines no services"


def test_compose_builds_locally(compose_service):
    assert compose_service.get("build") is not None, \
        "compose service has no 'build' directive"


def test_compose_maps_port_5000(compose_service):
    ports = compose_service.get("ports") or []
    assert any("5000:5000" in str(p) for p in ports), \
        "compose service does not map port 5000:5000"
