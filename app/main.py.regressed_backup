from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.orm import Session

from decimal import Decimal

from app.core.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    require_admin,
    require_permission,
    verify_password,
)
from app.core.database import get_db
from app.models import (
    Address,
    Category,
    FulfillmentMethod,
    Order,
    OrderItem,
    OrderStatus,
    Permission,
    Product,
    User,
    UserRole,
)
from app.schemas.address import AddressCreate, AddressRead, AddressUpdate
from app.schemas.checkout import (
    CartItemDetails,
    CheckoutRequest,
    CheckoutSummary,
    CheckoutValidationResponse,
)
from app.schemas.order import OrderCreate, OrderRead, OrderStatusUpdate
from app.schemas.product import (
    CategoryCreate,
    CategoryRead,
    ProductCreate,
    ProductListRead,
    ProductRead,
    ProductUpdate,
    StockUpdate,
)
from app.schemas.user import UserCreate, UserRead, UserUpdate

app = FastAPI(
    title="Twelve09 Kiddies Store API",
    version="1.0.0",
)


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
        role=user_in.role,
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
    db: Session = Depends(get_db),
):
    query = db.query(Product)
    if not include_inactive:
        query = query.filter(Product.is_active == True)
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

    delivery_fee = Decimal("0.00")
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

    products = db.query(Product).filter(Product.id.in_(product_quantities.keys())).all()
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

    delivery_fee = Decimal("0.00")
    if fulfillment_method == FulfillmentMethod.STORE_DELIVERY:
        delivery_fee = Decimal("0.00")
    if fulfillment_method == FulfillmentMethod.CUSTOMER_DISPATCH:
        delivery_fee = Decimal("0.00")

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

    order = db.query(Order).filter(Order.id == order_id).first()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    try:
        _validate_order_status_transition(order.status, payload.status, order.fulfillment_method)
    except HTTPException:
        raise

    order.status = payload.status
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
        product = db.query(Product).filter(Product.id == product_id).first()
        if product:
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
