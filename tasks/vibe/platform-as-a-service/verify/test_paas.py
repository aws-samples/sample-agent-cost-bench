"""
Harness tests for platform-as-a-service.

Tests the multi-tenant PaaS backend: project management, resource provisioning,
deployments, billing/metering, secrets, log aggregation, alerting, API keys,
webhooks, quotas, RBAC, audit trail, and tenant isolation.

WORKSPACE is set to the model's output directory by the harness.
"""

from __future__ import annotations

import base64
import importlib
import os
import sys
from pathlib import Path

import pytest

WS = Path(os.environ.get("WORKSPACE", "."))


@pytest.fixture(scope="module", autouse=True)
def setup_path():
    ws_str = str(WS)
    if ws_str not in sys.path:
        sys.path.insert(0, ws_str)
    yield
    if ws_str in sys.path:
        sys.path.remove(ws_str)


@pytest.fixture(scope="module")
def client():
    try:
        mod = importlib.import_module("main")
    except (ImportError, ModuleNotFoundError) as e:
        pytest.skip(f"main.py not importable: {e}")
    from starlette.testclient import TestClient
    return TestClient(mod.app)


def _make_token(user_id: str, tenant_id: str) -> str:
    raw = f"{user_id}:{tenant_id}"
    return base64.b64encode(raw.encode()).decode()


@pytest.fixture(scope="module")
def tenant_and_admin(client):
    r = client.post("/tenants", json={"name": "PaaSCorp", "plan": "pro"})
    assert r.status_code in (200, 201), f"Failed to create tenant: {r.text}"
    tenant_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "owner1", "email": "owner1@paascorp.com", "role": "owner"
    })
    assert r.status_code in (200, 201), f"Failed to create first user: {r.text}"
    admin = r.json()
    token = _make_token(admin["id"], tenant_id)
    return tenant_id, admin, token


@pytest.fixture(scope="module")
def developer_token(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "dev1", "email": "dev1@paascorp.com", "role": "developer"
    }, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code in (200, 201)
    dev = r.json()
    return _make_token(dev["id"], tenant_id)


# ---------------------------------------------------------------------------
# 1. File existence checks (16 files)
# ---------------------------------------------------------------------------

def test_models_py_exists():
    assert (WS / "models.py").exists()

def test_storage_py_exists():
    assert (WS / "storage.py").exists()

def test_auth_py_exists():
    assert (WS / "auth.py").exists()

def test_quotas_py_exists():
    assert (WS / "quotas.py").exists()

def test_projects_py_exists():
    assert (WS / "projects.py").exists()

def test_resources_py_exists():
    assert (WS / "resources.py").exists()

def test_deployments_py_exists():
    assert (WS / "deployments.py").exists()

def test_secrets_py_exists():
    assert (WS / "secrets.py").exists()

def test_logging_service_py_exists():
    assert (WS / "logging_service.py").exists()

def test_alerting_py_exists():
    assert (WS / "alerting.py").exists()

def test_billing_py_exists():
    assert (WS / "billing.py").exists()

def test_api_keys_py_exists():
    assert (WS / "api_keys.py").exists()

def test_webhooks_py_exists():
    assert (WS / "webhooks.py").exists()

def test_main_py_exists():
    assert (WS / "main.py").exists()

def test_middleware_py_exists():
    assert (WS / "middleware.py").exists()

def test_requirements_txt_exists():
    assert (WS / "requirements.txt").exists()


# ---------------------------------------------------------------------------
# 2. Tenant + auth (4 tests)
# ---------------------------------------------------------------------------

def test_create_tenant(client):
    r = client.post("/tenants", json={"name": "TestTenant", "plan": "starter"})
    assert r.status_code in (200, 201)
    assert "id" in r.json()

def test_bootstrap_first_user_no_auth(client):
    r = client.post("/tenants", json={"name": "BootstrapCorp", "plan": "pro"})
    tenant_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "first_owner", "email": "fo@test.com", "role": "owner"
    })
    assert r.status_code in (200, 201)

def test_auth_token_endpoint(client, tenant_and_admin):
    tenant_id, admin, _ = tenant_and_admin
    r = client.post("/auth/token", json={
        "username": admin["username"], "tenant_id": tenant_id
    })
    assert r.status_code == 200
    body = r.json()
    assert "token" in body or "access_token" in body

def test_unauthenticated_returns_401(client, tenant_and_admin):
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/projects")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. Projects (5 tests)
# ---------------------------------------------------------------------------

def test_create_project(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "MyApp", "description": "Test project", "region": "us-east"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    project = r.json()
    assert project["name"] == "MyApp"
    assert "id" in project

def test_list_projects(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/projects",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("projects", []))
    assert len(items) >= 1

def test_get_project(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create a project
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "GetProj", "description": "Get test", "region": "us-west"
    }, headers={"Authorization": f"Bearer {token}"})
    project_id = r.json()["id"]
    r = client.get(f"/tenants/{tenant_id}/projects/{project_id}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["id"] == project_id

def test_suspend_project(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "SuspendProj", "description": "Will be suspended", "region": "eu-west"
    }, headers={"Authorization": f"Bearer {token}"})
    project_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/projects/{project_id}/suspend",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "suspended"

def test_archive_project(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "ArchiveProj", "description": "Will be archived", "region": "us-east"
    }, headers={"Authorization": f"Bearer {token}"})
    project_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/projects/{project_id}/archive",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "archived"


# ---------------------------------------------------------------------------
# 4. Resources (5 tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def project_for_resources(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "ResourceProj", "description": "For resource tests", "region": "us-east"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    return r.json()["id"]


def test_provision_resource(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/resources", json={
        "resource_type": "compute", "name": "web-server", "size": "medium",
        "config": {"cpu": 2, "memory": "4GB"}
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    resource = r.json()
    assert resource["name"] == "web-server"
    assert resource["status"] in ("provisioning", "running")

def test_stop_resource(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/resources", json={
        "resource_type": "database", "name": "db-stop-test", "size": "small",
        "config": {}
    }, headers={"Authorization": f"Bearer {token}"})
    rid = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/resources/{rid}/stop",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"

def test_start_resource(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/resources", json={
        "resource_type": "cache", "name": "cache-start-test", "size": "small",
        "config": {}
    }, headers={"Authorization": f"Bearer {token}"})
    rid = r.json()["id"]
    # Stop first then start
    client.post(f"/tenants/{tenant_id}/resources/{rid}/stop",
                headers={"Authorization": f"Bearer {token}"})
    r = client.post(f"/tenants/{tenant_id}/resources/{rid}/start",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "running"

def test_terminate_resource(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/resources", json={
        "resource_type": "storage", "name": "storage-term-test", "size": "large",
        "config": {}
    }, headers={"Authorization": f"Bearer {token}"})
    rid = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/resources/{rid}/terminate",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "terminated"

def test_resize_resource(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/resources", json={
        "resource_type": "compute", "name": "resize-test", "size": "small",
        "config": {}
    }, headers={"Authorization": f"Bearer {token}"})
    rid = r.json()["id"]
    r = client.put(f"/tenants/{tenant_id}/resources/{rid}/resize",
                   json={"size": "large"},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["size"] == "large"


# ---------------------------------------------------------------------------
# 5. Quotas (3 tests)
# ---------------------------------------------------------------------------

def test_get_quota_usage(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/quotas",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    # Should contain limits or usage info
    assert "limits" in body or "usage" in body or "max_projects" in body or "plan" in body

def test_quota_exceeded_free_plan(client):
    """Create a free-plan tenant and exceed project quota (max 2)."""
    r = client.post("/tenants", json={"name": "FreeCorp", "plan": "free"})
    assert r.status_code in (200, 201)
    tenant_id = r.json()["id"]
    # Bootstrap user
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "freeadmin", "email": "fa@free.com", "role": "owner"
    })
    assert r.status_code in (200, 201)
    token = _make_token(r.json()["id"], tenant_id)
    # Create max projects (2 for free plan)
    for i in range(2):
        r = client.post(f"/tenants/{tenant_id}/projects", json={
            "name": f"FreeProj{i}", "description": "quota test", "region": "us-east"
        }, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code in (200, 201)
    # Third should be rejected
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "FreeProj3", "description": "over quota", "region": "us-east"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (400, 403, 409, 429)

def test_quota_usage_endpoint(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/quotas",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 6. Deployments (5 tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def project_for_deployments(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "DeployProj", "description": "For deployment tests", "region": "us-east"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    return r.json()["id"]


def test_create_deployment(client, tenant_and_admin, project_for_deployments):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_deployments
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/deployments", json={
        "version": "v1.0.0", "config": {"env": "production", "replicas": 3}
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    dep = r.json()
    assert dep["version"] == "v1.0.0"
    assert dep["status"] in ("pending", "building", "deploying", "live")

def test_execute_deployment(client, tenant_and_admin, project_for_deployments):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_deployments
    # Create a deployment
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/deployments", json={
        "version": "v1.1.0", "config": {"env": "production"}
    }, headers={"Authorization": f"Bearer {token}"})
    dep_id = r.json()["id"]
    # Execute it
    r = client.post(f"/tenants/{tenant_id}/deployments/{dep_id}/execute",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    dep = r.json()
    assert dep["status"] == "live"

def test_list_deployments(client, tenant_and_admin, project_for_deployments):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_deployments
    r = client.get(f"/tenants/{tenant_id}/projects/{pid}/deployments",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("deployments", []))
    assert len(items) >= 1

def test_rollback_deployment(client, tenant_and_admin, project_for_deployments):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_deployments
    # Create and execute a deployment first
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/deployments", json={
        "version": "v2.0.0", "config": {"env": "production"}
    }, headers={"Authorization": f"Bearer {token}"})
    dep_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/deployments/{dep_id}/execute",
                headers={"Authorization": f"Bearer {token}"})
    # Rollback
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/rollback",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    dep = r.json()
    assert dep["status"] == "live"
    assert dep.get("previous_deployment_id") is not None

def test_get_live_deployment(client, tenant_and_admin, project_for_deployments):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_deployments
    r = client.get(f"/tenants/{tenant_id}/projects/{pid}/deployments/live",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    dep = r.json()
    assert dep["status"] == "live"


# ---------------------------------------------------------------------------
# 7. Secrets (4 tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def project_for_secrets(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "SecretsProj", "description": "For secrets tests", "region": "us-west"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    return r.json()["id"]


def test_set_secret(client, tenant_and_admin, project_for_secrets):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_secrets
    r = client.put(f"/tenants/{tenant_id}/projects/{pid}/secrets/DB_PASSWORD",
                   json={"value": "super-secret-123"},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    secret = r.json()
    assert secret["key"] == "DB_PASSWORD"

def test_get_secret(client, tenant_and_admin, project_for_secrets):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_secrets
    r = client.get(f"/tenants/{tenant_id}/projects/{pid}/secrets/DB_PASSWORD",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    secret = r.json()
    assert secret["key"] == "DB_PASSWORD"

def test_list_secrets_keys_only(client, tenant_and_admin, project_for_secrets):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_secrets
    # Set another secret
    client.put(f"/tenants/{tenant_id}/projects/{pid}/secrets/API_KEY",
               json={"value": "my-api-key-456"},
               headers={"Authorization": f"Bearer {token}"})
    r = client.get(f"/tenants/{tenant_id}/projects/{pid}/secrets",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("secrets", []))
    # Verify no raw values are exposed
    for item in items:
        if isinstance(item, dict):
            assert "value" not in item or item.get("value") is None
            assert "value_encrypted" not in item or item.get("value_encrypted") is None

def test_delete_secret(client, tenant_and_admin, project_for_secrets):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_secrets
    # Set a secret to delete
    client.put(f"/tenants/{tenant_id}/projects/{pid}/secrets/TEMP_SECRET",
               json={"value": "temp-value"},
               headers={"Authorization": f"Bearer {token}"})
    r = client.delete(f"/tenants/{tenant_id}/projects/{pid}/secrets/TEMP_SECRET",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 204)


# ---------------------------------------------------------------------------
# 8. Logs (4 tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def project_for_logs(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "LogsProj", "description": "For log tests", "region": "us-east"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    return r.json()["id"]


def test_ingest_log(client, tenant_and_admin, project_for_logs):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_logs
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/logs", json={
        "level": "info", "message": "Application started",
        "source": "app-server", "metadata": {"version": "1.0"}
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    log = r.json()
    assert log["level"] == "info"

def test_query_logs_by_level(client, tenant_and_admin, project_for_logs):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_logs
    # Ingest an error log
    client.post(f"/tenants/{tenant_id}/projects/{pid}/logs", json={
        "level": "error", "message": "Connection timeout",
        "source": "db-client", "metadata": {}
    }, headers={"Authorization": f"Bearer {token}"})
    r = client.get(f"/tenants/{tenant_id}/logs",
                   params={"project_id": pid, "level": "error"},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("logs", body.get("entries", [])))
    assert len(items) >= 1
    for item in items:
        assert item["level"] == "error"

def test_get_error_rate(client, tenant_and_admin, project_for_logs):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_logs
    # Ingest mixed logs
    for level in ["info", "info", "info", "error", "error"]:
        client.post(f"/tenants/{tenant_id}/projects/{pid}/logs", json={
            "level": level, "message": f"Log entry {level}",
            "source": "test", "metadata": {}
        }, headers={"Authorization": f"Bearer {token}"})
    r = client.get(f"/tenants/{tenant_id}/projects/{pid}/logs/error-rate",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    rate = body.get("error_rate", body.get("rate", 0))
    assert isinstance(rate, (int, float))
    assert rate > 0

def test_get_log_stats(client, tenant_and_admin, project_for_logs):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_logs
    r = client.get(f"/tenants/{tenant_id}/projects/{pid}/logs/stats",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    # Should have counts by level
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# 9. Alerts (4 tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def project_for_alerts(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "AlertsProj", "description": "For alert tests", "region": "us-east"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    pid = r.json()["id"]
    # Ingest some error logs so evaluate_rules can fire
    for i in range(10):
        client.post(f"/tenants/{tenant_id}/projects/{pid}/logs", json={
            "level": "error", "message": f"Error #{i}",
            "source": "alerting-test", "metadata": {}
        }, headers={"Authorization": f"Bearer {token}"})
    # Also add some non-error logs
    for i in range(5):
        client.post(f"/tenants/{tenant_id}/projects/{pid}/logs", json={
            "level": "info", "message": f"Info #{i}",
            "source": "alerting-test", "metadata": {}
        }, headers={"Authorization": f"Bearer {token}"})
    return pid


def test_create_alert_rule(client, tenant_and_admin, project_for_alerts):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_alerts
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/alert-rules", json={
        "name": "High Error Rate", "condition": "error_rate > 0.05",
        "severity": "critical", "notification_channel": "webhook"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    rule = r.json()
    assert rule["name"] == "High Error Rate"
    assert rule["is_active"] is True

def test_evaluate_alert_rules(client, tenant_and_admin, project_for_alerts):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_alerts
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/alerts/evaluate",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    alerts = body if isinstance(body, list) else body.get("items", body.get("alerts", []))
    # Should fire since error rate is high
    assert len(alerts) >= 1

def test_list_alerts(client, tenant_and_admin, project_for_alerts):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/alerts",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("alerts", []))
    assert len(items) >= 1

def test_resolve_alert(client, tenant_and_admin, project_for_alerts):
    tenant_id, _, token = tenant_and_admin
    # Get an existing alert
    r = client.get(f"/tenants/{tenant_id}/alerts",
                   headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("alerts", []))
    assert len(items) >= 1
    alert_id = items[0]["id"]
    # Resolve it
    r = client.post(f"/tenants/{tenant_id}/alerts/{alert_id}/resolve",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    alert = r.json()
    assert alert["status"] == "resolved"


# ---------------------------------------------------------------------------
# 10. Billing (5 tests)
# ---------------------------------------------------------------------------

def test_record_usage(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    r = client.post(f"/tenants/{tenant_id}/usage", json={
        "project_id": pid, "resource_type": "compute",
        "quantity": 10.0, "unit": "hours"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    record = r.json()
    assert record["quantity"] == 10.0

def test_get_usage_summary(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/usage/summary",
                   params={"period_start": "2020-01-01T00:00:00Z",
                           "period_end": "2030-12-31T23:59:59Z"},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)

def test_generate_invoice(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    # Record some more usage
    client.post(f"/tenants/{tenant_id}/usage", json={
        "project_id": pid, "resource_type": "storage",
        "quantity": 5.0, "unit": "gb"
    }, headers={"Authorization": f"Bearer {token}"})
    r = client.post(f"/tenants/{tenant_id}/invoices/generate", json={
        "period_start": "2020-01-01T00:00:00Z",
        "period_end": "2030-12-31T23:59:59Z"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    invoice = r.json()
    assert invoice["status"] == "draft"
    assert invoice["total"] > 0

def test_issue_invoice(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    # Record usage and generate
    client.post(f"/tenants/{tenant_id}/usage", json={
        "project_id": pid, "resource_type": "compute",
        "quantity": 2.0, "unit": "hours"
    }, headers={"Authorization": f"Bearer {token}"})
    r = client.post(f"/tenants/{tenant_id}/invoices/generate", json={
        "period_start": "2024-01-01T00:00:00Z",
        "period_end": "2024-12-31T23:59:59Z"
    }, headers={"Authorization": f"Bearer {token}"})
    invoice_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/invoices/{invoice_id}/issue",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "issued"

def test_pay_invoice(client, tenant_and_admin, project_for_resources):
    tenant_id, _, token = tenant_and_admin
    pid = project_for_resources
    # Record usage and generate
    client.post(f"/tenants/{tenant_id}/usage", json={
        "project_id": pid, "resource_type": "database",
        "quantity": 3.0, "unit": "hours"
    }, headers={"Authorization": f"Bearer {token}"})
    r = client.post(f"/tenants/{tenant_id}/invoices/generate", json={
        "period_start": "2025-01-01T00:00:00Z",
        "period_end": "2025-06-30T23:59:59Z"
    }, headers={"Authorization": f"Bearer {token}"})
    invoice_id = r.json()["id"]
    # Issue first
    client.post(f"/tenants/{tenant_id}/invoices/{invoice_id}/issue",
                headers={"Authorization": f"Bearer {token}"})
    # Pay
    r = client.post(f"/tenants/{tenant_id}/invoices/{invoice_id}/pay",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "paid"


# ---------------------------------------------------------------------------
# 11. API Keys (3 tests)
# ---------------------------------------------------------------------------

def test_create_api_key(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/api-keys", json={
        "name": "Production Key", "tier": "premium"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    body = r.json()
    assert "api_key" in body or "key" in body
    assert "id" in body

def test_list_api_keys_masked(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/api-keys",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("keys", body.get("api_keys", [])))
    assert len(items) >= 1
    # Keys should be masked (not full plaintext exposed)
    for item in items:
        if isinstance(item, dict):
            key_hash = item.get("key_hash", item.get("key", ""))
            # Masked keys typically have asterisks or are truncated
            if key_hash:
                assert "***" in key_hash or len(key_hash) <= 12 or key_hash.startswith("***")

def test_revoke_api_key(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create a key to revoke
    r = client.post(f"/tenants/{tenant_id}/api-keys", json={
        "name": "Revoke Key", "tier": "standard"
    }, headers={"Authorization": f"Bearer {token}"})
    key_id = r.json()["id"]
    r = client.delete(f"/tenants/{tenant_id}/api-keys/{key_id}",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 204)


# ---------------------------------------------------------------------------
# 12. Webhooks (3 tests)
# ---------------------------------------------------------------------------

def test_register_webhook(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["deployment.live", "alert.fired"],
        "secret": "webhook-secret-123"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    webhook = r.json()
    assert webhook["url"] == "https://example.com/hook"
    assert webhook["is_active"] is True

def test_fire_webhook_event(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Register a webhook first
    client.post(f"/tenants/{tenant_id}/webhooks", json={
        "url": "https://example.com/fire-test",
        "events": ["test.event"],
        "secret": "secret"
    }, headers={"Authorization": f"Bearer {token}"})
    # Simulate firing an event (this might be internal, try via a deployment or direct)
    # Attempt to trigger via deployment which should fire webhooks
    # Create project and deploy
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "WebhookFireProj", "description": "fire test", "region": "us-east"
    }, headers={"Authorization": f"Bearer {token}"})
    pid = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/projects/{pid}/deployments", json={
        "version": "v1.0.0", "config": {}
    }, headers={"Authorization": f"Bearer {token}"})
    dep_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/deployments/{dep_id}/execute",
                headers={"Authorization": f"Bearer {token}"})
    # Check deliveries
    r = client.get(f"/tenants/{tenant_id}/webhooks",
                   headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    webhooks = body if isinstance(body, list) else body.get("items", body.get("webhooks", []))
    assert len(webhooks) >= 1

def test_list_webhook_deliveries(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Get first webhook
    r = client.get(f"/tenants/{tenant_id}/webhooks",
                   headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    webhooks = body if isinstance(body, list) else body.get("items", body.get("webhooks", []))
    if webhooks:
        wid = webhooks[0]["id"]
        r = client.get(f"/tenants/{tenant_id}/webhooks/{wid}/deliveries",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 13. Tenant isolation (2 tests)
# ---------------------------------------------------------------------------

def test_tenant_isolation_projects(client, tenant_and_admin):
    tenant_id_a, _, token_a = tenant_and_admin
    # Create tenant B
    r = client.post("/tenants", json={"name": "OtherCorp", "plan": "starter"})
    tenant_id_b = r.json()["id"]
    # Try to access tenant B's projects with tenant A's token
    r = client.get(f"/tenants/{tenant_id_b}/projects",
                   headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 403

def test_tenant_isolation_resources(client, tenant_and_admin):
    tenant_id_a, _, token_a = tenant_and_admin
    # Create tenant C
    r = client.post("/tenants", json={"name": "ThirdCorp", "plan": "pro"})
    tenant_id_c = r.json()["id"]
    # Try to access tenant C's resources
    r = client.get(f"/tenants/{tenant_id_c}/quotas",
                   headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 14. Health (1 test)
# ---------------------------------------------------------------------------

def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "healthy"


# ---------------------------------------------------------------------------
# 15. Request ID (1 test)
# ---------------------------------------------------------------------------

def test_request_id_header(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/projects",
                   headers={"Authorization": f"Bearer {token}"})
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    assert "x-request-id" in headers_lower


# ---------------------------------------------------------------------------
# 16. Audit (2 tests)
# ---------------------------------------------------------------------------

def test_audit_log_entries(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/audit",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    entries = body if isinstance(body, list) else body.get("entries", body.get("items", body.get("audit", [])))
    assert len(entries) >= 1

def test_audit_contains_action_fields(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/audit",
                   headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    entries = body if isinstance(body, list) else body.get("entries", body.get("items", body.get("audit", [])))
    if entries:
        entry = entries[0]
        assert "action" in entry or "event" in entry


# ---------------------------------------------------------------------------
# 17. RBAC (2 tests)
# ---------------------------------------------------------------------------

def test_viewer_cannot_create_project(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    # Create a viewer user
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "viewer1", "email": "viewer1@paascorp.com", "role": "viewer"
    }, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code in (200, 201)
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)
    # Viewer should not be able to create projects
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "ViewerProj", "description": "Should fail", "region": "us-east"
    }, headers={"Authorization": f"Bearer {viewer_token}"})
    assert r.status_code == 403

def test_developer_can_create_project(client, tenant_and_admin, developer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/projects", json={
        "name": "DevProj", "description": "Developer created", "region": "us-west"
    }, headers={"Authorization": f"Bearer {developer_token}"})
    assert r.status_code in (200, 201)
    assert r.json()["name"] == "DevProj"


# ---------------------------------------------------------------------------
# 18. Additional coverage tests
# ---------------------------------------------------------------------------

def test_get_tenant(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == tenant_id
    assert body["plan"] == "pro"

def test_list_invoices(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/invoices",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("invoices", []))
    assert len(items) >= 1

def test_list_alert_rules(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/alert-rules",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("rules", []))
    assert len(items) >= 1
