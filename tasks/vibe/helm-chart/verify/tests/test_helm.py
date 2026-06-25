"""
Harness tests for helm-chart task.

Runs inside the agent-cost-bench-helm image after `helm lint` and `helm template` have
already executed. The rendered multi-document YAML is at $RENDERED. Tests parse
it and verify structural requirements.

Score is the fraction of checks that pass (graduated partial credit).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WS = Path(os.environ.get("WORKSPACE", "."))
RENDERED = Path(os.environ.get("RENDERED", "/tmp/rendered.yaml"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_rendered() -> list[dict]:
    if not RENDERED.exists():
        return []
    text = RENDERED.read_text(encoding="utf-8", errors="replace")
    docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    return docs


def _docs_of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


@pytest.fixture(scope="module")
def docs() -> list[dict]:
    d = _load_rendered()
    if not d:
        pytest.skip("no rendered YAML produced by helm template")
    return d


# ---------------------------------------------------------------------------
# Helm lint and template succeed
# ---------------------------------------------------------------------------

def test_helm_lint():
    result = subprocess.run(
        ["helm", "lint", ".", "--strict"],
        capture_output=True, text=True, cwd=str(WS),
    )
    assert result.returncode == 0, f"helm lint failed:\n{result.stdout}\n{result.stderr}"


def test_helm_template():
    result = subprocess.run(
        ["helm", "template", "test-release", ".", "--values", "values.yaml"],
        capture_output=True, text=True, cwd=str(WS),
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stdout}\n{result.stderr}"


# ---------------------------------------------------------------------------
# Chart.yaml
# ---------------------------------------------------------------------------

def test_chart_yaml_exists():
    assert (WS / "Chart.yaml").exists(), "Chart.yaml missing"


def test_chart_yaml_fields():
    data = yaml.safe_load((WS / "Chart.yaml").read_text())
    assert data.get("name"), "Chart.yaml missing 'name'"
    assert data.get("version"), "Chart.yaml missing 'version'"
    assert data.get("appVersion"), "Chart.yaml missing 'appVersion'"


# ---------------------------------------------------------------------------
# values.yaml
# ---------------------------------------------------------------------------

def test_values_yaml_exists():
    assert (WS / "values.yaml").exists(), "values.yaml missing"


def test_values_has_image(docs):
    v = yaml.safe_load((WS / "values.yaml").read_text()) or {}
    img = v.get("image", {})
    assert img.get("repository") and img.get("tag"), \
        "values.yaml missing image.repository / image.tag"


def test_values_has_resources(docs):
    v = yaml.safe_load((WS / "values.yaml").read_text()) or {}
    res = v.get("resources", {})
    assert res.get("requests") and res.get("limits"), \
        "values.yaml missing resources.requests/limits"


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

def test_deployment_exists(docs):
    assert _docs_of_kind(docs, "Deployment"), "no Deployment in rendered output"


def test_deployment_has_resources(docs):
    dep = _docs_of_kind(docs, "Deployment")[0]
    containers = dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    assert containers and containers[0].get("resources"), \
        "Deployment container missing resources"


def test_deployment_references_serviceaccount(docs):
    dep = _docs_of_kind(docs, "Deployment")[0]
    sa_name = dep.get("spec", {}).get("template", {}).get("spec", {}).get("serviceAccountName")
    assert sa_name, "Deployment does not reference a serviceAccountName"


def test_deployment_has_envfrom_configmap(docs):
    dep = _docs_of_kind(docs, "Deployment")[0]
    containers = dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    if not containers:
        pytest.fail("no containers")
    c = containers[0]
    env_from = c.get("envFrom") or []
    env_vars = c.get("env") or []
    # Accept envFrom with configMapRef OR individual env entries from configMapKeyRef
    has_cm = any("configMapRef" in str(e) for e in env_from)
    has_env = any("configMapKeyRef" in str(e) for e in env_vars)
    assert has_cm or has_env, "Deployment does not mount ConfigMap as env"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

def test_service_exists(docs):
    assert _docs_of_kind(docs, "Service"), "no Service in rendered output"


# ---------------------------------------------------------------------------
# Ingress
# ---------------------------------------------------------------------------

def test_ingress_exists(docs):
    assert _docs_of_kind(docs, "Ingress"), "no Ingress in rendered output"


def test_ingress_has_tls(docs):
    ing = _docs_of_kind(docs, "Ingress")
    if not ing:
        pytest.skip("no Ingress")
    tls = ing[0].get("spec", {}).get("tls")
    assert tls, "Ingress has no TLS section"


def test_ingress_has_host(docs):
    ing = _docs_of_kind(docs, "Ingress")
    if not ing:
        pytest.skip("no Ingress")
    rules = ing[0].get("spec", {}).get("rules", [])
    assert rules and rules[0].get("host"), "Ingress has no host rule"


# ---------------------------------------------------------------------------
# HPA
# ---------------------------------------------------------------------------

def test_hpa_exists(docs):
    assert _docs_of_kind(docs, "HorizontalPodAutoscaler"), \
        "no HorizontalPodAutoscaler in rendered output"


def test_hpa_targets_deployment(docs):
    hpa = _docs_of_kind(docs, "HorizontalPodAutoscaler")
    if not hpa:
        pytest.skip("no HPA")
    ref = hpa[0].get("spec", {}).get("scaleTargetRef", {})
    assert ref.get("kind") == "Deployment", "HPA does not target a Deployment"


# ---------------------------------------------------------------------------
# ConfigMap
# ---------------------------------------------------------------------------

def test_configmap_exists(docs):
    assert _docs_of_kind(docs, "ConfigMap"), "no ConfigMap in rendered output"


# ---------------------------------------------------------------------------
# ServiceAccount
# ---------------------------------------------------------------------------

def test_serviceaccount_exists(docs):
    assert _docs_of_kind(docs, "ServiceAccount"), "no ServiceAccount in rendered output"
