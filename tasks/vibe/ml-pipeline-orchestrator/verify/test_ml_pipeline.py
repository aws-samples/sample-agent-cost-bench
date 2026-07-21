"""
Harness tests for ml-pipeline-orchestrator.

Tests the multi-tenant ML pipeline orchestrator: dataset registry, feature store,
experiment tracking, model registry, serving, A/B testing, drift detection,
pipeline execution, and hyperparameter search.

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
    r = client.post("/tenants", json={"name": "MLCorp"})
    assert r.status_code in (200, 201), f"Failed to create tenant: {r.text}"
    tenant_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "admin1", "email": "admin1@mlcorp.com", "role": "admin"
    })
    assert r.status_code in (200, 201), f"Failed to create admin: {r.text}"
    admin = r.json()
    token = _make_token(admin["id"], tenant_id)
    return tenant_id, admin, token


@pytest.fixture(scope="module")
def scientist_token(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "scientist1", "email": "sci1@mlcorp.com", "role": "data_scientist"
    }, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code in (200, 201)
    user = r.json()
    return _make_token(user["id"], tenant_id)


# ---------------------------------------------------------------------------
# 1. File existence checks (14 files)
# ---------------------------------------------------------------------------

def test_models_py_exists():
    assert (WS / "models.py").exists()

def test_storage_py_exists():
    assert (WS / "storage.py").exists()

def test_auth_py_exists():
    assert (WS / "auth.py").exists()

def test_dataset_registry_py_exists():
    assert (WS / "dataset_registry.py").exists()

def test_feature_store_py_exists():
    assert (WS / "feature_store.py").exists()

def test_experiment_tracker_py_exists():
    assert (WS / "experiment_tracker.py").exists()

def test_model_registry_py_exists():
    assert (WS / "model_registry.py").exists()

def test_serving_py_exists():
    assert (WS / "serving.py").exists()

def test_drift_detector_py_exists():
    assert (WS / "drift_detector.py").exists()

def test_pipeline_py_exists():
    assert (WS / "pipeline.py").exists()

def test_hyperparameter_py_exists():
    assert (WS / "hyperparameter.py").exists()

def test_main_py_exists():
    assert (WS / "main.py").exists()

def test_middleware_py_exists():
    assert (WS / "middleware.py").exists()

def test_requirements_txt_exists():
    assert (WS / "requirements.txt").exists()


# ---------------------------------------------------------------------------
# 2. Tenant + Auth (4 tests)
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
    r = client.get(f"/tenants/{tenant_id}/datasets")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. Dataset Registry (5 tests)
# ---------------------------------------------------------------------------

def test_create_dataset(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "sales_data",
        "description": "Monthly sales data",
        "schema": {"amount": "float", "date": "datetime", "customer_id": "string"},
        "row_count": 10000,
        "size_bytes": 5242880,
        "source": "s3://bucket/sales.csv",
        "tags": ["sales", "monthly"]
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    ds = r.json()
    assert ds["name"] == "sales_data"
    assert "id" in ds


def test_list_datasets(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/datasets",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("datasets", []))
    assert len(items) >= 1


def test_get_dataset(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create dataset
    r = client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "get_test_ds",
        "description": "For get test",
        "schema": {"val": "int"},
        "row_count": 100,
        "size_bytes": 1024,
        "source": "local",
        "tags": []
    }, headers={"Authorization": f"Bearer {token}"})
    ds_id = r.json()["id"]
    r = client.get(f"/tenants/{tenant_id}/datasets/{ds_id}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["id"] == ds_id


def test_dataset_versions(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create same-name dataset twice for versioning
    for i in range(2):
        client.post(f"/tenants/{tenant_id}/datasets", json={
            "name": "versioned_ds",
            "description": f"Version {i+1}",
            "schema": {"col": "int"},
            "row_count": 100 * (i + 1),
            "size_bytes": 512,
            "source": "local",
            "tags": ["versioned"]
        }, headers={"Authorization": f"Bearer {token}"})
    # Get first dataset to retrieve its id for the versions endpoint
    r = client.get(f"/tenants/{tenant_id}/datasets",
                   headers={"Authorization": f"Bearer {token}"},
                   params={"tag": "versioned"})
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("datasets", []))
    ds_id = items[0]["id"]
    r = client.get(f"/tenants/{tenant_id}/datasets/{ds_id}/versions",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    versions = r.json()
    v_list = versions if isinstance(versions, list) else versions.get("items", versions.get("versions", []))
    assert len(v_list) >= 2


def test_deactivate_dataset(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "to_deactivate",
        "description": "Will be deactivated",
        "schema": {"x": "float"},
        "row_count": 50,
        "size_bytes": 256,
        "source": "local",
        "tags": []
    }, headers={"Authorization": f"Bearer {token}"})
    ds_id = r.json()["id"]
    r = client.delete(f"/tenants/{tenant_id}/datasets/{ds_id}",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("is_active") is False or body.get("status") == "inactive"


# ---------------------------------------------------------------------------
# 4. Feature Store (4 tests)
# ---------------------------------------------------------------------------

def _create_dataset_for_features(client, tenant_id, token):
    """Helper: create a dataset and return its id."""
    r = client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "feature_source_ds",
        "description": "Source for features",
        "schema": {"age": "int", "income": "float", "city": "string"},
        "row_count": 5000,
        "size_bytes": 2048,
        "source": "warehouse",
        "tags": ["features"]
    }, headers={"Authorization": f"Bearer {token}"})
    return r.json()["id"]


def test_create_feature(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id = _create_dataset_for_features(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/features", json={
        "name": "normalized_income",
        "description": "Log-transformed income",
        "feature_type": "numerical",
        "source_dataset_id": ds_id,
        "transformation": "log_transform"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    feat = r.json()
    assert feat["name"] == "normalized_income"
    assert "id" in feat


def test_list_features(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/features",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("features", []))
    assert len(items) >= 1


def test_get_features_by_dataset(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create a dataset and a feature for it
    ds_id = _create_dataset_for_features(client, tenant_id, token)
    client.post(f"/tenants/{tenant_id}/features", json={
        "name": "city_encoded",
        "description": "One-hot encoded city",
        "feature_type": "categorical",
        "source_dataset_id": ds_id,
        "transformation": "one_hot"
    }, headers={"Authorization": f"Bearer {token}"})
    r = client.get(f"/tenants/{tenant_id}/features",
                   params={"dataset_id": ds_id},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("features", []))
    assert len(items) >= 1


def test_compute_features(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id = _create_dataset_for_features(client, tenant_id, token)
    # Create a feature
    r = client.post(f"/tenants/{tenant_id}/features", json={
        "name": "age_normalized",
        "description": "Normalized age",
        "feature_type": "numerical",
        "source_dataset_id": ds_id,
        "transformation": "normalize"
    }, headers={"Authorization": f"Bearer {token}"})
    feat_id = r.json()["id"]
    # Compute
    r = client.post(f"/tenants/{tenant_id}/features/compute", json={
        "feature_ids": [feat_id],
        "dataset_id": ds_id
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "computed" or "feature_count" in body


# ---------------------------------------------------------------------------
# 5. Experiments (6 tests)
# ---------------------------------------------------------------------------

def _create_experiment_prereqs(client, tenant_id, token):
    """Create dataset + feature, return (dataset_id, feature_id)."""
    r = client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "exp_dataset",
        "description": "Dataset for experiments",
        "schema": {"x": "float", "y": "float", "label": "int"},
        "row_count": 2000,
        "size_bytes": 4096,
        "source": "gen",
        "tags": ["experiment"]
    }, headers={"Authorization": f"Bearer {token}"})
    ds_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/features", json={
        "name": "exp_feature_x",
        "description": "Feature x",
        "feature_type": "numerical",
        "source_dataset_id": ds_id,
        "transformation": "normalize"
    }, headers={"Authorization": f"Bearer {token}"})
    feat_id = r.json()["id"]
    return ds_id, feat_id


def test_create_experiment(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id, feat_id = _create_experiment_prereqs(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/experiments", json={
        "name": "baseline_rf",
        "description": "Random forest baseline",
        "dataset_id": ds_id,
        "feature_ids": [feat_id],
        "algorithm": "random_forest",
        "hyperparameters": {"n_estimators": 100, "max_depth": 5}
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    exp = r.json()
    assert exp["name"] == "baseline_rf"
    assert exp.get("status") == "created"
    assert "id" in exp


def test_run_experiment(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id, feat_id = _create_experiment_prereqs(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/experiments", json={
        "name": "run_test_exp",
        "description": "Experiment to run",
        "dataset_id": ds_id,
        "feature_ids": [feat_id],
        "algorithm": "xgboost",
        "hyperparameters": {"n_estimators": 50}
    }, headers={"Authorization": f"Bearer {token}"})
    exp_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/experiments/{exp_id}/run",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    exp = r.json()
    assert exp.get("status") in ("running", "completed")
    if exp.get("status") == "completed":
        assert "metrics" in exp
        assert "accuracy" in exp["metrics"]


def test_list_experiments(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/experiments",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("experiments", []))
    assert len(items) >= 1


def test_get_experiment(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id, feat_id = _create_experiment_prereqs(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/experiments", json={
        "name": "get_exp_test",
        "description": "Get test",
        "dataset_id": ds_id,
        "feature_ids": [feat_id],
        "algorithm": "linear_regression",
        "hyperparameters": {}
    }, headers={"Authorization": f"Bearer {token}"})
    exp_id = r.json()["id"]
    r = client.get(f"/tenants/{tenant_id}/experiments/{exp_id}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["id"] == exp_id


def test_compare_experiments(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id, feat_id = _create_experiment_prereqs(client, tenant_id, token)
    exp_ids = []
    for alg in ["random_forest", "xgboost"]:
        r = client.post(f"/tenants/{tenant_id}/experiments", json={
            "name": f"compare_{alg}",
            "description": f"Compare {alg}",
            "dataset_id": ds_id,
            "feature_ids": [feat_id],
            "algorithm": alg,
            "hyperparameters": {"n_estimators": 100}
        }, headers={"Authorization": f"Bearer {token}"})
        eid = r.json()["id"]
        # Run experiment
        client.post(f"/tenants/{tenant_id}/experiments/{eid}/run",
                    headers={"Authorization": f"Bearer {token}"})
        exp_ids.append(eid)
    r = client.post(f"/tenants/{tenant_id}/experiments/compare",
                    json={"experiment_ids": exp_ids},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    results = body if isinstance(body, list) else body.get("items", body.get("comparisons", body.get("experiments", [])))
    assert len(results) == 2


def test_cancel_experiment(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id, feat_id = _create_experiment_prereqs(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/experiments", json={
        "name": "cancel_exp",
        "description": "To cancel",
        "dataset_id": ds_id,
        "feature_ids": [feat_id],
        "algorithm": "random_forest",
        "hyperparameters": {}
    }, headers={"Authorization": f"Bearer {token}"})
    exp_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/experiments/{exp_id}/cancel",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json().get("status") == "cancelled"


# ---------------------------------------------------------------------------
# 6. Model Registry (6 tests)
# ---------------------------------------------------------------------------

def _create_trained_experiment(client, tenant_id, token):
    """Create dataset, feature, experiment, run it. Return (exp_id, ds_id)."""
    ds_id, feat_id = _create_experiment_prereqs(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/experiments", json={
        "name": "model_exp",
        "description": "For model registration",
        "dataset_id": ds_id,
        "feature_ids": [feat_id],
        "algorithm": "random_forest",
        "hyperparameters": {"n_estimators": 100}
    }, headers={"Authorization": f"Bearer {token}"})
    exp_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/experiments/{exp_id}/run",
                headers={"Authorization": f"Bearer {token}"})
    return exp_id, ds_id


def test_register_model(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    exp_id, _ = _create_trained_experiment(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/models", json={
        "name": "sales_predictor",
        "experiment_id": exp_id
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    model = r.json()
    assert model["name"] == "sales_predictor"
    assert model.get("status") == "staged"
    assert "id" in model


def test_promote_model(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    exp_id, _ = _create_trained_experiment(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/models", json={
        "name": "promo_model",
        "experiment_id": exp_id
    }, headers={"Authorization": f"Bearer {token}"})
    model_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/models/{model_id}/promote",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json().get("status") == "production"


def test_get_production_model(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    exp_id, _ = _create_trained_experiment(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/models", json={
        "name": "prod_lookup_model",
        "experiment_id": exp_id
    }, headers={"Authorization": f"Bearer {token}"})
    model_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/models/{model_id}/promote",
                headers={"Authorization": f"Bearer {token}"})
    r = client.get(f"/tenants/{tenant_id}/models",
                   params={"status": "production", "name": "prod_lookup_model"},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("models", []))
    assert len(items) >= 1
    assert items[0].get("status") == "production"


def test_rollback_model(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create two versions, promote both (first gets archived), then rollback
    exp_id1, _ = _create_trained_experiment(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/models", json={
        "name": "rollback_model",
        "experiment_id": exp_id1
    }, headers={"Authorization": f"Bearer {token}"})
    model_id1 = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/models/{model_id1}/promote",
                headers={"Authorization": f"Bearer {token}"})

    exp_id2, _ = _create_trained_experiment(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/models", json={
        "name": "rollback_model",
        "experiment_id": exp_id2
    }, headers={"Authorization": f"Bearer {token}"})
    model_id2 = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/models/{model_id2}/promote",
                headers={"Authorization": f"Bearer {token}"})

    # Now rollback
    r = client.post(f"/tenants/{tenant_id}/models/rollback_model/rollback",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "production"


def test_archive_model(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    exp_id, _ = _create_trained_experiment(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/models", json={
        "name": "archive_model",
        "experiment_id": exp_id
    }, headers={"Authorization": f"Bearer {token}"})
    model_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/models/{model_id}/archive",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json().get("status") == "archived"


def test_list_models(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/models",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("models", []))
    assert len(items) >= 1


# ---------------------------------------------------------------------------
# 7. Serving Endpoints (4 tests)
# ---------------------------------------------------------------------------

def _create_production_model(client, tenant_id, token):
    """Create dataset, feature, experiment, run, register model, promote. Return model_id."""
    exp_id, _ = _create_trained_experiment(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/models", json={
        "name": "serving_model",
        "experiment_id": exp_id
    }, headers={"Authorization": f"Bearer {token}"})
    model_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/models/{model_id}/promote",
                headers={"Authorization": f"Bearer {token}"})
    return model_id


def test_create_serving_endpoint(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    model_id = _create_production_model(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/endpoints", json={
        "name": "predictions_v1",
        "model_id": model_id
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    ep = r.json()
    assert ep.get("status") == "active"
    assert "id" in ep


def test_predict(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    model_id = _create_production_model(client, tenant_id, token)
    client.post(f"/tenants/{tenant_id}/endpoints", json={
        "name": "predict_ep",
        "model_id": model_id
    }, headers={"Authorization": f"Bearer {token}"})
    r = client.post(f"/tenants/{tenant_id}/endpoints/predict_ep/predict",
                    json={"input_data": {"feature_a": 1.5, "feature_b": 3.2}},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "prediction" in body
    assert "model_id" in body


def test_update_traffic(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    model_id = _create_production_model(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/endpoints", json={
        "name": "traffic_ep",
        "model_id": model_id
    }, headers={"Authorization": f"Bearer {token}"})
    ep_id = r.json()["id"]
    r = client.put(f"/tenants/{tenant_id}/endpoints/{ep_id}/traffic",
                   json={"percentage": 50},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json().get("traffic_percentage") == 50


def test_deactivate_endpoint(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    model_id = _create_production_model(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/endpoints", json={
        "name": "deactivate_ep",
        "model_id": model_id
    }, headers={"Authorization": f"Bearer {token}"})
    ep_id = r.json()["id"]
    r = client.delete(f"/tenants/{tenant_id}/endpoints/{ep_id}",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") in ("inactive", "draining")


# ---------------------------------------------------------------------------
# 8. A/B Testing (4 tests)
# ---------------------------------------------------------------------------

def _create_two_models(client, tenant_id, token):
    """Create two promoted models for A/B testing. Return (model_id_1, model_id_2)."""
    ids = []
    for i in range(2):
        exp_id, _ = _create_trained_experiment(client, tenant_id, token)
        r = client.post(f"/tenants/{tenant_id}/models", json={
            "name": f"ab_model_{i}",
            "experiment_id": exp_id
        }, headers={"Authorization": f"Bearer {token}"})
        mid = r.json()["id"]
        client.post(f"/tenants/{tenant_id}/models/{mid}/promote",
                    headers={"Authorization": f"Bearer {token}"})
        ids.append(mid)
    return ids[0], ids[1]


def test_create_ab_test(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    control_id, challenger_id = _create_two_models(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/ab-tests", json={
        "name": "model_comparison_1",
        "control_model_id": control_id,
        "challenger_model_id": challenger_id,
        "traffic_split": 30
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    ab = r.json()
    assert ab.get("status") == "running"
    assert "id" in ab


def test_evaluate_ab_test(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    control_id, challenger_id = _create_two_models(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/ab-tests", json={
        "name": "eval_ab_test",
        "control_model_id": control_id,
        "challenger_model_id": challenger_id,
        "traffic_split": 50
    }, headers={"Authorization": f"Bearer {token}"})
    test_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/ab-tests/{test_id}/evaluate",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "completed"
    assert body.get("winner") in ("control", "challenger")


def test_list_ab_tests(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/ab-tests",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("tests", body.get("ab_tests", [])))
    assert len(items) >= 1


def test_cancel_ab_test(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    control_id, challenger_id = _create_two_models(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/ab-tests", json={
        "name": "cancel_ab_test",
        "control_model_id": control_id,
        "challenger_model_id": challenger_id,
        "traffic_split": 20
    }, headers={"Authorization": f"Bearer {token}"})
    test_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/ab-tests/{test_id}/cancel",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json().get("status") == "cancelled"


# ---------------------------------------------------------------------------
# 9. Drift Detection (3 tests)
# ---------------------------------------------------------------------------

def test_check_drift(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    model_id = _create_production_model(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/models/{model_id}/check-drift",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    body = r.json()
    assert "drift_score" in body
    assert "severity" in body
    assert body["severity"] in ("low", "medium", "high")


def test_get_drift_reports(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    model_id = _create_production_model(client, tenant_id, token)
    # Generate a report first
    client.post(f"/tenants/{tenant_id}/models/{model_id}/check-drift",
                headers={"Authorization": f"Bearer {token}"})
    r = client.get(f"/tenants/{tenant_id}/drift-reports",
                   params={"model_id": model_id},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("reports", []))
    assert len(items) >= 1


def test_drift_severity_levels(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    model_id = _create_production_model(client, tenant_id, token)
    # Generate multiple reports to get different severities
    severities = set()
    for _ in range(10):
        r = client.post(f"/tenants/{tenant_id}/models/{model_id}/check-drift",
                        headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (200, 201):
            severities.add(r.json().get("severity"))
    # At least one severity level should appear
    assert len(severities) >= 1
    assert severities.issubset({"low", "medium", "high"})


# ---------------------------------------------------------------------------
# 10. Pipeline Execution (4 tests)
# ---------------------------------------------------------------------------

def test_create_pipeline(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/pipelines", json={
        "name": "training_pipeline",
        "steps": ["validate_data", "compute_features", "train_model", "evaluate", "register_model"]
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    pipeline = r.json()
    assert pipeline["name"] == "training_pipeline"
    assert pipeline.get("overall_status") in ("pending", "created")
    assert "id" in pipeline


def test_run_pipeline(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/pipelines", json={
        "name": "run_pipeline_test",
        "steps": ["validate_data", "compute_features", "train_model", "evaluate", "register_model"]
    }, headers={"Authorization": f"Bearer {token}"})
    pipeline_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/pipelines/{pipeline_id}/run",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("overall_status") in ("completed", "failed", "running")


def test_get_pipeline(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/pipelines", json={
        "name": "get_pipeline_test",
        "steps": ["validate_data", "compute_features"]
    }, headers={"Authorization": f"Bearer {token}"})
    pipeline_id = r.json()["id"]
    r = client.get(f"/tenants/{tenant_id}/pipelines/{pipeline_id}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["id"] == pipeline_id


def test_retry_failed_pipeline(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/pipelines", json={
        "name": "retry_pipeline_test",
        "steps": ["validate_data", "compute_features", "train_model", "evaluate", "register_model"]
    }, headers={"Authorization": f"Bearer {token}"})
    pipeline_id = r.json()["id"]
    # Run pipeline (may fail at train_model step)
    client.post(f"/tenants/{tenant_id}/pipelines/{pipeline_id}/run",
                headers={"Authorization": f"Bearer {token}"})
    # Retry
    r = client.post(f"/tenants/{tenant_id}/pipelines/{pipeline_id}/retry",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("overall_status") in ("completed", "failed", "running", "pending")


# ---------------------------------------------------------------------------
# 11. Hyperparameter Search (2 tests)
# ---------------------------------------------------------------------------

def test_grid_search(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id, feat_id = _create_experiment_prereqs(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/hyperparameter-search", json={
        "dataset_id": ds_id,
        "feature_ids": [feat_id],
        "algorithm": "random_forest",
        "param_grid": {"n_estimators": [50, 100], "max_depth": [3, 5]}
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    body = r.json()
    experiments = body if isinstance(body, list) else body.get("items", body.get("experiments", []))
    # 2x2 grid = 4 experiments
    assert len(experiments) >= 4


def test_get_best_params(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    ds_id, feat_id = _create_experiment_prereqs(client, tenant_id, token)
    r = client.post(f"/tenants/{tenant_id}/hyperparameter-search", json={
        "dataset_id": ds_id,
        "feature_ids": [feat_id],
        "algorithm": "xgboost",
        "param_grid": {"n_estimators": [50, 100], "max_depth": [3, 5]}
    }, headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    experiments = body if isinstance(body, list) else body.get("items", body.get("experiments", []))
    exp_ids = [e["id"] for e in experiments]
    # Get best params - try dedicated endpoint or check sorted results
    r = client.post(f"/tenants/{tenant_id}/hyperparameter-search/best",
                    json={"experiment_ids": exp_ids},
                    headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        body = r.json()
        assert "hyperparameters" in body or "n_estimators" in body or "params" in body
    else:
        # Fallback: first experiment in sorted results should be the best
        assert len(experiments) >= 1
        assert "metrics" in experiments[0] or "hyperparameters" in experiments[0]


# ---------------------------------------------------------------------------
# 12. Tenant Isolation (2 tests)
# ---------------------------------------------------------------------------

def test_tenant_isolation_datasets(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create second tenant
    r = client.post("/tenants", json={"name": "OtherCorp"})
    other_tenant_id = r.json()["id"]
    r = client.post(f"/tenants/{other_tenant_id}/users", json={
        "username": "other_admin", "email": "oa@other.com", "role": "admin"
    })
    other_admin = r.json()
    other_token = _make_token(other_admin["id"], other_tenant_id)

    # Create dataset in first tenant
    r = client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "private_data",
        "description": "Should not be visible",
        "schema": {"secret": "string"},
        "row_count": 1,
        "size_bytes": 64,
        "source": "internal",
        "tags": []
    }, headers={"Authorization": f"Bearer {token}"})
    ds_id = r.json()["id"]

    # Other tenant should not see it
    r = client.get(f"/tenants/{tenant_id}/datasets/{ds_id}",
                   headers={"Authorization": f"Bearer {other_token}"})
    assert r.status_code in (403, 404)


def test_tenant_isolation_experiments(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create second tenant
    r = client.post("/tenants", json={"name": "IsolationCorp"})
    other_tenant_id = r.json()["id"]
    r = client.post(f"/tenants/{other_tenant_id}/users", json={
        "username": "iso_admin", "email": "iso@other.com", "role": "admin"
    })
    other_admin = r.json()
    other_token = _make_token(other_admin["id"], other_tenant_id)

    # List experiments from other tenant - should be empty or forbidden
    r = client.get(f"/tenants/{tenant_id}/experiments",
                   headers={"Authorization": f"Bearer {other_token}"})
    if r.status_code == 200:
        body = r.json()
        items = body if isinstance(body, list) else body.get("items", body.get("experiments", []))
        assert len(items) == 0
    else:
        assert r.status_code in (403, 404)


# ---------------------------------------------------------------------------
# 13. Health (1 test)
# ---------------------------------------------------------------------------

def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "healthy"


# ---------------------------------------------------------------------------
# 14. Request ID Middleware (1 test)
# ---------------------------------------------------------------------------

def test_request_id_header(client):
    r = client.get("/health")
    # Check for X-Request-ID in response headers (case-insensitive)
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    assert "x-request-id" in headers_lower
    assert len(headers_lower["x-request-id"]) > 0


# ---------------------------------------------------------------------------
# 15. Audit Log (2 tests)
# ---------------------------------------------------------------------------

def test_audit_log_exists(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Perform a mutation first (create a dataset)
    client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "audit_trigger_ds",
        "description": "Triggers audit",
        "schema": {"a": "int"},
        "row_count": 10,
        "size_bytes": 128,
        "source": "test",
        "tags": []
    }, headers={"Authorization": f"Bearer {token}"})
    # Check audit log
    r = client.get(f"/tenants/{tenant_id}/audit",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("entries", body.get("audit", [])))
    assert len(items) >= 1


def test_audit_log_contains_action(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/audit",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("entries", body.get("audit", [])))
    if len(items) > 0:
        entry = items[0]
        # Should have action and resource info
        assert "action" in entry or "event" in entry
        assert "resource_type" in entry or "resource_id" in entry or "details" in entry


# ---------------------------------------------------------------------------
# 16. RBAC (2 tests)
# ---------------------------------------------------------------------------

def test_viewer_cannot_create_dataset(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    # Create a viewer user
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "viewer1", "email": "viewer1@mlcorp.com", "role": "viewer"
    }, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code in (200, 201)
    viewer = r.json()
    viewer_token = _make_token(viewer["id"], tenant_id)

    # Viewer tries to create dataset - should be forbidden
    r = client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "viewer_ds",
        "description": "Should fail",
        "schema": {"x": "int"},
        "row_count": 1,
        "size_bytes": 64,
        "source": "test",
        "tags": []
    }, headers={"Authorization": f"Bearer {viewer_token}"})
    assert r.status_code == 403


def test_data_scientist_can_create_dataset(client, tenant_and_admin, scientist_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/datasets", json={
        "name": "scientist_ds",
        "description": "Created by data scientist",
        "schema": {"val": "float"},
        "row_count": 500,
        "size_bytes": 2048,
        "source": "lab",
        "tags": ["science"]
    }, headers={"Authorization": f"Bearer {scientist_token}"})
    assert r.status_code in (200, 201)
    assert r.json()["name"] == "scientist_ds"
