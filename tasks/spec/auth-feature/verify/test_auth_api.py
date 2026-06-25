"""
Harness-owned functional tests for task-010-auth-feature.

Environment-hardened: no framework or environment assumption should cause a
false failure. Every known variation in how models implement the auth API is
handled gracefully.

Robustness notes
----------------
- Login: tries JSON body first ({email, password}), falls back to form-encoded
  ({username, password}) for models using OAuth2PasswordRequestForm.
- App discovery: tries 9 entry-point patterns; surfaces real import errors
  instead of swallowing them so failures are diagnosable.
- User seeding: logs what happened; never silently hides registration errors.
- All assertions include the actual response body in the failure message.
"""

import os
import secrets
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Generate deterministic test credentials from a fixed seed so they are
# reproducible across runs but not stored as plaintext literals in source.
_SEED = b"agent-cost-bench-auth-feature-test-fixture"
TEST_EMAIL = "testuser@example.com"
TEST_PASSWORD = hashlib.sha256(_SEED + b":password").hexdigest()[:16] + "!A1"
FRESH_EMAIL = "fresh_harness_user@example.com"


# ---------------------------------------------------------------------------
# App discovery — surface real errors, try all common entry-point patterns
# ---------------------------------------------------------------------------

def _workspace_root() -> Path:
    """The model's workspace. score.py sets WORKSPACE and cd's into it."""
    return Path(os.environ.get("WORKSPACE", os.getcwd())).resolve()


def _is_inside_workspace(mod, workspace: Path) -> bool:
    """
    True only if the module's source file lives inside the model's workspace.

    This rejects modules that resolve to unrelated projects leaked onto
    sys.path via stray editable-install .pth files (e.g. a global `main`
    module from another repo), which would otherwise shadow the model's app
    and make every endpoint return 404.
    """
    mod_file = getattr(mod, "__file__", None)
    if not mod_file:
        return False  # namespace package / builtin — never the model's app
    try:
        return Path(mod_file).resolve().is_relative_to(workspace)
    except (ValueError, OSError):
        return False


def _find_app():
    import importlib
    import sys

    workspace = _workspace_root()
    import_errors = []

    # Clear any previously cached modules from earlier test runs to prevent
    # one model's app leaking into another's evaluation (sys.modules pollution),
    # and to drop any leaked external module (e.g. a global `main`) cached by a
    # prior import so a workspace-local candidate can be resolved cleanly.
    cached = [k for k in list(sys.modules) if k in {
        "main", "app", "app.main", "src", "src.main",
        "application", "server", "api", "api.main",
    } or k.startswith((
        "app.", "src.", "api.", "routers.", "models.",
        "schemas.", "core.", "auth.", "db.", "config.", "dependencies.",
        "services.", "repositories.", "utils.", "middleware.",
    ))]
    for key in cached:
        del sys.modules[key]

    # Make sure the workspace wins over anything else on sys.path.
    ws = str(workspace)
    if ws in sys.path:
        sys.path.remove(ws)
    sys.path.insert(0, ws)

    for module_name, attr in [
        ("main", "app"),
        ("app.main", "app"),
        ("src.main", "app"),
        ("application", "app"),
        ("main", "application"),
        ("app", "app"),
        ("server", "app"),
        ("api.main", "app"),
        ("api", "app"),
    ]:
        try:
            mod = importlib.import_module(module_name)
            if not _is_inside_workspace(mod, workspace):
                # Leaked/external module shadowing the model's code — drop it
                # from the cache and keep searching workspace-local candidates.
                import_errors.append(
                    f"{module_name}: ignored (resolved outside workspace: "
                    f"{getattr(mod, '__file__', '?')})"
                )
                sys.modules.pop(module_name, None)
                continue
            app_obj = getattr(mod, attr, None)
            if app_obj is not None:
                return app_obj
        except (ImportError, ModuleNotFoundError) as e:
            import_errors.append(f"{module_name}: {e}")
        except SystemExit:
            pass  # Some apps call sys.exit() on import if config is missing
        except Exception as e:
            # Surface non-import errors (config errors, etc.) in the message
            import_errors.append(f"{module_name}: {type(e).__name__}: {e}")

    tried = ["main.app", "app.main.app", "src.main.app", "application.app",
             "main.application", "app.app", "server.app", "api.main.app", "api.app"]
    raise RuntimeError(
        f"Could not find a FastAPI 'app' in any of: {', '.join(tried)}. "
        f"Import errors: {'; '.join(import_errors) or 'none'}"
    )


@pytest.fixture(scope="module")
def client():
    app = _find_app()
    # Use TestClient as a context manager so lifespan events fire.
    # This ensures database tables are created (via on_startup / lifespan)
    # before any test runs. Without this, apps using SQLAlchemy + lifespan
    # will boot but have no tables, causing all endpoint calls to fail.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Login helper — handles both JSON and form-encoded login
# ---------------------------------------------------------------------------

def _login(client, email=TEST_EMAIL, password=TEST_PASSWORD):
    """
    Try JSON body first ({email, password}), then OAuth2 form encoding
    ({username, password}) for models using OAuth2PasswordRequestForm.
    Returns whichever gets a non-422 response, or the last response.
    """
    # Attempt 1: JSON with email field
    r = client.post("/auth/login", json={"email": email, "password": password})
    if r.status_code != 422:
        return r

    # Attempt 2: JSON with username field (some models use username instead of email)
    r = client.post("/auth/login", json={"username": email, "password": password})
    if r.status_code != 422:
        return r

    # Attempt 3: OAuth2 form-encoded (username + password as form fields)
    r = client.post(
        "/auth/token",  # OAuth2 standard endpoint
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 422 and r.status_code != 404:
        return r

    # Attempt 4: form-encoded on /auth/login
    r = client.post(
        "/auth/login",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return r


# ---------------------------------------------------------------------------
# User seeding — register via the API (required by spec)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def seed_test_user(client):
    """
    Register the test user through POST /auth/register before any test runs.
    Logs what happened so failures downstream are diagnosable.
    409 (already exists) is acceptable — user is already there.
    """
    last_status = None
    last_body = None

    for path in ["/auth/register", "/register", "/auth/signup", "/users"]:
        try:
            r = client.post(path, json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
            last_status = r.status_code
            last_body = r.text[:200]
            if r.status_code in (200, 201, 409):
                return  # success or already exists — either is fine
        except Exception as e:
            last_body = str(e)

    # None of the paths worked — print a warning but don't crash the suite
    print(
        f"\n[harness] WARNING: seed_test_user could not create test user. "
        f"Last response: {last_status} {last_body}. "
        "Login tests will show the real failure reason."
    )


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

def test_register_new_user(client):
    """POST /auth/register with a fresh email should return 200 or 201."""
    r = client.post(
        "/auth/register",
        json={"email": FRESH_EMAIL, "password": TEST_PASSWORD},
    )
    assert r.status_code in (200, 201), (
        f"Expected 200/201 on register, got {r.status_code}: {r.text[:300]}"
    )


def test_register_duplicate_email_conflicts(client):
    """Registering the already-seeded email again should return 409."""
    # Ensure the user exists first (seeding may have used a different path)
    client.post("/auth/register", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
    # Now try again — must conflict
    r = client.post(
        "/auth/register",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert r.status_code == 409, (
        f"Expected 409 for duplicate email, got {r.status_code}: {r.text[:200]}"
    )


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------

def test_login_valid_credentials(client):
    r = _login(client)
    assert r.status_code == 200, (
        f"Login failed with {r.status_code}: {r.text[:300]}"
    )
    body = r.json()
    # Accept access_token or token (some models return {"token": ...})
    has_access = "access_token" in body or "token" in body
    has_refresh = "refresh_token" in body
    assert has_access, f"Response missing access_token: {body}"
    assert has_refresh, f"Response missing refresh_token: {body}"


def test_login_returns_non_empty_tokens(client):
    body = _login(client).json()
    access = body.get("access_token") or body.get("token")
    assert access, f"access_token is empty in: {body}"
    assert body.get("refresh_token"), f"refresh_token is empty in: {body}"


def test_login_wrong_password_returns_401(client):
    r = _login(client, password="definitely_wrong_password_xyz")
    assert r.status_code == 401, (
        f"Expected 401 for wrong password, got {r.status_code}: {r.text[:200]}"
    )


def test_login_unknown_email_returns_401(client):
    r = _login(client, email="nobody_harness@example.com")
    assert r.status_code == 401, (
        f"Expected 401 for unknown email, got {r.status_code}: {r.text[:200]}"
    )


# ---------------------------------------------------------------------------
# /auth/me tests
# ---------------------------------------------------------------------------

def _get_access_token(client):
    """Return the access token string, handling both field names."""
    body = _login(client).json()
    return body.get("access_token") or body.get("token")


def test_me_with_valid_token(client):
    access = _get_access_token(client)
    assert access, "Could not get access token from login"
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, (
        f"Expected 200 on /auth/me, got {r.status_code}: {r.text[:300]}"
    )


def test_me_without_auth_returns_401(client):
    r = client.get("/auth/me")
    assert r.status_code == 401, (
        f"Expected 401 with no auth, got {r.status_code}"
    )


def test_me_with_bad_token_returns_401(client):
    r = client.get("/auth/me", headers={"Authorization": "Bearer not.a.real.token"})
    assert r.status_code == 401, (
        f"Expected 401 with bad token, got {r.status_code}"
    )


def test_me_returns_email(client):
    access = _get_access_token(client)
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200
    body = r.json()
    # Email should appear in one of the response fields
    assert any(TEST_EMAIL in str(v) for v in body.values()), (
        f"Expected {TEST_EMAIL!r} in profile response, got: {body}"
    )


def test_me_never_returns_password(client):
    access = _get_access_token(client)
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200
    body = r.json()
    assert not any(TEST_PASSWORD in str(v) for v in body.values()), (
        f"Raw password leaked in /auth/me response: {body}"
    )


# ---------------------------------------------------------------------------
# Refresh tests
# ---------------------------------------------------------------------------

def test_refresh_valid_token_returns_new_access_token(client):
    tokens = _login(client).json()
    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200, (
        f"Refresh failed: {r.status_code} {r.text[:200]}"
    )
    body = r.json()
    assert "access_token" in body or "token" in body, (
        f"Response missing access_token: {body}"
    )


def test_refresh_new_token_is_usable(client):
    tokens = _login(client).json()
    new_resp = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert new_resp.status_code == 200, f"Refresh failed: {new_resp.text}"
    new_tokens = new_resp.json()
    new_access = new_tokens.get("access_token") or new_tokens.get("token")
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert r.status_code == 200, f"New access token rejected: {r.text}"


def test_refresh_invalid_token_returns_401(client):
    r = client.post("/auth/refresh", json={"refresh_token": "not.a.real.refresh.token"})
    assert r.status_code == 401, (
        f"Expected 401 for invalid refresh token, got {r.status_code}: {r.text[:200]}"
    )


# ---------------------------------------------------------------------------
# Logout tests
# ---------------------------------------------------------------------------

def test_logout_returns_success(client):
    tokens = _login(client).json()
    r = client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code in (200, 204), (
        f"Logout failed: {r.status_code} {r.text[:200]}"
    )


def test_logout_invalidates_refresh_token(client):
    tokens = _login(client).json()
    refresh = tokens["refresh_token"]
    client.post("/auth/logout", json={"refresh_token": refresh})
    r = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401, (
        f"Revoked refresh token was accepted (expected 401, got {r.status_code})"
    )
