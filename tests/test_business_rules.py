import pytest
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import require_admin, require_permission
from app.core.database import Base, get_db
from app.main import app
from app.models import Address, Category, FulfillmentMethod, OrderStatus, Permission, Product, User, UserRole


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


def _create_product(client, name: str, price: str | float = 100.00, stock: int = 10):
    from app.models import Category, Product

    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        category = db.query(Category).filter(Category.name == "Test Category").first()
        if category is None:
            category = Category(name="Test Category", description="Test category")
            db.add(category)
            db.commit()
            db.refresh(category)

        product = Product(
            category_id=category.id,
            name=name,
            description="Test product",
            price=Decimal(str(price)),
            stock_quantity=stock,
            is_active=True,
        )
        db.add(product)
        db.commit()
        db.refresh(product)
        return product
    finally:
        db.close()


def _register_and_login(client, email: str = "alice@example.com", password: str = "secretpass123", role: str = "CUSTOMER"):
    register = client.post(
        "/auth/register",
        json={
            "name": email.split("@")[0].title() if "@" in email else "User",
            "email": email,
            "password": password,
            "role": role,
        },
    )
    assert register.status_code == 200, register.text
    login = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_user_roles_and_permissions_are_defined():
    assert UserRole.CUSTOMER.value == "CUSTOMER"
    assert UserRole.STAFF.value == "STAFF"
    assert UserRole.ADMIN.value == "ADMIN"
    assert Permission.MANAGE_PRODUCTS.value == "MANAGE_PRODUCTS"
    assert Permission.MANAGE_INVENTORY.value == "MANAGE_INVENTORY"
    assert Permission.MANAGE_ORDERS.value == "MANAGE_ORDERS"


def test_admin_has_all_permissions_by_business_rule():
    admin = User(role=UserRole.ADMIN, permissions=[])
    assert require_admin(admin) is True
    assert require_permission(admin, Permission.MANAGE_PRODUCTS) is True
    assert require_permission(admin, Permission.MANAGE_ORDERS) is True


def test_staff_requires_explicit_permission_for_staff_routes():
    staff = User(role=UserRole.STAFF, permissions=[Permission.MANAGE_PRODUCTS])
    assert require_permission(staff, Permission.MANAGE_PRODUCTS) is True
    assert require_permission(staff, Permission.MANAGE_ORDERS) is False


def test_customer_cannot_access_staff_routes():
    customer = User(role=UserRole.CUSTOMER, permissions=[])
    assert require_permission(customer, Permission.MANAGE_PRODUCTS) is False
    assert require_admin(customer) is False


def test_fulfillment_and_order_status_values_match_finalized_design():
    assert FulfillmentMethod.STORE_DELIVERY.value == "STORE_DELIVERY"
    assert FulfillmentMethod.CUSTOMER_DISPATCH.value == "CUSTOMER_DISPATCH"
    assert FulfillmentMethod.STORE_PICKUP.value == "STORE_PICKUP"
    assert OrderStatus.READY_FOR_PICKUP.value == "READY_FOR_PICKUP"
    assert OrderStatus.OUT_FOR_DELIVERY.value == "OUT_FOR_DELIVERY"


def test_address_model_uses_customer_friendly_fields():
    address = Address(
        user_id=7,
        recipient_name="Jane Doe",
        phone_number="08012345678",
        address_line="12 Allen Avenue",
        city="Ikeja",
        state="Lagos",
        additional_directions="Near the bus stop",
        is_default=True,
    )
    assert address.recipient_name == "Jane Doe"
    assert address.phone_number == "08012345678"
    assert address.is_default is True


def test_address_routes_require_auth_and_default_status(client):
    response = client.post(
        "/addresses",
        json={
            "recipient_name": "Alice Example",
            "phone_number": "08012345678",
            "address_line": "12 Allen Avenue",
            "city": "Ikeja",
            "state": "Lagos",
            "additional_directions": "Near the park",
        },
    )
    assert response.status_code == 401

    headers = _register_and_login(client)

    create_response = client.post(
        "/addresses",
        headers=headers,
        json={
            "recipient_name": "Alice Example",
            "phone_number": "08012345678",
            "address_line": "12 Allen Avenue",
            "city": "Ikeja",
            "state": "Lagos",
            "additional_directions": "Near the park",
        },
    )
    assert create_response.status_code == 200, create_response.text
    payload = create_response.json()
    assert payload["is_default"] is True
    assert payload["recipient_name"] == "Alice Example"

    second_response = client.post(
        "/addresses",
        headers=headers,
        json={
            "recipient_name": "Alice Example 2",
            "phone_number": "08087654321",
            "address_line": "88 Bode Thomas Street",
            "city": "Surulere",
            "state": "Lagos",
            "is_default": True,
        },
    )
    assert second_response.status_code == 200, second_response.text
    second_payload = second_response.json()
    assert second_payload["is_default"] is True

    list_response = client.get("/addresses", headers=headers)
    assert list_response.status_code == 200
    addresses = list_response.json()
    assert len(addresses) == 2
    assert sum(1 for item in addresses if item["is_default"]) == 1


def test_address_ownership_and_default_rules(client):
    first_user = _register_and_login(client, "alice@example.com", "secretpass123")
    second_user = _register_and_login(client, "bob@example.com", "secretpass456")

    first_address = client.post(
        "/addresses",
        headers=first_user,
        json={
            "recipient_name": "Alice",
            "phone_number": "08000000001",
            "address_line": "1 Alice Street",
            "city": "Ikeja",
            "state": "Lagos",
        },
    )
    assert first_address.status_code == 200
    first_id = first_address.json()["id"]

    other_address = client.post(
        "/addresses",
        headers=second_user,
        json={
            "recipient_name": "Bob",
            "phone_number": "08000000002",
            "address_line": "2 Bob Street",
            "city": "Yaba",
            "state": "Lagos",
        },
    )
    assert other_address.status_code == 200

    list_response = client.get("/addresses", headers=first_user)
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1
    assert list_response.json()[0]["id"] == first_id

    other_list = client.get("/addresses", headers=second_user)
    assert len(other_list.json()) == 1

    unauthorized = client.get(f"/addresses/{other_address.json()['id']}", headers=first_user)
    assert unauthorized.status_code == 404

    patch_default = client.patch(f"/addresses/{first_id}/default", headers=first_user)
    assert patch_default.status_code == 200
    assert patch_default.json()["is_default"] is True

    update_response = client.put(
        f"/addresses/{first_id}",
        headers=first_user,
        json={
            "recipient_name": "Alice Updated",
            "phone_number": "08000000099",
            "address_line": "9 Alice Avenue",
            "city": "Lekki",
            "state": "Lagos",
            "is_default": True,
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["recipient_name"] == "Alice Updated"

    validation = client.post(
        "/addresses",
        headers=first_user,
        json={
            "recipient_name": "A",
            "phone_number": "080",
            "address_line": "A",
            "city": "A",
            "state": "A",
        },
    )
    assert validation.status_code == 422

    user_id_attack = client.post(
        "/addresses",
        headers=first_user,
        json={
            "user_id": 999,
            "recipient_name": "Hacker",
            "phone_number": "08011111111",
            "address_line": "123 Fake Street",
            "city": "Ikeja",
            "state": "Lagos",
        },
    )
    assert user_id_attack.status_code == 200
    assert user_id_attack.json()["user_id"] == 1


def test_address_delete_promotes_other_default(client):
    headers = _register_and_login(client, "charlie@example.com", "secretpass789")
    first = client.post(
        "/addresses",
        headers=headers,
        json={
            "recipient_name": "Charlie A",
            "phone_number": "08010000001",
            "address_line": "1 A Street",
            "city": "Yaba",
            "state": "Lagos",
        },
    )
    second = client.post(
        "/addresses",
        headers=headers,
        json={
            "recipient_name": "Charlie B",
            "phone_number": "08010000002",
            "address_line": "2 B Street",
            "city": "Yaba",
            "state": "Lagos",
            "is_default": True,
        },
    )
    assert first.status_code == 200 and second.status_code == 200
    first_id = first.json()["id"]
    second_id = second.json()["id"]
    assert first.json()["is_default"] is True
    assert second.json()["is_default"] is True

    delete_response = client.delete(f"/addresses/{second_id}", headers=headers)
    assert delete_response.status_code == 200
    remaining = client.get("/addresses", headers=headers).json()
    assert len(remaining) == 1
    assert remaining[0]["id"] == first_id
    assert remaining[0]["is_default"] is True

    third = client.post(
        "/addresses",
        headers=headers,
        json={
            "recipient_name": "Charlie C",
            "phone_number": "08010000003",
            "address_line": "3 C Street",
            "city": "Surulere",
            "state": "Lagos",
        },
    )
    assert third.status_code == 200
    assert third.json()["is_default"] is False


def test_order_creation_uses_authenticated_user_and_server_side_pricing(client):
    headers = _register_and_login(client, "dora@example.com", "secretpass321")
    product = _create_product(client, "Toy Car", price=150.00, stock=9)
    address_response = client.post(
        "/addresses",
        headers=headers,
        json={
            "recipient_name": "Dora Example",
            "phone_number": "08022223333",
            "address_line": "15 Orchid Road",
            "city": "Lekki",
            "state": "Lagos",
        },
    )
    assert address_response.status_code == 200, address_response.text
    address_id = address_response.json()["id"]

    order_response = client.post(
        "/orders",
        headers=headers,
        json={
            "fulfillment_method": "STORE_DELIVERY",
            "address_id": address_id,
            "items": [{"product_id": product.id, "quantity": 2}],
        },
    )
    assert order_response.status_code == 200, order_response.text
    payload = order_response.json()
    assert payload["user_id"] == 1
    assert payload["status"] == "PENDING"
    assert payload["fulfillment_method"] == "STORE_DELIVERY"
    assert payload["delivery_fee"] == 0.0
    assert payload["total_amount"] == 300.0
    assert payload["order_items"][0]["unit_price"] == 150.0
    assert payload["order_items"][0]["subtotal"] == 300.0
    assert payload["delivery_recipient_name"] == "Dora Example"


def test_order_stock_and_auth_validation(client):
    headers = _register_and_login(client, "erin@example.com", "pass123456")
    product = _create_product(client, "Puzzle", price=80.00, stock=2)

    unauthorized = client.post(
        "/orders",
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
        },
    )
    assert unauthorized.status_code == 401

    invalid_stock = client.post(
        "/orders",
        headers=headers,
        json={
            "items": [{"product_id": product.id, "quantity": 5}],
        },
    )
    assert invalid_stock.status_code == 400

    good = client.post(
        "/orders",
        headers=headers,
        json={
            "fulfillment_method": "STORE_PICKUP",
            "items": [{"product_id": product.id, "quantity": 1}],
        },
    )
    assert good.status_code == 200, good.text
    assert good.json()["status"] == "PENDING"


def test_store_pickup_and_order_ownership(client):
    customer = _register_and_login(client, "frank@example.com", "pass654321")
    owner = _register_and_login(client, "frank2@example.com", "pass654322")
    product = _create_product(client, "Blocks", price=50.00, stock=5)

    order_response = client.post(
        "/orders",
        headers=customer,
        json={
            "fulfillment_method": "STORE_PICKUP",
            "items": [{"product_id": product.id, "quantity": 1}],
        },
    )
    assert order_response.status_code == 200, order_response.text
    order_id = order_response.json()["id"]

    my_orders = client.get("/orders", headers=customer)
    assert my_orders.status_code == 200
    assert any(item["id"] == order_id for item in my_orders.json())

    other_user = client.get(f"/orders/{order_id}", headers=owner)
    assert other_user.status_code == 404


def test_product_listing_is_public(client):
    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        from app.models import Category

        category = Category(name="Toys", description="Toy products")
        db.add(category)
        db.commit()
        db.refresh(category)

        for i in range(3):
            product = Product(
                category_id=category.id,
                name=f"Product {i}",
                description=f"Description {i}",
                price=Decimal(str(50.00 + i * 10)),
                stock_quantity=10 + i,
                is_active=True,
            )
            db.add(product)
        db.commit()

        response = client.get("/products")
        assert response.status_code == 200
        products = response.json()
        assert len(products) == 3
        assert products[0]["name"] == "Product 2"
        assert products[0]["price"] == "70.00"
    finally:
        db.close()


def test_product_detail_requires_active(client):
    product = _create_product(client, "Inactive Widget", price=99.99, stock=5)
    
    active_response = client.get(f"/products/{product.id}")
    assert active_response.status_code == 200
    assert active_response.json()["is_active"] is True

    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        db.query(Product).filter(Product.id == product.id).update({"is_active": False})
        db.commit()
    finally:
        db.close()

    inactive_response = client.get(f"/products/{product.id}")
    assert inactive_response.status_code == 404


def test_product_creation_requires_permission(client):
    customer = _register_and_login(client, "gina@example.com", "pass123456")
    _create_product(client, "Test Product", price=100.00, stock=1)

    category_id = _create_category(client)

    no_auth = client.post(
        "/products",
        json={
            "name": "Test",
            "price": "50.00",
            "category_id": category_id,
            "stock_quantity": 10,
        },
    )
    assert no_auth.status_code == 401

    customer_attempt = client.post(
        "/products",
        headers=customer,
        json={
            "name": "Test",
            "price": "50.00",
            "category_id": category_id,
            "stock_quantity": 10,
        },
    )
    assert customer_attempt.status_code == 403


def test_product_update_requires_permission(client):
    product = _create_product(client, "Original Name", price=100.00, stock=5)
    customer = _register_and_login(client, "hannah@example.com", "pass123456")

    customer_attempt = client.put(
        f"/products/{product.id}",
        headers=customer,
        json={"name": "Modified Name"},
    )
    assert customer_attempt.status_code == 403


def test_product_stock_update_requires_inventory_permission(client):
    product = _create_product(client, "Stock Test", price=50.00, stock=10)

    customer = _register_and_login(client, "iris@example.com", "pass123456")
    customer_attempt = client.patch(
        f"/products/{product.id}/stock",
        headers=customer,
        json={"quantity": 20},
    )
    assert customer_attempt.status_code == 403


def test_product_activation_deactivation(client):
    product = _create_product(client, "Deactivatable", price=75.00, stock=5)
    assert product.is_active is True

    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        admin = _register_and_login(client, "admin@example.com", "pass123456", role="ADMIN")

        deactivate = client.patch(f"/products/{product.id}/deactivate", headers=admin)
        assert deactivate.status_code == 200
        assert deactivate.json()["is_active"] is False

        activate = client.patch(f"/products/{product.id}/activate", headers=admin)
        assert activate.status_code == 200
        assert activate.json()["is_active"] is True
    finally:
        db.close()


def test_inactive_product_cannot_be_ordered(client):
    customer = _register_and_login(client, "jack@example.com", "pass123456")
    product = _create_product(client, "Will Be Deactivated", price=100.00, stock=10)

    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        db.query(Product).filter(Product.id == product.id).update({"is_active": False})
        db.commit()
    finally:
        db.close()

    order_attempt = client.post(
        "/orders",
        headers=customer,
        json={
            "fulfillment_method": "STORE_PICKUP",
            "items": [{"product_id": product.id, "quantity": 1}],
        },
    )
    assert order_attempt.status_code == 400
    assert "not available" in order_attempt.json()["detail"]


def test_category_endpoints(client):
    no_auth_list = client.get("/categories")
    assert no_auth_list.status_code == 200
    initial_count = len(no_auth_list.json())

    customer = _register_and_login(client, "kate@example.com", "pass123456")
    admin_headers = _get_admin_headers(client)

    customer_create = client.post(
        "/categories",
        headers=customer,
        json={"name": "Customer Category", "description": "Should fail"},
    )
    assert customer_create.status_code == 403

    admin_create = client.post(
        "/categories",
        headers=admin_headers,
        json={"name": "Admin Category", "description": "New category"},
    )
    assert admin_create.status_code == 200
    category_id = admin_create.json()["id"]

    get_category = client.get(f"/categories/{category_id}")
    assert get_category.status_code == 200
    assert get_category.json()["name"] == "Admin Category"

    all_categories = client.get("/categories")
    assert len(all_categories.json()) > initial_count


def _create_category(client):
    admin_headers = _get_admin_headers(client)
    response = client.post(
        "/categories",
        headers=admin_headers,
        json={"name": f"Category {hash(client)}", "description": "Test category"},
    )
    if response.status_code == 200:
        return response.json()["id"]
    raise Exception(f"Failed to create category: {response.text}")


def _get_admin_headers(client):
    try:
        existing = client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "pass123456"},
        )
        if existing.status_code == 200:
            return {"Authorization": f"Bearer {existing.json()['access_token']}"}
    except Exception:
        pass

    register = client.post(
        "/auth/register",
        json={
            "name": "Admin User",
            "email": "admin@example.com",
            "password": "pass123456",
            "role": "ADMIN",
        },
    )
    if register.status_code == 200:
        login = client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "pass123456"},
        )
        return {"Authorization": f"Bearer {login.json()['access_token']}"}
    raise Exception("Failed to get admin headers")


def test_checkout_validation_empty_cart(client):
    customer = _register_and_login(client, "leo@example.com", "pass123456")

    response = client.post(
        "/checkout/validate",
        headers=customer,
        json={
            "items": [],
            "fulfillment_method": "STORE_PICKUP",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert any("empty" in err.lower() for err in data["errors"])


def test_checkout_validation_inactive_product(client):
    customer = _register_and_login(client, "mia@example.com", "pass123456")
    product = _create_product(client, "Will Deactivate", price=100.00, stock=10)

    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        db.query(Product).filter(Product.id == product.id).update({"is_active": False})
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/checkout/validate",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
            "fulfillment_method": "STORE_PICKUP",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert any("not available" in err for err in data["errors"])


def test_checkout_validation_insufficient_stock(client):
    customer = _register_and_login(client, "noah@example.com", "pass123456")
    product = _create_product(client, "Limited Stock", price=50.00, stock=2)

    response = client.post(
        "/checkout/validate",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 5}],
            "fulfillment_method": "STORE_PICKUP",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert any("Insufficient stock" in err for err in data["errors"])


def test_checkout_validation_server_side_pricing(client):
    customer = _register_and_login(client, "olivia@example.com", "pass123456")
    product = _create_product(client, "Price Test", price=100.00, stock=10)

    response = client.post(
        "/checkout/validate",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 2}],
            "fulfillment_method": "STORE_PICKUP",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert data["summary"]["items"][0]["unit_price"] == 100.0
    assert data["summary"]["items"][0]["subtotal"] == 200.0
    assert data["summary"]["subtotal"] == 200.0
    assert data["summary"]["total_amount"] == 200.0


def test_checkout_validation_with_address(client):
    customer = _register_and_login(client, "paul@example.com", "pass123456")
    product = _create_product(client, "Address Test", price=75.00, stock=5)

    address_response = client.post(
        "/addresses",
        headers=customer,
        json={
            "recipient_name": "Paul Recipient",
            "phone_number": "08033333333",
            "address_line": "30 Street Name",
            "city": "Lagos",
            "state": "Lagos",
        },
    )
    assert address_response.status_code == 200
    address_id = address_response.json()["id"]

    response = client.post(
        "/checkout/validate",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
            "fulfillment_method": "STORE_DELIVERY",
            "address_id": address_id,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert data["summary"]["delivery_address"] is not None
    assert data["summary"]["delivery_address"]["recipient_name"] == "Paul Recipient"


def test_checkout_validation_requires_address_for_delivery(client):
    customer = _register_and_login(client, "quinn@example.com", "pass123456")
    product = _create_product(client, "Delivery Product", price=50.00, stock=5)

    response = client.post(
        "/checkout/validate",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
            "fulfillment_method": "STORE_DELIVERY",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert any("Address is required" in err for err in data["errors"])


def test_checkout_unauthenticated_rejected(client):
    response = client.post(
        "/checkout/validate",
        json={
            "items": [{"product_id": 1, "quantity": 1}],
            "fulfillment_method": "STORE_PICKUP",
        },
    )
    assert response.status_code == 401


def test_checkout_creates_order_successfully(client):
    customer = _register_and_login(client, "rachel@example.com", "pass123456")
    product = _create_product(client, "Checkout Product", price=200.00, stock=5)

    response = client.post(
        "/checkout",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 2}],
            "fulfillment_method": "STORE_PICKUP",
        },
    )
    assert response.status_code == 200
    order = response.json()
    assert order["user_id"] == 1
    assert order["status"] == "PENDING"
    assert order["fulfillment_method"] == "STORE_PICKUP"
    assert order["total_amount"] == 400.0
    assert len(order["order_items"]) == 1
    assert order["order_items"][0]["unit_price"] == 200.0
    assert order["order_items"][0]["subtotal"] == 400.0

    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        updated_product = db.query(Product).filter(Product.id == product.id).first()
        assert updated_product.stock_quantity == 3
    finally:
        db.close()


def test_checkout_deducts_stock(client):
    customer = _register_and_login(client, "sam@example.com", "pass123456")
    product = _create_product(client, "Stock Deduction", price=100.00, stock=10)

    engine = app.state.test_engine
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        initial_stock = db.query(Product).filter(Product.id == product.id).first().stock_quantity
        assert initial_stock == 10
    finally:
        db.close()

    response = client.post(
        "/checkout",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 3}],
            "fulfillment_method": "STORE_PICKUP",
        },
    )
    assert response.status_code == 200

    db = SessionLocal()
    try:
        final_stock = db.query(Product).filter(Product.id == product.id).first().stock_quantity
        assert final_stock == 7
    finally:
        db.close()


def test_checkout_multiple_items(client):
    customer = _register_and_login(client, "tina@example.com", "pass123456")
    product1 = _create_product(client, "Product A", price=100.00, stock=10)
    product2 = _create_product(client, "Product B", price=200.00, stock=5)

    response = client.post(
        "/checkout",
        headers=customer,
        json={
            "items": [
                {"product_id": product1.id, "quantity": 2},
                {"product_id": product2.id, "quantity": 1},
            ],
            "fulfillment_method": "STORE_PICKUP",
        },
    )
    assert response.status_code == 200
    order = response.json()
    assert order["total_amount"] == 400.0
    assert len(order["order_items"]) == 2


def test_checkout_with_delivery_address(client):
    customer = _register_and_login(client, "uma@example.com", "pass123456")
    product = _create_product(client, "Delivery Item", price=150.00, stock=8)

    address = client.post(
        "/addresses",
        headers=customer,
        json={
            "recipient_name": "Uma Delivery",
            "phone_number": "08044444444",
            "address_line": "40 Delivery St",
            "city": "Lagos",
            "state": "Lagos",
        },
    )
    assert address.status_code == 200
    address_id = address.json()["id"]

    response = client.post(
        "/checkout",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
            "fulfillment_method": "STORE_DELIVERY",
            "address_id": address_id,
        },
    )
    assert response.status_code == 200
    order = response.json()
    assert order["delivery_recipient_name"] == "Uma Delivery"
    assert order["delivery_address_line"] == "40 Delivery St"


def test_checkout_customer_cannot_use_other_address(client):
    customer1 = _register_and_login(client, "vera@example.com", "pass123456")
    customer2 = _register_and_login(client, "vera2@example.com", "pass123457")
    product = _create_product(client, "Cross Address Test", price=100.00, stock=10)

    address = client.post(
        "/addresses",
        headers=customer1,
        json={
            "recipient_name": "Vera 1",
            "phone_number": "08055555555",
            "address_line": "50 Vera St",
            "city": "Lagos",
            "state": "Lagos",
        },
    )
    assert address.status_code == 200
    address_id = address.json()["id"]

    response = client.post(
        "/checkout",
        headers=customer2,
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
            "fulfillment_method": "STORE_DELIVERY",
            "address_id": address_id,
        },
    )
    assert response.status_code == 404
    assert "Address not found" in response.json()["detail"]


def test_checkout_with_invalid_fulfillment_method(client):
    customer = _register_and_login(client, "wendy@example.com", "pass123456")
    product = _create_product(client, "Method Test", price=100.00, stock=5)

    response = client.post(
        "/checkout",
        headers=customer,
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
            "fulfillment_method": "INVALID_METHOD",
        },
    )
    assert response.status_code == 400
    assert "Invalid fulfillment method" in response.json()["detail"]


