"""
Harness-owned functional tests for task-002-rest-api.

These tests are written by the benchmark harness and are NEVER shown to the
model. They are copied into the workspace at evaluation time and run against
whatever implementation the model produced.

The model is expected to implement a FastAPI Todo CRUD API in main.py with:
  GET    /todos          — list all todos
  POST   /todos          — create a todo {title, done}
  GET    /todos/{id}     — get one todo
  PUT    /todos/{id}     — update a todo
  DELETE /todos/{id}     — delete a todo

Each todo has: id (int, auto-increment), title (str), done (bool, default False)
"""

import pytest
from fastapi.testclient import TestClient


def get_client():
    """Import the model's app. Try common entry-point patterns."""
    import importlib
    for module_name, attr in [
        ("main", "app"),
        ("app.main", "app"),
        ("src.main", "app"),
        ("application", "app"),
        ("main", "application"),
    ]:
        try:
            mod = importlib.import_module(module_name)
            app = getattr(mod, attr, None)
            if app is not None:
                return TestClient(app)
        except (ImportError, ModuleNotFoundError):
            continue
    raise RuntimeError(
        "Could not find a FastAPI app. Tried: main.app, app.main.app, "
        "src.main.app, application.app, main.application"
    )


@pytest.fixture(scope="module")
def client():
    return get_client()


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def test_list_empty_initially(client):
    """GET /todos returns an empty list on a fresh app."""
    r = client.get("/todos")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def test_create_returns_201_or_200(client):
    r = client.post("/todos", json={"title": "Buy milk", "done": False})
    assert r.status_code in (200, 201)
    body = r.json()
    assert "id" in body
    assert body["title"] == "Buy milk"
    assert body["done"] is False


def test_create_done_defaults_to_false(client):
    r = client.post("/todos", json={"title": "No done field"})
    assert r.status_code in (200, 201)
    assert r.json()["done"] is False


def test_create_with_done_true(client):
    r = client.post("/todos", json={"title": "Already done", "done": True})
    assert r.status_code in (200, 201)
    assert r.json()["done"] is True


def test_create_increments_id(client):
    r1 = client.post("/todos", json={"title": "First"})
    r2 = client.post("/todos", json={"title": "Second"})
    assert r1.status_code in (200, 201)
    assert r2.status_code in (200, 201)
    assert r2.json()["id"] != r1.json()["id"]


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------

def test_get_existing_todo(client):
    created = client.post("/todos", json={"title": "Get me", "done": False}).json()
    r = client.get(f"/todos/{created['id']}")
    assert r.status_code == 200
    assert r.json()["title"] == "Get me"


def test_get_nonexistent_returns_404(client):
    r = client.get("/todos/999999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def test_update_title(client):
    created = client.post("/todos", json={"title": "Old title", "done": False}).json()
    r = client.put(f"/todos/{created['id']}", json={"title": "New title", "done": False})
    assert r.status_code == 200
    assert r.json()["title"] == "New title"


def test_update_done_flag(client):
    created = client.post("/todos", json={"title": "Mark done", "done": False}).json()
    r = client.put(f"/todos/{created['id']}", json={"title": "Mark done", "done": True})
    assert r.status_code == 200
    assert r.json()["done"] is True


def test_update_nonexistent_returns_404(client):
    r = client.put("/todos/999999", json={"title": "Ghost", "done": False})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_existing(client):
    created = client.post("/todos", json={"title": "Delete me", "done": False}).json()
    r = client.delete(f"/todos/{created['id']}")
    assert r.status_code in (200, 204)


def test_delete_removes_from_list(client):
    created = client.post("/todos", json={"title": "Gone soon", "done": False}).json()
    client.delete(f"/todos/{created['id']}")
    r = client.get(f"/todos/{created['id']}")
    assert r.status_code == 404


def test_delete_nonexistent_returns_404(client):
    r = client.delete("/todos/999999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# List after mutations
# ---------------------------------------------------------------------------

def test_list_contains_created_items(client):
    r1 = client.post("/todos", json={"title": "List item A"})
    r2 = client.post("/todos", json={"title": "List item B"})
    ids = {r1.json()["id"], r2.json()["id"]}
    listed = {t["id"] for t in client.get("/todos").json()}
    assert ids.issubset(listed)


def test_list_excludes_deleted_items(client):
    created = client.post("/todos", json={"title": "Will be deleted"}).json()
    client.delete(f"/todos/{created['id']}")
    listed_ids = {t["id"] for t in client.get("/todos").json()}
    assert created["id"] not in listed_ids
