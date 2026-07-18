"""
Harness tests for multitenant-workflow-engine.

Tests the multi-tenant REST API with workflow state machine, RBAC,
transition guards, notifications, SLA tracking, bulk operations,
and webhook delivery.

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


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

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
    """Import the model's FastAPI app and create a TestClient."""
    try:
        mod = importlib.import_module("main")
    except (ImportError, ModuleNotFoundError):
        pytest.skip("main.py not found or not importable")
    from starlette.testclient import TestClient
    return TestClient(mod.app)


def _make_token(user_id: str, tenant_id: str) -> str:
    """Create a bearer token in the expected format."""
    raw = f"{user_id}:{tenant_id}"
    return base64.b64encode(raw.encode()).decode()


@pytest.fixture(scope="module")
def tenant_and_admin(client):
    """Create a tenant and admin user, return (tenant_id, admin_user, token)."""
    # Create tenant
    r = client.post("/tenants", json={"name": "WorkflowCorp"})
    assert r.status_code in (200, 201), f"Failed to create tenant: {r.text}"
    tenant = r.json()
    tenant_id = tenant["id"]

    # Create admin user — first user in a tenant can be created without auth
    admin_data = {
        "username": "wf_admin",
        "email": "admin@workflowcorp.com",
        "role": "admin",
    }
    r = client.post(f"/tenants/{tenant_id}/users", json=admin_data)
    if r.status_code in (200, 201):
        admin_user = r.json()
    else:
        pytest.fail(
            f"Cannot create first user (bootstrap) without auth: "
            f"status={r.status_code} body={r.text}"
        )

    token = _make_token(admin_user["id"], tenant_id)
    return tenant_id, admin_user, token


# ---------------------------------------------------------------------------
# 1. File existence checks
# ---------------------------------------------------------------------------

def test_main_py_exists():
    assert (WS / "main.py").exists(), "main.py not produced"


def test_models_py_exists():
    assert (WS / "models.py").exists(), "models.py not produced"


def test_storage_py_exists():
    assert (WS / "storage.py").exists(), "storage.py not produced"


def test_auth_py_exists():
    assert (WS / "auth.py").exists(), "auth.py not produced"


def test_workflow_py_exists():
    assert (WS / "workflow.py").exists(), "workflow.py not produced"


def test_requirements_txt_exists():
    assert (WS / "requirements.txt").exists(), "requirements.txt not produced"


# ---------------------------------------------------------------------------
# 2. Tenant and user management
# ---------------------------------------------------------------------------

def test_create_tenant(client):
    """POST /tenants creates a new tenant."""
    r = client.post("/tenants", json={"name": "AcmeCorp"})
    assert r.status_code in (200, 201)
    body = r.json()
    assert "id" in body
    assert body["name"] == "AcmeCorp"


def test_get_tenant(client, tenant_and_admin):
    """GET /tenants/{id} returns tenant info with auth."""
    tenant_id, _, token = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == tenant_id


def test_create_user_as_admin(client, tenant_and_admin):
    """Admin can create a new user in their tenant."""
    tenant_id, _, token = tenant_and_admin
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "editor1", "email": "editor1@wf.com", "role": "editor"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    assert r.json()["role"] == "editor"


def test_create_user_viewer_forbidden(client, tenant_and_admin):
    """Viewer cannot create users."""
    tenant_id, _, admin_token = tenant_and_admin
    # Create a viewer first
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "viewer_nocreate", "email": "vn@wf.com", "role": "viewer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 201)
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)

    # Viewer tries to create user
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "hacker", "email": "h@wf.com", "role": "viewer"},
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 3. Document CRUD
# ---------------------------------------------------------------------------

def test_create_document(client, tenant_and_admin):
    """Admin can create documents, default state is draft."""
    tenant_id, _, token = tenant_and_admin
    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "Workflow Doc", "content": "Initial content", "tags": ["test"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    doc = r.json()
    assert doc["title"] == "Workflow Doc"
    assert "id" in doc
    # Default state should be draft
    state = doc.get("current_state") or doc.get("state") or doc.get("status")
    assert state == "draft"



def test_list_documents_with_state_filter(client, tenant_and_admin):
    """Can filter documents by state."""
    tenant_id, _, token = tenant_and_admin
    # Create a document (will be in draft)
    client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "Filter Doc", "content": "Content", "tags": ["filter"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = client.get(
        f"/tenants/{tenant_id}/documents?state=draft",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    if isinstance(body, list):
        items = body
    else:
        items = body.get("items") or body.get("documents") or body.get("data", [])
    assert len(items) >= 1


# ---------------------------------------------------------------------------
# 4. Workflow transitions — happy path
# ---------------------------------------------------------------------------

def _create_doc_for_workflow(client, tenant_id, token, title="WF Test Doc"):
    """Helper to create a document ready for workflow testing."""
    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": title, "content": "Workflow content", "tags": ["workflow"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    return r.json()


def test_transition_draft_to_review(client, tenant_and_admin):
    """Editor/admin can submit a document for review (draft -> review)."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "Submit for Review")

    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    result = r.json()
    state = result.get("current_state") or result.get("state") or result.get("status")
    assert state == "review"


def test_transition_review_to_approved(client, tenant_and_admin):
    """Admin can approve a document in review (review -> approved)."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "To Approve")

    # Move to review first
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Approve
    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "approved"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    result = r.json()
    state = result.get("current_state") or result.get("state") or result.get("status")
    assert state == "approved"


def test_transition_approved_to_published(client, tenant_and_admin):
    """Admin can publish an approved document (approved -> published)."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "To Publish")

    # draft -> review -> approved -> published
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "approved"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "published"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    result = r.json()
    state = result.get("current_state") or result.get("state") or result.get("status")
    assert state == "published"


def test_transition_review_to_rejected(client, tenant_and_admin):
    """Admin can reject a document in review."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "To Reject")

    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "rejected", "comment": "Needs more detail"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    result = r.json()
    state = result.get("current_state") or result.get("state") or result.get("status")
    assert state == "rejected"


def test_transition_rejected_back_to_draft(client, tenant_and_admin):
    """Rejected document can be resubmitted by moving back to draft."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "Resubmit Doc")

    # draft -> review -> rejected -> draft
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "rejected"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "draft"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    result = r.json()
    state = result.get("current_state") or result.get("state") or result.get("status")
    assert state == "draft"


# ---------------------------------------------------------------------------
# 5. Transition guards and invalid transitions
# ---------------------------------------------------------------------------

def test_invalid_transition_returns_error(client, tenant_and_admin):
    """Cannot skip states (e.g., draft -> published directly)."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "Skip States")

    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "published"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Should return 400 or 409 for invalid transition
    assert r.status_code in (400, 409, 422)


def test_guard_draft_to_review_requires_content(client, tenant_and_admin):
    """Guard: draft -> review requires non-empty title and content."""
    tenant_id, _, token = tenant_and_admin
    # Create document with empty content
    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "Empty Content", "content": "", "tags": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    doc = r.json()

    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Should fail due to guard
    assert r.status_code in (400, 409, 422)


def test_guard_review_to_approved_requires_tags(client, tenant_and_admin):
    """Guard: review -> approved requires at least one tag."""
    tenant_id, _, token = tenant_and_admin
    # Create document without tags
    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "No Tags Doc", "content": "Has content", "tags": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    doc = r.json()

    # Move to review (should work since has title and content)
    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)

    # Try to approve — should fail because no tags
    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "approved"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (400, 409, 422)


def test_viewer_cannot_trigger_transition(client, tenant_and_admin):
    """Viewer role cannot trigger any transition."""
    tenant_id, _, admin_token = tenant_and_admin
    # Create viewer
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "wf_viewer", "email": "wfv@wf.com", "role": "viewer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 201)
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)

    doc = _create_doc_for_workflow(client, tenant_id, admin_token, "Viewer Block")

    r = client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 6. Transition history
# ---------------------------------------------------------------------------

def test_transition_history_recorded(client, tenant_and_admin):
    """Transitions are recorded in the document's history."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "History Doc")

    # Perform a transition
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review", "comment": "Ready for review"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Get history
    r = client.get(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    history = r.json()
    if isinstance(history, dict):
        history = history.get("transitions") or history.get("items") or history.get("data", [])
    assert len(history) >= 1
    entry = history[0] if history else {}
    # Should have from_state and to_state
    assert "from_state" in entry or "from" in entry
    assert "to_state" in entry or "to" in entry


def test_available_transitions(client, tenant_and_admin):
    """GET available transitions returns valid next states."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "Available Trans Doc")

    r = client.get(
        f"/tenants/{tenant_id}/documents/{doc['id']}/available-transitions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    # Should contain at least "review" since doc is in draft
    transitions = body if isinstance(body, list) else body.get("transitions", body.get("states", []))
    # Flatten if items are dicts
    if transitions and isinstance(transitions[0], dict):
        states = [t.get("to_state") or t.get("state") or t.get("to") for t in transitions]
    else:
        states = transitions
    assert "review" in states


# ---------------------------------------------------------------------------
# 7. Notifications
# ---------------------------------------------------------------------------

def test_notifications_created_on_transition(client, tenant_and_admin):
    """Transitioning a document creates notifications."""
    tenant_id, admin_user, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "Notify Doc")

    # Transition to review — should notify admins
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Check notifications for the admin
    r = client.get(
        f"/tenants/{tenant_id}/notifications",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    notifications = body if isinstance(body, list) else body.get("notifications") or body.get("items") or body.get("data", [])
    assert len(notifications) >= 1


def test_mark_notification_as_read(client, tenant_and_admin):
    """Can mark a notification as read."""
    tenant_id, _, token = tenant_and_admin

    # Get existing notifications
    r = client.get(
        f"/tenants/{tenant_id}/notifications",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    notifications = body if isinstance(body, list) else body.get("notifications") or body.get("items") or body.get("data", [])

    if not notifications:
        pytest.skip("No notifications to mark as read")

    notif_id = notifications[0]["id"]

    r = client.put(
        f"/tenants/{tenant_id}/notifications/{notif_id}/read",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200





# ---------------------------------------------------------------------------
# 8. Bulk operations
# ---------------------------------------------------------------------------

def test_bulk_transition_success(client, tenant_and_admin):
    """Bulk transition moves multiple documents successfully."""
    tenant_id, _, token = tenant_and_admin
    # Create multiple documents
    doc_ids = []
    for i in range(3):
        doc = _create_doc_for_workflow(client, tenant_id, token, f"Bulk Doc {i}")
        doc_ids.append(doc["id"])

    # Bulk transition draft -> review
    r = client.post(
        f"/tenants/{tenant_id}/documents/bulk-transition",
        json={"document_ids": doc_ids, "to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    results = body.get("results") or body
    assert len(results) == 3
    for result in results:
        assert result.get("success") is True or result.get("status") == "success"


def test_bulk_transition_partial_failure(client, tenant_and_admin):
    """Bulk transition handles partial failures gracefully."""
    tenant_id, _, token = tenant_and_admin
    # Create one valid doc and use one non-existent ID
    doc = _create_doc_for_workflow(client, tenant_id, token, "Bulk Partial")
    doc_ids = [doc["id"], "nonexistent-doc-id-99999"]

    r = client.post(
        f"/tenants/{tenant_id}/documents/bulk-transition",
        json={"document_ids": doc_ids, "to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    results = body.get("results") or body
    assert len(results) == 2

    # At least one should succeed and one should fail
    successes = [res for res in results if res.get("success") is True or res.get("status") == "success"]
    failures = [res for res in results if res.get("success") is False or res.get("status") in ("failed", "error")]
    assert len(successes) >= 1
    assert len(failures) >= 1





# ---------------------------------------------------------------------------
# 9. SLA / Deadline tracking
# ---------------------------------------------------------------------------

def test_set_deadline(client, tenant_and_admin):
    """Admin can set a review deadline on a document."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "Deadline Doc")

    future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    r = client.put(
        f"/tenants/{tenant_id}/documents/{doc['id']}/deadline",
        json={"deadline": future},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_overdue_documents_endpoint(client, tenant_and_admin):
    """GET overdue documents returns docs past their deadline in review state."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "Overdue Doc")

    # Move to review
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Set a deadline in the past
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    client.put(
        f"/tenants/{tenant_id}/documents/{doc['id']}/deadline",
        json={"deadline": past},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Get overdue documents
    r = client.get(
        f"/tenants/{tenant_id}/documents/overdue",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    overdue = body if isinstance(body, list) else body.get("documents") or body.get("items") or body.get("data", [])
    assert len(overdue) >= 1
    # The overdue doc should be in the list
    overdue_ids = [d.get("id") for d in overdue]
    assert doc["id"] in overdue_ids


# ---------------------------------------------------------------------------
# 10. Webhooks
# ---------------------------------------------------------------------------

def test_register_webhook(client, tenant_and_admin):
    """Admin can register a webhook."""
    tenant_id, _, token = tenant_and_admin
    r = client.post(
        f"/tenants/{tenant_id}/webhooks",
        json={
            "url": "https://example.com/webhook",
            "events": ["document.transition", "document.created"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    webhook = r.json()
    assert "id" in webhook
    assert webhook.get("url") == "https://example.com/webhook"


def test_list_webhooks(client, tenant_and_admin):
    """Can list registered webhooks."""
    tenant_id, _, token = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}/webhooks",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    webhooks = body if isinstance(body, list) else body.get("webhooks") or body.get("items") or body.get("data", [])
    assert len(webhooks) >= 1


def test_webhook_delivery_logged(client, tenant_and_admin):
    """Webhook deliveries are logged when transitions occur."""
    tenant_id, _, token = tenant_and_admin

    # Register a webhook for transitions
    r = client.post(
        f"/tenants/{tenant_id}/webhooks",
        json={
            "url": "https://example.com/hook-delivery",
            "events": ["document.transition"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    webhook = r.json()
    webhook_id = webhook["id"]

    # Create doc and do a transition
    doc = _create_doc_for_workflow(client, tenant_id, token, "Webhook Trigger Doc")
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Check delivery log
    r = client.get(
        f"/tenants/{tenant_id}/webhooks/{webhook_id}/deliveries",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    deliveries = body if isinstance(body, list) else body.get("deliveries") or body.get("items") or body.get("data", [])
    assert len(deliveries) >= 1
    delivery = deliveries[0]
    assert "status" in delivery or "state" in delivery
    assert "payload" in delivery or "body" in delivery


def test_deactivate_webhook(client, tenant_and_admin):
    """Admin can deactivate (delete) a webhook."""
    tenant_id, _, token = tenant_and_admin
    # Create a webhook to deactivate
    r = client.post(
        f"/tenants/{tenant_id}/webhooks",
        json={"url": "https://example.com/to-delete", "events": ["document.created"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    webhook_id = r.json()["id"]

    r = client.delete(
        f"/tenants/{tenant_id}/webhooks/{webhook_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 204)


# ---------------------------------------------------------------------------
# 11. Tenant isolation
# ---------------------------------------------------------------------------

def test_tenant_isolation_documents(client, tenant_and_admin):
    """User from tenant A cannot access tenant B's documents."""
    tenant_id_a, _, token_a = tenant_and_admin

    # Create tenant B
    r = client.post("/tenants", json={"name": "IsolatedCorp"})
    tenant_id_b = r.json()["id"]

    r = client.get(
        f"/tenants/{tenant_id_b}/documents",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r.status_code == 403


def test_tenant_isolation_transitions(client, tenant_and_admin):
    """User from tenant A cannot trigger transitions in tenant B."""
    tenant_id_a, _, token_a = tenant_and_admin

    # Create tenant B with a user and document
    r = client.post("/tenants", json={"name": "OtherWorkflow"})
    tenant_id_b = r.json()["id"]
    r = client.post(
        f"/tenants/{tenant_id_b}/users",
        json={"username": "b_admin", "email": "b@other.com", "role": "admin"},
    )
    assert r.status_code in (200, 201)
    b_admin = r.json()
    b_token = _make_token(b_admin["id"], tenant_id_b)

    # Create document in tenant B
    r = client.post(
        f"/tenants/{tenant_id_b}/documents",
        json={"title": "B Doc", "content": "Isolated", "tags": ["b"]},
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert r.status_code in (200, 201)
    doc_b = r.json()

    # Tenant A tries to transition tenant B's document
    r = client.post(
        f"/tenants/{tenant_id_b}/documents/{doc_b['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 12. Authentication
# ---------------------------------------------------------------------------

def test_unauthenticated_request_returns_401(client, tenant_and_admin):
    """Requests without auth token return 401."""
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/documents")
    assert r.status_code == 401


def test_invalid_token_returns_401(client, tenant_and_admin):
    """Invalid bearer token returns 401."""
    tenant_id, _, _ = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}/documents",
        headers={"Authorization": "Bearer invalidtoken123"},
    )
    assert r.status_code == 401


def test_auth_token_endpoint(client, tenant_and_admin):
    """POST /auth/token returns a token for valid credentials."""
    tenant_id, admin_user, _ = tenant_and_admin
    r = client.post("/auth/token", json={
        "username": admin_user["username"],
        "tenant_id": tenant_id,
    })
    assert r.status_code == 200
    body = r.json()
    assert "token" in body or "access_token" in body


# ---------------------------------------------------------------------------
# 13. Audit logging
# ---------------------------------------------------------------------------

def test_audit_log_records_transitions(client, tenant_and_admin):
    """Workflow transitions create audit log entries."""
    tenant_id, _, token = tenant_and_admin
    doc = _create_doc_for_workflow(client, tenant_id, token, "Audit Trans Doc")
    client.post(
        f"/tenants/{tenant_id}/documents/{doc['id']}/transitions",
        json={"to_state": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )

    r = client.get(
        f"/tenants/{tenant_id}/audit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    entries = body if isinstance(body, list) else body.get("entries") or body.get("items") or body.get("data", [])
    assert len(entries) >= 1
    entry = entries[0]
    assert "action" in entry or "event" in entry
    assert "timestamp" in entry or "created_at" in entry


def test_audit_log_admin_only(client, tenant_and_admin):
    """Only admins can access the audit log."""
    tenant_id, _, admin_token = tenant_and_admin
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "audit_blocked", "email": "ab@wf.com", "role": "viewer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    if r.status_code not in (200, 201):
        pytest.skip("Could not create viewer for audit test")
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)

    r = client.get(
        f"/tenants/{tenant_id}/audit",
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 14. Middleware
# ---------------------------------------------------------------------------

def test_request_id_header(client, tenant_and_admin):
    """Responses include an X-Request-ID header."""
    tenant_id, _, token = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}/documents",
        headers={"Authorization": f"Bearer {token}"},
    )
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    assert "x-request-id" in headers_lower, \
        f"No X-Request-ID header found. Headers: {dict(r.headers)}"


def test_not_found_returns_404(client, tenant_and_admin):
    """Accessing nonexistent document returns 404."""
    tenant_id, _, token = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}/documents/nonexistent-id-xyz",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404



