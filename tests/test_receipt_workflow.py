"""Bank-transfer receipt workflow integration tests.

Covers the four new endpoints:
- POST /payments/{payment_id}/receipt (customer upload)
- GET /payments/{payment_id}/receipt (customer/admin retrieval)
- POST /payments/{payment_id}/receipt/approve (admin approval)
- POST /payments/{payment_id}/receipt/reject (admin rejection)

Uses live PostgreSQL via IMPORT_TEST_TARGET_URL.
"""

import io
import os
import struct
import zlib
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("PAYSTACK_PUBLIC_KEY", "pk_test_dummy")
os.environ.setdefault("PAYSTACK_WEBHOOK_SECRET", "whsec_test")

import pytest
from fastapi.testclient import TestClient

from app.core.auth import hash_password
from app.core.database import Base, get_db, utcnow
from app.main import app
from app.models import (
    Category,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Product,
    User,
    UserRole,
)
from postgres_test_safety import create_approved_postgres_engine

# Test database URL - uses the same disposable PostgreSQL as pg_target_smoke tests
# Requires IMPORT_TEST_TARGET_URL environment variable to be set
IMPORT_TEST_TARGET_URL = os.environ.get("IMPORT_TEST_TARGET_URL")

# ---------------------------------------------------------------------------
# Helpers and Fixtures
# ---------------------------------------------------------------------------


def _make_pdf_content() -> bytes:
    """Minimal valid PDF content for testing."""
    # A valid minimal PDF that passes pypdf validation
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n"
        b"<</Type /Catalog /Pages 2 0 R>>\n"
        b"endobj\n"
        b"2 0 obj\n"
        b"<</Type /Pages /Kids [3 0 R] /Count 1>>\n"
        b"endobj\n"
        b"3 0 obj\n"
        b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources <<>> /Contents 4 0 R>>\n"
        b"endobj\n"
        b"4 0 obj\n"
        b"<</Length 44>>\n"
        b"stream\n"
        b"BT /F1 12 Tf 72 720 Td (Test) Tj ET\n"
        b"endstream\n"
        b"endobj\n"
        b"xref\n"
        b"0 5\n"
        b"0000000000 65535 f \n"
        b"0000000010 00000 n \n"
        b"0000000060 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000210 00000 n \n"
        b"trailer\n"
        b"<</Size 5 /Root 1 0 R>>\n"
        b"startxref\n"
        b"278\n"
        b"%%EOF"
    )


def _make_jpeg_content() -> bytes:
    """Valid JPEG image generated with Pillow."""
    from PIL import Image
    import io
    img = Image.new("RGB", (10, 10), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png_content() -> bytes:
    """Valid PNG image generated with Pillow."""
    from PIL import Image
    import io
    img = Image.new("RGB", (10, 10), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_webp_content() -> bytes:
    """Valid WebP image generated with Pillow."""
    from PIL import Image
    import io
    img = Image.new("RGB", (10, 10), color="green")
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


def _pad_to_size(content: bytes, target_size: int) -> bytes:
    """Pad content to exactly target_size bytes (trailing bytes are ignored by readers)."""
    if len(content) >= target_size:
        return content[:target_size]
    return content + b"\n" + b"x" * (target_size - len(content) - 1)


@pytest.fixture(scope="session")
def pg_engine():
    """Create a PostgreSQL engine for live tests. Requires IMPORT_TEST_TARGET_URL."""
    if not IMPORT_TEST_TARGET_URL:
        pytest.skip("IMPORT_TEST_TARGET_URL not set; skipping live PostgreSQL tests")

    from sqlalchemy import create_engine

    # Normalize URL for psycopg2
    engine = create_approved_postgres_engine(isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture
def pg_db(pg_engine):
    """Create a session bound to the live PostgreSQL database."""
    SessionLocal = sessionmaker(bind=pg_engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def test_client(pg_db):
    """Create a test client that uses the live PostgreSQL database."""
    from app.core.database import get_db

    def override_get_db():
        yield pg_db

    app.dependency_overrides[get_db] = lambda: pg_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _register_and_login(client: TestClient, email: str, password: str = "secretpass123"):
    r = client.post("/auth/register", json={"name": email.split("@")[0].title(), "email": email, "password": "secretpass123"})
    if r.status_code == 400 and "already registered" in r.text:
        # User already exists, just login
        login = client.post("/auth/login", json={"email": email, "password": "secretpass123"})
        assert login.status_code == 200, login.text
        return {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert r.status_code == 200, r.text
    login = client.post("/auth/login", json={"email": email, "password": "secretpass123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _login(client: TestClient, email: str, password: str = "secretpass123"):
    """Log in an existing user and return Bearer headers."""
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _get_admin_headers(client: TestClient):
    """Get admin authentication headers. Creates admin user if needed."""
    from app.models import User, UserRole
    from app.core.auth import hash_password

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin_receipt_test@example.com").first()
        if not admin:
            admin = User(
                name="Admin User",
                email="admin_receipt_test@example.com",
                password_hash=hash_password("adminpass123"),
                role=UserRole.ADMIN,
                permissions=[],
                is_active=True,
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
    finally:
        db.close()

    login = client.post("/auth/login", json={"email": "admin_receipt_test@example.com", "password": "adminpass123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_product(db, name: str, price: str = "100.00", stock: int = 10):
    from app.models import Category, Product
    from decimal import Decimal

    category = db.query(Category).filter(Category.name == "Receipt Test Category").first()
    if category is None:
        category = Category(name="Receipt Test Category", description="Test")
        db.add(category)
        db.commit()
        db.refresh(category)
    product = Product(
        category_id=category.id,
        name=name,
        description="Test",
        price=Decimal("100.00"),
        stock_quantity=10,
        is_active=True,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _create_pickup_order(client, headers, product_id: int):
    resp = client.post(
        "/checkout",
        headers=headers,
        json={"fulfillment_method": "STORE_PICKUP", "items": [{"product_id": product_id, "quantity": 1}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_bank_transfer_payment(db, order_id: int, amount: str = "100.00"):
    from decimal import Decimal
    payment = Payment(
        order_id=order_id,
        amount=Decimal(amount),
        payment_method="BANK_TRANSFER",
        status="PENDING",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


# ---------------------------------------------------------------------------
# Tests: POST /payments/{payment_id}/receipt (Upload)
# ---------------------------------------------------------------------------


class TestReceiptUpload:
    """Tests for POST /payments/{payment_id}/receipt"""

    def test_upload_valid_pdf_receipt(self, test_client, pg_db):
        """Valid PDF receipt upload by payment owner."""
        client = TestClient(app)
        headers = _register_and_login(TestClient(app), "upload1@example.com")
        # Use the live test_client with live DB
        headers = _register_and_login(test_client, "upload1@example.com")
        product = _create_product(pg_db, "Upload Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["receipt_filename"] == "receipt.pdf"
        assert data["receipt_mime_type"] == "application/pdf"
        assert data["receipt_size"] == len(_make_pdf_content())

        # Verify receipt stored in DB
        pg_db.expire_all()
        payment_db = pg_db.query(Payment).filter(Payment.id == payment.id).one()
        assert payment_db.receipt_data is not None
        assert payment_db.receipt_filename == "receipt.pdf"
        assert payment_db.receipt_mime_type == "application/pdf"
        assert payment_db.receipt_size == len(_make_pdf_content())

    def test_upload_valid_jpeg_receipt(self, test_client, pg_db):
        """Valid JPEG receipt upload."""
        headers = _register_and_login(test_client, "upload2@example.com")
        product = _create_product(pg_db, "JPEG Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        jpeg_content = _make_jpeg_content()
        files = {"file": ("receipt.jpg", jpeg_content, "image/jpeg")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["receipt_mime_type"] == "image/jpeg"

    def test_upload_valid_png_receipt(self, test_client, pg_db):
        """Valid PNG receipt upload."""
        headers = _register_and_login(test_client, "upload3@example.com")
        product = _create_product(pg_db, "PNG Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        png_content = _make_png_content()
        files = {"file": ("receipt.png", png_content, "image/png")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["receipt_mime_type"] == "image/png"

    def test_upload_valid_webp_receipt(self, test_client, pg_db):
        """Valid WebP receipt upload."""
        headers = _register_and_login(test_client, "upload4@example.com")
        product = _create_product(pg_db, "WebP Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        webp_content = _make_webp_content()
        files = {"file": ("receipt.webp", webp_content, "image/webp")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["receipt_mime_type"] == "image/webp"

    def test_upload_unsupported_extension_rejected(self, test_client, pg_db):
        """Unsupported file extension rejected."""
        headers = _register_and_login(test_client, "upload5@example.com")
        product = _create_product(pg_db, "Ext Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        files = {"file": ("receipt.txt", b"text content", "text/plain")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert "Invalid file extension" in resp.json()["detail"]

    def test_upload_unsupported_mime_type_rejected(self, test_client, pg_db):
        """Unsupported MIME type rejected."""
        headers = _register_and_login(test_client, "upload6@example.com")
        product = _create_product(pg_db, "Mime Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # Allowed extension (.pdf) but disallowed client MIME type (image/gif)
        files = {"file": ("receipt.pdf", _make_pdf_content(), "image/gif")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert "Unsupported MIME type" in resp.json()["detail"]

    def test_upload_mismatched_mime_and_signature_rejected(self, test_client, pg_db):
        """MIME type vs file signature mismatch rejected."""
        headers = _register_and_login(test_client, "upload7@example.com")
        product = _create_product(pg_db, "Sig Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # PDF content but claiming to be JPEG
        files = {"file": ("receipt.jpg", _make_pdf_content(), "image/jpeg")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert "MIME type mismatch" in resp.json()["detail"]

    def test_upload_empty_file_rejected(self, test_client, pg_db):
        """Empty file rejected."""
        headers = _register_and_login(test_client, "upload8@example.com")
        product = _create_product(pg_db, "Empty Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        files = {"file": ("empty.pdf", b"", "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert "Empty file" in resp.json()["detail"]

    def test_upload_malformed_pdf_rejected(self, test_client, pg_db):
        """Malformed PDF rejected."""
        headers = _register_and_login(test_client, "upload9@example.com")
        product = _create_product(pg_db, "Malformed Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # Valid PDF signature but invalid/truncated structure
        files = {"file": ("bad.pdf", b"%PDF-1.4\n%this is not a valid PDF structure", "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert "Invalid file content" in resp.json()["detail"]

    def test_upload_malformed_image_rejected(self, test_client, pg_db):
        """Malformed image rejected."""
        headers = _register_and_login(test_client, "upload10@example.com")
        product = _create_product(pg_db, "Malformed Image Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # Valid JPEG signature but invalid/corrupt image data
        files = {"file": ("bad.jpg", b"\xff\xd8\xff" + b"\x00" * 100, "image/jpeg")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert "Invalid file content" in resp.json()["detail"]

    def test_upload_oversized_file_rejected(self, test_client, pg_db):
        """File > 5 MiB rejected."""
        headers = _register_and_login(test_client, "upload11@example.com")
        product = _create_product(pg_db, "Large File Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # Create a file larger than 5 MiB
        large_content = b"x" * (5 * 1024 * 1024 + 1)
        files = {"file": ("large.pdf", large_content, "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 413, resp.text
        assert "too large" in resp.json()["detail"].lower()

    def test_upload_file_at_5mb_limit_accepted(self, test_client, pg_db):
        """File exactly at 5 MiB limit accepted (valid content)."""
        headers = _register_and_login(test_client, "upload12@example.com")
        product = _create_product(pg_db, "Limit Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # Valid PDF content padded to exactly 5 MiB
        limit_content = _pad_to_size(_make_pdf_content(), 5 * 1024 * 1024)
        assert len(limit_content) == 5 * 1024 * 1024
        files = {"file": ("limit.pdf", limit_content, "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["receipt_size"] == 5 * 1024 * 1024

    def test_upload_file_just_below_5mb_limit_accepted(self, test_client, pg_db):
        """File just below 5 MiB limit accepted (valid content)."""
        headers = _register_and_login(test_client, "upload12b@example.com")
        product = _create_product(pg_db, "LimitBelow Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        just_below_content = _pad_to_size(_make_pdf_content(), 5 * 1024 * 1024 - 1)
        assert len(just_below_content) == 5 * 1024 * 1024 - 1
        files = {"file": ("just_below.pdf", just_below_content, "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["receipt_size"] == 5 * 1024 * 1024 - 1

    def test_upload_duplicate_receipt_rejected(self, test_client, pg_db):
        """Duplicate upload rejected."""
        headers = _register_and_login(test_client, "upload13@example.com")
        product = _create_product(pg_db, "Dup Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}

        # First upload
        resp1 = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp1.status_code == 200, resp1.text

        # Second upload should fail
        resp2 = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp2.status_code == 409, resp2.text
        assert "already uploaded" in resp2.json()["detail"]

    def test_upload_non_bank_transfer_rejected(self, test_client, pg_db):
        """Non-BANK_TRANSFER payment method rejected."""
        headers = _register_and_login(test_client, "upload14@example.com")
        product = _create_product(pg_db, "NonBT Product")
        order = _create_pickup_order(test_client, headers, product.id)

        # Create a CARD payment (not BANK_TRANSFER)
        payment = Payment(
            order_id=order["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            status=PaymentStatus.PENDING,
        )
        pg_db.add(payment)
        pg_db.commit()
        pg_db.refresh(payment)

        files = {"file": ("receipt.pdf", _make_pdf_content(), "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert resp.status_code == 400, resp.text
        assert "BANK_TRANSFER" in resp.json()["detail"]

    def test_upload_non_pending_payment_rejected(self, test_client, pg_db):
            """Payment not in PENDING state rejected."""
            headers = _register_and_login(test_client, "upload15@example.com")
            product = _create_product(pg_db, "NonPending Product")
            order = _create_pickup_order(test_client, headers, product.id)
    
            # Create a SUCCESS payment (not PENDING)
            payment = Payment(
                order_id=order["id"],
                amount=Decimal("100.00"),
                payment_method="BANK_TRANSFER",
                status=PaymentStatus.SUCCESS,
            )
            pg_db.add(payment)
            pg_db.commit()
            pg_db.refresh(payment)
    
            files = {"file": ("receipt.pdf", _make_pdf_content(), "application/pdf")}
            resp = test_client.post(
                f"/payments/{payment.id}/receipt",
                headers=headers,
                files=files,
            )
            assert resp.status_code == 400, resp.text
            assert "SUCCESS" in resp.json()["detail"]

    def test_upload_other_customer_payment_rejected(self, test_client, pg_db):
        """Another customer's payment rejected."""
        # Customer 1 creates order and payment
        headers1 = _register_and_login(test_client, "owner1@example.com")
        product = _create_product(pg_db, "Owner1 Product")
        order1 = _create_pickup_order(test_client, headers1, product.id)
        payment1 = _create_bank_transfer_payment(pg_db, order1["id"])

        # Customer 2 tries to upload receipt for customer 1's payment
        headers2 = _register_and_login(test_client, "owner2@example.com")
        files = {"file": ("receipt.pdf", _make_pdf_content(), "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment1.id}/receipt",
            headers=headers2,
            files=files,
        )
        assert resp.status_code == 403, resp.text
        assert "Not authorized" in resp.json()["detail"]

    def test_upload_unauthenticated_rejected(self, test_client, pg_db):
        """Unauthenticated request rejected."""
        product = _create_product(pg_db, "NoAuth Product")
        # Need a valid payment first
        headers = _register_and_login(test_client, "authuser@example.com")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        files = {"file": ("receipt.pdf", _make_pdf_content(), "application/pdf")}
        resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            files=files,  # No headers = no auth
        )
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Tests: GET /payments/{payment_id}/receipt (Retrieval)
# ---------------------------------------------------------------------------


class TestReceiptRetrieval:
    """Tests for GET /payments/{payment_id}/receipt"""

    def test_retrieve_own_receipt(self, test_client, pg_db):
        """Owner can retrieve their receipt."""
        headers = _register_and_login(test_client, "retrieve1@example.com")
        product = _create_product(pg_db, "Retrieve Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # Upload a receipt first
        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # Retrieve the receipt
        resp = test_client.get(
            f"/payments/{payment.id}/receipt",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["content-length"] == str(len(_make_pdf_content()))
        assert resp.content == _make_pdf_content()

    def test_admin_can_retrieve_receipt(self, test_client, pg_db):
        """Admin can retrieve any receipt."""
        headers = _register_and_login(test_client, "retrieve1@example.com")
        product = _create_product(pg_db, "Admin Retrieve Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.get(
            f"/payments/{payment.id}/receipt",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == _make_pdf_content()

    def test_other_customer_cannot_retrieve_receipt(self, test_client, pg_db):
        """Other customer cannot access receipt."""
        headers1 = _register_and_login(test_client, "owner1@example.com")
        product = _create_product(pg_db, "Owner1 Product")
        order = _create_pickup_order(test_client, headers1, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers1,
            files=files,
        )
        assert upload_resp.status_code == 200

        headers2 = _register_and_login(test_client, "owner2@example.com")
        resp = test_client.get(
            f"/payments/{payment.id}/receipt",
            headers=headers2,
        )
        assert resp.status_code == 403, resp.text
        assert "Not authorized" in resp.json()["detail"]

    def test_staff_cannot_retrieve_receipt(self, test_client, pg_db):
        """STAFF cannot access receipt."""
        # Create a STAFF user with MANAGE_ORDERS permission
        from app.models import User, UserRole, Permission
        from app.core.auth import hash_password
        from app.core.database import get_db
        from app.main import app
        from sqlalchemy.orm import sessionmaker

        engine = create_approved_postgres_engine()
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        db = SessionLocal()
        try:
            from app.models import User, UserRole, Permission
            from app.core.auth import hash_password
            staff = db.query(User).filter(User.email == "staff_receipt@example.com").first()
            if not staff:
                staff = User(
                    name="Staff User",
                    email="staff_receipt@example.com",
                    password_hash=hash_password("staffpass123"),
                    role=UserRole.STAFF,
                    permissions=[Permission.MANAGE_ORDERS.value],
                    is_active=True,
                )
                db.add(staff)
                db.commit()
        finally:
            db.close()

        # Upload receipt as customer
        headers = _register_and_login(test_client, "staffcust@example.com")
        product = _create_product(pg_db, "Staff Cust Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # STAFF tries to retrieve
        staff_headers = _login(test_client, "staff_receipt@example.com", "staffpass123")
        resp = test_client.get(
            f"/payments/{payment.id}/receipt",
            headers=staff_headers,
        )
        assert resp.status_code == 403, resp.text
        assert "Not authorized" in resp.json()["detail"]

    def test_receipt_not_found(self, test_client, pg_db):
        """No receipt uploaded returns 404."""
        headers = _register_and_login(test_client, "notfound1@example.com")
        product = _create_product(pg_db, "NotFound Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        resp = test_client.get(
            f"/payments/{payment.id}/receipt",
            headers=headers,
        )
        assert resp.status_code == 404, resp.text
        assert "No receipt uploaded" in resp.json()["detail"]

    def test_payment_not_found(self, test_client, pg_db):
        """Non-existent payment returns 404."""
        headers = _register_and_login(test_client, "notfound1@example.com")
        resp = test_client.get(
            "/payments/999999/receipt",
            headers=headers,
        )
        assert resp.status_code == 404, resp.text
        assert "Payment not found" in resp.json()["detail"]

    def test_unauthenticated_retrieval_rejected(self, test_client, pg_db):
        """Unauthenticated request rejected."""
        headers = _register_and_login(test_client, "unauth1@example.com")
        product = _create_product(pg_db, "Unauth Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        resp = test_client.get(f"/payments/{payment.id}/receipt")
        assert resp.status_code == 401, resp.text

    def test_receipt_content_and_headers(self, test_client, pg_db):
        """Receipt content streamed with correct headers."""
        headers = _register_and_login(test_client, "headers1@example.com")
        product = _create_product(pg_db, "Headers Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        resp = test_client.get(
            f"/payments/{payment.id}/receipt",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["content-disposition"] == 'attachment; filename="receipt.pdf"'
        assert int(resp.headers["content-length"]) == len(pdf_content)
        assert resp.content == pdf_content

    def test_receipt_binary_not_in_normal_response(self, test_client, pg_db):
        """Receipt binary not exposed in normal payment response."""
        from app.schemas.payment import PaymentRead

        headers = _register_and_login(test_client, "binary1@example.com")
        product = _create_product(pg_db, "Binary Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # Get payment via API
        resp = test_client.get(
            f"/payments/{payment.id}",
            headers=headers,
        )
        assert resp.status_code == 200
        payment_data = resp.json()

        # Check receipt_data is NOT in the response
        assert "receipt_data" not in payment_data
        # But metadata should be present
        assert payment_data["receipt_filename"] == "receipt.pdf"
        assert payment_data["receipt_mime_type"] == "application/pdf"
        assert payment_data["receipt_size"] == len(pdf_content)


# ---------------------------------------------------------------------------
# Tests: POST /payments/{payment_id}/receipt/approve
# ---------------------------------------------------------------------------


class TestReceiptApproval:
    """Tests for POST /payments/{payment_id}/receipt/approve"""

    def test_approve_valid_receipt(self, test_client, pg_db):
        """Admin approves valid receipt."""
        headers = _register_and_login(test_client, "approve1@example.com")
        product = _create_product(pg_db, "Approve Test Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "APPROVED"
        assert data["payment_id"] == payment.id

    def test_approve_non_bank_transfer_rejected(self, test_client, pg_db):
        """Non-BANK_TRANSFER payment rejected."""
        headers = _register_and_login(test_client, "approve2@example.com")
        product = _create_product(pg_db, "NonBT Approve Product")
        order = _create_pickup_order(test_client, headers, product.id)

        # Create a CARD payment
        payment = Payment(
            order_id=order["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            status=PaymentStatus.PENDING,
        )
        pg_db.add(payment)
        pg_db.commit()
        pg_db.refresh(payment)

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert "BANK_TRANSFER" in resp.json()["detail"]

    def test_approve_without_receipt_rejected(self, test_client, pg_db):
        """Payment without receipt rejected."""
        headers = _register_and_login(test_client, "approve3@example.com")
        product = _create_product(pg_db, "NoReceipt Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # No receipt uploaded
        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert "No receipt uploaded" in resp.json()["detail"]

    def test_approve_non_pending_rejected(self, test_client, pg_db):
        """Non-PENDING payment rejected."""
        headers = _register_and_login(test_client, "approve4@example.com")
        product = _create_product(pg_db, "NonPending Product")
        order = _create_pickup_order(test_client, headers, product.id)

        # Create a PENDING payment, upload a receipt, then move it out of PENDING
        payment = Payment(
            order_id=order["id"],
            amount=Decimal("100.00"),
            payment_method="BANK_TRANSFER",
            status=PaymentStatus.PENDING,
        )
        pg_db.add(payment)
        pg_db.commit()
        pg_db.refresh(payment)

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # Transition payment out of PENDING (ineligible state transition)
        payment.status = PaymentStatus.SUCCESS
        pg_db.commit()

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert "SUCCESS" in resp.json()["detail"]

    def test_approve_duplicate_rejected(self, test_client, pg_db):
        """Duplicate approval rejected."""
        headers = _register_and_login(test_client, "dup1@example.com")
        product = _create_product(pg_db, "DupApprove Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)

        # First approval
        resp1 = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp1.status_code == 200, resp1.text

        # Second approval should be rejected (payment is now SUCCESS)
        resp2 = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp2.status_code == 400, resp2.text
        assert "SUCCESS" in resp2.json()["detail"]

    def test_approve_other_admin_receipt(self, test_client, pg_db):
        """Admin can approve another admin's payment."""
        headers1 = _register_and_login(test_client, "admin1@example.com")
        product = _create_product(pg_db, "Admin1 Product")
        order1 = _create_pickup_order(test_client, headers1, product.id)
        payment1 = _create_bank_transfer_payment(pg_db, order1["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment1.id}/receipt",
            headers=headers1,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment1.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "APPROVED"

    def test_approve_idempotent(self, test_client, pg_db):
        """Duplicate approval is idempotent or safely rejected."""
        headers = _register_and_login(test_client, "idempotent1@example.com")
        product = _create_product(pg_db, "Idempotent Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)

        # First approval
        resp1 = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp1.status_code == 200

        # Second call - should be rejected (not idempotent, but safely rejected)
        resp2 = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp2.status_code == 400, resp2.text
        assert "SUCCESS" in resp2.json()["detail"]

    def test_staff_cannot_approve(self, test_client, pg_db):
        """STAFF with MANAGE_ORDERS cannot approve."""
        # Create STAFF user with MANAGE_ORDERS
        from app.models import User, UserRole, Permission
        from app.core.auth import hash_password
        from sqlalchemy.orm import sessionmaker

        engine = create_approved_postgres_engine()
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        db = SessionLocal()
        try:
            from app.models import User, UserRole, Permission
            from app.core.auth import hash_password
            staff = db.query(User).filter(User.email == "staff_approve@example.com").first()
            if not staff:
                staff = User(
                    name="Staff User",
                    email="staff_approve@example.com",
                    password_hash=hash_password("staffpass123"),
                    role=UserRole.STAFF,
                    permissions=[Permission.MANAGE_ORDERS.value],
                    is_active=True,
                )
                db.add(staff)
                db.commit()
        finally:
            db.close()

        # Upload receipt as customer
        headers = _register_and_login(test_client, "staffcust@example.com")
        product = _create_product(pg_db, "StaffCust Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # STAFF tries to approve
        staff_headers = _login(test_client, "staff_approve@example.com", "staffpass123")
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=staff_headers,
        )
        assert resp.status_code == 403, resp.text
        assert "Administrator privileges required" in resp.json()["detail"]

    def test_unauthorized_approve_rejected(self, test_client, pg_db):
        """Non-admin rejected."""
        headers = _register_and_login(test_client, "unauth1@example.com")
        product = _create_product(pg_db, "Unauth Approve Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # Try to approve without admin auth
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=headers,  # Customer headers, not admin
        )
        assert resp.status_code == 403, resp.text
        assert "Administrator privileges required" in resp.json()["detail"]

    def test_approve_updates_payment_and_order(self, test_client, pg_db):
        """Approval updates payment to SUCCESS and order to CONFIRMED."""
        headers = _register_and_login(test_client, "approve5@example.com")
        product = _create_product(pg_db, "Approve5 Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp.status_code == 200

        # Verify payment status
        pg_db.expire_all()
        payment_db = pg_db.query(Payment).filter(Payment.id == payment.id).one()
        assert payment_db.status == PaymentStatus.SUCCESS
        assert payment_db.paid_at is not None

        # Verify order status
        from app.models import Order as OrderModel
        order_db = pg_db.query(OrderModel).filter(OrderModel.id == order["id"]).one()
        assert order_db.status == OrderStatus.CONFIRMED

    def test_approve_records_reviewer_and_timestamp(self, test_client, pg_db):
        """Approval records reviewer and timestamp."""
        headers = _register_and_login(test_client, "timestamp1@example.com")
        product = _create_product(pg_db, "Timestamp Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp.status_code == 200

        # Verify receipt reviewed fields
        pg_db.expire_all()
        payment_db = pg_db.query(Payment).filter(Payment.id == payment.id).one()
        assert payment_db.receipt_reviewed_at is not None
        assert payment_db.receipt_reviewed_by is not None
        assert payment_db.payment_metadata.get("receipt_approval", {}).get("action") == "APPROVED"

    def test_approve_cancelled_order_flagged(self, test_client, pg_db):
        """Late approval on cancelled order flagged for review."""
        headers = _register_and_login(test_client, "cancelled1@example.com")
        product = _create_product(pg_db, "Cancelled Approve Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # Cancel the order first
        cancel_resp = test_client.post(
            f"/orders/{order['id']}/cancel",
            headers=headers,
        )
        assert cancel_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/approve",
            headers=admin_headers,
        )
        assert resp.status_code == 200

        # Approval on a cancelled order marks the payment SUCCESS but does not
        # revive the cancelled order (no late_success flagging in receipt flow).
        pg_db.expire_all()
        payment_db = pg_db.query(Payment).filter(Payment.id == payment.id).one()
        assert payment_db.status == PaymentStatus.SUCCESS
        order_db = pg_db.query(Order).filter(Order.id == order["id"]).one()
        assert order_db.status == OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# Tests: POST /payments/{payment_id}/receipt/reject
# ---------------------------------------------------------------------------


class TestReceiptRejection:
    """Tests for POST /payments/{payment_id}/receipt/reject"""

    def test_reject_valid_receipt(self, test_client, pg_db):
        """Admin rejects valid receipt with reason."""
        headers = _register_and_login(test_client, "reject1@example.com")
        product = _create_product(pg_db, "Reject1 Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": "Invalid receipt - amount mismatch"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "REJECTED"
        assert data["payment_id"] == payment.id
        assert data["rejection_reason"] == "Invalid receipt - amount mismatch"

    def test_reject_requires_reason(self, test_client, pg_db):
        """Rejection requires non-empty reason."""
        headers = _register_and_login(test_client, "reject2@example.com")
        product = _create_product(pg_db, "Reject2 Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)

        # No reason
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": ""},
        )
        assert resp.status_code == 400, resp.text
        assert "Rejection reason is required" in resp.json()["detail"]

        # Whitespace only
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": "   "},
        )
        assert resp.status_code == 400, resp.text
        assert "Rejection reason is required" in resp.json()["detail"]

    def test_reject_reason_length_limit(self, test_client, pg_db):
        """Rejection reason length limited."""
        headers = _register_and_login(test_client, "reject3@example.com")
        product = _create_product(pg_db, "RejectLen Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)

        # Too long reason
        long_reason = "x" * 1001
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": long_reason},
        )
        assert resp.status_code == 400, resp.text
        assert "1000 characters" in resp.json()["detail"]

    def test_reject_without_receipt_rejected(self, test_client, pg_db):
        """Payment without receipt rejected."""
        headers = _register_and_login(test_client, "reject4@example.com")
        product = _create_product(pg_db, "NoReceipt Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        # No receipt uploaded
        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": "No receipt provided"},
        )
        assert resp.status_code == 400, resp.text
        assert "No receipt uploaded" in resp.json()["detail"]

    def test_reject_non_pending_rejected(self, test_client, pg_db):
        """Non-PENDING payment rejected."""
        headers = _register_and_login(test_client, "reject5@example.com")
        product = _create_product(pg_db, "NonPending Reject Product")
        order = _create_pickup_order(test_client, headers, product.id)

        # Create a PENDING payment, upload a receipt, then move it out of PENDING
        payment = Payment(
            order_id=order["id"],
            amount=Decimal("100.00"),
            payment_method="BANK_TRANSFER",
            status=PaymentStatus.PENDING,
        )
        pg_db.add(payment)
        pg_db.commit()
        pg_db.refresh(payment)

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # Transition payment out of PENDING (ineligible state transition)
        payment.status = PaymentStatus.SUCCESS
        pg_db.commit()

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": "Test reject"},
        )
        assert resp.status_code == 400, resp.text
        assert "SUCCESS" in resp.json()["detail"]

    def test_reject_non_bank_transfer_rejected(self, test_client, pg_db):
        """Non-BANK_TRANSFER payment rejected."""
        headers = _register_and_login(test_client, "reject6@example.com")
        product = _create_product(pg_db, "NonBT Reject Product")
        order = _create_pickup_order(test_client, headers, product.id)

        # Create a CARD payment
        payment = Payment(
            order_id=order["id"],
            amount=Decimal("100.00"),
            payment_method="CARD",
            status=PaymentStatus.PENDING,
        )
        pg_db.add(payment)
        pg_db.commit()
        pg_db.refresh(payment)

        # Non-BANK_TRANSFER payments cannot have receipts; the payment-method
        # guard rejects review before any receipt check.
        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": "Test reject"},
        )
        assert resp.status_code == 400, resp.text
        assert "BANK_TRANSFER" in resp.json()["detail"]

    def test_reject_duplicate_rejected(self, test_client, pg_db):
        """Duplicate rejection rejected."""
        headers = _register_and_login(test_client, "reject7@example.com")
        product = _create_product(pg_db, "DupReject Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)

        # First rejection
        resp1 = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": "First rejection"},
        )
        assert resp1.status_code == 200

        # Second rejection should fail (payment is now FAILED)
        resp2 = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": "Second rejection"},
        )
        assert resp2.status_code == 400, resp2.text
        assert "FAILED" in resp2.json()["detail"]

    def test_reject_does_not_restore_stock(self, test_client, pg_db):
        """Rejection does not restore stock (order stays PENDING)."""
        headers = _register_and_login(test_client, "reject8@example.com")
        product = _create_product(pg_db, "RejectStock Product", stock=5)
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # Check stock before rejection
        pg_db.expire_all()
        product_before = pg_db.query(Product).filter(Product.id == product.id).one()
        stock_before = product_before.stock_quantity

        admin_headers = _get_admin_headers(test_client)
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": "Customer cancelled"},
        )
        assert resp.status_code == 200

        # Stock should remain unchanged (still held for PENDING order)
        pg_db.expire_all()
        product_after = pg_db.query(Product).filter(Product.id == product.id).one()
        assert product_after.stock_quantity == stock_before

        # Order should still be PENDING
        pg_db.expire_all()
        from app.models import Order as OrderModel
        order_db = pg_db.query(Order).filter(Order.id == order["id"]).one()
        assert order_db.status == OrderStatus.PENDING

    def test_reject_records_reviewer_and_reason(self, test_client, pg_db):
        """Rejection records reviewer, timestamp, and reason."""
        headers = _register_and_login(test_client, "reject9@example.com")
        product = _create_product(pg_db, "RejectMeta Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        admin_headers = _get_admin_headers(test_client)
        reason = "Receipt image too blurry"
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=admin_headers,
            json={"rejection_reason": reason},
        )
        assert resp.status_code == 200

        # Verify rejection metadata
        pg_db.expire_all()
        payment_db = pg_db.query(Payment).filter(Payment.id == payment.id).one()
        assert payment_db.receipt_reviewed_at is not None
        assert payment_db.receipt_reviewed_by is not None
        assert payment_db.receipt_rejection_reason == reason
        assert payment_db.payment_metadata.get("receipt_rejection", {}).get("action") == "REJECTED"
        assert payment_db.payment_metadata.get("receipt_rejection", {}).get("reason") == reason

    def test_staff_cannot_reject(self, test_client, pg_db):
        """STAFF cannot reject."""
        # Create STAFF user with MANAGE_ORDERS
        from app.models import User, UserRole, Permission
        from app.core.auth import hash_password
        from sqlalchemy.orm import sessionmaker

        engine = create_approved_postgres_engine()
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        db = SessionLocal()
        try:
            from app.models import User, UserRole, Permission
            from app.core.auth import hash_password
            staff = db.query(User).filter(User.email == "staff_reject@example.com").first()
            if not staff:
                staff = User(
                    name="Staff User",
                    email="staff_reject@example.com",
                    password_hash=hash_password("staffpass123"),
                    role=UserRole.STAFF,
                    permissions=[Permission.MANAGE_ORDERS.value],
                    is_active=True,
                )
                db.add(staff)
                db.commit()
        finally:
            db.close()

        # Upload receipt as customer
        headers = _register_and_login(test_client, "staffcust2@example.com")
        product = _create_product(pg_db, "StaffCustReject Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # STAFF tries to reject
        staff_headers = _login(test_client, "staff_reject@example.com", "staffpass123")
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=staff_headers,
            json={"rejection_reason": "Staff reject"},
        )
        assert resp.status_code == 403, resp.text
        assert "Administrator privileges required" in resp.json()["detail"]

    def test_unauthorized_reject_rejected(self, test_client, pg_db):
        """Non-admin rejected."""
        headers = _register_and_login(test_client, "unauth1@example.com")
        product = _create_product(pg_db, "UnauthReject Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        # Try to reject without admin auth
        resp = test_client.post(
            f"/payments/{payment.id}/receipt/reject",
            headers=headers,  # Customer headers, not admin
            json={"rejection_reason": "Unauthorized reject"},
        )
        assert resp.status_code == 403, resp.text
        assert "Administrator privileges required" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Concurrency Tests (Live PostgreSQL)
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Concurrency tests using live PostgreSQL row-level locking."""

    def _default_engine(self):
        """Engine with default isolation (READ COMMITTED) so explicit row locks
        (SELECT ... FOR UPDATE) are held across the transaction. The shared
        pg_engine fixture uses AUTOCOMMIT which releases FOR UPDATE locks after
        each statement and therefore cannot exercise true PostgreSQL locking."""
        return create_approved_postgres_engine()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _setup_payment_with_receipt(self, pg_engine):
        """Create a pending BANK_TRANSFER payment with a receipt; return (payment_id, order_id, product_id)."""
        from sqlalchemy.orm import sessionmaker
        from app.models import (
            User, UserRole, Category, Product, Order, OrderStatus,
            FulfillmentMethod, OrderItem, Payment, PaymentStatus,
        )
        from app.core.auth import hash_password
        from app.core.database import utcnow
        from decimal import Decimal
        import uuid

        SessionLocal = sessionmaker(bind=self._default_engine(), autocommit=False, autoflush=False)
        db = SessionLocal()
        try:
            user = User(
                name="Concurrency User",
                email=f"concurrent_{uuid.uuid4().hex[:10]}@example.com",
                password_hash=hash_password("concypass123"),
                role=UserRole.CUSTOMER,
                permissions=[],
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

            category = Category(
                name=f"Concurrency Category {uuid.uuid4().hex[:8]}",
                description="Test",
                is_active=True,
            )
            db.add(category)
            db.commit()
            db.refresh(category)

            product = Product(
                category_id=category.id,
                name=f"Concurrency Product {uuid.uuid4().hex[:8]}",
                description="Test",
                price=Decimal("100.00"),
                stock_quantity=10,
                is_active=True,
            )
            db.add(product)
            db.commit()
            db.refresh(product)

            order = Order(
                user_id=user.id,
                address_id=None,
                total_amount=Decimal("100.00"),
                status=OrderStatus.PENDING,
                fulfillment_method=FulfillmentMethod.STORE_PICKUP,
                delivery_fee=Decimal("0.00"),
            )
            db.add(order)
            db.commit()
            db.refresh(order)

            order_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=1,
                unit_price=Decimal("100.00"),
                subtotal=Decimal("100.00"),
            )
            db.add(order_item)
            db.commit()

            payment = Payment(
                order_id=order.id,
                amount=Decimal("100.00"),
                payment_method="BANK_TRANSFER",
                status=PaymentStatus.PENDING,
            )
            db.add(payment)
            db.commit()
            db.refresh(payment)

            pdf_content = b"%PDF-1.4\nConcurrency receipt test"
            payment.receipt_data = pdf_content
            payment.receipt_filename = "receipt.pdf"
            payment.receipt_mime_type = "application/pdf"
            payment.receipt_size = len(pdf_content)
            payment.receipt_uploaded_at = utcnow()
            db.commit()

            return payment.id, order.id, product.id
        finally:
            db.close()

    def _session_factory(self, pg_engine):
        from sqlalchemy.orm import sessionmaker
        return sessionmaker(bind=self._default_engine(), autocommit=False, autoflush=False)

    def _run_concurrent_approve_reject(self, pg_engine, payment_id, order_id):
        """Run concurrent approve and reject against the same pending payment.

        Each thread uses its own PostgreSQL session and row-level lock
        (SELECT ... FOR UPDATE). Returns a dict with per-outcome results.
        """
        import threading
        import time
        from app.models import Payment, PaymentStatus, Order, OrderStatus
        from app.core.database import utcnow

        SessionLocal = self._session_factory(pg_engine)
        results = {"approve": None, "reject": None}

        def try_approve():
            db = SessionLocal()
            try:
                payment = (
                    db.query(Payment)
                    .filter(Payment.id == payment_id)
                    .with_for_update()
                    .first()
                )
                if (
                    payment is None
                    or payment.status != PaymentStatus.PENDING
                    or payment.receipt_data is None
                ):
                    results["approve"] = "skipped"
                    return
                # Hold the row lock briefly to force real lock contention.
                time.sleep(0.25)
                payment.status = PaymentStatus.SUCCESS
                payment.paid_at = utcnow()
                payment.receipt_reviewed_at = utcnow()
                payment.receipt_reviewed_by = 1
                payment.payment_metadata = {
                    **(payment.payment_metadata or {}),
                    "receipt_approval": {
                        "reviewed_by": 1,
                        "reviewed_at": utcnow().isoformat(),
                        "action": "APPROVED",
                    },
                }
                order = (
                    db.query(Order)
                    .filter(Order.id == order_id)
                    .with_for_update()
                    .first()
                )
                if order and order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.CONFIRMED
                db.add(payment)
                db.commit()
                results["approve"] = "success"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results["approve"] = f"error: {exc}"
            finally:
                db.close()

        def try_reject():
            db = SessionLocal()
            try:
                payment = (
                    db.query(Payment)
                    .filter(Payment.id == payment_id)
                    .with_for_update()
                    .first()
                )
                if (
                    payment is None
                    or payment.status != PaymentStatus.PENDING
                    or payment.receipt_data is None
                ):
                    results["reject"] = "skipped"
                    return
                payment.status = PaymentStatus.FAILED
                payment.receipt_reviewed_at = utcnow()
                payment.receipt_reviewed_by = 1
                payment.receipt_rejection_reason = "Concurrent rejection test"
                payment.payment_metadata = {
                    **(payment.payment_metadata or {}),
                    "receipt_rejection": {
                        "reviewed_by": 1,
                        "reviewed_at": utcnow().isoformat(),
                        "action": "REJECTED",
                        "reason": "Concurrent test",
                    },
                }
                # Hold the row lock briefly to force real lock contention.
                time.sleep(0.25)
                db.add(payment)
                db.commit()
                results["reject"] = "success"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results["reject"] = f"error: {exc}"
            finally:
                db.close()

        t1 = threading.Thread(target=try_approve)
        t2 = threading.Thread(target=try_reject)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        return results

    def _fetch_final_state(self, pg_engine, payment_id, order_id, product_id):
        from sqlalchemy.orm import sessionmaker
        from app.models import Payment, Order, Product

        SessionLocal = self._session_factory(pg_engine)
        db = SessionLocal()
        try:
            payment = db.query(Payment).filter(Payment.id == payment_id).one()
            order = db.query(Order).filter(Order.id == order_id).one()
            product = db.query(Product).filter(Product.id == product_id).one()
            return payment, order, product
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Conflicting review operations
    # ------------------------------------------------------------------

    def test_concurrent_approve_and_reject(self, pg_engine):
        """Concurrent approve and reject cannot both finalize conflicting decisions."""
        payment_id, order_id, product_id = self._setup_payment_with_receipt(pg_engine)
        results = self._run_concurrent_approve_reject(pg_engine, payment_id, order_id)

        successes = [key for key, value in results.items() if value == "success"]
        assert len(successes) == 1, f"Expected exactly one successful review, got {results}"

        from app.models import PaymentStatus, OrderStatus

        payment, order, product = self._fetch_final_state(
            pg_engine, payment_id, order_id, product_id
        )
        metadata = payment.payment_metadata or {}

        if results["approve"] == "success":
            assert payment.status == PaymentStatus.SUCCESS
            assert order.status == OrderStatus.CONFIRMED
            assert metadata.get("receipt_approval", {}).get("action") == "APPROVED"
            assert "receipt_rejection" not in metadata
        else:
            assert results["reject"] == "success"
            assert payment.status == PaymentStatus.FAILED
            assert order.status == OrderStatus.PENDING
            assert metadata.get("receipt_rejection", {}).get("action") == "REJECTED"
            assert "receipt_approval" not in metadata

        # Exactly one review decision is persisted.
        assert payment.receipt_reviewed_by is not None
        assert payment.receipt_reviewed_at is not None
        # Review operations do not touch inventory.
        assert product.stock_quantity == 10

    def test_concurrent_duplicate_approval(self, pg_engine):
        """Concurrent duplicate approvals: only one succeeds."""
        import threading
        import time
        from app.models import Payment, PaymentStatus, Order, OrderStatus
        from app.core.database import utcnow

        payment_id, order_id, product_id = self._setup_payment_with_receipt(pg_engine)
        SessionLocal = self._session_factory(pg_engine)
        results = {"a": None, "b": None}

        def try_approve(tag):
            db = SessionLocal()
            try:
                payment = (
                    db.query(Payment)
                    .filter(Payment.id == payment_id)
                    .with_for_update()
                    .first()
                )
                if (
                    payment is None
                    or payment.status != PaymentStatus.PENDING
                    or payment.receipt_data is None
                ):
                    results[tag] = "skipped"
                    return
                payment.status = PaymentStatus.SUCCESS
                payment.paid_at = utcnow()
                payment.receipt_reviewed_at = utcnow()
                payment.receipt_reviewed_by = 1
                payment.payment_metadata = {
                    **(payment.payment_metadata or {}),
                    "receipt_approval": {
                        "reviewed_by": 1,
                        "reviewed_at": utcnow().isoformat(),
                        "action": "APPROVED",
                    },
                }
                # Hold the row lock briefly to force real lock contention.
                time.sleep(0.25)
                order = (
                    db.query(Order).filter(Order.id == order_id).with_for_update().first()
                )
                if order and order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.CONFIRMED
                db.add(payment)
                db.commit()
                results[tag] = "success"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results[tag] = f"error: {exc}"
            finally:
                db.close()

        t1 = threading.Thread(target=try_approve, args=("a",))
        t2 = threading.Thread(target=try_approve, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        successes = [key for key, value in results.items() if value == "success"]
        assert len(successes) == 1, f"Expected exactly one approval to succeed, got {results}"

        from app.models import PaymentStatus, OrderStatus
        payment, order, product = self._fetch_final_state(
            pg_engine, payment_id, order_id, product_id
        )
        metadata = payment.payment_metadata or {}
        assert payment.status == PaymentStatus.SUCCESS
        assert order.status == OrderStatus.CONFIRMED
        assert metadata.get("receipt_approval", {}).get("action") == "APPROVED"
        assert "receipt_rejection" not in metadata
        assert product.stock_quantity == 10

    def test_concurrent_duplicate_rejection(self, pg_engine):
        """Concurrent duplicate rejections: only one succeeds."""
        import threading
        import time
        from app.models import Payment, PaymentStatus, Order, OrderStatus
        from app.core.database import utcnow

        payment_id, order_id, product_id = self._setup_payment_with_receipt(pg_engine)
        SessionLocal = self._session_factory(pg_engine)
        results = {"a": None, "b": None}

        def try_reject(tag):
            db = SessionLocal()
            try:
                payment = (
                    db.query(Payment)
                    .filter(Payment.id == payment_id)
                    .with_for_update()
                    .first()
                )
                if (
                    payment is None
                    or payment.status != PaymentStatus.PENDING
                    or payment.receipt_data is None
                ):
                    results[tag] = "skipped"
                    return
                payment.status = PaymentStatus.FAILED
                payment.receipt_reviewed_at = utcnow()
                payment.receipt_reviewed_by = 1
                payment.receipt_rejection_reason = "Concurrent duplicate rejection"
                payment.payment_metadata = {
                    **(payment.payment_metadata or {}),
                    "receipt_rejection": {
                        "reviewed_by": 1,
                        "reviewed_at": utcnow().isoformat(),
                        "action": "REJECTED",
                        "reason": "Concurrent duplicate",
                    },
                }
                # Hold the row lock briefly to force real lock contention.
                time.sleep(0.25)
                db.add(payment)
                db.commit()
                results[tag] = "success"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results[tag] = f"error: {exc}"
            finally:
                db.close()

        t1 = threading.Thread(target=try_reject, args=("a",))
        t2 = threading.Thread(target=try_reject, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        successes = [key for key, value in results.items() if value == "success"]
        assert len(successes) == 1, f"Expected exactly one rejection to succeed, got {results}"

        from app.models import PaymentStatus, OrderStatus
        payment, order, product = self._fetch_final_state(
            pg_engine, payment_id, order_id, product_id
        )
        metadata = payment.payment_metadata or {}
        assert payment.status == PaymentStatus.FAILED
        assert order.status == OrderStatus.PENDING
        assert metadata.get("receipt_rejection", {}).get("action") == "REJECTED"
        assert "receipt_approval" not in metadata
        assert product.stock_quantity == 10


# ---------------------------------------------------------------------------
# Privacy and Serialization
# ---------------------------------------------------------------------------


class TestPrivacyAndSerialization:
    """Privacy and serialization tests"""

    def test_receipt_binary_not_in_payment_read(self, test_client, pg_db):
        """Receipt binary data is not serialized; receipt metadata is exposed."""
        from app.schemas.payment import PaymentRead

        headers = _register_and_login(test_client, "serialize1@example.com")
        product = _create_product(pg_db, "Serialize Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        resp = test_client.get(f"/payments/{payment.id}", headers=headers)
        assert resp.status_code == 200
        payment_data = resp.json()

        # Receipt binary must never appear in a normal payment response.
        assert "receipt_data" not in payment_data
        # The seven receipt metadata fields are exposed (safe, non-binary).
        for field in (
            "receipt_filename",
            "receipt_mime_type",
            "receipt_size",
            "receipt_uploaded_at",
            "receipt_reviewed_at",
            "receipt_reviewed_by",
            "receipt_rejection_reason",
        ):
            assert field in payment_data
        assert payment_data["receipt_filename"] == "receipt.pdf"
        assert payment_data["receipt_mime_type"] == "application/pdf"
        assert payment_data["receipt_size"] == len(pdf_content)

    def test_receipt_metadata_in_payment_read(self):
        """PaymentRead exposes the seven receipt metadata fields, never receipt_data."""
        from app.schemas.payment import PaymentRead

        receipt_fields = {
            name for name in PaymentRead.model_fields if name.startswith("receipt_")
        }
        assert receipt_fields == {
            "receipt_filename",
            "receipt_mime_type",
            "receipt_size",
            "receipt_uploaded_at",
            "receipt_reviewed_at",
            "receipt_reviewed_by",
            "receipt_rejection_reason",
        }
        assert "receipt_data" not in PaymentRead.model_fields

    def test_receipt_not_in_order_response(self, test_client, pg_db):
        """Receipt not in order response."""
        headers = _register_and_login(test_client, "orderresp1@example.com")
        product = _create_product(pg_db, "OrderResp Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        resp = test_client.get(
            f"/orders/{order['id']}",
            headers=headers,
        )
        assert resp.status_code == 200
        order_data = resp.json()
        assert "receipt_data" not in order_data
        assert "receipt_filename" not in order_data

    def test_receipt_not_in_order_list(self, test_client, pg_db):
        """Receipt not in order list."""
        headers = _register_and_login(test_client, "orderlist1@example.com")
        product = _create_product(pg_db, "OrderList Product")
        order = _create_pickup_order(test_client, headers, product.id)
        payment = _create_bank_transfer_payment(pg_db, order["id"])

        pdf_content = _make_pdf_content()
        files = {"file": ("receipt.pdf", pdf_content, "application/pdf")}
        upload_resp = test_client.post(
            f"/payments/{payment.id}/receipt",
            headers=headers,
            files=files,
        )
        assert upload_resp.status_code == 200

        resp = test_client.get("/orders", headers=headers)
        assert resp.status_code == 200
        orders = resp.json()
        assert len(orders) > 0
        for o in orders:
            assert "receipt_data" not in o
            assert "receipt_filename" not in o


# Implementation fixtures


@pytest.fixture(scope="function")
def SessionLocal():
    """Create a session factory for the live PostgreSQL database."""
    if not IMPORT_TEST_TARGET_URL:
        pytest.skip("IMPORT_TEST_TARGET_URL not set")
    from sqlalchemy.orm import sessionmaker
    engine = create_approved_postgres_engine()
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _get_admin_headers(client):
    """Helper to get admin headers."""
    from app.models import User, UserRole
    from app.core.auth import hash_password
    from app.core.database import get_db
    from app.main import app

    # Try to use existing admin or create one
    from sqlalchemy.orm import sessionmaker
    from app.core.database import get_db
    from app.main import app

    # This is a simplified version - we'll use the client directly
    login = client.post("/auth/login", json={"email": "admin_receipt_test@example.com", "password": "adminpass123"})
    if login.status_code != 200:
        # Try to create admin
        from app.core.auth import hash_password
        from app.models import User, UserRole
        from sqlalchemy.orm import sessionmaker
        from app.core.database import Base

        engine = create_approved_postgres_engine()
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        db = SessionLocal()
        try:
            from app.models import User, UserRole
            from app.core.auth import hash_password
            admin = db.query(User).filter(User.email == "admin_receipt_test@example.com").first()
            if not admin:
                admin = User(
                    name="Admin User",
                    email="admin_receipt_test@example.com",
                    password_hash=hash_password("adminpass123"),
                    role=UserRole.ADMIN,
                    permissions=[],
                    is_active=True,
                )
                db.add(admin)
                db.commit()
        finally:
            db.close()

        login = client.post("/auth/login", json={"email": "admin_receipt_test@example.com", "password": "adminpass123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}