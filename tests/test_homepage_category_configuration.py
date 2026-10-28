import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import create_access_token, hash_password
from app.core.database import Base, get_db
from app.main import app
from app.models import Category, User, UserRole


@pytest.fixture
def homepage_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    app.state.test_engine = engine

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    db = session_factory()
    try:
        admin = User(
            name="Homepage Admin",
            email="homepage-admin@example.com",
            password_hash=hash_password("test-password-123"),
            role=UserRole.ADMIN,
            permissions=[],
            is_active=True,
        )
        customer = User(
            name="Homepage Customer",
            email="homepage-customer@example.com",
            password_hash=hash_password("test-password-123"),
            role=UserRole.CUSTOMER,
            permissions=[],
            is_active=True,
        )
        db.add_all([admin, customer])
        db.commit()
        admin_id = admin.id
        customer_id = customer.id
    finally:
        db.close()

    app.state.homepage_test_session_factory = session_factory
    app.state.homepage_test_admin_headers = {
        "Authorization": f"Bearer {create_access_token(admin_id)}"
    }
    app.state.homepage_test_customer_headers = {
        "Authorization": f"Bearer {create_access_token(customer_id)}"
    }

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _create_category(homepage_client, name: str, is_active: bool = True) -> Category:
    session_factory = app.state.homepage_test_session_factory
    db = session_factory()
    try:
        category = Category(name=name, description=None, is_active=is_active)
        db.add(category)
        db.commit()
        db.refresh(category)
        return category
    finally:
        db.close()


def test_public_config_lists_active_category_even_without_products(homepage_client):
    category = _create_category(homepage_client, "Homepage Empty Category")
    admin_headers = app.state.homepage_test_admin_headers

    saved = homepage_client.put(
        "/admin/homepage-categories",
        headers=admin_headers,
        json={
            "items": [
                {
                    "category_id": category.id,
                    "label": "  Playful Label  ",
                    "icon_key": "blocks",
                    "display_order": 0,
                }
            ]
        },
    )

    assert saved.status_code == 200
    public_response = homepage_client.get("/homepage-categories")
    assert public_response.status_code == 200
    assert public_response.json() == [
        {
            "category_id": category.id,
            "category_name": category.name,
            "label": "Playful Label",
            "icon_key": "blocks",
            "display_order": 0,
        }
    ]

    db = app.state.homepage_test_session_factory()
    try:
        db.query(Category).filter(Category.id == category.id).update({"is_active": False})
        db.commit()
    finally:
        db.close()

    assert homepage_client.get("/homepage-categories").json() == []


def test_admin_configuration_write_is_protected(homepage_client):
    category = _create_category(homepage_client, "Protected Homepage Category")
    payload = {
        "items": [
            {
                "category_id": category.id,
                "label": "Protected",
                "icon_key": "backpack",
                "display_order": 0,
            }
        ]
    }

    assert homepage_client.get("/admin/homepage-categories").status_code == 401
    assert homepage_client.put("/admin/homepage-categories", json=payload).status_code == 401
    assert homepage_client.put(
        "/admin/homepage-categories",
        headers=app.state.homepage_test_customer_headers,
        json=payload,
    ).status_code == 403


def test_invalid_replacement_does_not_remove_saved_configuration(homepage_client):
    category = _create_category(homepage_client, "Atomic Homepage Category")
    admin_headers = app.state.homepage_test_admin_headers
    valid_payload = {
        "items": [
            {
                "category_id": category.id,
                "label": "Saved",
                "icon_key": "gift",
                "display_order": 0,
            }
        ]
    }
    assert homepage_client.put(
        "/admin/homepage-categories", headers=admin_headers, json=valid_payload
    ).status_code == 200

    invalid_payload = {
        "items": [
            {
                "category_id": 999999,
                "label": "Invalid category",
                "icon_key": "gift",
                "display_order": 0,
            }
        ]
    }
    assert homepage_client.put(
        "/admin/homepage-categories", headers=admin_headers, json=invalid_payload
    ).status_code == 422
    assert homepage_client.get("/homepage-categories").json()[0]["category_id"] == category.id


def test_configuration_rejects_duplicate_values_and_unknown_icons(homepage_client):
    category_a = _create_category(homepage_client, "Duplicate Homepage A")
    category_b = _create_category(homepage_client, "Duplicate Homepage B")
    headers = app.state.homepage_test_admin_headers

    duplicate_category_payload = {
        "items": [
            {"category_id": category_a.id, "label": "A", "icon_key": "gift", "display_order": 0},
            {"category_id": category_a.id, "label": "B", "icon_key": "shirt", "display_order": 1},
        ]
    }
    duplicate_order_payload = {
        "items": [
            {"category_id": category_a.id, "label": "A", "icon_key": "gift", "display_order": 0},
            {"category_id": category_b.id, "label": "B", "icon_key": "shirt", "display_order": 0},
        ]
    }
    unknown_icon_payload = {
        "items": [
            {"category_id": category_a.id, "label": "A", "icon_key": "arbitrary-svg", "display_order": 0}
        ]
    }
    too_many_payload = {
        "items": [
            {"category_id": category_a.id, "label": "A", "icon_key": "gift", "display_order": 0},
            {"category_id": category_b.id, "label": "B", "icon_key": "shirt", "display_order": 1},
            {"category_id": 100001, "label": "C", "icon_key": "blocks", "display_order": 2},
            {"category_id": 100002, "label": "D", "icon_key": "backpack", "display_order": 3},
            {"category_id": 100003, "label": "E", "icon_key": "shopping-bag", "display_order": 4},
            {"category_id": 100004, "label": "F", "icon_key": "gift", "display_order": 5},
        ]
    }

    for payload in (
        duplicate_category_payload,
        duplicate_order_payload,
        unknown_icon_payload,
        too_many_payload,
    ):
        assert homepage_client.put(
            "/admin/homepage-categories", headers=headers, json=payload
        ).status_code == 422


def test_empty_configuration_clears_public_selection(homepage_client):
    headers = app.state.homepage_test_admin_headers
    category = _create_category(homepage_client, "Clearable Homepage Category")
    response = homepage_client.put(
        "/admin/homepage-categories",
        headers=headers,
        json={
            "items": [
                {"category_id": category.id, "label": "One", "icon_key": "gift", "display_order": 0}
            ]
        },
    )
    assert response.status_code == 200

    cleared = homepage_client.put(
        "/admin/homepage-categories", headers=headers, json={"items": []}
    )
    assert cleared.status_code == 200
    assert homepage_client.get("/homepage-categories").json() == []
