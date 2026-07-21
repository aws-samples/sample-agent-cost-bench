"""
Harness tests for ecommerce-order-saga.

Tests the multi-tenant e-commerce backend: product catalog, inventory reservations,
shopping cart with expiry, order saga with compensation, payment processing,
shipping state machine, coupon engine, recommendation engine, email queue, and analytics.

WORKSPACE is set to the model's output directory by the harness.
"""

from __future__ import annotations

import base64
import importlib
import os
import sys
from pathlib import Path

import pytest

WORKSPACE = Path(os.environ.get("WORKSPACE", "."))


@pytest.fixture(scope="module", autouse=True)
def setup_path():
    ws_str = str(WORKSPACE)
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
    r = client.post("/tenants", json={"name": "ShopCorp"})
    assert r.status_code in (200, 201), f"Failed to create tenant: {r.text}"
    tenant_id = r.json()["id"]

    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "admin1", "email": "admin1@shopcorp.com", "role": "admin"
    })
    assert r.status_code in (200, 201), f"Failed to create admin: {r.text}"
    admin = r.json()
    token = _make_token(admin["id"], tenant_id)
    return tenant_id, admin, token


@pytest.fixture(scope="module")
def customer_token(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/users", json={
        "username": "customer1", "email": "cust1@shopcorp.com", "role": "customer"
    }, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code in (200, 201)
    cust = r.json()
    return _make_token(cust["id"], tenant_id)


# ---------------------------------------------------------------------------
# 1. File existence checks
# ---------------------------------------------------------------------------

def test_models_py_exists():
    assert (WORKSPACE / "models.py").exists()

def test_storage_py_exists():
    assert (WORKSPACE / "storage.py").exists()

def test_auth_py_exists():
    assert (WORKSPACE / "auth.py").exists()

def test_inventory_py_exists():
    assert (WORKSPACE / "inventory.py").exists()

def test_cart_py_exists():
    assert (WORKSPACE / "cart.py").exists()

def test_coupon_py_exists():
    assert (WORKSPACE / "coupon.py").exists()

def test_order_saga_py_exists():
    assert (WORKSPACE / "order_saga.py").exists()

def test_payment_py_exists():
    assert (WORKSPACE / "payment.py").exists()

def test_shipping_py_exists():
    assert (WORKSPACE / "shipping.py").exists()

def test_recommendations_py_exists():
    assert (WORKSPACE / "recommendations.py").exists()

def test_email_queue_py_exists():
    assert (WORKSPACE / "email_queue.py").exists()

def test_analytics_py_exists():
    assert (WORKSPACE / "analytics.py").exists()

def test_main_py_exists():
    assert (WORKSPACE / "main.py").exists()

def test_middleware_py_exists():
    assert (WORKSPACE / "middleware.py").exists()

def test_requirements_txt_exists():
    assert (WORKSPACE / "requirements.txt").exists()


# ---------------------------------------------------------------------------
# 2. Tenant creation and auth
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
    r = client.get(f"/tenants/{tenant_id}/products")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. Product CRUD
# ---------------------------------------------------------------------------

def test_create_product(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Widget A", "description": "A fine widget",
        "price": 29.99, "category": "widgets", "tags": ["sale", "new"]
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    product = r.json()
    assert product["name"] == "Widget A"
    assert "id" in product

def test_list_products(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/products",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("products", []))
    assert len(items) >= 1

def test_get_product_detail(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create a product first
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Widget B", "description": "Another widget",
        "price": 19.99, "category": "widgets", "tags": ["budget"]
    }, headers={"Authorization": f"Bearer {token}"})
    product_id = r.json()["id"]
    r = client.get(f"/tenants/{tenant_id}/products/{product_id}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["id"] == product_id

def test_update_product(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Widget C", "description": "Will be updated",
        "price": 9.99, "category": "widgets", "tags": []
    }, headers={"Authorization": f"Bearer {token}"})
    product_id = r.json()["id"]
    r = client.put(f"/tenants/{tenant_id}/products/{product_id}", json={
        "name": "Widget C Updated", "price": 12.99
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["name"] == "Widget C Updated"


# ---------------------------------------------------------------------------
# 4. Inventory management and reservations
# ---------------------------------------------------------------------------

def test_add_stock(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    # Create product
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Stock Item", "description": "For stock test",
        "price": 5.00, "category": "test", "tags": []
    }, headers={"Authorization": f"Bearer {token}"})
    product_id = r.json()["id"]
    # Add stock
    r = client.post(f"/tenants/{tenant_id}/products/{product_id}/stock",
                    json={"quantity": 50},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)

def test_add_stock_increases_available(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Stock Item 2", "description": "For stock test 2",
        "price": 7.00, "category": "test", "tags": []
    }, headers={"Authorization": f"Bearer {token}"})
    product_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/products/{product_id}/stock",
                json={"quantity": 20},
                headers={"Authorization": f"Bearer {token}"})
    client.post(f"/tenants/{tenant_id}/products/{product_id}/stock",
                json={"quantity": 30},
                headers={"Authorization": f"Bearer {token}"})
    # Total should be 50, verify via low-stock endpoint (should NOT appear)
    r = client.get(f"/tenants/{tenant_id}/inventory/low-stock",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200

def test_low_stock_endpoint(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/inventory/low-stock",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    # Should be a list
    items = body if isinstance(body, list) else body.get("items", body.get("products", []))
    assert isinstance(items, list)


# ---------------------------------------------------------------------------
# 5. Cart operations with expiry
# ---------------------------------------------------------------------------

def test_get_empty_cart(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/cart",
                   headers={"Authorization": f"Bearer {customer_token}"})
    # Either 200 with empty cart or 404 if no cart created yet
    assert r.status_code in (200, 404)

def test_add_to_cart(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    # Create product and add stock
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Cart Product", "description": "For cart test",
        "price": 15.00, "category": "test", "tags": []
    }, headers={"Authorization": f"Bearer {admin_token}"})
    product_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/products/{product_id}/stock",
                json={"quantity": 10},
                headers={"Authorization": f"Bearer {admin_token}"})
    # Add to cart
    r = client.post(f"/tenants/{tenant_id}/cart/items", json={
        "product_id": product_id, "quantity": 2
    }, headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code in (200, 201)

def test_get_cart_after_add(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/cart",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    cart = r.json()
    items = cart.get("items", [])
    assert len(items) >= 1

def test_remove_from_cart(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    # Create product and add stock
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Remove Cart Product", "description": "Will be removed",
        "price": 10.00, "category": "test", "tags": []
    }, headers={"Authorization": f"Bearer {admin_token}"})
    product_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/products/{product_id}/stock",
                json={"quantity": 10},
                headers={"Authorization": f"Bearer {admin_token}"})
    # Add then remove
    client.post(f"/tenants/{tenant_id}/cart/items", json={
        "product_id": product_id, "quantity": 1
    }, headers={"Authorization": f"Bearer {customer_token}"})
    r = client.delete(f"/tenants/{tenant_id}/cart/items/{product_id}",
                      headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code in (200, 204)

def test_clear_cart(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.delete(f"/tenants/{tenant_id}/cart",
                      headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code in (200, 204)


# ---------------------------------------------------------------------------
# 6. Coupon validation and application
# ---------------------------------------------------------------------------

def test_create_coupon(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/coupons", json={
        "code": "SAVE10", "discount_type": "percentage",
        "discount_value": 10.0, "min_order_amount": 20.0,
        "max_uses": 100, "is_active": True,
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_until": "2030-12-31T23:59:59Z"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)
    coupon = r.json()
    assert coupon["code"] == "SAVE10"

def test_validate_coupon_valid(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/coupons/validate", json={
        "code": "SAVE10", "subtotal": 50.0
    }, headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("valid") is True
    assert body.get("discount_amount", 0) > 0

def test_validate_coupon_below_min_amount(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/coupons/validate", json={
        "code": "SAVE10", "subtotal": 5.0
    }, headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("valid") is False

def test_validate_coupon_invalid_code(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/coupons/validate", json={
        "code": "NONEXISTENT", "subtotal": 50.0
    }, headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code in (200, 400, 404)
    if r.status_code == 200:
        assert r.json().get("valid") is False

def test_create_fixed_coupon(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/coupons", json={
        "code": "FLAT5", "discount_type": "fixed",
        "discount_value": 5.0, "min_order_amount": 10.0,
        "max_uses": 50, "is_active": True,
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_until": "2030-12-31T23:59:59Z"
    }, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (200, 201)


# ---------------------------------------------------------------------------
# 7. Full order saga
# ---------------------------------------------------------------------------

def _setup_order_cart(client, tenant_id, admin_token, customer_token):
    """Helper: create product, add stock, add to cart. Returns product_id."""
    # Create product
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Order Product", "description": "For order test",
        "price": 25.00, "category": "electronics", "tags": ["order"]
    }, headers={"Authorization": f"Bearer {admin_token}"})
    product_id = r.json()["id"]
    # Add stock
    client.post(f"/tenants/{tenant_id}/products/{product_id}/stock",
                json={"quantity": 100},
                headers={"Authorization": f"Bearer {admin_token}"})
    # Clear any existing cart
    client.delete(f"/tenants/{tenant_id}/cart",
                  headers={"Authorization": f"Bearer {customer_token}"})
    # Add to cart
    client.post(f"/tenants/{tenant_id}/cart/items", json={
        "product_id": product_id, "quantity": 2
    }, headers={"Authorization": f"Bearer {customer_token}"})
    return product_id


def test_place_order_success(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    # Place order
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "123 Main St, City, ST 12345"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code in (200, 201)
    order = r.json()
    assert order["status"] == "paid"
    assert "id" in order

def test_order_has_payment(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "456 Oak Ave, Town, ST 67890"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order = r.json()
    assert order.get("payment_id") is not None

def test_order_has_shipping(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "789 Pine Rd, Village, ST 11111"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order = r.json()
    assert order.get("shipping_id") is not None

def test_order_creates_email(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "101 Elm Blvd, Hamlet, ST 22222"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    # Check email queue
    r = client.get(f"/tenants/{tenant_id}/emails",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    body = r.json()
    emails = body if isinstance(body, list) else body.get("items", body.get("messages", []))
    assert len(emails) >= 1

def test_order_creates_analytics(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "debit",
        "shipping_address": "202 Maple Ln, Borough, ST 33333"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    # Check analytics events
    r = client.get(f"/tenants/{tenant_id}/analytics/events",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    events = body if isinstance(body, list) else body.get("items", body.get("events", []))
    assert len(events) >= 1

def test_list_orders(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/orders",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    body = r.json()
    orders = body if isinstance(body, list) else body.get("items", body.get("orders", []))
    assert len(orders) >= 1


# ---------------------------------------------------------------------------
# 8. Order cancellation with compensation
# ---------------------------------------------------------------------------

def test_cancel_order(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "303 Cedar Dr, Town, ST 44444"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order_id = r.json()["id"]
    # Cancel the order
    r = client.post(f"/tenants/{tenant_id}/orders/{order_id}/cancel",
                    headers={"Authorization": f"Bearer {customer_token}"},
                    json={"reason": "Changed my mind"})
    assert r.status_code == 200
    cancelled = r.json()
    assert cancelled["status"] in ("cancelled", "refunded")

def test_cancel_order_refunds_payment(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "wallet",
        "shipping_address": "404 Birch Way, City, ST 55555"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order_id = r.json()["id"]
    r = client.post(f"/tenants/{tenant_id}/orders/{order_id}/cancel",
                    headers={"Authorization": f"Bearer {customer_token}"},
                    json={"reason": "Found cheaper"})
    assert r.status_code == 200
    order = r.json()
    assert order["status"] in ("cancelled", "refunded")

def test_cancel_sends_email(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "505 Spruce Ct, Village, ST 66666"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/orders/{order_id}/cancel",
                headers={"Authorization": f"Bearer {customer_token}"},
                json={"reason": "Duplicate order"})
    # Check emails for cancellation notification
    r = client.get(f"/tenants/{tenant_id}/emails",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    body = r.json()
    emails = body if isinstance(body, list) else body.get("items", body.get("messages", []))
    # Should have at least one cancellation email
    assert len(emails) >= 1


# ---------------------------------------------------------------------------
# 9. Payment decline simulation (amount > 10000)
# ---------------------------------------------------------------------------

def test_payment_decline_high_amount(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    # Create expensive product
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Luxury Item", "description": "Very expensive",
        "price": 6000.00, "category": "luxury", "tags": ["expensive"]
    }, headers={"Authorization": f"Bearer {admin_token}"})
    product_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/products/{product_id}/stock",
                json={"quantity": 50},
                headers={"Authorization": f"Bearer {admin_token}"})
    # Clear cart and add enough to exceed 10000
    client.delete(f"/tenants/{tenant_id}/cart",
                  headers={"Authorization": f"Bearer {customer_token}"})
    client.post(f"/tenants/{tenant_id}/cart/items", json={
        "product_id": product_id, "quantity": 2
    }, headers={"Authorization": f"Bearer {customer_token}"})
    # Place order — should fail payment (total = 12000)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "606 Gold St, Metro, ST 77777"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    # Accept either a failed order in 200/201 or a 400/402/422 error
    if r.status_code in (200, 201):
        order = r.json()
        assert order["status"] in ("cancelled", "failed", "payment_failed")
    else:
        assert r.status_code in (400, 402, 422)

def test_payment_decline_releases_inventory(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    # Create expensive product
    r = client.post(f"/tenants/{tenant_id}/products", json={
        "name": "Premium Item", "description": "Also expensive",
        "price": 5500.00, "category": "luxury", "tags": ["premium"]
    }, headers={"Authorization": f"Bearer {admin_token}"})
    product_id = r.json()["id"]
    client.post(f"/tenants/{tenant_id}/products/{product_id}/stock",
                json={"quantity": 10},
                headers={"Authorization": f"Bearer {admin_token}"})
    # Clear cart and add enough to exceed 10000
    client.delete(f"/tenants/{tenant_id}/cart",
                  headers={"Authorization": f"Bearer {customer_token}"})
    client.post(f"/tenants/{tenant_id}/cart/items", json={
        "product_id": product_id, "quantity": 2
    }, headers={"Authorization": f"Bearer {customer_token}"})
    # Place order — should fail
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "debit",
        "shipping_address": "707 Silver Ln, Metro, ST 88888"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    # After decline, we should be able to add to cart again (inventory released)
    # Clear cart first
    client.delete(f"/tenants/{tenant_id}/cart",
                  headers={"Authorization": f"Bearer {customer_token}"})
    r = client.post(f"/tenants/{tenant_id}/cart/items", json={
        "product_id": product_id, "quantity": 2
    }, headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code in (200, 201)


# ---------------------------------------------------------------------------
# 10. Shipping state machine
# ---------------------------------------------------------------------------

def test_get_shipment_for_order(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "808 Ship St, Port, ST 99999"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order_id = r.json()["id"]
    r = client.get(f"/tenants/{tenant_id}/orders/{order_id}/shipment",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    shipment = r.json()
    assert shipment.get("status") == "preparing"

def test_advance_shipment_status(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "909 Dock Ave, Harbor, ST 10101"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order_id = r.json()["id"]
    # Advance: preparing -> picked_up
    r = client.post(f"/tenants/{tenant_id}/orders/{order_id}/shipment/advance",
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    shipment = r.json()
    assert shipment.get("status") == "picked_up"

def test_advance_shipment_to_in_transit(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "1010 Wave Blvd, Beach, ST 20202"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order_id = r.json()["id"]
    # Advance twice: preparing -> picked_up -> in_transit
    client.post(f"/tenants/{tenant_id}/orders/{order_id}/shipment/advance",
                headers={"Authorization": f"Bearer {admin_token}"})
    r = client.post(f"/tenants/{tenant_id}/orders/{order_id}/shipment/advance",
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    shipment = r.json()
    assert shipment.get("status") == "in_transit"

def test_advance_shipment_to_delivered(client, tenant_and_admin, customer_token):
    tenant_id, _, admin_token = tenant_and_admin
    _setup_order_cart(client, tenant_id, admin_token, customer_token)
    r = client.post(f"/tenants/{tenant_id}/orders", json={
        "payment_method": "credit_card",
        "shipping_address": "1111 Final St, Dest, ST 30303"
    }, headers={"Authorization": f"Bearer {customer_token}"})
    order_id = r.json()["id"]
    # Advance three times: preparing -> picked_up -> in_transit -> delivered
    client.post(f"/tenants/{tenant_id}/orders/{order_id}/shipment/advance",
                headers={"Authorization": f"Bearer {admin_token}"})
    client.post(f"/tenants/{tenant_id}/orders/{order_id}/shipment/advance",
                headers={"Authorization": f"Bearer {admin_token}"})
    r = client.post(f"/tenants/{tenant_id}/orders/{order_id}/shipment/advance",
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    shipment = r.json()
    assert shipment.get("status") == "delivered"


# ---------------------------------------------------------------------------
# 11. Recommendations
# ---------------------------------------------------------------------------

def test_get_recommendations(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/recommendations",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    body = r.json()
    # Should be a list of product IDs or objects
    items = body if isinstance(body, list) else body.get("items", body.get("recommendations", body.get("product_ids", [])))
    assert isinstance(items, list)

def test_get_popular_products(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/products/popular",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("products", body.get("product_ids", [])))
    assert isinstance(items, list)

def test_recommendations_after_purchase(client, tenant_and_admin, customer_token):
    """After placing orders, recommendations should have some data."""
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/recommendations",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 12. Email queue
# ---------------------------------------------------------------------------

def test_get_emails(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/emails",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 200
    body = r.json()
    emails = body if isinstance(body, list) else body.get("items", body.get("messages", []))
    assert isinstance(emails, list)

def test_process_email_queue(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/emails/process",
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    processed = body if isinstance(body, list) else body.get("processed", body.get("messages", []))
    assert isinstance(processed, list)

def test_process_email_queue_admin_only(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.post(f"/tenants/{tenant_id}/emails/process",
                    headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 13. Analytics
# ---------------------------------------------------------------------------

def test_analytics_summary(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/analytics",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    assert "total_events" in body or "events_by_type" in body or "summary" in body

def test_analytics_events(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/analytics/events",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    events = body if isinstance(body, list) else body.get("items", body.get("events", []))
    assert isinstance(events, list)
    assert len(events) >= 1

def test_analytics_has_order_placed_event(client, tenant_and_admin):
    tenant_id, _, admin_token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/analytics/events",
                   headers={"Authorization": f"Bearer {admin_token}"})
    body = r.json()
    events = body if isinstance(body, list) else body.get("items", body.get("events", []))
    event_types = [e.get("event_type", "") for e in events if isinstance(e, dict)]
    assert "order_placed" in event_types or "order.placed" in event_types or len(events) >= 1


# ---------------------------------------------------------------------------
# 14. Tenant isolation
# ---------------------------------------------------------------------------

def test_tenant_isolation_products(client, tenant_and_admin):
    tenant_id_a, _, token_a = tenant_and_admin
    # Create tenant B
    r = client.post("/tenants", json={"name": "OtherShop"})
    tenant_id_b = r.json()["id"]
    # Try to access tenant B's products with tenant A's token
    r = client.get(f"/tenants/{tenant_id_b}/products",
                   headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 403

def test_tenant_isolation_orders(client, tenant_and_admin):
    tenant_id_a, _, token_a = tenant_and_admin
    # Create tenant C
    r = client.post("/tenants", json={"name": "ThirdShop"})
    tenant_id_c = r.json()["id"]
    # Try to access tenant C's orders with tenant A's token
    r = client.get(f"/tenants/{tenant_id_c}/orders",
                   headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 15. Health endpoint
# ---------------------------------------------------------------------------

def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "healthy"


# ---------------------------------------------------------------------------
# 16. Request ID middleware
# ---------------------------------------------------------------------------

def test_request_id_header(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/products",
                   headers={"Authorization": f"Bearer {token}"})
    headers_lower = {k.lower(): v for k, v in r.headers.items()}
    assert "x-request-id" in headers_lower


# ---------------------------------------------------------------------------
# 17. Audit log
# ---------------------------------------------------------------------------

def test_audit_log(client, tenant_and_admin):
    tenant_id, _, token = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/audit",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    entries = body if isinstance(body, list) else body.get("entries", body.get("items", []))
    assert len(entries) >= 1

def test_audit_admin_only(client, tenant_and_admin, customer_token):
    tenant_id, _, _ = tenant_and_admin
    r = client.get(f"/tenants/{tenant_id}/audit",
                   headers={"Authorization": f"Bearer {customer_token}"})
    assert r.status_code == 403
