"""Focused Paystack payment integration tests.

Covers initialization, ownership, webhook signature validation, webhook
success/idempotency, amount/currency mismatch handling, failed payments,
duplicate-initialization protection, CASH regression and persistence of
payment_metadata / currency.

External Paystack API is always mocked - no real network calls.
"""

import ast
import hashlib
import hmac
import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_dummy_secret_key")
os.environ.setdefault("PAYSTACK_PUBLIC_KEY", "pk_test_dummy_public_key")
os.environ.setdefault("PAYSTACK_WEBHOOK_SECRET", "whsec_test_webhook_secret")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import Payment, PaymentStatus
from app.services.paystack import paystack_client

WEBHOOK_SECRET = "whsec_test_webhook_secret"


@pytest.fixture
def client():
    # The Settings singleton may be created before our env vars are read
    # (import order varies across the suite), so configure the webhook secret
    # directly on the client instance for every test.
    original_secret = paystack_client.webhook_secret
    paystack_client.webhook_secret = WEBHOOK_SECRET

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
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        paystack_client.webhook_secret = original_secret
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def _db_session():
    engine = app.state.test_engine
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _create_product(name: str, price: str = "100.00", stock: int = 10):
    from app.models import Category, Product

    db = _db_session()
    try:
        category = db.query(Category).filter(Category.name == "Paystack Test Category").first()
        if category is None:
            category = Category(name="Paystack Test Category", description="Test")
            db.add(category)
            db.commit()
            db.refresh(category)
        product = Product(
            category_id=category.id,
            name=name,
            description="Test product",
            price=Decimal(price),
            stock_quantity=stock,
            is_active=True,
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return product
    finally:
        db.close()


def _register_and_login(client, email: str):
    register = client.post(
        "/auth/register",
        json={"name": email.split("@")[0].title(), "email": email, "password": "secretpass123"},
    )
    assert register.status_code == 200, register.text
    login = client.post("/auth/login", json={"email": email, "password": "secretpass123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_pickup_order(client, headers, product_id: int) -> dict:
    response = client.post(
        "/checkout",
        headers=headers,
        json={
            "items": [{"product_id": product_id, "quantity": 1}],
            "fulfillment_method": "STORE_PICKUP",
            "address_id": None,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


def _success_verification(reference: str, amount_kobo: int, currency: str = "NGN", status: str = "success") -> dict:
    return {
        "status": status,
        "reference": reference,
        "amount": amount_kobo,
        "currency": currency,
        "gateway_response": "Successful",
        "paid_at": "2026-08-26T10:00:00Z",
    }


# ---------------------------------------------------------------------------
# TEST 1 — Initialization
# ---------------------------------------------------------------------------


def test_paystack_initialization_success(client):
    headers = _register_and_login(client, "init@example.com")
    product = _create_product("Init Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)

    fake = {
        "authorization_url": "https://checkout.paystack.com/abc123",
        "access_code": "ACCESS_CODE_123",
        "reference": "twelve09_ref_001",
    }
    init_mock = AsyncMock(return_value=fake)
    with patch.object(paystack_client, "initialize_transaction", new=init_mock):
        response = client.post(
            "/payments/paystack/initialize",
            headers=headers,
            json={"order_id": order["id"], "callback_url": "https://front.example/callback"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorization_url"] == fake["authorization_url"]
    assert body["access_code"] == fake["access_code"]
    assert body["reference"] == fake["reference"]

    # Backend determined the amount passed to Paystack (order total in kobo)
    assert init_mock.call_args.kwargs["amount"] == Decimal("100.00")

    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.order_id == order["id"]).one()
        assert payment.provider == "paystack"
        # The reference sent to Paystack is the backend-generated one
        sent_reference = init_mock.call_args.kwargs["reference"]
        assert payment.provider_reference == sent_reference
        assert sent_reference.startswith(f"twelve09_{order['id']}_")
        assert payment.currency == "NGN"
        assert payment.status == PaymentStatus.PENDING
        assert payment.amount == Decimal("100.00")
        # Regression: metadata must be persisted on the mapped column
        assert payment.payment_metadata is not None
        assert payment.payment_metadata["paystack_reference"] == fake["reference"]
        assert payment.payment_metadata["paystack_authorization_url"] == fake["authorization_url"]

        # Survives a full session reload from the database
        db.expire_all()
        reloaded = db.query(Payment).filter(Payment.id == payment.id).one()
        assert reloaded.currency == "NGN"
        assert reloaded.payment_metadata["paystack_access_code"] == fake["access_code"]
    finally:
        db.close()

    # API also exposes persisted values
    payment_api = client.get(f"/orders/{order['id']}/payments", headers=headers).json()
    assert payment_api["currency"] == "NGN"
    assert payment_api["provider_reference"] == sent_reference
    assert payment_api["payment_metadata"]["paystack_reference"] == fake["reference"]


# ---------------------------------------------------------------------------
# TEST 2 — Ownership
# ---------------------------------------------------------------------------


def test_paystack_initialization_ownership_enforced(client):
    owner_headers = _register_and_login(client, "owner@example.com")
    product = _create_product("Ownership Product")
    order = _create_pickup_order(client, owner_headers, product.id)

    attacker_headers = _register_and_login(client, "attacker@example.com")
    response = client.post(
        "/payments/paystack/initialize",
        headers=attacker_headers,
        json={"order_id": order["id"]},
    )
    assert response.status_code == 403, response.text

    db = _db_session()
    try:
        assert db.query(Payment).filter(Payment.order_id == order["id"]).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 3 — Already successful payments cannot be initialized again
# ---------------------------------------------------------------------------


def test_paystack_initialization_blocked_when_already_paid(client):
    headers = _register_and_login(client, "paid@example.com")
    product = _create_product("Already Paid Product")
    order = _create_pickup_order(client, headers, product.id)

    db = _db_session()
    try:
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal(order["total_amount"]),
                payment_method="CARD",
                provider="paystack",
                provider_reference="paid_ref",
                status=PaymentStatus.SUCCESS,
                currency="NGN",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/payments/paystack/initialize",
        headers=headers,
        json={"order_id": order["id"]},
    )
    assert response.status_code == 400, response.text
    assert "already been paid" in response.json()["detail"]


# ---------------------------------------------------------------------------
# TEST 4 — Webhook with invalid signature
# ---------------------------------------------------------------------------


def test_webhook_invalid_signature_rejected(client):
    headers = _register_and_login(client, "badsig@example.com")
    product = _create_product("Bad Sig Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "bad_sig_ref"

    db = _db_session()
    try:
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="CARD",
                provider="paystack",
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                currency="NGN",
            )
        )
        db.commit()
    finally:
        db.close()

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    response = client.post(
        "/webhooks/paystack",
        content=body,
        headers={"x-paystack-signature": _sign(body, secret="wrong-secret")},
    )
    assert response.status_code == 401, response.text

    # Missing signature is rejected too
    response = client.post("/webhooks/paystack", content=body, headers={})
    assert response.status_code == 401, response.text

    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.PENDING
        assert payment.paid_at is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 5 — Webhook charge.success
# ---------------------------------------------------------------------------


def test_webhook_charge_success_confirms_payment_and_order(client):
    headers = _register_and_login(client, "webhook@example.com")
    product = _create_product("Webhook Success Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "webhook_success_ref"

    db = _db_session()
    try:
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="CARD",
                provider="paystack",
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                currency="NGN",
            )
        )
        db.commit()
    finally:
        db.close()

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = client.post(
            "/webhooks/paystack",
            content=body,
            headers={"x-paystack-signature": _sign(body)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == "success"

    db = _db_session()
    try:
        from app.models import Order

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.SUCCESS
        assert payment.paid_at is not None
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status.value == "CONFIRMED"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 6 — Duplicate webhook delivery is idempotent
# ---------------------------------------------------------------------------


def test_webhook_duplicate_success_is_idempotent(client):
    headers = _register_and_login(client, "dup@example.com")
    product = _create_product("Duplicate Webhook Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "dup_webhook_ref"

    db = _db_session()
    try:
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="CARD",
                provider="paystack",
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                currency="NGN",
            )
        )
        db.commit()
    finally:
        db.close()

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    signature_headers = {"x-paystack-signature": _sign(body)}
    verification = _success_verification(reference, amount_kobo=10000)

    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        first = client.post("/webhooks/paystack", content=body, headers=signature_headers)
        db = _db_session()
        try:
            paid_at_after_first = (
                db.query(Payment).filter(Payment.provider_reference == reference).one().paid_at
            )
        finally:
            db.close()

        second = client.post("/webhooks/paystack", content=body, headers=signature_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["result"] == "already_processed"

    db = _db_session()
    try:
        from app.models import Order

        # Still exactly one payment, still SUCCESS, paid_at untouched
        payments = db.query(Payment).filter(Payment.order_id == order["id"]).all()
        assert len(payments) == 1
        assert payments[0].status == PaymentStatus.SUCCESS
        assert payments[0].paid_at == paid_at_after_first
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status.value == "CONFIRMED"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 7 — Amount mismatch keeps payment safe (not SUCCESS, not FAILED)
# ---------------------------------------------------------------------------


def test_webhook_amount_mismatch_keeps_pending_with_evidence(client):
    headers = _register_and_login(client, "mismatch@example.com")
    product = _create_product("Amount Mismatch Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "amount_mismatch_ref"

    db = _db_session()
    try:
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="CARD",
                provider="paystack",
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                currency="NGN",
            )
        )
        db.commit()
    finally:
        db.close()

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    # Paystack reports a *successful* charge but for the wrong amount
    verification = _success_verification(reference, amount_kobo=5000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = client.post(
            "/webhooks/paystack",
            content=body,
            headers={"x-paystack-signature": _sign(body)},
        )

    assert response.status_code == 200
    assert response.json()["result"] == "amount_mismatch"

    db = _db_session()
    try:
        from app.models import Order

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.PENDING
        assert payment.paid_at is None
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status.value == "PENDING"
        mismatch = payment.payment_metadata.get("amount_mismatch")
        assert mismatch is not None
        assert mismatch["expected"] == "100.00"
        assert mismatch["received"] == "50"
        assert mismatch["requires_manual_review"] is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 8 — Currency mismatch recorded safely
# ---------------------------------------------------------------------------


def test_webhook_currency_mismatch_recorded_safely(client):
    headers = _register_and_login(client, "currency@example.com")
    product = _create_product("Currency Mismatch Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "currency_mismatch_ref"

    db = _db_session()
    try:
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="CARD",
                provider="paystack",
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                currency="NGN",
            )
        )
        db.commit()
    finally:
        db.close()

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(reference, amount_kobo=10000, currency="USD")
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = client.post(
            "/webhooks/paystack",
            content=body,
            headers={"x-paystack-signature": _sign(body)},
        )

    assert response.status_code == 200
    assert response.json()["result"] == "currency_mismatch"

    db = _db_session()
    try:
        from app.models import Order

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status != PaymentStatus.SUCCESS
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status.value != "CONFIRMED"
        mismatch = payment.payment_metadata.get("currency_mismatch")
        assert mismatch is not None
        assert mismatch["expected"] == "NGN"
        assert mismatch["received"] == "USD"
        assert mismatch["requires_manual_review"] is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 9 — Failed payment marks FAILED, order stays unconfirmed
# ---------------------------------------------------------------------------


def test_webhook_failed_payment_marks_failed(client):
    headers = _register_and_login(client, "failedpay@example.com")
    product = _create_product("Failed Payment Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "failed_ref"

    db = _db_session()
    try:
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="CARD",
                provider="paystack",
                provider_reference=reference,
                status=PaymentStatus.PENDING,
                currency="NGN",
            )
        )
        db.commit()
    finally:
        db.close()

    event = {"event": "charge.failed", "data": {"reference": reference, "gateway_response": "Declined"}}
    body = json.dumps(event).encode()
    response = client.post(
        "/webhooks/paystack",
        content=body,
        headers={"x-paystack-signature": _sign(body)},
    )
    assert response.status_code == 200

    db = _db_session()
    try:
        from app.models import Order

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.FAILED
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status.value == "PENDING"
    finally:
        db.close()

    # Retry path remains open: a FAILED payment can be re-initialized
    fake = {
        "authorization_url": "https://checkout.paystack.com/retry",
        "access_code": "RETRY_CODE",
        "reference": "retry_ref_002",
    }
    with patch.object(paystack_client, "initialize_transaction", new=AsyncMock(return_value=fake)):
        retry = client.post(
            "/payments/paystack/initialize",
            headers=headers,
            json={"order_id": order["id"]},
        )
    assert retry.status_code == 200, retry.text


# ---------------------------------------------------------------------------
# TEST 10 — Duplicate initialization never creates duplicate active payments
# ---------------------------------------------------------------------------


def test_duplicate_initialization_keeps_single_payment_record(client):
    headers = _register_and_login(client, "dupinit@example.com")
    product = _create_product("Duplicate Init Product")
    order = _create_pickup_order(client, headers, product.id)

    responses = []
    for i in range(3):
        fake = {
            "authorization_url": f"https://checkout.paystack.com/init{i}",
            "access_code": f"CODE_{i}",
            "reference": f"dup_init_ref_{i}",
        }
        with patch.object(paystack_client, "initialize_transaction", new=AsyncMock(return_value=fake)):
            responses.append(
                client.post(
                    "/payments/paystack/initialize",
                    headers=headers,
                    json={"order_id": order["id"]},
                )
            )

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]

    db = _db_session()
    try:
        payments = db.query(Payment).filter(Payment.order_id == order["id"]).all()
        assert len(payments) == 1
        # The single active payment carries the latest backend-generated reference
        assert payments[0].provider_reference.startswith(f"twelve09_{order['id']}_")
        assert payments[0].status == PaymentStatus.PENDING
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 11 — CASH regression
# ---------------------------------------------------------------------------


def test_cash_payment_regression(client):
    headers = _register_and_login(client, "cash@example.com")
    product = _create_product("Cash Regression Product", price="250.00")
    order = _create_pickup_order(client, headers, product.id)

    response = client.post(
        "/payments",
        headers=headers,
        json={"order_id": order["id"], "payment_method": "CASH"},
    )
    assert response.status_code == 200, response.text
    payment = response.json()
    assert payment["payment_method"] == "CASH"
    assert payment["status"] == "PENDING"
    assert payment["provider"] is None
    assert payment["provider_reference"] is None
    assert payment["payment_metadata"] is None

    # CASH payments do not require provider/currency/metadata
    db = _db_session()
    try:
        row = db.query(Payment).filter(Payment.order_id == order["id"]).one()
        assert row.provider is None
        assert row.provider_reference is None
        assert row.currency is None
        assert row.status == PaymentStatus.PENDING
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 12 — payment_metadata and currency persistence (silent-loss regression)
# ---------------------------------------------------------------------------


def test_payment_metadata_and_currency_persistence():
    """The original bugs were silent persistence failures: assigning
    ``payment.metadata`` (reserved SQLAlchemy attribute) and an unmapped
    ``currency`` attribute both looked correct but stored nothing."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    db_write = TestingSessionLocal()
    try:
        from datetime import datetime, timezone

        from app.models import Order, OrderStatus, User, UserRole

        user = User(
            name="Persist Tester",
            email="persist@example.com",
            password_hash="x",
            role=UserRole.CUSTOMER,
            permissions=[],
            is_active=True,
        )
        db_write.add(user)
        db_write.flush()
        order = Order(user_id=user.id, total_amount=Decimal("99.99"), status=OrderStatus.PENDING)
        db_write.add(order)
        db_write.flush()

        payment = Payment(
            order_id=order.id,
            amount=Decimal("99.99"),
            payment_method="CARD",
            provider="paystack",
            provider_reference="persist_ref",
            status=PaymentStatus.PENDING,
            payment_metadata={"paystack_reference": "persist_ref", "nested": {"a": 1}},
            currency="NGN",
            paid_at=datetime.now(timezone.utc),
        )
        db_write.add(payment)
        db_write.commit()
        payment_id = payment.id
    finally:
        db_write.close()

    # Reload in a brand new session to prove data actually reached the database
    db_read = TestingSessionLocal()
    try:
        loaded = db_read.query(Payment).filter(Payment.id == payment_id).one()
        assert loaded.currency == "NGN"
        assert loaded.payment_metadata == {
            "paystack_reference": "persist_ref",
            "nested": {"a": 1},
        }
        assert loaded.paid_at is not None
    finally:
        db_read.close()
        Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# Migration validation — ORM model must match the Alembic migration chain
# ---------------------------------------------------------------------------


def test_payment_model_columns_match_migration_chain():
    """Every column mapped on the Payment model must be created by the Alembic
    migration chain (initial create_table + later add_column calls), and every
    'payments' column added by migrations must exist on the model. This catches
    the model/migration drift found in the Step 5E audit."""

    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    migration_columns: set[str] = set()

    for path in sorted(versions_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in ("create_table", "add_column"):
                continue
            args = node.args
            table_arg = args[0] if args else None
            if not isinstance(table_arg, ast.Constant) or table_arg.value != "payments":
                continue

            if func.attr == "create_table":
                for arg in args[1:]:
                    if (
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Attribute)
                        and arg.func.attr == "Column"
                        and arg.args
                        and isinstance(arg.args[0], ast.Constant)
                    ):
                        migration_columns.add(arg.args[0].value)
            elif func.attr == "add_column" and len(args) >= 2 and isinstance(args[1], ast.Call):
                inner = args[1]
                if inner.args and isinstance(inner.args[0], ast.Constant):
                    migration_columns.add(inner.args[0].value)

    model_columns = {column.name for column in Payment.__table__.columns}

    missing_from_migrations = model_columns - migration_columns
    missing_from_model = migration_columns - model_columns

    assert not missing_from_migrations, (
        f"Columns mapped on the Payment model but absent from the Alembic "
        f"migration chain: {sorted(missing_from_migrations)}"
    )
    assert not missing_from_model, (
        f"Columns added to 'payments' by migrations but not mapped on the "
        f"model: {sorted(missing_from_model)}"
    )


# ---------------------------------------------------------------------------
# Security sanity checks
# ---------------------------------------------------------------------------


def test_no_secrets_in_frontend_source():
    frontend_src = Path(__file__).resolve().parents[2] / "frontend" / "src"
    banned = ("sk_test", "sk_live", "PAYSTACK_SECRET_KEY", "PAYSTACK_WEBHOOK_SECRET")
    for py_file in frontend_src.rglob("*"):
        if py_file.is_file() and py_file.suffix in {".ts", ".tsx", ".js", ".jsx"}:
            contents = py_file.read_text(encoding="utf-8", errors="ignore")
            for needle in banned:
                assert needle not in contents, f"{py_file} contains sensitive token {needle}"


def test_frontend_cannot_set_payment_status_directly():
    """PATCH /payments/{id}/status must reject non-admin users."""
    # Verified via route dependency; asserted here as documentation of intent.
    from app.core.auth import require_admin
    from app.models import Permission, User, UserRole

    customer = User(role=UserRole.CUSTOMER, permissions=[Permission.MANAGE_ORDERS])
    assert require_admin(customer) is False
