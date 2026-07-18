"""
Harness tests for multitenant-rbac-api.

Tests the multi-tenant REST API with RBAC, pagination, rate limiting,
and audit logging.

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
    r = client.post("/tenants", json={"name": "TestCorp"})
    assert r.status_code in (200, 201), f"Failed to create tenant: {r.text}"
    tenant = r.json()
    tenant_id = tenant["id"]

    # Create admin user — per the prompt, first user in a tenant can be created
    # without auth (bootstrap). Try without auth first.
    admin_data = {
        "username": "admin1",
        "email": "admin1@testcorp.com",
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


def test_rate_limiter_py_exists():
    assert (WS / "rate_limiter.py").exists(), "rate_limiter.py not produced"


def test_requirements_txt_exists():
    assert (WS / "requirements.txt").exists(), "requirements.txt not produced"


# ---------------------------------------------------------------------------
# 2. Tenant management
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


# ---------------------------------------------------------------------------
# 3. User management (admin only)
# ---------------------------------------------------------------------------

def test_create_user_as_admin(client, tenant_and_admin):
    """Admin can create a new user in their tenant."""
    tenant_id, _, token = tenant_and_admin
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "editor1", "email": "editor1@test.com", "role": "editor"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    assert r.json()["role"] == "editor"


def test_create_user_as_viewer_forbidden(client, tenant_and_admin):
    """Viewer cannot create users."""
    tenant_id, admin_user, admin_token = tenant_and_admin
    # First create a viewer
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "viewer1", "email": "viewer1@test.com", "role": "viewer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 201)
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)

    # Viewer tries to create user
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "hacker", "email": "h@test.com", "role": "viewer"},
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert r.status_code == 403


def test_list_users_as_admin(client, tenant_and_admin):
    """Admin can list users in their tenant."""
    tenant_id, _, token = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) >= 1


# ---------------------------------------------------------------------------
# 4. Document CRUD with RBAC
# ---------------------------------------------------------------------------

def test_create_document_as_editor(client, tenant_and_admin):
    """Editor can create documents."""
    tenant_id, _, admin_token = tenant_and_admin
    # Create an editor user
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "doc_editor", "email": "de@test.com", "role": "editor"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 201)
    editor = r.json()
    editor_token = _make_token(editor["id"], tenant_id)

    # Editor creates a document
    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "My Doc", "content": "Hello world", "tags": ["test"]},
        headers={"Authorization": f"Bearer {editor_token}"},
    )
    assert r.status_code in (200, 201)
    doc = r.json()
    assert doc["title"] == "My Doc"
    assert "id" in doc


def test_create_document_as_viewer_forbidden(client, tenant_and_admin):
    """Viewer cannot create documents."""
    tenant_id, _, admin_token = tenant_and_admin
    # Create a viewer
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "doc_viewer", "email": "dv@test.com", "role": "viewer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 201)
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)

    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "Forbidden", "content": "No", "tags": []},
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert r.status_code == 403


def test_list_documents(client, tenant_and_admin):
    """All authenticated users can list documents in their tenant."""
    tenant_id, _, token = tenant_and_admin
    # Create a document first
    client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "Listed Doc", "content": "Content", "tags": ["listed"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = client.get(
        f"/tenants/{tenant_id}/documents",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    # Response should have documents (as list or in a wrapper with items key)
    if isinstance(body, list):
        assert len(body) >= 1
    elif isinstance(body, dict):
        items = body.get("items") or body.get("documents") or body.get("data", [])
        assert len(items) >= 1


def test_get_document(client, tenant_and_admin):
    """Can retrieve a specific document by ID."""
    tenant_id, _, token = tenant_and_admin
    # Create document
    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "Fetchable", "content": "Get me", "tags": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 201)
    doc_id = r.json()["id"]

    r = client.get(
        f"/tenants/{tenant_id}/documents/{doc_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Fetchable"


def test_update_document_as_admin(client, tenant_and_admin):
    """Admin can update any document."""
    tenant_id, _, token = tenant_and_admin
    # Create document
    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "Original", "content": "Old content", "tags": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    doc_id = r.json()["id"]

    r = client.put(
        f"/tenants/{tenant_id}/documents/{doc_id}",
        json={"title": "Updated", "content": "New content", "tags": ["updated"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Updated"


def test_delete_document_archives_it(client, tenant_and_admin):
    """DELETE soft-deletes (archives) the document (admin only)."""
    tenant_id, _, token = tenant_and_admin
    r = client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "To Archive", "content": "Bye", "tags": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    doc_id = r.json()["id"]

    r = client.delete(
        f"/tenants/{tenant_id}/documents/{doc_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (200, 204)

    # Verify it is archived, not truly deleted
    r = client.get(
        f"/tenants/{tenant_id}/documents/{doc_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code == 200:
        assert r.json().get("is_archived") is True


# ---------------------------------------------------------------------------
# 5. Tenant isolation
# ---------------------------------------------------------------------------

def test_tenant_isolation_documents(client, tenant_and_admin):
    """User from tenant A cannot access tenant B's documents."""
    tenant_id_a, _, token_a = tenant_and_admin

    # Create tenant B
    r = client.post("/tenants", json={"name": "OtherCorp"})
    tenant_id_b = r.json()["id"]

    # Try to access tenant B's documents with tenant A's token
    r = client.get(
        f"/tenants/{tenant_id_b}/documents",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r.status_code == 403, \
        f"Expected 403 for cross-tenant access, got {r.status_code}"


def test_tenant_isolation_users(client, tenant_and_admin):
    """User from tenant A cannot list tenant B's users."""
    tenant_id_a, _, token_a = tenant_and_admin

    # Create tenant B
    r = client.post("/tenants", json={"name": "IsolatedCorp"})
    tenant_id_b = r.json()["id"]

    r = client.get(
        f"/tenants/{tenant_id_b}/users",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 6. Cursor-based pagination
# ---------------------------------------------------------------------------

def test_pagination_limit(client, tenant_and_admin):
    """Pagination respects limit parameter."""
    tenant_id, _, token = tenant_and_admin

    # Create several documents
    for i in range(5):
        client.post(
            f"/tenants/{tenant_id}/documents",
            json={"title": f"Page Doc {i}", "content": f"Content {i}", "tags": ["paginated"]},
            headers={"Authorization": f"Bearer {token}"},
        )

    r = client.get(
        f"/tenants/{tenant_id}/documents?limit=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    if isinstance(body, dict):
        items = body.get("items") or body.get("documents") or body.get("data", [])
    else:
        items = body
    assert len(items) <= 2


def test_pagination_cursor(client, tenant_and_admin):
    """Pagination with cursor returns next page of results."""
    tenant_id, _, token = tenant_and_admin

    # Get first page
    r = client.get(
        f"/tenants/{tenant_id}/documents?limit=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r.json()
    if isinstance(body, dict):
        cursor = body.get("next_cursor") or body.get("cursor") or body.get("next")
        if cursor:
            # Get next page
            r2 = client.get(
                f"/tenants/{tenant_id}/documents?limit=2&cursor={cursor}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r2.status_code == 200


# ---------------------------------------------------------------------------
# 7. Authentication and Authorization
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
# 8. Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limit_headers_present(client, tenant_and_admin):
    """Responses include rate limit headers."""
    tenant_id, _, token = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}/documents",
        headers={"Authorization": f"Bearer {token}"},
    )
    # Check for rate limit headers (case-insensitive)
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    has_limit = (
        "x-ratelimit-limit" in headers_lower
        or "x-rate-limit-limit" in headers_lower
        or "ratelimit-limit" in headers_lower
    )
    has_remaining = (
        "x-ratelimit-remaining" in headers_lower
        or "x-rate-limit-remaining" in headers_lower
        or "ratelimit-remaining" in headers_lower
    )
    assert has_limit or has_remaining, \
        f"No rate limit headers found. Headers: {dict(r.headers)}"


# ---------------------------------------------------------------------------
# 9. Audit logging
# ---------------------------------------------------------------------------

def test_audit_log_records_actions(client, tenant_and_admin):
    """Mutating operations create audit log entries."""
    tenant_id, _, token = tenant_and_admin

    # Perform a mutating action (create a document)
    client.post(
        f"/tenants/{tenant_id}/documents",
        json={"title": "Audited Doc", "content": "Audit me", "tags": ["audit"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Check audit log
    r = client.get(
        f"/tenants/{tenant_id}/audit",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    entries = body if isinstance(body, list) else body.get("entries", body.get("items", []))
    assert len(entries) >= 1

    # Verify audit entry has expected fields
    entry = entries[0]
    assert "action" in entry or "event" in entry
    assert "timestamp" in entry or "created_at" in entry


def test_audit_log_admin_only(client, tenant_and_admin):
    """Only admins can access the audit log."""
    tenant_id, _, admin_token = tenant_and_admin

    # Create a viewer
    r = client.post(
        f"/tenants/{tenant_id}/users",
        json={"username": "audit_viewer", "email": "av@test.com", "role": "viewer"},
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
# 10. Request ID middleware
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


# ---------------------------------------------------------------------------
# 11. Error handling
# ---------------------------------------------------------------------------

def test_not_found_returns_404(client, tenant_and_admin):
    """Accessing nonexistent document returns 404."""
    tenant_id, _, token = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}/documents/nonexistent-id-12345",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_error_response_format(client, tenant_and_admin):
    """Error responses have consistent JSON structure."""
    tenant_id, _, token = tenant_and_admin
    r = client.get(
        f"/tenants/{tenant_id}/documents/nonexistent-id-12345",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = r.json()
    # Should have detail/message field
    assert "detail" in body or "message" in body or "error" in body
