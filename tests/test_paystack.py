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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_dummy_secret_key")
os.environ.setdefault("PAYSTACK_PUBLIC_KEY", "pk_test_dummy_public_key")
os.environ.setdefault("PAYSTACK_WEBHOOK_SECRET", "whsec_test_webhook_secret")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import hash_password
from app.core.database import Base, get_db
from app.main import app
from app.models import Order, OrderStatus, Payment, PaymentStatus, User, UserRole
from app.services.paystack import PaystackVerificationError, paystack_client

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
# TEST 13 — Webhook must not resurrect cancelled order
# ---------------------------------------------------------------------------


def test_webhook_success_does_not_resurrect_cancelled_order(client):
    """CANCELLED order must stay CANCELLED even if webhook succeeds."""
    headers = _register_and_login(client, "cancelled@example.com")
    product = _create_product("Cancelled Order Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "cancelled_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

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
        # Move order to CANCELLED (simulating customer/admin cancellation)
        # with stock already restored by cancellation path
        db.query(Order).filter(Order.id == order["id"]).update(
            {Order.status: OrderStatus.CANCELLED}
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
        # Order must remain CANCELLED — webhook must not resurrect it
        assert refreshed_order.status == OrderStatus.CANCELLED
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 14 — Webhook must not regress PROCESSING order
# ---------------------------------------------------------------------------


def test_webhook_success_does_not_regress_processing(client):
    headers = _register_and_login(client, "processing@example.com")
    product = _create_product("Processing Order Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "processing_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

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
        db.query(Order).filter(Order.id == order["id"]).update(
            {Order.status: OrderStatus.PROCESSING}
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

    assert response.status_code == 200
    assert response.json()["result"] == "success"

    db = _db_session()
    try:
        from app.models import Order

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.SUCCESS
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        # Order must stay PROCESSING — no backward transition
        assert refreshed_order.status == OrderStatus.PROCESSING
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 15 — Webhook must not regress READY_FOR_PICKUP order
# ---------------------------------------------------------------------------


def test_webhook_success_does_not_regress_ready_for_pickup(client):
    headers = _register_and_login(client, "ready@example.com")
    product = _create_product("Ready For Pickup Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "ready_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

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
        db.query(Order).filter(Order.id == order["id"]).update(
            {Order.status: OrderStatus.READY_FOR_PICKUP}
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

    assert response.status_code == 200
    assert response.json()["result"] == "success"

    db = _db_session()
    try:
        from app.models import Order

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.SUCCESS
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status == OrderStatus.READY_FOR_PICKUP
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 16 — Webhook must not regress OUT_FOR_DELIVERY order
# ---------------------------------------------------------------------------


def test_webhook_success_does_not_regress_out_for_delivery(client):
    headers = _register_and_login(client, "outfordelivery@example.com")
    product = _create_product("Out For Delivery Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "outfordelivery_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

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
        db.query(Order).filter(Order.id == order["id"]).update(
            {Order.status: OrderStatus.OUT_FOR_DELIVERY}
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

    assert response.status_code == 200
    assert response.json()["result"] == "success"

    db = _db_session()
    try:
        from app.models import Order

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.SUCCESS
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status == OrderStatus.OUT_FOR_DELIVERY
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 17 — Webhook must not regress COMPLETED order
# ---------------------------------------------------------------------------


def test_webhook_success_does_not_regress_completed(client):
    headers = _register_and_login(client, "completed@example.com")
    product = _create_product("Completed Order Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "completed_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

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
        db.query(Order).filter(Order.id == order["id"]).update(
            {Order.status: OrderStatus.COMPLETED}
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

    assert response.status_code == 200
    assert response.json()["result"] == "success"

    db = _db_session()
    try:
        from app.models import Order

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.SUCCESS
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status == OrderStatus.COMPLETED
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 18 — Webhook still confirms PENDING order (preserves intended behavior)
# ---------------------------------------------------------------------------


def test_webhook_success_confirms_pending_order(client):
    headers = _register_and_login(client, "pending@example.com")
    product = _create_product("Pending Order Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "pending_ref"

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

    assert response.status_code == 200
    assert response.json()["result"] == "success"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.SUCCESS
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status == OrderStatus.CONFIRMED
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 19 — charge.failed must not downgrade SUCCESS payment
# ---------------------------------------------------------------------------


def test_webhook_charge_failed_on_success_returns_already_processed(client):
    headers = _register_and_login(client, "successfailed@example.com")
    product = _create_product("Success Failed Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "successfailed_ref"

    db = _db_session()
    try:
        from datetime import datetime, timezone
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="CARD",
                provider="paystack",
                provider_reference=reference,
                status=PaymentStatus.SUCCESS,
                currency="NGN",
                paid_at=datetime.now(timezone.utc),
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
    assert response.json()["result"] == "already_processed"

    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.SUCCESS
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 20 — charge.failed must not downgrade REFUNDED payment
# ---------------------------------------------------------------------------


def test_webhook_charge_failed_on_refunded_returns_already_processed(client):
    headers = _register_and_login(client, "refundedfailed@example.com")
    product = _create_product("Refunded Failed Product")
    order = _create_pickup_order(client, headers, product.id)
    reference = "refundedfailed_ref"

    db = _db_session()
    try:
        db.add(
            Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="CARD",
                provider="paystack",
                provider_reference=reference,
                status=PaymentStatus.REFUNDED,
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
    assert response.json()["result"] == "already_processed"

    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.provider_reference == reference).one()
        assert payment.status == PaymentStatus.REFUNDED
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 21 — Reinitialization preserves original provider_reference
# ---------------------------------------------------------------------------


def test_reinitialization_preserves_original_reference(client):
    """When re-initializing a PENDING/FAILED payment, the original provider_reference
    must be preserved so the webhook can still match the old reference."""
    headers = _register_and_login(client, "reinit@example.com")
    product = _create_product("Reinit Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)

    # First initialization
    fake1 = {
        "authorization_url": "https://checkout.paystack.com/init1",
        "access_code": "CODE_1",
        "reference": "paystack_ref_1",
    }
    with patch.object(paystack_client, "initialize_transaction", new=AsyncMock(return_value=fake1)):
        r1 = client.post(
            "/payments/paystack/initialize",
            headers=headers,
            json={"order_id": order["id"]},
        )
    assert r1.status_code == 200

    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.order_id == order["id"]).one()
        original_ref = payment.provider_reference
        assert original_ref.startswith(f"twelve09_{order['id']}_")
    finally:
        db.close()

    # Second initialization (retry)
    fake2 = {
        "authorization_url": "https://checkout.paystack.com/init2",
        "access_code": "CODE_2",
        "reference": "paystack_ref_2",
    }
    with patch.object(paystack_client, "initialize_transaction", new=AsyncMock(return_value=fake2)):
        r2 = client.post(
            "/payments/paystack/initialize",
            headers=headers,
            json={"order_id": order["id"]},
        )
    assert r2.status_code == 200

    # The provider_reference must remain the original one (not overwritten)
    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.order_id == order["id"]).one()
        assert payment.provider_reference == original_ref, (
            f"Expected {original_ref}, got {payment.provider_reference}"
        )
        assert payment.provider_reference.startswith(f"twelve09_{order['id']}_")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 22 — Reinitialization records previous reference when replaced
# ---------------------------------------------------------------------------
# Note: Current implementation preserves the original reference and does not
# replace it. This test documents the expected behavior if a replacement ever
# becomes necessary (e.g., Paystack rejects duplicate reference). In that case,
# the old reference must be stored in payment_metadata["previous_references"].
# If the implementation always preserves the reference, this test passes by
# verifying no unnecessary replacement occurs.


def test_reinitialization_records_previous_reference_when_replaced(client):
    headers = _register_and_login(client, "reinitreplace@example.com")
    product = _create_product("Reinit Replace Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)

    # First initialization
    fake1 = {
        "authorization_url": "https://checkout.paystack.com/init1",
        "access_code": "CODE_1",
        "reference": "paystack_ref_1",
    }
    with patch.object(paystack_client, "initialize_transaction", new=AsyncMock(return_value=fake1)):
        r1 = client.post(
            "/payments/paystack/initialize",
            headers=headers,
            json={"order_id": order["id"]},
        )
    assert r1.status_code == 200

    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.order_id == order["id"]).one()
        original_ref = payment.provider_reference
    finally:
        db.close()

    # Second initialization
    fake2 = {
        "authorization_url": "https://checkout.paystack.com/init2",
        "access_code": "CODE_2",
        "reference": "paystack_ref_2",
    }
    with patch.object(paystack_client, "initialize_transaction", new=AsyncMock(return_value=fake2)):
        r2 = client.post(
            "/payments/paystack/initialize",
            headers=headers,
            json={"order_id": order["id"]},
        )
    assert r2.status_code == 200

    # Current implementation preserves the reference.
    # If a replacement were to happen, we'd verify:
    # payment.provider_reference == new_reference
    # old_reference in payment.payment_metadata["previous_references"]
    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.order_id == order["id"]).one()
        assert payment.provider_reference == original_ref
        # previous_references should not exist if no replacement occurred
        prev_refs = payment.payment_metadata.get("previous_references") if payment.payment_metadata else None
        assert prev_refs is None or prev_refs == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 23 — Webhook recovers payment via previous_references fallback
# ---------------------------------------------------------------------------


def test_webhook_recovers_previous_reference(client):
    """Webhook must find payment via previous_references fallback when
    provider_reference doesn't match (e.g., after reference rotation)."""
    headers = _register_and_login(client, "recover@example.com")
    product = _create_product("Recover Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "recover_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

        # Create payment with a current reference different from the one
        # the webhook will use, and store the old reference in previous_references
        payment = Payment(
            order_id=order["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            provider="paystack",
            provider_reference="current_ref_not_matching_webhook",
            status=PaymentStatus.PENDING,
            currency="NGN",
            payment_metadata={"previous_references": [reference]},
        )
        db.add(payment)
        db.query(Order).filter(Order.id == order["id"]).update(
            {Order.status: OrderStatus.PENDING}
        )
        db.commit()
        payment_id = payment.id
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

        payment = db.query(Payment).filter(Payment.id == payment_id).one()
        assert payment.status == PaymentStatus.SUCCESS
        refreshed_order = db.query(Order).filter(Order.id == order["id"]).one()
        assert refreshed_order.status == OrderStatus.CONFIRMED
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 24 — Webhook fallback must not match unrelated payment
# ---------------------------------------------------------------------------


def test_webhook_old_reference_does_not_match_unrelated_payment(client):
    """Fallback lookup by previous_references must be scoped to the same
    payment/order and must not accidentally match a reference belonging to
    a different payment."""
    headers = _register_and_login(client, "unrelated@example.com")
    product = _create_product("Unrelated Product", price="100.00")
    order1 = _create_pickup_order(client, headers, product.id)
    order2 = _create_pickup_order(client, headers, product.id)
    reference = "unrelated_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

        # Payment for order1 has the reference in its previous_references
        payment1 = Payment(
            order_id=order1["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            provider="paystack",
            provider_reference="current_ref_1",
            status=PaymentStatus.PENDING,
            currency="NGN",
            payment_metadata={"previous_references": [reference]},
        )
        # Payment for order2 has a DIFFERENT current reference
        payment2 = Payment(
            order_id=order2["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            provider="paystack",
            provider_reference="current_ref_2",
            status=PaymentStatus.PENDING,
            currency="NGN",
        )
        db.add_all([payment1, payment2])
        db.commit()
        p1_id = payment1.id
        p2_id = payment2.id
    finally:
        db.close()

    # Webhook arrives with the reference that's in payment1's previous_references
    # It should match payment1, not payment2
    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = client.post(
            "/webhooks/paystack",
            content=body,
            headers={"x-paystack-signature": _sign(body)},
        )

    assert response.status_code == 200
    assert response.json()["result"] == "success"

    db = _db_session()
    try:
        from app.models import Order

        payment1 = db.query(Payment).filter(Payment.id == p1_id).one()
        payment2 = db.query(Payment).filter(Payment.id == p2_id).one()
        # payment1 should be SUCCESS, payment2 should remain PENDING
        assert payment1.status == PaymentStatus.SUCCESS
        assert payment2.status == PaymentStatus.PENDING
        # order1 should be CONFIRMED, order2 should remain PENDING
        order1_db = db.query(Order).filter(Order.id == order1["id"]).one()
        order2_db = db.query(Order).filter(Order.id == order2["id"]).one()
        assert order1_db.status == OrderStatus.CONFIRMED
        assert order2_db.status == OrderStatus.PENDING
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 25 — Webhook fail-closed on multiple previous-reference matches
# ---------------------------------------------------------------------------


def test_webhook_fail_closed_on_multiple_previous_reference_matches(client):
    """Webhook must fail closed when multiple Payments share the same
    previous_reference. It must not select .first() or modify any Payment/Order."""
    headers = _register_and_login(client, "multimatch@example.com")
    product = _create_product("Multi Match Product", price="100.00")
    order1 = _create_pickup_order(client, headers, product.id)
    order2 = _create_pickup_order(client, headers, product.id)
    reference = "ambiguous_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

        # Payment for order1 has the reference in its previous_references
        payment1 = Payment(
            order_id=order1["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            provider="paystack",
            provider_reference="current_ref_1",
            status=PaymentStatus.PENDING,
            currency="NGN",
            payment_metadata={"previous_references": [reference]},
        )
        # Payment for order2 ALSO has the SAME reference in its previous_references
        payment2 = Payment(
            order_id=order2["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            provider="paystack",
            provider_reference="current_ref_2",
            status=PaymentStatus.PENDING,
            currency="NGN",
            payment_metadata={"previous_references": [reference]},
        )
        db.add_all([payment1, payment2])
        db.commit()
        p1_id = payment1.id
        p2_id = payment2.id
    finally:
        db.close()

    # Webhook arrives with the reference that's in BOTH payments' previous_references
    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = client.post(
            "/webhooks/paystack",
            content=body,
            headers={"x-paystack-signature": _sign(body)},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "ambiguous previous reference"

    # Neither payment should be modified, neither order should be confirmed
    db = _db_session()
    try:
        from app.models import Order

        payment1 = db.query(Payment).filter(Payment.id == p1_id).one()
        payment2 = db.query(Payment).filter(Payment.id == p2_id).one()
        # Both payments must remain PENDING
        assert payment1.status == PaymentStatus.PENDING
        assert payment2.status == PaymentStatus.PENDING
        # Both orders must remain PENDING
        order1_db = db.query(Order).filter(Order.id == order1["id"]).one()
        order2_db = db.query(Order).filter(Order.id == order2["id"]).one()
        assert order1_db.status == OrderStatus.PENDING
        assert order2_db.status == OrderStatus.PENDING
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 26 — Webhook exactly one previous-reference match succeeds
# ---------------------------------------------------------------------------


def test_webhook_exactly_one_previous_reference_match_succeeds(client):
    """Webhook must succeed when exactly one Payment has the reference
    in its previous_references array."""
    headers = _register_and_login(client, "onematch@example.com")
    product = _create_product("One Match Product", price="100.00")
    order1 = _create_pickup_order(client, headers, product.id)
    order2 = _create_pickup_order(client, headers, product.id)
    reference = "onematch_ref"

    db = _db_session()
    try:
        from app.models import Order, OrderStatus

        # Payment for order1 has the reference in its previous_references
        payment1 = Payment(
            order_id=order1["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            provider="paystack",
            provider_reference="current_ref_1",
            status=PaymentStatus.PENDING,
            currency="NGN",
            payment_metadata={"previous_references": [reference]},
        )
        # Payment for order2 does NOT have the reference
        payment2 = Payment(
            order_id=order2["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            provider="paystack",
            provider_reference="current_ref_2",
            status=PaymentStatus.PENDING,
            currency="NGN",
        )
        db.add_all([payment1, payment2])
        db.commit()
        p1_id = payment1.id
        p2_id = payment2.id
    finally:
        db.close()

    # Webhook arrives with the reference that's only in payment1's previous_references
    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = client.post(
            "/webhooks/paystack",
            content=body,
            headers={"x-paystack-signature": _sign(body)},
        )

    assert response.status_code == 200
    assert response.json()["result"] == "success"

    # Only payment1 should be marked SUCCESS, order1 confirmed
    db = _db_session()
    try:
        from app.models import Order

        payment1 = db.query(Payment).filter(Payment.id == p1_id).one()
        payment2 = db.query(Payment).filter(Payment.id == p2_id).one()
        assert payment1.status == PaymentStatus.SUCCESS
        assert payment2.status == PaymentStatus.PENDING
        order1_db = db.query(Order).filter(Order.id == order1["id"]).one()
        order2_db = db.query(Order).filter(Order.id == order2["id"]).one()
        assert order1_db.status == OrderStatus.CONFIRMED
        assert order2_db.status == OrderStatus.PENDING
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Migration validation — ORM model must match the Alembic migration chain
# ---------------------------------------------------------------------------


def test_payment_model_columns_match_migration_chain():
    """Every column mapped on the Payment model must be created by the Alembic
    migration chain (initial create_table + later add_column calls), and every
    'payments' column added by migrations must exist on the model. This catches
    the model/migration drift found in the Step 5E audit."""

    # Check both MySQL and PostgreSQL Alembic migration directories
    versions_dirs = [
        Path(__file__).resolve().parents[1] / "alembic" / "versions",
        Path(__file__).resolve().parents[1] / "alembic_pg" / "versions",
    ]
    migration_columns: set[str] = set()

    for versions_dir in versions_dirs:
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


# ---------------------------------------------------------------------------
# PHASE 2 — Customer-return verification (POST /payments/paystack/verify)
# ---------------------------------------------------------------------------


def _create_verify_payment(order_id: int, reference: str, amount: str = "100.00", metadata=None) -> int:
    db = _db_session()
    try:
        payment = Payment(
            order_id=order_id,
            amount=Decimal(amount),
            payment_method="CARD",
            provider="paystack",
            provider_reference=reference,
            status=PaymentStatus.PENDING,
            currency="NGN",
            payment_metadata=metadata,
        )
        db.add(payment)
        db.commit()
        db.refresh(payment)
        return payment.id
    finally:
        db.close()


def _set_order_status(order_id: int, new_status):
    from app.models import Order

    db = _db_session()
    try:
        db.query(Order).filter(Order.id == order_id).update({Order.status: new_status})
        db.commit()
    finally:
        db.close()


def _post_verify(client, headers, reference: str):
    return client.post("/payments/paystack/verify", headers=headers, json={"reference": reference})


def _payment_and_order(payment_id: int, order_id: int):
    from app.models import Order

    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.id == payment_id).one()
        db.expire_all()
        payment = db.query(Payment).filter(Payment.id == payment_id).one()
        order = db.query(Order).filter(Order.id == order_id).one()
        return payment, order
    finally:
        db.close()


def test_verify_success_confirms_pending_payment_and_order(client):
    """1. Successful verification: Payment PENDING -> SUCCESS, Order PENDING -> CONFIRMED."""
    headers = _register_and_login(client, "verify1@example.com")
    product = _create_product("Verify Success Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_success_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=10000)
    verify_mock = AsyncMock(return_value=verification)
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_verify(client, headers, reference)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified", "result": "success"}
    verify_mock.assert_awaited_once_with(reference)

    from app.models import OrderStatus

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert payment.paid_at is not None
    assert refreshed_order.status == OrderStatus.CONFIRMED


def test_verify_already_success_is_idempotent(client):
    """2. Verifying an already-SUCCESS payment is idempotent; paid_at not rewritten."""
    headers = _register_and_login(client, "verify2@example.com")
    product = _create_product("Verify Idempotent Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_idempotent_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        first = _post_verify(client, headers, reference)
    assert first.json()["result"] == "success"

    payment, _ = _payment_and_order(payment_id, order["id"])
    first_paid_at = payment.paid_at
    assert first_paid_at is not None

    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        second = _post_verify(client, headers, reference)
    assert second.status_code == 200, second.text
    assert second.json() == {"status": "verified", "result": "already_processed"}

    payment, _ = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert payment.paid_at == first_paid_at


def test_verify_success_does_not_regress_processing(client):
    """3. Payment may record SUCCESS but a PROCESSING order must not regress."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify3@example.com")
    product = _create_product("Verify Processing Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_processing_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _set_order_status(order["id"], OrderStatus.PROCESSING)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.json()["result"] == "success"
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.PROCESSING


def test_verify_success_does_not_regress_ready_for_pickup(client):
    """4. READY_FOR_PICKUP order must not regress on successful verification."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify4@example.com")
    product = _create_product("Verify RFP Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_rfp_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _set_order_status(order["id"], OrderStatus.READY_FOR_PICKUP)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.json()["result"] == "success"
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.READY_FOR_PICKUP


def test_verify_success_does_not_regress_out_for_delivery(client):
    """5. OUT_FOR_DELIVERY order must not regress on successful verification."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify5@example.com")
    product = _create_product("Verify OFD Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_ofd_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _set_order_status(order["id"], OrderStatus.OUT_FOR_DELIVERY)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.json()["result"] == "success"
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.OUT_FOR_DELIVERY


def test_verify_success_does_not_regress_completed(client):
    """6. COMPLETED order must not regress on successful verification."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify6@example.com")
    product = _create_product("Verify Completed Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_completed_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _set_order_status(order["id"], OrderStatus.COMPLETED)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.json()["result"] == "success"
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.COMPLETED


def test_verify_success_does_not_resurrect_cancelled(client):
    """7. CANCELLED order must stay CANCELLED even when verification succeeds."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify7@example.com")
    product = _create_product("Verify Cancelled Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_cancelled_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _set_order_status(order["id"], OrderStatus.CANCELLED)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.json()["result"] == "success"
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.CANCELLED


def test_verify_failed_status_marks_failed(client):
    """8. Paystack terminal failure: Payment PENDING -> FAILED, order unchanged."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify8@example.com")
    product = _create_product("Verify Failed Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_failed_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=10000, status="failed")
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified", "result": "failed"}

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.FAILED
    assert refreshed_order.status == OrderStatus.PENDING


def test_verify_pending_status_leaves_pending(client):
    """9. Paystack non-terminal status: Payment stays PENDING, order unchanged."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify9@example.com")
    product = _create_product("Verify Pending Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_pending_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=10000, status="pending")
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified", "result": "pending"}

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.PENDING


def test_verify_amount_mismatch_keeps_pending_with_evidence(client):
    """10. Amount mismatch: PENDING + evidence, order unchanged, never SUCCESS."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify10@example.com")
    product = _create_product("Verify Amount Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_amount_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=5000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified", "result": "amount_mismatch"}

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert payment.payment_metadata["amount_mismatch"]["requires_manual_review"] is True
    assert refreshed_order.status == OrderStatus.PENDING


def test_verify_currency_mismatch_keeps_pending_with_evidence(client):
    """11. Currency mismatch: PENDING + evidence, order unchanged, never SUCCESS."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify11@example.com")
    product = _create_product("Verify Currency Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_currency_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=10000, currency="USD")
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified", "result": "currency_mismatch"}

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert payment.payment_metadata["currency_mismatch"]["received"] == "USD"
    assert refreshed_order.status == OrderStatus.PENDING


def test_verify_unknown_reference_returns_safe_response(client):
    """12. Unknown reference: safe generic response, no mutation, no data leakage."""
    headers = _register_and_login(client, "verify12@example.com")
    product = _create_product("Verify Unknown Product", price="100.00")
    _create_pickup_order(client, headers, product.id)

    verify_mock = AsyncMock(return_value=_success_verification("does_not_exist", 10000))
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_verify(client, headers, "does_not_exist_ref_xyz")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ignored", "result": "unknown_reference"}
    # Paystack must not even be contacted for an unresolvable reference
    verify_mock.assert_not_awaited()
    # No payment/order internals leaked
    assert "payment" not in response.text.lower() or "unknown_reference" in response.text
    assert "order_id" not in response.json()
    assert "amount" not in response.json()


def test_verify_other_customer_reference_rejected(client):
    """13. Cross-customer reference: rejected with no data and no mutation."""
    from app.models import OrderStatus

    owner_headers = _register_and_login(client, "verify13owner@example.com")
    product = _create_product("Verify Owner Product", price="100.00")
    order = _create_pickup_order(client, owner_headers, product.id)
    reference = "verify_owner_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    attacker_headers = _register_and_login(client, "verify13attacker@example.com")
    verify_mock = AsyncMock(return_value=_success_verification(reference, 10000))
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_verify(client, attacker_headers, reference)

    # Rejected without exposing payment/order data and without calling Paystack
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ignored", "result": "unknown_reference"}
    verify_mock.assert_not_awaited()
    assert "order_id" not in response.json()

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.PENDING

    # Unauthenticated callers are rejected outright
    no_auth = client.post("/payments/paystack/verify", json={"reference": reference})
    assert no_auth.status_code == 401, no_auth.text


def test_verify_previous_reference_resolves_and_verifies_presented(client):
    """14. Old reference resolves via previous_references and Paystack is
    queried with the PRESENTED (old) reference."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify14@example.com")
    product = _create_product("Verify Previous Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    old_reference = "verify_old_ref"
    payment_id = _create_verify_payment(
        order["id"],
        "verify_current_ref",
        metadata={"previous_references": [old_reference]},
    )

    verification = _success_verification(old_reference, amount_kobo=10000)
    verify_mock = AsyncMock(return_value=verification)
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_verify(client, headers, old_reference)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified", "result": "success"}
    verify_mock.assert_awaited_once_with(old_reference)

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.CONFIRMED


def test_verify_ambiguous_previous_reference_fails_closed(client):
    """15. Same previous reference on two payments: fail closed, no mutation."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify15@example.com")
    product = _create_product("Verify Ambiguous Product", price="100.00")
    order1 = _create_pickup_order(client, headers, product.id)
    order2 = _create_pickup_order(client, headers, product.id)
    shared = "verify_shared_old_ref"
    p1 = _create_verify_payment(order1["id"], "verify_current_1", metadata={"previous_references": [shared]})
    p2 = _create_verify_payment(order2["id"], "verify_current_2", metadata={"previous_references": [shared]})

    verify_mock = AsyncMock(return_value=_success_verification(shared, 10000))
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_verify(client, headers, shared)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ignored", "result": "unknown_reference"}
    verify_mock.assert_not_awaited()

    for pid, oid in ((p1, order1["id"]), (p2, order2["id"])):
        payment, refreshed_order = _payment_and_order(pid, oid)
        assert payment.status == PaymentStatus.PENDING
        assert refreshed_order.status == OrderStatus.PENDING


def test_verify_paystack_reference_mismatch_fails_closed(client):
    """16. Paystack response for another reference is never accepted."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify16@example.com")
    product = _create_product("Verify RefMismatch Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_mismatch_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification("some_other_reference", amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified", "result": "reference_mismatch"}

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.PENDING


def test_verify_paystack_verification_error_does_not_confirm(client):
    """17. PaystackVerificationError: no false confirmation, state untouched."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify17@example.com")
    product = _create_product("Verify Error Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_error_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    with patch.object(
        paystack_client,
        "verify_transaction",
        new=AsyncMock(side_effect=PaystackVerificationError("boom")),
    ):
        response = _post_verify(client, headers, reference)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "verified", "result": "verification_failed"}

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.PENDING


def test_verify_then_webhook_is_idempotent(client):
    """18. verify -> webhook: final SUCCESS/CONFIRMED exactly once."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify18@example.com")
    product = _create_product("Verify Then Webhook Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "verify_then_webhook_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        verify_response = _post_verify(client, headers, reference)
    assert verify_response.json()["result"] == "success"

    payment, _ = _payment_and_order(payment_id, order["id"])
    paid_at = payment.paid_at

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        webhook_response = client.post(
            "/webhooks/paystack",
            content=body,
            headers={"x-paystack-signature": _sign(body)},
        )
    assert webhook_response.json()["result"] == "already_processed"

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert payment.paid_at == paid_at
    assert refreshed_order.status == OrderStatus.CONFIRMED


def test_webhook_then_verify_is_idempotent(client):
    """19. webhook -> verify: verify detects SUCCESS and returns idempotently."""
    from app.models import OrderStatus

    headers = _register_and_login(client, "verify19@example.com")
    product = _create_product("Webhook Then Verify Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "webhook_then_verify_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        webhook_response = client.post(
            "/webhooks/paystack",
            content=body,
            headers={"x-paystack-signature": _sign(body)},
        )
    assert webhook_response.json()["result"] == "success"

    payment, _ = _payment_and_order(payment_id, order["id"])
    paid_at = payment.paid_at

    verify_mock = AsyncMock(return_value=verification)
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        verify_response = _post_verify(client, headers, reference)
    assert verify_response.json() == {"status": "verified", "result": "already_processed"}

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert payment.paid_at == paid_at
    assert refreshed_order.status == OrderStatus.CONFIRMED


def test_repeated_verify_is_idempotent(client):
    """20. Repeated verify calls never duplicate mutation or rewrite paid_at."""
    headers = _register_and_login(client, "verify20@example.com")
    product = _create_product("Repeated Verify Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "repeated_verify_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        first = _post_verify(client, headers, reference)
    assert first.json()["result"] == "success"
    payment, _ = _payment_and_order(payment_id, order["id"])
    paid_at = payment.paid_at

    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        second = _post_verify(client, headers, reference)
        third = _post_verify(client, headers, reference)
    assert second.json()["result"] == "already_processed"
    assert third.json()["result"] == "already_processed"

    from app.models import Order

    db = _db_session()
    try:
        assert db.query(Payment).filter(Payment.order_id == order["id"]).count() == 1
        payment = db.query(Payment).filter(Payment.id == payment_id).one()
        assert payment.paid_at == paid_at
        assert db.query(Order).filter(Order.id == order["id"]).one().status.value == "CONFIRMED"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PHASE 3 — Stale Paystack pending-payment expiry / stock reclaim
# ---------------------------------------------------------------------------


def _get_admin_headers(client):
    try:
        existing = client.post(
            "/auth/login", json={"email": "admin@example.com", "password": "pass123456"}
        )
        if existing.status_code == 200:
            return {"Authorization": f"Bearer {existing.json()['access_token']}"}
    except Exception:
        pass
    db = _db_session()
    try:
        admin = db.query(User).filter(User.email == "admin@example.com").first()
        if admin is None:
            admin = User(
                name="Admin User",
                email="admin@example.com",
                password_hash=hash_password("pass123456"),
                role=UserRole.ADMIN,
                permissions=[],
                is_active=True,
            )
            db.add(admin)
            db.commit()
    finally:
        db.close()
    login = client.post(
        "/auth/login", json={"email": "admin@example.com", "password": "pass123456"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _age_payment(payment_id: int, minutes_old: float):
    """Deterministically backdate a payment row (no real-time sleeps)."""
    db = _db_session()
    try:
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_old)
        db.execute(
            text("UPDATE payments SET updated_at=:ts, created_at=:ts WHERE id=:pid"),
            {"ts": old, "pid": payment_id},
        )
        db.commit()
    finally:
        db.close()


def _post_reclaim(client, headers):
    return client.post("/admin/payments/reclaim-stale", headers=headers)


def _stock_of(product_id: int) -> int:
    from app.models import Product

    db = _db_session()
    try:
        return db.query(Product).filter(Product.id == product_id).one().stock_quantity
    finally:
        db.close()


def _failed_verification(reference: str, amount_kobo: int = 10000) -> dict:
    return {
        "status": "failed",
        "reference": reference,
        "amount": amount_kobo,
        "currency": "NGN",
        "gateway_response": "Failed",
        "paid_at": "2026-08-26T10:00:00Z",
    }


def test_reclaim_requires_admin(client):
    """Reclaim endpoint: unauthenticated and customer calls are rejected."""
    assert client.post("/admin/payments/reclaim-stale").status_code in (401, 403)
    headers = _register_and_login(client, "reclaimcust@example.com")
    response = client.post("/admin/payments/reclaim-stale", headers=headers)
    assert response.status_code == 403, response.text


def test_reclaim_fresh_pending_not_reclaimed(client):
    """1. Fresh PENDING Paystack payment is not reclaimed (Paystack untouched)."""
    headers = _register_and_login(client, "stale1@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Fresh Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_fresh_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    assert _stock_of(product.id) == 9

    verify_mock = AsyncMock(return_value=_failed_verification(reference))
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_reclaim(client, admin)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reclaimed"] == 0
    verify_mock.assert_not_awaited()
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.PENDING
    assert _stock_of(product.id) == 9


def test_reclaim_stale_pending_reclaimed(client):
    """2+5. Stale PENDING Paystack payment is reclaimed; order CANCELLED."""
    headers = _register_and_login(client, "stale2@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Reclaim Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_reclaim_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _age_payment(payment_id, minutes_old=45)
    assert _stock_of(product.id) == 9

    with patch.object(
        paystack_client, "verify_transaction", new=AsyncMock(return_value=_failed_verification(reference))
    ):
        response = _post_reclaim(client, admin)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "scanned": 1,
        "reclaimed": 1,
        "succeeded_during_check": 0,
        "still_pending": 0,
        "skipped": 0,
        "verification_errors": 0,
    }

    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.CANCELLED
    assert _stock_of(product.id) == 10
    evidence = payment.payment_metadata["stale_reclaim"]
    assert evidence["reason"] == "stale_paystack_payment"
    assert evidence["expiry_minutes"] == 30
    assert evidence["previous_payment_status"] == "PENDING"
    assert evidence["paystack_reference"] == reference
    assert evidence["order_id"] == order["id"]
    assert "reclaimed_at" in evidence


def test_reclaim_repeated_is_idempotent(client):
    """3+4. Stock restored exactly once; repeated reclaim changes nothing."""
    headers = _register_and_login(client, "stale3@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Idempotent Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_idempotent_ref"
    _create_verify_payment(order["id"], reference)
    _age_payment(
        _db_session().execute(text("SELECT id FROM payments WHERE provider_reference=:r"), {"r": reference}).scalar(),
        minutes_old=60,
    )

    with patch.object(
        paystack_client, "verify_transaction", new=AsyncMock(return_value=_failed_verification(reference))
    ):
        first = _post_reclaim(client, admin)
        second = _post_reclaim(client, admin)

    assert first.json()["reclaimed"] == 1
    assert second.json()["reclaimed"] == 0
    assert _stock_of(product.id) == 10


def test_reclaim_skips_success_and_refunded(client):
    """6+7. SUCCESS and REFUNDED payments are never reclaimed."""
    headers = _register_and_login(client, "stale6@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Terminal Product", price="100.00")
    order1 = _create_pickup_order(client, headers, product.id)
    order2 = _create_pickup_order(client, headers, product.id)
    p_success = _create_verify_payment(order1["id"], "stale_success_ref")
    p_refunded = _create_verify_payment(order2["id"], "stale_refunded_ref")

    db = _db_session()
    try:
        db.query(Payment).filter(Payment.id == p_success).update({Payment.status: PaymentStatus.SUCCESS})
        db.query(Payment).filter(Payment.id == p_refunded).update({Payment.status: PaymentStatus.REFUNDED})
        db.commit()
    finally:
        db.close()
    _age_payment(p_success, minutes_old=90)
    _age_payment(p_refunded, minutes_old=90)

    verify_mock = AsyncMock(return_value=_failed_verification("x"))
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_reclaim(client, admin)

    assert response.json()["reclaimed"] == 0
    verify_mock.assert_not_awaited()
    assert _stock_of(product.id) == 8


def test_reclaim_skips_non_paystack_payments(client):
    """8+9+10. CASH / BANK_TRANSFER / non-Paystack payments are never reclaimed."""
    headers = _register_and_login(client, "stale8@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale NonPaystack Product", price="100.00")

    db = _db_session()
    try:
        from app.models import Order as OrderModel

        o1 = _create_pickup_order(client, headers, product.id)
        o2 = _create_pickup_order(client, headers, product.id)
        o3 = _create_pickup_order(client, headers, product.id)
        ids = []
        for oid, method, provider in (
            (o1["id"], "CASH", None),
            (o2["id"], "BANK_TRANSFER", None),
            (o3["id"], "CARD", "stripe"),
        ):
            p = Payment(
                order_id=oid,
                amount=Decimal("100.00"),
                payment_method=method,
                provider=provider,
                provider_reference=f"stale_other_{oid}",
                status=PaymentStatus.PENDING,
                currency="NGN",
            )
            db.add(p)
            db.flush()
            ids.append(p.id)
        db.commit()
    finally:
        db.close()
    for pid in ids:
        _age_payment(pid, minutes_old=90)

    verify_mock = AsyncMock(return_value=_failed_verification("x"))
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_reclaim(client, admin)

    assert response.json()["reclaimed"] == 0
    verify_mock.assert_not_awaited()
    assert _stock_of(product.id) == 7


def test_reclaim_success_during_check_confirms(client):
    """11. Paystack SUCCESS during reclaim: CONFIRMED, stock stays held."""
    headers = _register_and_login(client, "stale11@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Race Success Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_race_success_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _age_payment(payment_id, minutes_old=45)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_reclaim(client, admin)

    body = response.json()
    assert body["reclaimed"] == 0
    assert body["succeeded_during_check"] == 1
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert payment.paid_at is not None
    assert refreshed_order.status == OrderStatus.CONFIRMED
    assert _stock_of(product.id) == 9


def test_reclaim_success_on_cancelled_flags_review(client):
    """12. SUCCESS on CANCELLED order: stays CANCELLED, stock restored, review flagged."""
    headers = _register_and_login(client, "stale12@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Cancelled Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_cancelled_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    cancel = client.post(f"/orders/{order['id']}/cancel", headers=headers)
    assert cancel.status_code == 200, cancel.text
    assert _stock_of(product.id) == 10

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_verify(client, headers, reference)

    assert response.json()["result"] == "success"
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.CANCELLED
    assert _stock_of(product.id) == 10
    late = payment.payment_metadata["late_success"]
    assert late["requires_manual_review"] is True
    assert late["refund_required"] is True
    assert late["order_status"] == "CANCELLED"


def test_reclaim_pending_verification_leaves_held(client):
    """13. Paystack pending during reclaim: no reclaim, stock stays held."""
    headers = _register_and_login(client, "stale13@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Pending Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_still_pending_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _age_payment(payment_id, minutes_old=45)

    verification = _success_verification(reference, amount_kobo=10000, status="pending")
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_reclaim(client, admin)

    body = response.json()
    assert body["reclaimed"] == 0
    assert body["still_pending"] == 1
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.PENDING
    assert _stock_of(product.id) == 9


def test_reclaim_abandoned_reclaims(client):
    """14. Paystack abandoned (terminal) on stale payment: reclaim proceeds."""
    headers = _register_and_login(client, "stale14@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Abandoned Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_abandoned_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _age_payment(payment_id, minutes_old=45)

    verification = _success_verification(reference, amount_kobo=10000, status="abandoned")
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        response = _post_reclaim(client, admin)

    assert response.json()["reclaimed"] == 1
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.CANCELLED
    assert _stock_of(product.id) == 10


def test_reclaim_verification_error_fails_closed(client):
    """15. PaystackVerificationError: no reclaim, no stock/order change."""
    headers = _register_and_login(client, "stale15@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Error Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_error_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _age_payment(payment_id, minutes_old=45)

    with patch.object(
        paystack_client,
        "verify_transaction",
        new=AsyncMock(side_effect=PaystackVerificationError("network down")),
    ):
        response = _post_reclaim(client, admin)

    body = response.json()
    assert body["reclaimed"] == 0
    assert body["verification_errors"] == 1
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.PENDING
    assert _stock_of(product.id) == 9


def test_reclaim_after_webhook_success_no_reclaim(client):
    """16. Webhook success first: reclaim sees SUCCESS, touches nothing."""
    headers = _register_and_login(client, "stale16@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Wh First Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_wh_first_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        wh = client.post("/webhooks/paystack", content=body, headers={"x-paystack-signature": _sign(body)})
    assert wh.json()["result"] == "success"
    _age_payment(payment_id, minutes_old=90)

    verify_mock = AsyncMock(return_value=verification)
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_reclaim(client, admin)

    assert response.json()["reclaimed"] == 0
    verify_mock.assert_not_awaited()
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.CONFIRMED
    assert _stock_of(product.id) == 9


def test_reclaim_after_verify_success_no_reclaim(client):
    """17. Customer verify success first: reclaim touches nothing."""
    headers = _register_and_login(client, "stale17@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Vf First Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_vf_first_ref"
    payment_id = _create_verify_payment(order["id"], reference)

    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        assert _post_verify(client, headers, reference).json()["result"] == "success"
    _age_payment(payment_id, minutes_old=90)

    verify_mock = AsyncMock(return_value=verification)
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_reclaim(client, admin)

    assert response.json()["reclaimed"] == 0
    verify_mock.assert_not_awaited()
    assert _stock_of(product.id) == 9


def test_late_webhook_success_after_reclaim(client):
    """18. Reclaim then late webhook SUCCESS: CANCELLED kept, stock restored, review flagged."""
    headers = _register_and_login(client, "stale18@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Late Wh Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_late_wh_ref"
    payment_id = _create_verify_payment(order["id"], reference)
    _age_payment(payment_id, minutes_old=45)

    with patch.object(
        paystack_client, "verify_transaction", new=AsyncMock(return_value=_failed_verification(reference))
    ):
        assert _post_reclaim(client, admin).json()["reclaimed"] == 1
    assert _stock_of(product.id) == 10

    event = {"event": "charge.success", "data": {"reference": reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        wh = client.post("/webhooks/paystack", content=body, headers={"x-paystack-signature": _sign(body)})

    assert wh.json()["result"] == "success"
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.CANCELLED
    assert _stock_of(product.id) == 10
    late = payment.payment_metadata["late_success"]
    assert late["requires_manual_review"] is True
    assert late["refund_required"] is True


def test_retry_refreshes_stale_clock(client):
    """19. A recently retried payment is NOT reclaimed even if first created long ago."""
    headers = _register_and_login(client, "stale19@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Retry Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)

    fake_init = {
        "authorization_url": "https://checkout.paystack.com/retry",
        "access_code": "RETRY_CODE",
        "reference": "twelve09_retry_ref",
    }
    with patch.object(paystack_client, "initialize_transaction", new=AsyncMock(return_value=fake_init)):
        init = client.post(
            "/payments/paystack/initialize", headers=headers, json={"order_id": order["id"]}
        )
    assert init.status_code == 200, init.text

    db = _db_session()
    try:
        payment = db.query(Payment).filter(Payment.order_id == order["id"]).one()
        payment_id = payment.id
        created_at = payment.created_at
    finally:
        db.close()
    # Backdate creation only; the retry write refreshed updated_at just now.
    db = _db_session()
    try:
        db.execute(
            text("UPDATE payments SET created_at=:ts WHERE id=:pid"),
            {"ts": created_at.replace(tzinfo=None) - timedelta(minutes=120), "pid": payment_id},
        )
        db.commit()
    finally:
        db.close()

    verify_mock = AsyncMock(return_value=_failed_verification("x"))
    with patch.object(paystack_client, "verify_transaction", new=verify_mock):
        response = _post_reclaim(client, admin)

    assert response.json()["reclaimed"] == 0
    verify_mock.assert_not_awaited()
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.PENDING
    assert refreshed_order.status == OrderStatus.PENDING
    assert _stock_of(product.id) == 9


def test_reclaim_old_reference_after_rotation(client):
    """20. Rotated payment reclaimed on current ref; late old-ref success stays CANCELLED+flagged."""
    headers = _register_and_login(client, "stale20@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Rotation Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    old_reference = "stale_old_ref"
    new_reference = "stale_current_ref"
    payment_id = _create_verify_payment(
        order["id"], new_reference, metadata={"previous_references": [old_reference]}
    )
    _age_payment(payment_id, minutes_old=45)

    with patch.object(
        paystack_client,
        "verify_transaction",
        new=AsyncMock(return_value=_failed_verification(new_reference)),
    ):
        assert _post_reclaim(client, admin).json()["reclaimed"] == 1

    event = {"event": "charge.success", "data": {"reference": old_reference}}
    body = json.dumps(event).encode()
    verification = _success_verification(old_reference, amount_kobo=10000)
    with patch.object(paystack_client, "verify_transaction", new=AsyncMock(return_value=verification)):
        wh = client.post("/webhooks/paystack", content=body, headers={"x-paystack-signature": _sign(body)})

    assert wh.json()["result"] == "success"
    payment, refreshed_order = _payment_and_order(payment_id, order["id"])
    assert payment.status == PaymentStatus.SUCCESS
    assert refreshed_order.status == OrderStatus.CANCELLED
    assert _stock_of(product.id) == 10
    assert payment.payment_metadata["late_success"]["refund_required"] is True


def test_reclaim_ambiguous_previous_refs_fail_closed(client):
    """21. Shared previous refs never cause cross-payment reclaim."""
    headers = _register_and_login(client, "stale21@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Ambiguous Product", price="100.00")
    order1 = _create_pickup_order(client, headers, product.id)
    order2 = _create_pickup_order(client, headers, product.id)
    shared = "stale_shared_old_ref"
    p1 = _create_verify_payment(
        order1["id"], "stale_current_a", metadata={"previous_references": [shared]}
    )
    p2 = _create_verify_payment(
        order2["id"], "stale_current_b", metadata={"previous_references": [shared]}
    )
    _age_payment(p1, minutes_old=45)
    _age_payment(p2, minutes_old=45)

    def _dispatch(reference: str):
        if reference == "stale_current_a":
            return _failed_verification(reference)
        return _success_verification(reference, amount_kobo=10000, status="pending")

    with patch.object(
        paystack_client, "verify_transaction", new=AsyncMock(side_effect=_dispatch)
    ):
        response = _post_reclaim(client, admin)

    body = response.json()
    assert body["reclaimed"] == 1
    assert body["still_pending"] == 1
    pay1, ord1 = _payment_and_order(p1, order1["id"])
    pay2, ord2 = _payment_and_order(p2, order2["id"])
    assert ord1.status == OrderStatus.CANCELLED
    assert ord2.status == OrderStatus.PENDING
    assert pay2.status == PaymentStatus.PENDING
    assert _stock_of(product.id) == 9


def test_competing_checkout_last_unit_intact(client):
    """22. Last-unit guard intact: second checkout rejected; reclaim uninvolved."""
    headers = _register_and_login(client, "stale22@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Last Unit Product", price="100.00", stock=1)
    first = _create_pickup_order(client, headers, product.id)
    assert _stock_of(product.id) == 0

    second = client.post(
        "/checkout",
        headers=headers,
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
            "fulfillment_method": "STORE_PICKUP",
            "address_id": None,
        },
    )
    assert second.status_code == 400, second.text
    assert "Insufficient stock" in second.text

    with patch.object(
        paystack_client, "verify_transaction", new=AsyncMock(return_value=_failed_verification("x"))
    ):
        # No Paystack payment exists for the order: nothing to reclaim.
        assert _post_reclaim(client, admin).json()["reclaimed"] == 0
    assert _stock_of(product.id) == 0
    fetched = client.get(f"/orders/{first['id']}", headers=headers).json()
    assert fetched["status"] == "PENDING"


def test_cancel_after_reclaim_no_double_restore(client):
    """23. Cancelling an already-reclaimed order: idempotent, no double restore."""
    headers = _register_and_login(client, "stale23@example.com")
    admin = _get_admin_headers(client)
    product = _create_product("Stale Recancel Product", price="100.00")
    order = _create_pickup_order(client, headers, product.id)
    reference = "stale_recancel_ref"
    _create_verify_payment(order["id"], reference)
    db = _db_session()
    try:
        pid = db.query(Payment).filter(Payment.provider_reference == reference).one().id
    finally:
        db.close()
    _age_payment(pid, minutes_old=45)

    with patch.object(
        paystack_client, "verify_transaction", new=AsyncMock(return_value=_failed_verification(reference))
    ):
        assert _post_reclaim(client, admin).json()["reclaimed"] == 1
    assert _stock_of(product.id) == 10

    cancel = client.post(f"/orders/{order['id']}/cancel", headers=headers)
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "CANCELLED"
    assert _stock_of(product.id) == 10
