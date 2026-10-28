from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import sys
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, String, cast, func
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from decimal import Decimal
from pathlib import Path

from app.core.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    permission_required,
    require_admin,
    require_permission,
    verify_password,
)
from app.services.paystack import paystack_client, PaystackError, PaystackVerificationError
from app.core.database import get_db, settings, utcnow
from app.models import (
    Address,
    Category,
    DeliveryConfig,
    FulfillmentMethod,
    HomepageCategoryConfiguration,
    Order,
    OrderItem,
    OrderStatus,
    Permission,
    Payment,
    PaymentStatus,
    Expense,
    Product,
    User,
    UserRole,
)
from app.schemas.address import AddressCreate, AddressRead, AddressUpdate
from app.schemas.delivery_fee import DeliveryFeeRead, DeliveryFeeUpdate
from app.schemas.payment import PaymentCreate, PaymentRead, PaymentStatusUpdate, ReceiptUploadResponse, ReceiptReviewRequest, ReceiptReviewResponse
from app.schemas.expense import ExpenseCreate, ExpenseRead
from app.schemas.checkout import (
    CartItemDetails,
    CheckoutRequest,
    CheckoutSummary,
    CheckoutValidationResponse,
)
from app.schemas.order import (
    CustomerOrderHistoryResponse,
    CustomerOrderSummary,
    OrderCreate,
    OrderRead,
    OrderStatusUpdate,
)
from app.schemas.product import (
    CategoryCreate,
    CategoryRead,
    ProductCreate,
    ProductListRead,
    ProductRead,
    ProductUpdate,
    StockUpdate,
)
from app.schemas.homepage_category import (
    HomepageCategoryAdminEntry,
    HomepageCategoryPublicEntry,
    HomepageCategorySelectionRequest,
)
from app.schemas.product_image import (
    ProductImageDeleteResponse,
    ProductImageUploadResponse,
)
from app.schemas.user import CustomerListResponse, CustomerSummary, UserCreate, UserRead, UserUpdate


from app.services.product_image import (
    destroy_cloudinary_image,
    extract_our_public_id,
    is_our_cloudinary_url,
    upload_to_cloudinary,
    _validate_and_prepare_upload,
)
from app.services.receipt import validate_receipt_file
from app.api.admin_dashboard import router as admin_dashboard_router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("twelve09")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Twelve09 Kiddies Store API")
    from app.core.database import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection verified")
    except Exception as e:
        logger.error("Database connection failed: %s", e)
        raise
    yield
    logger.info("Shutting down Twelve09 Kiddies Store API")


docs_url = "/docs" if settings.ENVIRONMENT == "development" else None
redoc_url = "/redoc" if settings.ENVIRONMENT == "development" else None

app = FastAPI(
    title="Twelve09 Kiddies Store API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=docs_url,
    redoc_url=redoc_url,
)

allowed_origins = [o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_dashboard_router)


def _previous_references_filter(db: Session, reference_str: str):
    """Build a dialect-compatible filter for payment_metadata.previous_references.

    MySQL uses JSON path extraction; PostgreSQL uses the ->> JSONB operator.
    Both produce the same LIKE-based matching behavior.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return Payment.payment_metadata.op("->>")("previous_references").like(f"%{reference_str}%")
    return cast(
        func.json_extract(Payment.payment_metadata, "$.previous_references"),
        String,
    ).like(f"%{reference_str}%")


def _get_user_address_for_update(db: Session, current_user: User, address_id: int) -> Address | None:
    return (
        db.query(Address)
        .filter(Address.id == address_id, Address.user_id == current_user.id)
        .first()
    )


def _set_default_address(db: Session, current_user: User, address: Address) -> Address:
    db.query(Address).filter(
        Address.user_id == current_user.id,
        Address.id != address.id,
    ).update({"is_default": False}, synchronize_session=False)
    address.is_default = True
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def _ensure_default_after_delete(db: Session, current_user: User, deleted_address_id: int) -> None:
    remaining = (
        db.query(Address)
        .filter(Address.user_id == current_user.id, Address.id != deleted_address_id)
        .order_by(Address.created_at.asc(), Address.id.asc())
        .all()
    )
    if not remaining:
        return

    first_remaining = remaining[0]
    for item in remaining:
        item.is_default = item.id == first_remaining.id
    db.add_all(remaining)
    db.commit()


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "message": "Twelve09 API is running",
    }


@app.get("/admin/delivery-fee", response_model=DeliveryFeeRead)
def get_delivery_fee(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    config = db.query(DeliveryConfig).first()
    if config is None:
        config = DeliveryConfig(fee_amount=0.00, is_active=True)
        db.add(config)
        db.commit()
        db.refresh(config)

    return DeliveryFeeRead.model_validate(config)


@app.patch("/admin/delivery-fee", response_model=DeliveryFeeRead)
def update_delivery_fee(
    payload: DeliveryFeeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    config = db.query(DeliveryConfig).first()
    if config is None:
        config = DeliveryConfig(fee_amount=payload.fee_amount, is_active=True)
        db.add(config)
    else:
        config.fee_amount = payload.fee_amount

    db.commit()
    db.refresh(config)

    return DeliveryFeeRead.model_validate(config)


@app.post("/auth/register", response_model=UserRead)
def register_user(user_in: UserCreate, db: Session = Depends(get_db)) -> User:
    existing_user = db.query(User).filter(User.email == str(user_in.email)).first()
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    user = User(
        name=user_in.name,
        email=str(user_in.email),
        password_hash=hash_password(user_in.password),
        role=UserRole.CUSTOMER,
        permissions=[],
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/login")
def login_user(payload: dict, db: Session = Depends(get_db)):
    email = str(payload.get("email", "")).strip().lower()
    password = payload.get("password", "")

    if not email or not password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email and password are required",
        )

    user = db.query(User).filter(User.email == email).first()
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
        )

    token = create_access_token(user.id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserRead.model_validate(user).model_dump(),
    }


@app.get("/auth/me", response_model=UserRead)
def get_authenticated_user(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@app.get("/admin/users", response_model=list[UserRead])
def list_users(
    role: UserRole | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[User]:
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    query = db.query(User)
    if role is not None:
        query = query.filter(User.role == role)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)
    if search is not None:
        trimmed = search.strip()
        if trimmed:
            if len(trimmed) > 100:
                trimmed = trimmed[:100]
            escaped = trimmed.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            from sqlalchemy import or_

            query = query.filter(
                or_(
                    User.name.ilike(pattern, escape="\\"),
                    User.email.ilike(pattern, escape="\\"),
                )
            )
    return query.order_by(User.created_at.desc()).offset(skip).limit(limit).all()


@app.get("/admin/customers", response_model=CustomerListResponse)
def list_customers(
    is_active: bool | None = None,
    search: str | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(permission_required(Permission.MANAGE_CUSTOMERS)),
    db: Session = Depends(get_db),
) -> CustomerListResponse:
    query = db.query(User).filter(User.role == UserRole.CUSTOMER)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)
    if search is not None:
        trimmed = search.strip()
        if trimmed:
            if len(trimmed) > 100:
                trimmed = trimmed[:100]
            escaped = trimmed.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            from sqlalchemy import or_

            query = query.filter(
                or_(
                    User.name.ilike(pattern, escape="\\"),
                    User.email.ilike(pattern, escape="\\"),
                )
            )

    rows = query.order_by(User.created_at.desc(), User.id.desc()).offset(skip).limit(limit + 1).all()
    return CustomerListResponse(
        items=[
            CustomerSummary(
                id=customer.id,
                name=customer.name,
                email=customer.email,
                role=UserRole.CUSTOMER,
                is_active=customer.is_active,
            )
            for customer in rows[:limit]
        ],
        has_more=len(rows) > limit,
    )


@app.get(
    "/admin/customers/{customer_id}/orders",
    response_model=CustomerOrderHistoryResponse,
)
def list_customer_orders(
    customer_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(permission_required(Permission.MANAGE_CUSTOMERS)),
    db: Session = Depends(get_db),
) -> CustomerOrderHistoryResponse:
    customer_exists = (
        db.query(User.id)
        .filter(User.id == customer_id, User.role == UserRole.CUSTOMER)
        .first()
    )
    if customer_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    rows = (
        db.query(Order.id, Order.total_amount, Order.status, Order.created_at)
        .filter(Order.user_id == customer_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .offset(skip)
        .limit(limit + 1)
        .all()
    )
    return CustomerOrderHistoryResponse(
        items=[
            CustomerOrderSummary(
                id=order_id,
                total_amount=float(total_amount),
                status=status_value,
                created_at=created_at,
            )
            for order_id, total_amount, status_value, created_at in rows[:limit]
        ],
        has_more=len(rows) > limit,
    )


@app.patch("/admin/users/{user_id}/permissions", response_model=UserRead)
def update_user_permissions(
    user_id: int,
    permissions: list[Permission],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    target_user = db.query(User).filter(User.id == user_id).first()
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if target_user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrators cannot modify their own permissions",
        )

    if target_user.role != UserRole.STAFF:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Permissions can only be updated for staff users",
        )

    target_user.permissions = [permission.value for permission in permissions]
    db.add(target_user)
    db.commit()
    db.refresh(target_user)
    return target_user


@app.patch("/admin/users/{user_id}/role", response_model=UserRead)
def update_user_role(
    user_id: int,
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    target_user = db.query(User).filter(User.id == user_id).first()
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if target_user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrators cannot modify their own role",
        )

    if payload.role is not None:
        if payload.role == UserRole.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot promote a user to administrator. Only one administrator is allowed.",
            )
        target_user.role = payload.role
        if payload.role != UserRole.STAFF:
            target_user.permissions = []

    if payload.is_active is not None:
        target_user.is_active = payload.is_active

    db.add(target_user)
    db.commit()
    db.refresh(target_user)
    return target_user


@app.get("/products", response_model=list[ProductListRead])
def list_products(
    skip: int = 0,
    limit: int = 100,
    include_inactive: bool = False,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    from sqlalchemy import or_

    query = db.query(Product)
    if not include_inactive:
        query = query.filter(Product.is_active == True)
    if search is not None:
        trimmed = search.strip()
        if trimmed:
            # Limit length to prevent abuse; truncate safely
            if len(trimmed) > 100:
                trimmed = trimmed[:100]
            escaped = trimmed.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            query = query.filter(
                or_(
                    Product.name.ilike(pattern, escape="\\"),
                    Product.description.ilike(pattern, escape="\\"),
                )
            )
    return query.order_by(Product.created_at.desc()).offset(skip).limit(limit).all()


@app.get("/products/{product_id}", response_model=ProductRead)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
) -> Product:
    product = db.query(Product).filter(Product.id == product_id, Product.is_active == True).first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


@app.post("/products", response_model=ProductRead)
def create_product(
    payload: ProductCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Product:
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_PRODUCTS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Product management permission required",
        )

    category = db.query(Category).filter(Category.id == payload.category_id).first()
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    product = Product(
        name=payload.name,
        description=payload.description,
        price=payload.price,
        category_id=payload.category_id,
        stock_quantity=payload.stock_quantity,
        image_url=payload.image_url,
        is_active=True,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.put("/products/{product_id}", response_model=ProductRead)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Product:
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_PRODUCTS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Product management permission required",
        )

    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    if payload.category_id is not None:
        category = db.query(Category).filter(Category.id == payload.category_id).first()
        if category is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
        product.category_id = payload.category_id

    if payload.name is not None:
        product.name = payload.name
    if payload.description is not None:
        product.description = payload.description
    if payload.price is not None:
        product.price = payload.price
    if payload.image_url is not None:
        product.image_url = payload.image_url

    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.patch("/products/{product_id}/stock", response_model=ProductRead)
def update_product_stock(
    product_id: int,
    payload: StockUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Product:
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_INVENTORY)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inventory management permission required",
        )

    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    product.stock_quantity = payload.quantity
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.patch("/products/{product_id}/activate", response_model=ProductRead)
def activate_product(
    product_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Product:
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_PRODUCTS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Product management permission required",
        )

    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    product.is_active = True
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.patch("/products/{product_id}/deactivate", response_model=ProductRead)
def deactivate_product(
    product_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Product:
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_PRODUCTS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Product management permission required",
        )

    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    product.is_active = False
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.post("/products/{product_id}/upload-image", response_model=ProductImageUploadResponse)
def upload_product_image(
    product_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProductImageUploadResponse:
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_PRODUCTS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Product management permission required",
        )

    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    max_size = int(settings.PRODUCT_IMAGE_MAX_SIZE)
    chunks = []
    total_size = 0
    chunk_size = 8192
    while True:
        chunk = file.file.read(chunk_size)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_size:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File too large: {total_size / 1024 / 1024:.2f} MB. Maximum: {max_size / 1024 / 1024:.0f} MB",
            )
        chunks.append(chunk)
    file_content = b"".join(chunks)

    success, error_message, extension = _validate_and_prepare_upload(file_content, file.filename or "")
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_message)

    success, secure_url, public_id, size_bytes = upload_to_cloudinary(file_content, product_id, extension)
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=secure_url)

    previous_image_url = product.image_url
    product.image_url = secure_url
    db.add(product)
    db.commit()
    db.refresh(product)

    # Delete old managed Cloudinary asset if it exists
    if previous_image_url and is_our_cloudinary_url(previous_image_url):
        old_public_id = extract_our_public_id(previous_image_url)
        if old_public_id:
            destroy_cloudinary_image(old_public_id)

    return ProductImageUploadResponse(
        image_url=secure_url,
        filename=public_id,
        size_bytes=size_bytes or total_size,
    )


@app.delete("/products/{product_id}/image", response_model=ProductImageDeleteResponse)
def delete_product_image(
    product_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProductImageDeleteResponse:
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_PRODUCTS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Product management permission required",
        )

    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    previous_image_url = product.image_url
    deleted = False
    if previous_image_url and is_our_cloudinary_url(previous_image_url):
        public_id = extract_our_public_id(previous_image_url)
        if public_id:
            deleted = destroy_cloudinary_image(public_id)

    product.image_url = None
    db.add(product)
    db.commit()
    db.refresh(product)

    return ProductImageDeleteResponse(
        deleted=deleted,
        image_url_before=previous_image_url,
    )


@app.get("/categories", response_model=list[CategoryRead])
def list_categories(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    return (
        db.query(Category)
        .order_by(Category.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@app.get("/categories/{category_id}", response_model=CategoryRead)
def get_category(
    category_id: int,
    db: Session = Depends(get_db),
) -> Category:
    category = db.query(Category).filter(Category.id == category_id).first()
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return category


@app.post("/categories", response_model=CategoryRead)
def create_category(
    payload: CategoryCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Category:
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    existing = db.query(Category).filter(Category.name == payload.name).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category name already exists",
        )

    category = Category(
        name=payload.name,
        description=payload.description,
        is_active=True,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


HOMEPAGE_CATEGORY_ICON_KEYS = frozenset({
    "backpack",
    "blocks",
    "gift",
    "shirt",
    "shopping-bag",
})
DEFAULT_HOMEPAGE_CATEGORY_ICON_KEY = "backpack"


def _safe_homepage_category_icon_key(icon_key: str | None) -> str:
    if icon_key in HOMEPAGE_CATEGORY_ICON_KEYS:
        return icon_key
    return DEFAULT_HOMEPAGE_CATEGORY_ICON_KEY


def _homepage_category_admin_entries(db: Session) -> list[HomepageCategoryAdminEntry]:
    rows = (
        db.query(HomepageCategoryConfiguration, Category)
        .join(Category, Category.id == HomepageCategoryConfiguration.category_id)
        .order_by(HomepageCategoryConfiguration.display_order.asc())
        .all()
    )
    return [
        HomepageCategoryAdminEntry(
            id=configuration.id,
            category_id=configuration.category_id,
            category_name=category.name,
            category_is_active=category.is_active,
            label=configuration.label,
            icon_key=_safe_homepage_category_icon_key(configuration.icon_key),
            display_order=configuration.display_order,
        )
        for configuration, category in rows
    ]


@app.get(
    "/homepage-categories",
    response_model=list[HomepageCategoryPublicEntry],
)
def list_homepage_categories(db: Session = Depends(get_db)) -> list[HomepageCategoryPublicEntry]:
    rows = (
        db.query(HomepageCategoryConfiguration, Category)
        .join(Category, Category.id == HomepageCategoryConfiguration.category_id)
        .filter(Category.is_active == True)
        .order_by(HomepageCategoryConfiguration.display_order.asc())
        .all()
    )
    return [
        HomepageCategoryPublicEntry(
            category_id=configuration.category_id,
            category_name=category.name,
            label=configuration.label,
            icon_key=_safe_homepage_category_icon_key(configuration.icon_key),
            display_order=configuration.display_order,
        )
        for configuration, category in rows
    ]


@app.get(
    "/admin/homepage-categories",
    response_model=list[HomepageCategoryAdminEntry],
)
def get_admin_homepage_categories(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[HomepageCategoryAdminEntry]:
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )
    return _homepage_category_admin_entries(db)


@app.put(
    "/admin/homepage-categories",
    response_model=list[HomepageCategoryAdminEntry],
)
def replace_admin_homepage_categories(
    payload: HomepageCategorySelectionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[HomepageCategoryAdminEntry]:
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    category_ids = [item.category_id for item in payload.items]
    if len(category_ids) != len(set(category_ids)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Category IDs must be unique")

    display_orders = [item.display_order for item in payload.items]
    if len(display_orders) != len(set(display_orders)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Display orders must be unique")

    for item in payload.items:
        if not item.label.strip():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Labels must not be empty")
        if item.icon_key not in HOMEPAGE_CATEGORY_ICON_KEYS:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid homepage category icon key")

    if category_ids:
        eligible_ids = {
            category_id
            for (category_id,) in db.query(Category.id)
            .filter(Category.id.in_(category_ids), Category.is_active == True)
            .all()
        }
        if eligible_ids != set(category_ids):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Every selected category must exist and be active",
            )

    try:
        db.query(HomepageCategoryConfiguration).delete(synchronize_session=False)
        db.add_all([
            HomepageCategoryConfiguration(
                category_id=item.category_id,
                label=item.label,
                icon_key=item.icon_key,
                display_order=item.display_order,
            )
            for item in payload.items
        ])
        db.commit()
    except Exception:
        db.rollback()
        raise

    return _homepage_category_admin_entries(db)


def _validate_checkout_items(
    db: Session,
    items: list,
    fulfillment_method: str,
) -> tuple[bool, list[str] | None, dict | None]:
    """
    Validate checkout items and return (valid, errors, cart_data).
    Errors list is None if valid is True.
    """
    if not items:
        return False, ["Cart is empty"], None

    errors = []
    cart_items: list[CartItemDetails] = []
    subtotal = Decimal("0.00")
    product_map: dict[int, Product] = {}

    try:
        fulfillment_enum = FulfillmentMethod(fulfillment_method)
    except ValueError:
        return False, [f"Invalid fulfillment method: {fulfillment_method}"], None

    for item in items:
        product_id = item.product_id
        quantity = item.quantity

        if quantity <= 0:
            errors.append(f"Product {product_id}: Quantity must be greater than 0")
            continue

        product = db.query(Product).filter(Product.id == product_id).first()
        if product is None:
            errors.append(f"Product {product_id}: Product not found")
            continue

        if not product.is_active:
            errors.append(f"Product {product.name}: Product is not available")
            continue

        if quantity > product.stock_quantity:
            errors.append(
                f"Product {product.name}: Insufficient stock (requested: {quantity}, available: {product.stock_quantity})"
            )
            continue

        product_map[product_id] = product
        unit_price = Decimal(str(product.price))
        line_total = unit_price * Decimal(quantity)
        subtotal += line_total

        cart_items.append(
            CartItemDetails(
                product_id=product.id,
                quantity=quantity,
                product_name=product.name,
                unit_price=unit_price,
                subtotal=line_total,
            )
        )

    if errors:
        return False, errors, None

    # Read delivery fee from server configuration, with business rule:
    # - STORE_PICKUP always has fee = 0.00
    # - STORE_DELIVERY and CUSTOMER_DISPATCH use configured fee
    config = db.query(DeliveryConfig).first()
    if config is None:
        config = DeliveryConfig(fee_amount=0.00, is_active=True)
        db.add(config)
        db.commit()
        db.refresh(config)

    if fulfillment_enum == FulfillmentMethod.STORE_PICKUP:
        delivery_fee = Decimal("0.00")
    else:
        delivery_fee = config.fee_amount
    total_amount = subtotal + delivery_fee

    summary = CheckoutSummary(
        items=cart_items,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        total_amount=total_amount,
        fulfillment_method=fulfillment_method,
        delivery_address=None,
    )

    return True, None, {"items": cart_items, "summary": summary}


def _resolve_address_for_order(db: Session, current_user: User, address_id: int | None, fulfillment_method: FulfillmentMethod):
    if fulfillment_method == FulfillmentMethod.STORE_PICKUP:
        return None

    if address_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Address is required for this fulfillment method")

    address = (
        db.query(Address)
        .filter(Address.id == address_id, Address.user_id == current_user.id)
        .first()
    )
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")
    return address


def _validate_order_status_transition(current_status: OrderStatus, new_status: OrderStatus, fulfillment_method: FulfillmentMethod) -> None:
    allowed: dict[OrderStatus, set[OrderStatus]] = {
        OrderStatus.PENDING: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
        OrderStatus.CONFIRMED: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
        OrderStatus.PROCESSING: {OrderStatus.CANCELLED},
        OrderStatus.READY_FOR_PICKUP: {OrderStatus.COMPLETED, OrderStatus.CANCELLED},
        OrderStatus.OUT_FOR_DELIVERY: {OrderStatus.COMPLETED, OrderStatus.CANCELLED},
        OrderStatus.COMPLETED: set(),
        OrderStatus.CANCELLED: set(),
    }

    if fulfillment_method == FulfillmentMethod.STORE_PICKUP:
        allowed[OrderStatus.PROCESSING] = {OrderStatus.READY_FOR_PICKUP, OrderStatus.CANCELLED}
        allowed[OrderStatus.READY_FOR_PICKUP] = {OrderStatus.COMPLETED, OrderStatus.CANCELLED}
        allowed[OrderStatus.OUT_FOR_DELIVERY] = set()
    elif fulfillment_method == FulfillmentMethod.STORE_DELIVERY:
        allowed[OrderStatus.PROCESSING] = {OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED}
        allowed[OrderStatus.READY_FOR_PICKUP] = set()
        allowed[OrderStatus.OUT_FOR_DELIVERY] = {OrderStatus.COMPLETED, OrderStatus.CANCELLED}
    elif fulfillment_method == FulfillmentMethod.CUSTOMER_DISPATCH:
        allowed[OrderStatus.PROCESSING] = {OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED}
        allowed[OrderStatus.OUT_FOR_DELIVERY] = {OrderStatus.COMPLETED, OrderStatus.CANCELLED}
        allowed[OrderStatus.READY_FOR_PICKUP] = set()

    if new_status not in allowed.get(current_status, set()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition from {current_status.value} to {new_status.value} for {fulfillment_method.value}",
        )


def _restore_stock_for_cancelled_order(db: Session, order: Order) -> None:
    """Restore product stock for a cancelled order atomically.

    Must be called within the same transaction that sets order.status to CANCELLED
    and with the order row already locked via with_for_update.
    """
    if not order.order_items:
        return
    product_ids = [item.product_id for item in order.order_items]
    products = (
        db.query(Product)
        .filter(Product.id.in_(product_ids))
        .with_for_update()
        .all()
    )
    product_map = {p.id: p for p in products}
    for item in order.order_items:
        product = product_map.get(item.product_id)
        if product is not None:
            product.stock_quantity = product.stock_quantity + item.quantity
            db.add(product)


def _serialize_order_for_response(order: Order) -> Order:
    if not hasattr(order, "subtotal"):
        order.subtotal = sum(
            (Decimal(str(item.subtotal)) for item in order.order_items),
            Decimal("0.00"),
        )
    else:
        order.subtotal = sum(
            (Decimal(str(item.subtotal)) for item in order.order_items),
            Decimal("0.00"),
        )
    return order


@app.post("/orders", response_model=OrderRead)
def create_order(
    payload: OrderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Order:
    if not payload.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order must contain at least one item")

    fulfillment_method = payload.fulfillment_method
    if fulfillment_method not in {FulfillmentMethod.STORE_DELIVERY, FulfillmentMethod.CUSTOMER_DISPATCH, FulfillmentMethod.STORE_PICKUP}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid fulfillment method")

    address = _resolve_address_for_order(db, current_user, payload.address_id, fulfillment_method)

    product_quantities: dict[int, int] = {}
    for item in payload.items:
        if item.quantity <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Quantity must be greater than zero")
        product_quantities[item.product_id] = product_quantities.get(item.product_id, 0) + item.quantity

    products = (
        db.query(Product)
        .filter(Product.id.in_(product_quantities.keys()))
        .with_for_update()
        .all()
    )
    product_map = {product.id: product for product in products}

    missing_product_ids = set(product_quantities.keys()) - set(product_map.keys())
    if missing_product_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more products were not found")

    for product_id, quantity in product_quantities.items():
        product = product_map[product_id]
        if not product.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product {product.name} is not available")
        if quantity > product.stock_quantity:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Insufficient stock for product {product.name}")

    subtotal = Decimal("0.00")
    order_items: list[OrderItem] = []
    for product_id, quantity in product_quantities.items():
        product = product_map[product_id]
        unit_price = Decimal(str(product.price))
        line_total = unit_price * Decimal(quantity)
        subtotal += line_total
        order_items.append(
            OrderItem(
                product_id=product.id,
                quantity=quantity,
                unit_price=unit_price,
                subtotal=line_total,
            )
        )

    # Read delivery fee from server configuration, with business rule:
    # - STORE_PICKUP always has fee = 0.00
    # - STORE_DELIVERY and CUSTOMER_DISPATCH use configured fee
    config = db.query(DeliveryConfig).first()
    if config is None:
        config = DeliveryConfig(fee_amount=0.00, is_active=True)
        db.add(config)
        db.commit()
        db.refresh(config)

    if fulfillment_method == FulfillmentMethod.STORE_PICKUP:
        delivery_fee = Decimal("0.00")
    else:
        delivery_fee = config.fee_amount
    total_amount = subtotal + delivery_fee

    order = Order(
        user_id=current_user.id,
        address_id=address.id if address else None,
        total_amount=total_amount,
        status=OrderStatus.PENDING,
        fulfillment_method=fulfillment_method,
        delivery_fee=delivery_fee,
        delivery_recipient_name=(
            address.recipient_name if address else payload.delivery_recipient_name
        ),
        delivery_phone_number=(
            address.phone_number if address else payload.delivery_phone_number
        ),
        delivery_address_line=(
            address.address_line if address else payload.delivery_address_line
        ),
        delivery_city=(address.city if address else payload.delivery_city),
        delivery_state=(address.state if address else payload.delivery_state),
        delivery_additional_directions=(
            address.additional_directions if address else payload.delivery_additional_directions
        ),
    )
    db.add(order)
    db.flush()

    for item in order_items:
        item.order_id = order.id
        db.add(item)

    for product_id, quantity in product_quantities.items():
        product = product_map[product_id]
        product.stock_quantity -= quantity
        db.add(product)

    db.commit()
    db.refresh(order)
    order.order_items = list(order.order_items or [])
    for item in order.order_items:
        item.subtotal = Decimal(str(item.subtotal))
    order.subtotal = subtotal
    return _serialize_order_for_response(order)


@app.get("/orders", response_model=list[OrderRead])
def list_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if require_admin(current_user) or require_permission(current_user, Permission.MANAGE_ORDERS):
        return db.query(Order).order_by(Order.created_at.desc()).all()
    return db.query(Order).filter(Order.user_id == current_user.id).order_by(Order.created_at.desc()).all()


@app.get("/orders/{order_id}", response_model=OrderRead)
def get_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Order:
    if require_admin(current_user) or require_permission(current_user, Permission.MANAGE_ORDERS):
        order = db.query(Order).filter(Order.id == order_id).first()
    else:
        order = db.query(Order).filter(Order.id == order_id, Order.user_id == current_user.id).first()

    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    order.subtotal = sum((Decimal(str(item.subtotal)) for item in order.order_items), Decimal("0.00"))
    return order


@app.patch("/orders/{order_id}/status", response_model=OrderRead)
def update_order_status(
    order_id: int,
    payload: OrderStatusUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Order:
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_ORDERS)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Order management permission required",
        )

    order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    # Idempotent: already in target status → no stock change, return as-is
    if order.status == payload.status:
        order.subtotal = sum((Decimal(str(item.subtotal)) for item in order.order_items), Decimal("0.00"))
        return order

    try:
        _validate_order_status_transition(order.status, payload.status, order.fulfillment_method)
    except HTTPException:
        raise

    is_cancelling = payload.status == OrderStatus.CANCELLED and order.status != OrderStatus.CANCELLED
    if is_cancelling:
        _restore_stock_for_cancelled_order(db, order)

    order.status = payload.status
    db.add(order)
    db.commit()
    db.refresh(order)
    order.subtotal = sum((Decimal(str(item.subtotal)) for item in order.order_items), Decimal("0.00"))
    return order


@app.post("/orders/{order_id}/cancel", response_model=OrderRead)
def cancel_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Order:
    order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    # Ownership check: customer may cancel own order, admin/staff with permission may cancel any
    if order.user_id != current_user.id:
        if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_ORDERS)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to cancel this order",
            )

    # Idempotent: already cancelled → return without double-restoring stock
    if order.status == OrderStatus.CANCELLED:
        order.subtotal = sum((Decimal(str(item.subtotal)) for item in order.order_items), Decimal("0.00"))
        return order

    try:
        _validate_order_status_transition(order.status, OrderStatus.CANCELLED, order.fulfillment_method)
    except HTTPException:
        raise

    _restore_stock_for_cancelled_order(db, order)
    order.status = OrderStatus.CANCELLED
    db.add(order)
    db.commit()
    db.refresh(order)
    order.subtotal = sum((Decimal(str(item.subtotal)) for item in order.order_items), Decimal("0.00"))
    return order


@app.post("/checkout/validate", response_model=CheckoutValidationResponse)
def validate_checkout(
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutValidationResponse:
    """Validate checkout items without creating an order."""
    valid, errors, data = _validate_checkout_items(db, payload.items, payload.fulfillment_method)

    if not valid:
        return CheckoutValidationResponse(
            valid=False,
            message="Checkout validation failed",
            errors=errors,
        )

    try:
        fulfillment_enum = FulfillmentMethod(payload.fulfillment_method)
    except ValueError:
        return CheckoutValidationResponse(
            valid=False,
            message="Invalid fulfillment method",
            errors=[f"Unknown fulfillment method: {payload.fulfillment_method}"],
        )

    try:
        address = _resolve_address_for_order(db, current_user, payload.address_id, fulfillment_enum)
    except HTTPException as e:
        return CheckoutValidationResponse(
            valid=False,
            message="Checkout validation failed",
            errors=[e.detail],
        )

    delivery_address_data = None
    if address:
        delivery_address_data = {
            "id": address.id,
            "recipient_name": address.recipient_name,
            "phone_number": address.phone_number,
            "address_line": address.address_line,
            "city": address.city,
            "state": address.state,
        }

    summary = data["summary"]
    summary.delivery_address = delivery_address_data

    return CheckoutValidationResponse(
        valid=True,
        message="Checkout is valid",
        summary=summary,
    )


@app.post("/checkout", response_model=OrderRead)
def checkout(
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Order:
    """Create order from checkout request (combines validation and order creation)."""
    valid, errors, data = _validate_checkout_items(db, payload.items, payload.fulfillment_method)

    if not valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="; ".join(errors))

    try:
        fulfillment_enum = FulfillmentMethod(payload.fulfillment_method)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid fulfillment method: {payload.fulfillment_method}",
        )

    address = _resolve_address_for_order(db, current_user, payload.address_id, fulfillment_enum)

    cart_items = data["items"]
    summary = data["summary"]

    product_quantities: dict[int, int] = {item.product_id: item.quantity for item in cart_items}

    products = (
        db.query(Product)
        .filter(Product.id.in_(product_quantities.keys()))
        .with_for_update()
        .all()
    )
    product_map = {product.id: product for product in products}

    missing_product_ids = set(product_quantities.keys()) - set(product_map.keys())
    if missing_product_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more products were not found")

    for product_id, quantity in product_quantities.items():
        product = product_map[product_id]
        if not product.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product {product.name} is not available")
        if quantity > product.stock_quantity:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Insufficient stock for product {product.name}")

    order = Order(
        user_id=current_user.id,
        address_id=address.id if address else None,
        total_amount=summary.total_amount,
        status=OrderStatus.PENDING,
        fulfillment_method=fulfillment_enum,
        delivery_fee=summary.delivery_fee,
        delivery_recipient_name=(
            address.recipient_name if address else payload.delivery_recipient_name
        ),
        delivery_phone_number=(
            address.phone_number if address else payload.delivery_phone_number
        ),
        delivery_address_line=(
            address.address_line if address else payload.delivery_address_line
        ),
        delivery_city=(address.city if address else payload.delivery_city),
        delivery_state=(address.state if address else payload.delivery_state),
        delivery_additional_directions=(
            address.additional_directions if address else payload.delivery_additional_directions
        ),
    )
    db.add(order)
    db.flush()

    for item in cart_items:
        order_item = OrderItem(
            order_id=order.id,
            product_id=item.product_id,
            quantity=item.quantity,
            unit_price=item.unit_price,
            subtotal=item.subtotal,
        )
        db.add(order_item)

    for product_id, quantity in product_quantities.items():
        product = product_map[product_id]
        product.stock_quantity -= quantity
        db.add(product)

    db.commit()
    db.refresh(order)
    order.order_items = list(order.order_items or [])
    for item in order.order_items:
        item.subtotal = Decimal(str(item.subtotal))
    order.subtotal = summary.subtotal
    return _serialize_order_for_response(order)


@app.post("/addresses", response_model=AddressRead)
def create_address(
    payload: AddressCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Address:
    existing_address_count = db.query(Address).filter(Address.user_id == current_user.id).count()
    address = Address(
        user_id=current_user.id,
        recipient_name=payload.recipient_name,
        phone_number=payload.phone_number,
        address_line=payload.address_line,
        city=payload.city,
        state=payload.state,
        additional_directions=payload.additional_directions,
        is_default=False,
    )

    db.add(address)
    db.flush()

    if payload.is_default or existing_address_count == 0:
        return _set_default_address(db, current_user, address)

    db.commit()
    db.refresh(address)
    return address


# ---- Payment Routes ----

@app.post("/payments", response_model=PaymentRead)
def create_payment(
    payload: PaymentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaymentRead:
    """Create a payment for an order."""
    # Validate payment method is supported
    # PAYSTACK and CARD both map to the Paystack integration internally
    # STRIPE is an existing payment method used by tests/integration
    # BANK_TRANSFER is a new recognized payment method
    # CASH is permitted only for STORE_PICKUP (enforced below)
    allowed_methods = {"PAYSTACK", "CARD", "STRIPE", "BANK_TRANSFER", "CASH"}
    if payload.payment_method not in allowed_methods:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported payment method: {payload.payment_method}. Allowed methods: {sorted(allowed_methods)}",
        )

    # Validate order exists
    order = db.query(Order).filter(Order.id == payload.order_id).first()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )

    # Check ownership: user can pay for their own orders, admin can pay for any
    if order.user_id != current_user.id:
        if not require_admin(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to pay for this order",
            )

    # Enforce fulfillment/payment compatibility using server-side order data.
    # The database order is authoritative — do not trust client-supplied fulfillment.
    fulfillment = order.fulfillment_method

    # CASH is only permitted for STORE_PICKUP
    if payload.payment_method == "CASH":
        if fulfillment in {FulfillmentMethod.STORE_DELIVERY, FulfillmentMethod.CUSTOMER_DISPATCH}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"CASH payment is not allowed for {fulfillment.value}. CASH is only permitted for STORE_PICKUP.",
            )

    # Prevent duplicate payment
    existing_payment = db.query(Payment).filter(Payment.order_id == payload.order_id).first()
    if existing_payment is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A payment already exists for this order",
        )

    payment = Payment(
        order_id=payload.order_id,
        amount=order.total_amount,
        payment_method=payload.payment_method,
        transaction_reference=payload.transaction_reference,
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


@app.get("/payments", response_model=list[PaymentRead])
def list_payments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Payment]:
    """List payments. Admin sees all; users see only their order payments."""
    if require_admin(current_user):
        return db.query(Payment).all()

    # Users can only see payments for their own orders
    user_orders = db.query(Order).filter(Order.user_id == current_user.id).all()
    order_ids = [o.id for o in user_orders]
    return db.query(Payment).filter(Payment.order_id.in_(order_ids)).all()


@app.get("/payments/{payment_id}", response_model=PaymentRead)
def get_payment(
    payment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Payment:
    """Retrieve a specific payment."""
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    # Check ownership: user can access payment for their order, admin can access any
    if payment.order.user_id != current_user.id:
        if not require_admin(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this payment",
            )

    return payment


@app.get("/orders/{order_id}/payments", response_model=PaymentRead | None)
def get_payment_for_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Payment | None:
    """Retrieve payment for a specific order."""
    # Validate order exists and user owns it (or admin)
    order = db.query(Order).filter(Order.id == order_id).first()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )

    if order.user_id != current_user.id:
        if not require_admin(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access payments for this order",
            )

    payment = db.query(Payment).filter(Payment.order_id == order_id).first()
    return payment


@app.patch("/payments/{payment_id}/status", response_model=PaymentRead)
def update_payment_status(
    payment_id: int,
    payload: PaymentStatusUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Payment:
    """Update payment status (admin only)."""
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    # Validate status transition
    valid_transitions: dict[str, set[str]] = {
        "PENDING": {"SUCCESS", "FAILED"},
        "SUCCESS": {"REFUNDED"},
        "FAILED": {"PENDING", "REFUNDED"},
        "REFUNDED": set(),  # terminal
    }

    current = payment.status
    new_status = payload.status

    if new_status not in valid_transitions.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition from {current} to {new_status}",
        )

    payment.status = new_status
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


# ---- Bank Transfer Receipt Routes ----

# Maximum file size for receipt uploads (5 MiB)
MAX_RECEIPT_SIZE = 5 * 1024 * 1024
# Allowed MIME types and extensions for receipts
ALLOWED_RECEIPT_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
ALLOWED_RECEIPT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}


def _get_extension(filename: str) -> str:
    """Get the lowercase file extension from a filename."""
    from pathlib import Path
    return Path(filename).suffix.lower()


def _is_allowed_extension(ext: str) -> bool:
    """Check if the extension is in the allowed list."""
    return ext in ALLOWED_RECEIPT_EXTENSIONS


def _is_allowed_mime_type(mime_type: str) -> bool:
    """Check if the MIME type is in the allowed list."""
    return mime_type in ALLOWED_RECEIPT_MIME_TYPES


# MIME type signatures for file signature validation
MIME_SIGNATURES: dict[str, list[bytes]] = {
    "application/pdf": [b"%PDF"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/webp": [b"RIFF", b"WEBP"],
}


def _validate_file_signature(file_content: bytes, mime_type: str) -> bool:
    """
    Validate file signature (magic bytes) matches the expected MIME type.
    """
    signatures = MIME_SIGNATURES.get(mime_type, [])
    return any(file_content.startswith(sig) for sig in signatures)


@app.post("/payments/{payment_id}/receipt", response_model=ReceiptUploadResponse)
async def upload_payment_receipt(
    payment_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a receipt for a bank-transfer payment.

    Only the payment owner can upload a receipt.
    Only BANK_TRANSFER payments in PENDING state without an existing receipt are accepted.
    """
    # Get payment with row lock to prevent concurrent uploads
    payment = db.query(Payment).filter(Payment.id == payment_id).with_for_update().first()
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    # Check ownership
    if payment.order.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to upload receipt for this payment",
        )

    # Only BANK_TRANSFER payments can have receipts uploaded
    if payment.payment_method != "BANK_TRANSFER":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Receipts can only be uploaded for BANK_TRANSFER payments",
        )

    # Payment must be in a reviewable state (PENDING)
    if payment.status != PaymentStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot upload receipt for payment with status {payment.status.value}",
        )

    # Prevent duplicate uploads
    if payment.receipt_data is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Receipt already uploaded for this payment",
        )

    # Read file content
    file_content = await file.read()
    if not file_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file uploaded",
        )

    # Enforce file size limit
    if len(file_content) > MAX_RECEIPT_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size: 5 MiB",
        )

    # Validate file
    client_mime_type = file.content_type
    is_valid, error_msg, detected_mime_type = validate_receipt_file(
        file_content, file.filename or "", client_mime_type
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # Use detected MIME type if client didn't provide one
    if not client_mime_type:
        client_mime_type = detected_mime_type

    # Store receipt data
    payment.receipt_data = file_content
    payment.receipt_filename = file.filename or "receipt"
    payment.receipt_mime_type = client_mime_type
    payment.receipt_size = len(file_content)
    payment.receipt_uploaded_at = utcnow()
    db.add(payment)
    db.commit()
    db.refresh(payment)

    return ReceiptUploadResponse(
        receipt_filename=payment.receipt_filename or "",
        receipt_mime_type=payment.receipt_mime_type or "",
        receipt_size=payment.receipt_size or 0,
        receipt_uploaded_at=payment.receipt_uploaded_at or utcnow(),
    )


@app.get("/payments/{payment_id}/receipt")
async def get_payment_receipt(
    payment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve a payment receipt.

    Payment owner and ADMIN users can access the receipt.
    """
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    # Check ownership
    if payment.order.user_id != current_user.id:
        if not require_admin(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this receipt",
            )

    if payment.receipt_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No receipt uploaded for this payment",
        )

    # Stream the receipt content
    from fastapi.responses import StreamingResponse
    from io import BytesIO

    media_type = payment.receipt_mime_type or "application/octet-stream"
    filename = payment.receipt_filename or "receipt"

    return StreamingResponse(
        BytesIO(payment.receipt_data),
        media_type=payment.receipt_mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payment.receipt_data)),
        },
    )


@app.post("/payments/{payment_id}/receipt/approve", response_model=ReceiptReviewResponse)
def approve_payment_receipt(
    payment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve a bank-transfer payment receipt (ADMIN only)."""
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    # Lock payment and order rows to prevent concurrent modifications
    payment = db.query(Payment).filter(Payment.id == payment_id).with_for_update().first()
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    # Only BANK_TRANSFER payments with receipts can be approved
    if payment.payment_method != "BANK_TRANSFER":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only BANK_TRANSFER payments can be approved via receipt review",
        )

    if payment.receipt_data is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No receipt uploaded for this payment",
        )

    # Payment must be in a reviewable state
    if payment.status != PaymentStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve payment with status {payment.status.value}",
        )

    # Update payment status to SUCCESS
    payment.status = PaymentStatus.SUCCESS
    payment.paid_at = utcnow()
    payment.receipt_reviewed_at = utcnow()
    payment.receipt_reviewed_by = current_user.id
    payment.payment_metadata = {
        **(payment.payment_metadata or {}),
        "receipt_approval": {
            "reviewed_by": current_user.id,
            "reviewed_at": utcnow().isoformat(),
            "action": "APPROVED",
        },
    }

    # Lock and update order status if still PENDING
    order = db.query(Order).filter(Order.id == payment.order_id).with_for_update().first()
    if order and order.status == OrderStatus.PENDING:
        order.status = OrderStatus.CONFIRMED
        db.add(order)

    db.add(payment)
    db.commit()
    db.refresh(payment)

    return ReceiptReviewResponse(
        payment_id=payment.id,
        status="APPROVED",
        reviewed_at=payment.receipt_reviewed_at or utcnow(),
        reviewed_by=payment.receipt_reviewed_by or 0,
        rejection_reason=None,
    )


@app.post("/payments/{payment_id}/receipt/reject", response_model=ReceiptReviewResponse)
def reject_payment_receipt(
    payment_id: int,
    payload: ReceiptReviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reject a bank-transfer payment receipt (ADMIN only)."""
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    if not payload.rejection_reason or not payload.rejection_reason.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rejection reason is required",
        )

    if len(payload.rejection_reason) > 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rejection reason must not exceed 1000 characters",
        )

    # Lock payment and order rows
    payment = db.query(Payment).filter(Payment.id == payment_id).with_for_update().first()
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    if payment.payment_method != "BANK_TRANSFER":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only BANK_TRANSFER payments can be rejected via receipt review",
        )

    if payment.receipt_data is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No receipt uploaded for this payment",
        )

    if payment.status != PaymentStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot reject payment with status {payment.status.value}",
        )

    # Update payment status to FAILED
    payment.status = PaymentStatus.FAILED
    payment.receipt_reviewed_at = utcnow()
    payment.receipt_reviewed_by = current_user.id
    payment.receipt_rejection_reason = payload.rejection_reason.strip()
    payment.payment_metadata = {
        **(payment.payment_metadata or {}),
        "receipt_rejection": {
            "reviewed_by": current_user.id,
            "reviewed_at": utcnow().isoformat(),
            "action": "REJECTED",
            "reason": payload.rejection_reason.strip(),
        },
    }

    # Keep order in PENDING state (stock already held from order creation)
    # The order remains PENDING so customer can retry payment or admin can take action

    db.add(payment)
    db.commit()
    db.refresh(payment)

    return ReceiptReviewResponse(
        payment_id=payment.id,
        status="REJECTED",
        reviewed_at=payment.receipt_reviewed_at or utcnow(),
        reviewed_by=payment.receipt_reviewed_by or 0,
        rejection_reason=payment.receipt_rejection_reason,
    )


# ---- Paystack Payment Routes ----

class PaystackInitializeRequest(BaseModel):
    order_id: int
    callback_url: str | None = None


@app.post("/payments/paystack/initialize")
async def initialize_paystack_payment(
    payload: PaystackInitializeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Initialize a Paystack transaction for an order."""
    # Validate order exists. Lock the order row so concurrent initialize
    # requests for the same order are serialized (prevents duplicate payments).
    order = db.query(Order).filter(Order.id == payload.order_id).with_for_update().first()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )

    # Check ownership
    if order.user_id != current_user.id:
        if not require_admin(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to pay for this order",
            )

    # Check if order is payable (PENDING status)
    if order.status != OrderStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order is not in a payable state",
        )

    # Check if payment already exists and is successful
    existing_payment = db.query(Payment).filter(Payment.order_id == order.id).first()
    if existing_payment and existing_payment.status == PaymentStatus.SUCCESS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order has already been paid",
        )

    # If there's a failed payment, we can retry by creating a new payment record
    # or reuse the failed payment record
    if existing_payment and existing_payment.status == PaymentStatus.FAILED:
        # We'll reuse the existing failed payment record
        payment = existing_payment
    elif existing_payment and existing_payment.status == PaymentStatus.PENDING:
        # Reuse pending payment
        payment = existing_payment
    else:
        # Create new payment record
        payment = Payment(
            order_id=order.id,
            amount=order.total_amount,
            payment_method="CARD",  # Will be updated based on actual method used
            status=PaymentStatus.PENDING,
        )
        db.add(payment)
        db.flush()

    # Preserve existing provider_reference if present.
    # Only generate a new reference if the payment doesn't have one yet.
    # If a new reference must replace an existing one (e.g., Paystack rejects
    # the duplicate), store the old reference in payment_metadata for recovery.
    import uuid
    if payment.provider_reference:
        # Reuse the existing reference - Paystack may accept re-initialization
        # with the same reference if the original transaction is still pending.
        reference = payment.provider_reference
    else:
        reference = f"twelve09_{order.id}_{uuid.uuid4().hex[:12]}"
        payment.provider_reference = reference
    payment.provider = "paystack"
    payment.amount = order.total_amount
    payment.currency = "NGN"

    db.add(payment)
    db.commit()
    db.refresh(payment)

    # Initialize Paystack transaction
    try:
        callback_url = payload.callback_url
        if not callback_url:
            # Default callback to frontend confirmation page
            callback_url = f"{settings.CORS_ALLOWED_ORIGINS.split(',')[0]}/orders/{order.id}/confirmation"

        paystack_data = await paystack_client.initialize_transaction(
            email=current_user.email,
            amount=order.total_amount,
            reference=reference,
            callback_url=callback_url,
            metadata={
                "order_id": order.id,
                "payment_id": payment.id,
                "user_id": current_user.id,
            },
        )

        # Store Paystack response metadata (payment_metadata is the mapped column)
        payment.payment_metadata = {
            "paystack_authorization_url": paystack_data.get("authorization_url"),
            "paystack_access_code": paystack_data.get("access_code"),
            "paystack_reference": paystack_data.get("reference"),
        }
        db.add(payment)
        db.commit()
        db.refresh(payment)

        return {
            "authorization_url": paystack_data.get("authorization_url"),
            "access_code": paystack_data.get("access_code"),
            "reference": paystack_data.get("reference"),
        }

    except PaystackError as e:
        # Check if this is a duplicate reference error
        if getattr(e, "is_duplicate_reference", False):
            # Duplicate reference - rotate the reference and retry once
            old_reference = payment.provider_reference
            # Generate new reference
            import uuid
            new_reference = f"twelve09_{order.id}_{uuid.uuid4().hex[:12]}"
            
            # Update payment_metadata with previous_references
            existing_metadata = payment.payment_metadata or {}
            prev_refs = existing_metadata.get("previous_references", [])
            if old_reference not in prev_refs:
                prev_refs.append(old_reference)
            
            payment.payment_metadata = {
                **existing_metadata,
                "previous_references": prev_refs,
            }
            
            # Update provider_reference to new reference
            payment.provider_reference = new_reference
            payment.status = PaymentStatus.PENDING
            db.add(payment)
            db.commit()
            db.refresh(payment)
            
            # Retry initialization with new reference
            try:
                paystack_data = await paystack_client.initialize_transaction(
                    email=current_user.email,
                    amount=order.total_amount,
                    reference=new_reference,
                    callback_url=callback_url,
                    metadata={
                        "order_id": order.id,
                        "payment_id": payment.id,
                        "user_id": current_user.id,
                    },
                )
                
                # Store Paystack response metadata
                payment.payment_metadata = {
                    **(payment.payment_metadata or {}),
                    "paystack_authorization_url": paystack_data.get("authorization_url"),
                    "paystack_access_code": paystack_data.get("access_code"),
                    "paystack_reference": paystack_data.get("reference"),
                }
                db.add(payment)
                db.commit()
                db.refresh(payment)
                
                return {
                    "authorization_url": paystack_data.get("authorization_url"),
                    "access_code": paystack_data.get("access_code"),
                    "reference": paystack_data.get("reference"),
                }
                
            except PaystackError as retry_error:
                # Retry failed - mark payment as failed, preserve metadata
                payment.status = "FAILED"
                db.add(payment)
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Payment initialization failed after reference rotation: {retry_error.message}",
                )
        
        # Not a duplicate reference error, or rotation failed - mark as failed
        payment.status = "FAILED"
        db.add(payment)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Payment initialization failed: {e.message}",
        )


class PaystackVerifyRequest(BaseModel):
    reference: str = Field(
        ..., min_length=1, max_length=255, description="Paystack transaction reference from customer return"
    )


class PaystackVerifyRequest(BaseModel):
    reference: str = Field(
        ..., min_length=1, max_length=255, description="Paystack transaction reference from customer return"
    )


@app.post("/payments/paystack/verify")
async def verify_paystack_payment(
    payload: PaystackVerifyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify a Paystack transaction for the returning customer.

    The presented reference is the only payment identity accepted from the
    customer. Success, amount, currency and status are never trusted from
    the frontend — the transaction is always re-verified with Paystack and
    compared against the server-side Payment record.
    """
    presented_reference = (payload.reference or "").strip()
    if not presented_reference:
        return {"status": "ignored", "result": "unknown_reference"}

    # 1. Resolve the Payment: primary lookup, then the same
    # previous_references fallback used by the hardened webhook.
    payment = db.query(Payment).filter(Payment.provider_reference == presented_reference).first()
    if payment is None:
        candidates = (
            db.query(Payment)
            .filter(_previous_references_filter(db, presented_reference))
            .all()
        )
        if len(candidates) == 1:
            payment = candidates[0]
        else:
            # Zero matches (unknown reference) or multiple matches (ambiguous):
            # fail closed with the same generic response so callers cannot
            # distinguish unknown, ambiguous, or other-customer references.
            if len(candidates) > 1:
                logger.warning("Ambiguous previous reference presented to verify endpoint")
            return {"status": "ignored", "result": "unknown_reference"}

    # 2. Ownership: the Payment's Order must belong to the caller.
    # Admin bypass mirrors payment initialization; anything else (including a
    # reference belonging to another customer) gets the generic response with
    # no payment/order data and no state change.
    order = db.query(Order).filter(Order.id == payment.order_id).first()
    if order is None or order.user_id != current_user.id:
        if order is None or not require_admin(current_user):
            return {"status": "ignored", "result": "unknown_reference"}

    # Idempotency fast path (re-checked under row lock below).
    if payment.status == PaymentStatus.SUCCESS:
        return {"status": "verified", "result": "already_processed"}

    # 3. Re-verify with Paystack using the PRESENTED reference (never the
    # stored provider_reference when they differ after rotation).
    try:
        verification = await paystack_client.verify_transaction(presented_reference)
    except PaystackVerificationError:
        # Do not falsely confirm; leave state untouched so the existing
        # polling/webhook flow can proceed.
        return {"status": "verified", "result": "verification_failed"}

    # The Paystack response must correspond to the presented reference, and
    # the presented reference must be recorded on the resolved Payment.
    paystack_reference = verification.get("reference")
    if paystack_reference != presented_reference:
        return {"status": "verified", "result": "reference_mismatch"}
    recorded_references = {payment.provider_reference} | set(
        (payment.payment_metadata or {}).get("previous_references", []) or []
    )
    if presented_reference not in recorded_references:
        return {"status": "verified", "result": "reference_mismatch"}

    # 4/5. Amount (NGN kobo conversion) and currency validation — same
    # conventions as the webhook. The Paystack server response is authoritative.
    amount = Decimal(str(verification.get("amount", 0))) / 100  # Convert from kobo
    currency = verification.get("currency", "NGN")
    gateway_status = verification.get("status", "").lower()

    # Lock the Payment row and re-check status so concurrent webhook/verify
    # deliveries cannot double-confirm.
    locked_payment = db.query(Payment).filter(Payment.id == payment.id).with_for_update().first()
    if locked_payment is None:
        return {"status": "ignored", "result": "unknown_reference"}
    if locked_payment.status == PaymentStatus.SUCCESS:
        return {"status": "verified", "result": "already_processed"}

    if amount != locked_payment.amount:
        # Amount mismatch: keep PENDING for manual review with full evidence
        # (funds may actually have been received — never mark FAILED here).
        locked_payment.status = PaymentStatus.PENDING
        locked_payment.payment_metadata = {
            **(locked_payment.payment_metadata or {}),
            "amount_mismatch": {
                "expected": str(locked_payment.amount),
                "received": str(amount),
                "paystack_reference": presented_reference,
                "paystack_verification": verification,
                "requires_manual_review": True,
            },
        }
        db.add(locked_payment)
        db.commit()
        return {"status": "verified", "result": "amount_mismatch"}

    if currency != "NGN":
        # Currency mismatch: keep PENDING for manual review, never confirm.
        locked_payment.status = PaymentStatus.PENDING
        locked_payment.payment_metadata = {
            **(locked_payment.payment_metadata or {}),
            "currency_mismatch": {
                "expected": "NGN",
                "received": currency,
                "paystack_reference": presented_reference,
                "paystack_verification": verification,
                "requires_manual_review": True,
            },
        }
        db.add(locked_payment)
        db.commit()
        return {"status": "verified", "result": "currency_mismatch"}

    if gateway_status == "success":
        locked_payment.status = PaymentStatus.SUCCESS
        locked_payment.paid_at = datetime.now(timezone.utc)
        locked_payment.payment_metadata = {
            **(locked_payment.payment_metadata or {}),
            "paystack_verification": verification,
        }
        db.add(locked_payment)

        # Confirm the order only if still PENDING — never resurrect CANCELLED
        # or regress PROCESSING/READY_FOR_PICKUP/OUT_FOR_DELIVERY/COMPLETED.
        locked_order = (
            db.query(Order)
            .filter(Order.id == locked_payment.order_id)
            .with_for_update()
            .first()
        )
        if locked_order and locked_order.status == OrderStatus.PENDING:
            locked_order.status = OrderStatus.CONFIRMED
            db.add(locked_order)
        elif locked_order and locked_order.status == OrderStatus.CANCELLED:
            # Late success after cancellation/expiry: record success but never
            # resurrect. Flag for manual review / refund (no automatic refund).
            locked_payment.payment_metadata = {
                **(locked_payment.payment_metadata or {}),
                "late_success": {
                    "requires_manual_review": True,
                    "refund_required": True,
                    "order_status": locked_order.status.value,
                    "paystack_reference": presented_reference,
                    "recorded_at": utcnow().isoformat(),
                },
            }
            db.add(locked_payment)

        db.commit()
        return {"status": "verified", "result": "success"}

    if gateway_status in {"failed", "abandoned"}:
        # Terminal Paystack failure: mark FAILED, leave the order unchanged.
        locked_payment.status = PaymentStatus.FAILED
        locked_payment.payment_metadata = {
            **(locked_payment.payment_metadata or {}),
            "paystack_status": gateway_status,
        }
        db.add(locked_payment)
        db.commit()
        return {"status": "verified", "result": "failed"}

    # Non-terminal/unknown gateway status: leave PENDING so the existing
    # webhook polling flow (or a later webhook) can complete the payment.
    return {"status": "verified", "result": "pending"}


# Phase 3: a PENDING Paystack payment holds decremented stock from order
# creation. If it stays unpaid beyond this window, a scheduled external
# caller (Railway cron) invokes POST /admin/payments/reclaim-stale below.
STALE_PAYSTACK_PAYMENT_EXPIRY_MINUTES = 30


def _as_naive_utc(value: datetime | None) -> datetime | None:
    """Normalize a timestamp for age comparison.

    SQLite/MySQL return naive datetimes while utcnow() is tz-aware; compare
    in naive UTC space so candidate selection behaves identically on both.
    """
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


@app.post("/admin/payments/reclaim-stale")
async def reclaim_stale_paystack_payments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reclaim stock from stale PENDING Paystack payments (admin only).

    A payment is stale when it uses the Paystack provider, is still PENDING,
    its order is still PENDING, and the payment row (whose updated_at is
    refreshed by every initialize/retry/rotation write) is older than
    STALE_PAYSTACK_PAYMENT_EXPIRY_MINUTES.

    For each candidate the Payment and Order rows are locked and re-checked,
    then Paystack is re-verified with the CURRENT provider_reference before
    any stock is touched. Verification errors fail closed (no reclaim).
    Safe to call repeatedly; stock is restored exactly once.
    """
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    cutoff = utcnow() - timedelta(minutes=STALE_PAYSTACK_PAYMENT_EXPIRY_MINUTES)
    cutoff_naive = _as_naive_utc(cutoff)

    candidates = (
        db.query(Payment)
        .join(Order, Order.id == Payment.order_id)
        .filter(
            Payment.provider == "paystack",
            Payment.status == PaymentStatus.PENDING,
            Order.status == OrderStatus.PENDING,
        )
        .all()
    )

    counts = {
        "scanned": 0,
        "reclaimed": 0,
        "succeeded_during_check": 0,
        "still_pending": 0,
        "skipped": 0,
        "verification_errors": 0,
    }

    for candidate in candidates:
        # Pre-lock age screen (re-evaluated after locks below).
        if _as_naive_utc(candidate.updated_at) is None or _as_naive_utc(candidate.updated_at) >= cutoff_naive:
            counts["skipped"] += 1
            continue
        counts["scanned"] += 1

        # Lock the Payment row and re-check every eligibility condition.
        locked_payment = db.query(Payment).filter(Payment.id == candidate.id).with_for_update().first()
        if (
            locked_payment is None
            or locked_payment.provider != "paystack"
            or locked_payment.status != PaymentStatus.PENDING
            or _as_naive_utc(locked_payment.updated_at) is None
            or _as_naive_utc(locked_payment.updated_at) >= cutoff_naive
        ):
            counts["skipped"] += 1
            continue

        # Lock the Order row and re-check.
        locked_order = (
            db.query(Order)
            .filter(Order.id == locked_payment.order_id)
            .with_for_update()
            .first()
        )
        if locked_order is None or locked_order.status != OrderStatus.PENDING:
            counts["skipped"] += 1
            continue

        reference = locked_payment.provider_reference
        if not reference:
            # Nothing verifiable at Paystack — leave untouched.
            counts["skipped"] += 1
            continue

        # Re-verify with Paystack using the CURRENT reference before touching
        # stock. A customer may have paid while the reaper was running.
        try:
            verification = await paystack_client.verify_transaction(reference)
        except PaystackVerificationError:
            # Fail closed: network/gateway errors must never cause reclamation.
            counts["verification_errors"] += 1
            continue

        if verification.get("reference") != reference:
            counts["still_pending"] += 1
            continue

        amount = Decimal(str(verification.get("amount", 0))) / 100
        currency = verification.get("currency", "NGN")
        gateway_status = verification.get("status", "").lower()

        if gateway_status == "success" and amount == locked_payment.amount and currency == "NGN":
            # Paid during the check: confirm, never reclaim.
            locked_payment.status = PaymentStatus.SUCCESS
            locked_payment.paid_at = datetime.now(timezone.utc)
            locked_payment.payment_metadata = {
                **(locked_payment.payment_metadata or {}),
                "paystack_verification": verification,
            }
            db.add(locked_payment)
            locked_order.status = OrderStatus.CONFIRMED
            db.add(locked_order)
            db.commit()
            counts["succeeded_during_check"] += 1
            continue

        if gateway_status == "success":
            # Success-shaped response that fails amount/currency validation:
            # funds may exist — record evidence, keep holding, never reclaim.
            key = "amount_mismatch" if amount != locked_payment.amount else "currency_mismatch"
            locked_payment.payment_metadata = {
                **(locked_payment.payment_metadata or {}),
                key: {
                    "expected": str(locked_payment.amount) if key == "amount_mismatch" else "NGN",
                    "received": str(amount) if key == "amount_mismatch" else currency,
                    "paystack_reference": reference,
                    "paystack_verification": verification,
                    "requires_manual_review": True,
                },
            }
            db.add(locked_payment)
            db.commit()
            counts["still_pending"] += 1
            continue

        if gateway_status not in {"failed", "abandoned"}:
            # Non-terminal (pending/unknown): leave PENDING for a later
            # webhook or cleanup pass.
            counts["still_pending"] += 1
            continue

        # Terminal Paystack failure on a stale unpaid payment: reclaim.
        _restore_stock_for_cancelled_order(db, locked_order)
        _validate_order_status_transition(
            locked_order.status, OrderStatus.CANCELLED, locked_order.fulfillment_method
        )
        locked_order.status = OrderStatus.CANCELLED
        db.add(locked_order)
        locked_payment.payment_metadata = {
            **(locked_payment.payment_metadata or {}),
            "stale_reclaim": {
                "reason": "stale_paystack_payment",
                "expiry_minutes": STALE_PAYSTACK_PAYMENT_EXPIRY_MINUTES,
                "reclaimed_at": utcnow().isoformat(),
                "previous_payment_status": PaymentStatus.PENDING.value,
                "paystack_status": gateway_status,
                "paystack_reference": reference,
                "order_id": locked_order.id,
            },
        }
        db.add(locked_payment)
        db.commit()
        counts["reclaimed"] += 1

    return counts


@app.post("/webhooks/paystack")
async def paystack_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Handle Paystack webhook events."""
    # Get raw body for signature verification
    body = await request.body()

    # Get signature from header
    signature = request.headers.get("x-paystack-signature")
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Paystack signature",
        )

    # Verify signature
    if not paystack_client.verify_signature(body, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Paystack signature",
        )

    # Parse event
    try:
        event_data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    event = event_data.get("event")
    data = event_data.get("data", {})

    # Handle charge.success event
    if event == "charge.success":
        reference = data.get("reference")
        if not reference:
            return {"status": "ignored", "reason": "no reference"}

        # Find payment by provider_reference (primary lookup)
        payment = db.query(Payment).filter(Payment.provider_reference == reference).first()

        # Fallback: if not found, check payment_metadata["previous_references"] array.
        # For re-initialized payments where the reference was rotated, the old
        # reference is stored in payment_metadata["previous_references"].
        # Use JSON array containment check (works for SQLite JSON1 and MySQL JSON).
        # Fail closed if multiple payments match the same reference.
        if not payment:
            candidates = (
                db.query(Payment)
                .filter(_previous_references_filter(db, reference))
                .all()
            )
            if len(candidates) == 1:
                payment = candidates[0]
            elif len(candidates) > 1:
                # Multiple payments share the same previous reference — fail closed.
                return {"status": "ignored", "reason": "ambiguous previous reference"}
            # else: zero candidates, payment remains None

        if not payment:
            # Log but don't fail - might be a test or unknown reference
            return {"status": "ignored", "reason": "payment not found"}

        # Idempotency guard: already-processed successful payments must not be
        # reprocessed, re-timestamped, or have their order updated again.
        # This prevents duplicate webhook deliveries from altering a confirmed
        # payment state. If a "charge.success" webhook is repeated, the handler
        # returns "already_processed" without modifying the payment or order.
        if payment.status == PaymentStatus.SUCCESS:
            return {"status": "verified", "result": "already_processed"}

        # Verify transaction with Paystack
        try:
            verification = await paystack_client.verify_transaction(reference)
        except PaystackVerificationError:
            # Mark payment as failed
            payment.status = "FAILED"
            db.add(payment)
            db.commit()
            return {"status": "verified", "result": "failed"}

        # Verify amount and currency
        amount = Decimal(str(verification.get("amount", 0))) / 100  # Convert from kobo
        currency = verification.get("currency", "NGN")
        gateway_status = verification.get("status", "").lower()

        if amount != payment.amount:
            # Amount mismatch on a transaction Paystack reports as successful.
            # Money may actually have been received, so the payment must NOT be
            # marked FAILED (that would hide real funds). Keep it PENDING for
            # manual investigation and preserve full evidence.
            payment.status = PaymentStatus.PENDING
            payment.payment_metadata = {
                **(payment.payment_metadata or {}),
                "amount_mismatch": {
                    "expected": str(payment.amount),
                    "received": str(amount),
                    "paystack_reference": reference,
                    "paystack_verification": verification,
                    "requires_manual_review": True,
                },
            }
            db.add(payment)
            db.commit()
            return {"status": "verified", "result": "amount_mismatch"}

        if currency != "NGN":
            # Currency mismatch: do not confirm, and do not mark FAILED —
            # funds may exist in another currency. Keep PENDING with evidence
            # for manual review.
            payment.status = PaymentStatus.PENDING
            payment.payment_metadata = {
                **(payment.payment_metadata or {}),
                "currency_mismatch": {
                    "expected": "NGN",
                    "received": currency,
                    "paystack_reference": reference,
                    "paystack_verification": verification,
                    "requires_manual_review": True,
                },
            }
            db.add(payment)
            db.commit()
            return {"status": "verified", "result": "currency_mismatch"}

        if gateway_status == "success":
            # Mark payment as successful
            payment.status = "SUCCESS"
            payment.paid_at = datetime.now(timezone.utc)
            payment.payment_metadata = {
                **(payment.payment_metadata or {}),
                "paystack_verification": verification,
            }
            db.add(payment)

            # Update order status to CONFIRMED only if currently PENDING.
            # Lock the order row to prevent race with cancellation.
            order = (
                db.query(Order)
                .filter(Order.id == payment.order_id)
                .with_for_update()
                .first()
            )
            if order and order.status == OrderStatus.PENDING:
                order.status = OrderStatus.CONFIRMED
                db.add(order)
            elif order and order.status == OrderStatus.CANCELLED:
                # Late success after cancellation/expiry: keep CANCELLED and
                # flag for manual review / refund (no automatic refund).
                payment.payment_metadata = {
                    **(payment.payment_metadata or {}),
                    "late_success": {
                        "requires_manual_review": True,
                        "refund_required": True,
                        "order_status": order.status.value,
                        "paystack_reference": reference,
                        "recorded_at": utcnow().isoformat(),
                    },
                }
                db.add(payment)

            db.commit()
            return {"status": "verified", "result": "success"}

        else:
            # Payment failed on Paystack side
            payment.status = "FAILED"
            payment.payment_metadata = {
                **(payment.payment_metadata or {}),
                "paystack_status": gateway_status,
            }
            db.add(payment)
            db.commit()
            return {"status": "verified", "result": "failed"}

    # Handle failed charge
    elif event == "charge.failed":
        reference = data.get("reference")
        if reference:
            payment = db.query(Payment).filter(Payment.provider_reference == reference).first()
            if payment:
                # Guard: do not downgrade terminal successful/refunded payments
                if payment.status in {PaymentStatus.SUCCESS, PaymentStatus.REFUNDED}:
                    return {"status": "verified", "result": "already_processed"}
                payment.status = "FAILED"
                payment.payment_metadata = {
                    **(payment.payment_metadata or {}),
                    "paystack_failure_reason": data.get("gateway_response"),
                }
                db.add(payment)
                db.commit()
        # Duplicate failed event deliveries are idempotent — the payment
        # status is already FAILED, so re-processing does not change the state.
        return {"status": "ignored"}

    # Ignore other (unhandled) Paystack events
    # These events are received but not processed — payment state is not changed.
    # Returns "ignored" to acknowledge receipt without side effects.
    # Duplicate deliveries are idempotent — the same "ignored" response is returned.
    return {"status": "ignored"}


@app.get("/addresses", response_model=list[AddressRead])
def list_addresses(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(Address)
        .filter(Address.user_id == current_user.id)
        .order_by(Address.created_at.asc(), Address.id.asc())
        .all()
    )


@app.get("/addresses/{address_id}", response_model=AddressRead)
def get_address(
    address_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Address:
    address = _get_user_address_for_update(db, current_user, address_id)
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")
    return address


@app.put("/addresses/{address_id}", response_model=AddressRead)
def update_address(
    address_id: int,
    payload: AddressUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Address:
    address = _get_user_address_for_update(db, current_user, address_id)
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "user_id" in update_data:
        update_data.pop("user_id")

    if "id" in update_data:
        update_data.pop("id")

    if "created_at" in update_data:
        update_data.pop("created_at")

    if "updated_at" in update_data:
        update_data.pop("updated_at")

    should_set_default = update_data.pop("is_default", None)
    for field, value in update_data.items():
        setattr(address, field, value)

    if should_set_default is True:
        _set_default_address(db, current_user, address)
        db.refresh(address)
    elif should_set_default is False and address.is_default:
        other_addresses = (
            db.query(Address)
            .filter(Address.user_id == current_user.id, Address.id != address.id)
            .order_by(Address.created_at.asc(), Address.id.asc())
            .all()
        )
        if other_addresses:
            next_default = other_addresses[0]
            next_default.is_default = True
            address.is_default = False
            db.add(next_default)
            db.add(address)
            db.commit()
            db.refresh(address)
            db.refresh(next_default)

    db.add(address)
    db.commit()
    db.refresh(address)
    return address


@app.delete("/addresses/{address_id}")
def delete_address(
    address_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    address = _get_user_address_for_update(db, current_user, address_id)
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")

    order_reference = db.query(Order).filter(Order.address_id == address_id).first()
    if order_reference is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete an address that is referenced by an existing order.",
        )

    was_default = address.is_default
    db.delete(address)
    db.commit()

    if was_default:
        _ensure_default_after_delete(db, current_user, address_id)

    return {"message": "Address deleted successfully"}


# ---- Expense Routes ----

@app.post("/expenses", response_model=ExpenseRead)
def create_expense(
    payload: ExpenseCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Expense:
    """Create an expense. Admin or staff with MANAGE_EXPENSES permission required."""
    if not (require_admin(current_user) or require_permission(current_user, Permission.MANAGE_EXPENSES)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator or expense management permission required",
        )

    expense = Expense(
        description=payload.description,
        amount=payload.amount,
        category=payload.category,
        recorded_by=current_user.id,
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense


@app.get("/expenses", response_model=list[ExpenseRead])
def list_expenses(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Expense]:
    """List expenses. Admin sees all; users see only their own."""
    if require_admin(current_user):
        return db.query(Expense).all()

    return db.query(Expense).filter(Expense.recorded_by == current_user.id).all()


@app.get("/expenses/{expense_id}", response_model=ExpenseRead)
def get_expense(
    expense_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Expense:
    """Retrieve a specific expense."""
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Expense not found",
        )

    # Non-admin users can only access their own expenses
    if expense.recorded_by != current_user.id:
        if not require_admin(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this expense",
            )

    return expense


@app.patch("/expenses/{expense_id}", response_model=ExpenseRead)
def update_expense(
    expense_id: int,
    payload: ExpenseCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Expense:
    """Update an expense. Admin-only."""
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Expense not found",
        )

    expense.description = payload.description
    expense.amount = payload.amount
    expense.category = payload.category
    # recorded_by is never changed

    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense


@app.delete("/expenses/{expense_id}")
def delete_expense(
    expense_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Delete an expense. Admin-only."""
    if not require_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )

    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Expense not found",
        )

    db.delete(expense)
    db.commit()
    return {"message": "Expense deleted successfully"}


@app.patch("/addresses/{address_id}/default", response_model=AddressRead)
def set_default_address(
    address_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Address:
    address = _get_user_address_for_update(db, current_user, address_id)
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")

    return _set_default_address(db, current_user, address)