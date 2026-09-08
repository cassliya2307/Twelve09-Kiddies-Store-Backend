import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test-cloud")
os.environ.setdefault("CLOUDINARY_API_KEY", "test-key")
os.environ.setdefault("CLOUDINARY_API_SECRET", "test-secret")

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import hash_password
from app.core.database import Base, get_db
from app.main import app
from app.models import Category, OrderStatus, Product, User, UserRole


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    app.state.test_engine = engine

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _register_and_login(client, email="alice@example.com", password="secretpass123"):
    r = client.post("/auth/register", json={"name": email.split("@")[0].title(), "email": email, "password": password})
    assert r.status_code == 200, r.text
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _get_admin_headers(client):
    try:
        existing = client.post("/auth/login", json={"email": "admin@example.com", "password": "pass123456"})
        if existing.status_code == 200:
            return {"Authorization": f"Bearer {existing.json()['access_token']}"}
    except Exception:
        pass
    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin@example.com").first()
        if admin is None:
            admin = User(name="Admin User", email="admin@example.com", password_hash=hash_password("pass123456"), role=UserRole.ADMIN, permissions=[], is_active=True)
            db.add(admin)
            db.commit()
            db.refresh(admin)
    finally:
        db.close()
    login = client.post("/auth/login", json={"email": "admin@example.com", "password": "pass123456"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_product(client, name, price=100.00, stock=10):
    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        cat = db.query(Category).filter(Category.name == "Test Category").first()
        if cat is None:
            cat = Category(name="Test Category", description="test")
            db.add(cat)
            db.commit()
            db.refresh(cat)
        p = Product(category_id=cat.id, name=name, description="test", price=Decimal(str(price)), stock_quantity=stock, is_active=True)
        db.add(p)
        db.commit()
        db.refresh(p)
        return p
    finally:
        db.close()


def _get_stock(product_id, client):
    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        return db.query(Product).filter(Product.id == product_id).first().stock_quantity
    finally:
        db.close()


def test_cancel_restores_stock_single_item(client):
    headers = _register_and_login(client, "cancel1@example.com")
    product = _create_product(client, "Cancel Single", price=50, stock=10)
    resp = client.post("/orders", headers=headers, json={"fulfillment_method": "STORE_PICKUP", "items": [{"product_id": product.id, "quantity": 2}]})
    assert resp.status_code == 200, resp.text
    order_id = resp.json()["id"]
    assert _get_stock(product.id, client) == 8
    cancel = client.post(f"/orders/{order_id}/cancel", headers=headers)
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "CANCELLED"
    assert _get_stock(product.id, client) == 10


def test_cancel_restores_stock_multiple_items(client):
    headers = _register_and_login(client, "cancel2@example.com")
    p1 = _create_product(client, "Cancel Multi A", price=30, stock=10)
    p2 = _create_product(client, "Cancel Multi B", price=70, stock=5)
    resp = client.post("/orders", headers=headers, json={"fulfillment_method": "STORE_PICKUP", "items": [{"product_id": p1.id, "quantity": 3}, {"product_id": p2.id, "quantity": 2}]})
    assert resp.status_code == 200, resp.text
    order_id = resp.json()["id"]
    assert _get_stock(p1.id, client) == 7
    assert _get_stock(p2.id, client) == 3
    cancel = client.post(f"/orders/{order_id}/cancel", headers=headers)
    assert cancel.status_code == 200, cancel.text
    assert _get_stock(p1.id, client) == 10
    assert _get_stock(p2.id, client) == 5


def test_cancel_idempotent_does_not_double_restore(client):
    headers = _register_and_login(client, "cancel3@example.com")
    product = _create_product(client, "Cancel Idempotent", price=20, stock=10)
    resp = client.post("/orders", headers=headers, json={"fulfillment_method": "STORE_PICKUP", "items": [{"product_id": product.id, "quantity": 3}]})
    assert resp.status_code == 200
    order_id = resp.json()["id"]
    assert _get_stock(product.id, client) == 7
    first = client.post(f"/orders/{order_id}/cancel", headers=headers)
    assert first.status_code == 200
    assert _get_stock(product.id, client) == 10
    second = client.post(f"/orders/{order_id}/cancel", headers=headers)
    # second is idempotent success (200) and must not double-increment
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "CANCELLED"
    assert _get_stock(product.id, client) == 10, "stock must not be restored twice"


def test_cancel_unauthorized_rejected(client):
    owner = _register_and_login(client, "owner@example.com")
    intruder = _register_and_login(client, "intruder@example.com")
    product = _create_product(client, "Cancel Auth", price=10, stock=10)
    resp = client.post("/orders", headers=owner, json={"fulfillment_method": "STORE_PICKUP", "items": [{"product_id": product.id, "quantity": 1}]})
    assert resp.status_code == 200
    order_id = resp.json()["id"]
    # stock deducted
    assert _get_stock(product.id, client) == 9
    bad = client.post(f"/orders/{order_id}/cancel", headers=intruder)
    assert bad.status_code == 403, bad.text
    # stock unchanged by failed attempt
    assert _get_stock(product.id, client) == 9
    # order still PENDING
    fetched = client.get(f"/orders/{order_id}", headers=owner)
    assert fetched.json()["status"] == "PENDING"


def test_cancel_invalid_transition_rejected(client):
    headers = _register_and_login(client, "cancel4@example.com")
    admin = _get_admin_headers(client)
    product = _create_product(client, "Cancel Invalid", price=40, stock=10)
    resp = client.post("/orders", headers=headers, json={"fulfillment_method": "STORE_PICKUP", "items": [{"product_id": product.id, "quantity": 1}]})
    assert resp.status_code == 200
    order_id = resp.json()["id"]
    # Drive to COMPLETED via admin transitions: PENDING->CONFIRMED->PROCESSING->READY_FOR_PICKUP->COMPLETED
    for status in ["CONFIRMED", "PROCESSING", "READY_FOR_PICKUP", "COMPLETED"]:
        r = client.patch(f"/orders/{order_id}/status", headers=admin, json={"status": status})
        assert r.status_code == 200, f"{status} failed: {r.text}"
    # stock after completed should still be deducted (9)
    assert _get_stock(product.id, client) == 9
    # Customer cancel now must be rejected (400)
    bad = client.post(f"/orders/{order_id}/cancel", headers=headers)
    assert bad.status_code == 400, bad.text
    assert "Invalid status transition" in bad.text
    assert _get_stock(product.id, client) == 9


def test_admin_cancel_via_status_patch_restores_stock(client):
    # Admin path PATCH /orders/{id}/status -> CANCELLED must also restore stock
    customer = _register_and_login(client, "custadmin@example.com")
    admin = _get_admin_headers(client)
    product = _create_product(client, "Admin Cancel Stock", price=100, stock=10)
    resp = client.post("/orders", headers=customer, json={"fulfillment_method": "STORE_PICKUP", "items": [{"product_id": product.id, "quantity": 4}]})
    assert resp.status_code == 200
    order_id = resp.json()["id"]
    assert _get_stock(product.id, client) == 6
    patch = client.patch(f"/orders/{order_id}/status", headers=admin, json={"status": "CANCELLED"})
    assert patch.status_code == 200, patch.text
    assert patch.json()["status"] == "CANCELLED"
    assert _get_stock(product.id, client) == 10
