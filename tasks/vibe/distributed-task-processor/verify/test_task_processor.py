"""
Harness tests for distributed-task-processor.

Tests the multi-tenant distributed task processing system: plugins, event bus,
scheduling, metrics, versioned config, and task execution.

WORKSPACE is set to the model's output directory by the harness.
"""

from __future__ import annotations

import base64
import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
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
    r = client.post("/tenants", json={"name": "ProcessCorp"})
    assert r.status_code in (200, 201), f"Failed to create tenant: {r.text}"
    tenant_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "admin1", "email": "admin1@processcorp.com", "role": "admin"
    })
    assert r.status_code in (200, 201), f"Failed to create admin: {r.text}"
    admin = r.json()
    token = _make_token(admin["id"], tenant_id)
    return tenant_id, admin, token


@pytest.fixture(scope="module")
def operator_token(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "operator1", "email": "op1@processcorp.com", "role": "operator"
    }, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code in (200, 201)
    op = r.json()
    return _make_token(op["id"], tenant_id)


# ---------------------------------------------------------------------------
# 1. File existence
# ---------------------------------------------------------------------------

def test_main_py_exists():
    assert (WS / "main.py").exists()

def test_models_py_exists():
    assert (WS / "models.py").exists()

def test_storage_py_exists():
    assert (WS / "storage.py").exists()

def test_auth_py_exists():
    assert (WS / "auth.py").exists()

def test_plugins_py_exists():
    assert (WS / "plugins.py").exists()

def test_event_bus_py_exists():
    assert (WS / "event_bus.py").exists()

def test_scheduler_py_exists():
    assert (WS / "scheduler.py").exists()

def test_executor_py_exists():
    assert (WS / "executor.py").exists()

def test_metrics_py_exists():
    assert (WS / "metrics.py").exists()

def test_config_manager_py_exists():
    assert (WS / "config_manager.py").exists()


# ---------------------------------------------------------------------------
# 2. Tenant and auth
# ---------------------------------------------------------------------------

def test_create_tenant(client):
    r = client.post("/tenants", json={"name": "TestTenant"})
    assert r.status_code in (200, 201)
    assert "id" in r.json()

def test_bootstrap_first_user(client):
    r = client.post("/tenants", json={"name": "BootstrapCorp"})
    tenant_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "first_admin", "email": "fa@test.com", "role": "admin"
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
    r = client.get(f"/tenants/{tenant_id}/tasks")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. Task CRUD
# ---------------------------------------------------------------------------

def test_create_task(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "data_transform",
        "payload": {"input_data": [{"x": 1}], "operations": []},
        "priority": 5
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    task = r.json()
    assert task["status"] == "pending"
    assert "id" in task

def test_list_tasks(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/tasks",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("tasks", []))
    assert len(items) >= 1

def test_get_task_detail(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "send_notification",
        "payload": {"recipients": ["u1"], "message": "hi", "channel": "in_app"},
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]
    r = client.get(f"/tenants/{tenant_id}/tasks/{task_id}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["id"] == task_id

def test_create_task_viewer_forbidden(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "viewer1", "email": "v@test.com", "role": "viewer"
    }, headers={"Authorization": f"Bearer {admin_token}"})
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "data_transform", "payload": {}, "priority": 1
    }, headers={"Authorization": f"Bearer {viewer_token}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 4. Task execution
# ---------------------------------------------------------------------------

def test_execute_data_transform_task(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "data_transform",
        "payload": {
            "input_data": [{"name": "a", "val": 3}, {"name": "b", "val": 1}, {"name": "c", "val": 2}],
            "operations": [{"op": "sort", "key": "val"}]
        }
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/tasks/{task_id}/execute",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    result = r.json()
    assert result["status"] == "completed"
    assert result.get("result") is not None

def test_execute_notification_task(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "send_notification",
        "payload": {"recipients": ["user1", "user2"], "message": "Hello!", "channel": "email"}
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/tasks/{task_id}/execute",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    result = r.json()
    assert result["status"] == "completed"

def test_execute_report_task(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "generate_report",
        "payload": {"report_type": "task_summary", "time_range_hours": 24}
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/tasks/{task_id}/execute",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

def test_cancel_task(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "data_transform", "payload": {"input_data": [], "operations": []}
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/tasks/{task_id}/cancel",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

def test_execute_unknown_task_type_fails(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "nonexistent_plugin", "payload": {}
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/tasks/{task_id}/execute",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    result = r.json()
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# 5. Batch operations
# ---------------------------------------------------------------------------

def test_batch_create_tasks(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks/batch", json={
        "tasks": [
            {"task_type": "data_transform", "payload": {"input_data": [], "operations": []}, "priority": 3},
            {"task_type": "send_notification", "payload": {"recipients": ["u1"], "message": "hi", "channel": "in_app"}, "priority": 7},
        ]
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    body = r.json()
    created = body.get("created", body.get("tasks", []))
    assert len(created) >= 2


# ---------------------------------------------------------------------------
# 6. Scheduling
# ---------------------------------------------------------------------------

def test_create_scheduled_task(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    future = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "data_transform",
        "payload": {"input_data": [{"v": 1}], "operations": []},
        "scheduled_at": future,
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    task = r.json()
    assert task.get("scheduled_at") is not None

def test_get_due_tasks(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/tasks/due",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200

def test_create_recurring_task(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "generate_report",
        "payload": {"report_type": "metrics_summary", "time_range_hours": 1},
        "recurrence_rule": "hourly",
        "scheduled_at": datetime.now(timezone.utc).isoformat(),
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    task = r.json()
    assert task.get("recurrence_rule") == "hourly"

def test_get_recurring_tasks(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/tasks/recurring",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("tasks", []))
    assert len(items) >= 1


# ---------------------------------------------------------------------------
# 7. Event bus
# ---------------------------------------------------------------------------

def test_events_published_on_execution(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create and execute a task
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "data_transform",
        "payload": {"input_data": [{"x": 1}], "operations": []}
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/tasks/{task_id}/execute",
                headers={"Authorization": f"Bearer {token}"})

    # Check events
    r = client.get(f"/tenants/{tenant_id}/events",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    events = body if isinstance(body, list) else body.get("items", body.get("events", []))
    assert len(events) >= 1

def test_subscribe_to_events(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/events/subscribe", json={
        "event_type": "task.completed", "callback_name": "my_callback"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    sub = r.json()
    assert "id" in sub

def test_list_subscriptions(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/events/subscriptions",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200

def test_unsubscribe(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/events/subscribe", json={
        "event_type": "task.failed", "callback_name": "alert_cb"
    }, headers={"Authorization": f"Bearer {token}"})
    sub_id = r.json()["id"]
    r = client.delete(f"/tenants/{tenant_id}/events/subscriptions/{sub_id}",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 204)


# ---------------------------------------------------------------------------
# 8. Plugin configuration
# ---------------------------------------------------------------------------

def test_list_plugins(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/plugins",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    plugins = body if isinstance(body, list) else body.get("plugins", [])
    names = [p if isinstance(p, str) else p.get("name", "") for p in plugins]
    assert "data_transform" in names or "DataTransformPlugin" in names or len(plugins) >= 3

def test_set_plugin_config(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.put(f"/tenants/{tenant_id}/plugins/data_transform/config", json={
        "config_data": {"max_items": 1000, "timeout": 30}
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    cfg = r.json()
    assert cfg.get("version") == 1 or cfg.get("version") is not None

def test_get_plugin_config(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/plugins/data_transform/config",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json().get("config_data", {}).get("max_items") == 1000

def test_config_versioning(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Set a second version
    client.put(f"/tenants/{tenant_id}/plugins/data_transform/config", json={
        "config_data": {"max_items": 2000, "timeout": 60}
    }, headers={"Authorization": f"Bearer {token}"})

    r = client.get(f"/tenants/{tenant_id}/plugins/data_transform/config/versions",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    versions = body if isinstance(body, list) else body.get("versions", [])
    assert len(versions) >= 2

def test_config_rollback(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/plugins/data_transform/config/rollback",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    cfg = r.json()
    # Should revert to previous config_data
    assert cfg.get("config_data", {}).get("max_items") == 1000


# ---------------------------------------------------------------------------
# 9. Metrics
# ---------------------------------------------------------------------------

def test_task_metrics(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/metrics/tasks",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "total_tasks" in body or "by_status" in body

def test_metric_points(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/metrics/task.duration_seconds",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200

def test_metric_summary(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/metrics/task.duration_seconds/summary",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    # Should have aggregation keys
    assert any(k in body for k in ("count", "sum", "avg", "min", "max"))


# ---------------------------------------------------------------------------
# 10. Health check
# ---------------------------------------------------------------------------

def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "healthy"


# ---------------------------------------------------------------------------
# 11. Tenant isolation
# ---------------------------------------------------------------------------

def test_tenant_isolation_tasks(client, tenant_and_admin):
    tenant_id_a, _, token_a = tenant_and_admin

    # Create tenant B
    r = client.post("/tenants", json={"name": "OtherCorp"})
    tenant_id_b = r.json()["id"]

    # Try to access tenant B's tasks with tenant A's token
    r = client.get(f"/tenants/{tenant_id_b}/tasks",
                   headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 12. Audit log
# ---------------------------------------------------------------------------

def test_audit_log(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/audit",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    entries = body if isinstance(body, list) else body.get("entries", body.get("items", []))
    assert len(entries) >= 1

def test_audit_admin_only(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    # Create viewer
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "audit_viewer2", "email": "av2@test.com", "role": "viewer"
    }, headers={"Authorization": f"Bearer {admin_token}"})
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)

    r = client.get(f"/tenants/{tenant_id}/audit",
                   headers={"Authorization": f"Bearer {viewer_token}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 13. Error handling and middleware
# ---------------------------------------------------------------------------

def test_request_id_header(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/tasks",
                   headers={"Authorization": f"Bearer {token}"})
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    assert "x-request-id" in headers_lower

def test_not_found_returns_404(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/tasks/nonexistent-id",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 14. Data transform plugin logic
# ---------------------------------------------------------------------------

def test_data_transform_filter(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "data_transform",
        "payload": {
            "input_data": [{"name": "a", "active": True}, {"name": "b", "active": False}],
            "operations": [{"op": "filter", "key": "active", "value": True}]
        }
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/tasks/{task_id}/execute",
                    headers={"Authorization": f"Bearer {token}"})
    result = r.json().get("result", {})
    data = result.get("data", result.get("output", result.get("result", [])))
    if isinstance(data, list):
        assert len(data) == 1
        assert data[0].get("name") == "a"

def test_data_transform_aggregate(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/tasks", json={
        "task_type": "data_transform",
        "payload": {
            "input_data": [{"val": 10}, {"val": 20}, {"val": 30}],
            "operations": [{"op": "aggregate", "field": "val", "func": "sum"}]
        }
    }, headers={"Authorization": f"Bearer {token}"})
    task_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/tasks/{task_id}/execute",
                    headers={"Authorization": f"Bearer {token}"})
    result = r.json().get("result", {})
    # Should contain sum = 60 somewhere in result
    assert result is not None
