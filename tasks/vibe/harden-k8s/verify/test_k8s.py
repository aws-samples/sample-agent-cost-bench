"""
Harness tests for task-008-harden-k8s.

Parses the hardened Deployment manifest with PyYAML and verifies each
hardening requirement. securityContext settings are accepted at either the
pod level (spec.template.spec.securityContext) or the container level — they
are merged before checking, mirroring how Kubernetes resolves them.

WORKSPACE is set to the model's output directory by the harness.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

WS = Path(os.environ.get("WORKSPACE", "."))
_EXCLUDE = {".venv-verify", ".venv", "venv", "__pycache__", "site-packages", ".git"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_manifest() -> Path | None:
    for base in (WS / "src", WS):
        for name in ("deployment.yaml", "deployment.yml"):
            p = base / name
            if p.exists():
                return p
    for p in WS.rglob("*.y*ml"):
        if not any(part in _EXCLUDE for part in p.parts):
            return p
    return None


@pytest.fixture(scope="module")
def deployment() -> dict:
    manifest = _find_manifest()
    if manifest is None:
        pytest.skip("deployment manifest not found")
    docs = [d for d in yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
            if isinstance(d, dict)]
    dep = next((d for d in docs if d.get("kind") == "Deployment"), None)
    if dep is None:
        pytest.fail("no Deployment document found in manifest")
    return dep


@pytest.fixture(scope="module")
def container(deployment) -> dict:
    containers = (
        deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    )
    if not containers:
        pytest.fail("Deployment has no containers")
    return containers[0]


@pytest.fixture(scope="module")
def security_ctx(deployment, container) -> dict:
    pod_sc = (
        deployment.get("spec", {}).get("template", {}).get("spec", {})
                  .get("securityContext", {}) or {}
    )
    cont_sc = container.get("securityContext", {}) or {}
    # container-level overrides pod-level; merge capabilities separately
    merged = {**pod_sc, **cont_sc}
    caps_pod = pod_sc.get("capabilities", {}) or {}
    caps_cont = cont_sc.get("capabilities", {}) or {}
    merged["_capabilities"] = {**caps_pod, **caps_cont}
    return merged


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_manifest_exists():
    assert _find_manifest() is not None, "deployment manifest not found"


def test_deployment_named_web(deployment):
    assert deployment.get("metadata", {}).get("name") == "web", \
        "Deployment is not named 'web'"


def test_image_is_pinned(container):
    image = container.get("image", "") or ""
    if "@sha256:" in image:
        return  # digest pin is fine
    assert ":" in image.split("/")[-1], f"image '{image}' has no tag"
    tag = image.split("/")[-1].split(":", 1)[1]
    assert tag and tag != "latest", f"image tag is '{tag}', not a pinned version"


def test_resource_requests_cpu(container):
    assert "cpu" in (container.get("resources", {}).get("requests", {}) or {}), \
        "resources.requests.cpu missing"


def test_resource_requests_memory(container):
    assert "memory" in (container.get("resources", {}).get("requests", {}) or {}), \
        "resources.requests.memory missing"


def test_resource_limits_cpu(container):
    assert "cpu" in (container.get("resources", {}).get("limits", {}) or {}), \
        "resources.limits.cpu missing"


def test_resource_limits_memory(container):
    assert "memory" in (container.get("resources", {}).get("limits", {}) or {}), \
        "resources.limits.memory missing"


def test_run_as_non_root(security_ctx):
    assert security_ctx.get("runAsNonRoot") is True, "runAsNonRoot is not true"


def test_read_only_root_filesystem(security_ctx):
    assert security_ctx.get("readOnlyRootFilesystem") is True, \
        "readOnlyRootFilesystem is not true"


def test_drop_all_capabilities(security_ctx):
    drop = security_ctx["_capabilities"].get("drop", []) or []
    assert "ALL" in [str(x).upper() for x in drop], \
        "capabilities.drop does not include ALL"


def test_no_privilege_escalation(security_ctx):
    assert security_ctx.get("allowPrivilegeEscalation") is False, \
        "allowPrivilegeEscalation is not false"


def test_liveness_probe(container):
    assert container.get("livenessProbe"), "livenessProbe missing"


def test_readiness_probe(container):
    assert container.get("readinessProbe"), "readinessProbe missing"
