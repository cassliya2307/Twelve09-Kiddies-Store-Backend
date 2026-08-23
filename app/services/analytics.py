from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, and_, or_
from sqlalchemy.orm import Session

from app.models import Order, OrderItem, OrderStatus, Payment, PaymentStatus, Product, Expense, Category


def _get_date_range(days: int | None, start_date: date | None, end_date: date | None) -> tuple[datetime | None, datetime | None]:
    """Calculate start and end datetime for queries."""
    if start_date or end_date:
        start_dt = datetime.combine(start_date, datetime.min.time()) if start_date else None
        end_dt = datetime.combine(end_date, datetime.max.time()) if end_date else None
        return start_dt, end_dt
    if days:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        return start_dt, end_dt
    return None, None


def get_kpi_summary(
    db: Session,
    days: int | None = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """Get key performance indicators."""
    start_dt, end_dt = _get_date_range(days, start_date, end_date)

    # Base query for completed orders
    completed_orders = db.query(Order).filter(Order.status == OrderStatus.COMPLETED)
    if start_dt:
        completed_orders = completed_orders.filter(Order.created_at >= start_dt)
    if end_dt:
        completed_orders = completed_orders.filter(Order.created_at <= end_dt)

    total_orders = completed_orders.count()
    total_revenue = completed_orders.with_entities(func.sum(Order.total_amount)).scalar() or Decimal("0")

    # Refunded payments for completed orders
    refunded_payments = db.query(func.sum(Payment.amount)).join(Order).filter(
        Order.status == OrderStatus.COMPLETED,
        Payment.status == PaymentStatus.REFUNDED,
    )
    if start_dt:
        refunded_payments = refunded_payments.filter(Payment.created_at >= start_dt)
    if end_dt:
        refunded_payments = refunded_payments.filter(Payment.created_at <= end_dt)
    total_refunds = refunded_payments.scalar() or Decimal("0")

    net_revenue = total_revenue - total_refunds

    # Expenses
    expenses_query = db.query(func.sum(Expense.amount))
    if start_dt:
        expenses_query = expenses_query.filter(Expense.created_at >= start_dt)
    if end_dt:
        expenses_query = expenses_query.filter(Expense.created_at <= end_dt)
    total_expenses = expenses_query.scalar() or Decimal("0")

    profit_proxy = net_revenue - total_expenses

    avg_order_value = (net_revenue / total_orders) if total_orders > 0 else Decimal("0")

    return {
        "total_revenue": total_revenue,
        "total_orders": total_orders,
        "completed_orders": total_orders,
        "total_refunds": total_refunds,
        "net_revenue": net_revenue,
        "total_expenses": total_expenses,
        "profit_proxy": profit_proxy,
        "avg_order_value": avg_order_value,
    }


def get_sales_timeseries(
    db: Session,
    granularity: str = "day",
    days: int | None = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """Get sales time series data.

    Fetches daily completed-order aggregates from the database, then groups
    by day/week/month in Python for database-agnostic behavior (works on
    both SQLite and MySQL).
    """
    start_dt, end_dt = _get_date_range(days, start_date, end_date)

    # Base query: daily completed-order aggregates
    daily_query = db.query(
        func.date(Order.created_at).label("day"),
        func.sum(Order.total_amount).label("revenue"),
        func.count(Order.id).label("order_count"),
    ).filter(Order.status == OrderStatus.COMPLETED)

    if start_dt:
        daily_query = daily_query.filter(Order.created_at >= start_dt)
    if end_dt:
        daily_query = daily_query.filter(Order.created_at <= end_dt)

    daily_rows = daily_query.group_by(func.date(Order.created_at)).order_by(func.date(Order.created_at)).all()

    # Convert to dict for easy grouping
    daily_data = {}
    for row in daily_rows:
        day = row.day
        if isinstance(day, str):
            day = datetime.strptime(day, "%Y-%m-%d").date()
        daily_data[day] = {
            "period": day,
            "value": row.revenue or Decimal("0"),
            "count": row.order_count,
        }

    # Group by granularity
    daily = []
    weekly = []
    monthly = []

    for day, point in sorted(daily_data.items()):
        if granularity == "day":
            daily.append(point)
        elif granularity == "week":
            # Monday-start week: Monday = day - weekday() days
            # weekday() returns 0=Monday, 6=Sunday
            monday = day - timedelta(days=day.weekday())
            week_key = monday
            if week_key not in weekly:
                weekly.append({"period": week_key, "value": Decimal("0"), "count": 0})
            # Find and accumulate
            for w in weekly:
                if w["period"] == week_key:
                    w["value"] += point["value"]
                    w["count"] += point["count"]
                    break
        elif granularity == "month":
            month_key = day.replace(day=1)
            if month_key not in monthly:
                monthly.append({"period": month_key, "value": Decimal("0"), "count": 0})
            for m in monthly:
                if m["period"] == month_key:
                    m["value"] += point["value"]
                    m["count"] += point["count"]
                    break

    # Sort results
    daily.sort(key=lambda x: x["period"])
    weekly.sort(key=lambda x: x["period"])
    monthly.sort(key=lambda x: x["period"])

    return {
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
    }


def get_top_products(
    db: Session,
    limit: int = 10,
    days: int | None = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    """Get top selling products by revenue."""
    start_dt, end_dt = _get_date_range(days, start_date, end_date)

    query = db.query(
        Product.id.label("product_id"),
        Product.name.label("product_name"),
        Category.name.label("category_name"),
        func.sum(OrderItem.quantity).label("total_quantity"),
        func.sum(OrderItem.subtotal).label("total_revenue"),
        func.count(OrderItem.id).label("order_count"),
    ).join(OrderItem, Product.id == OrderItem.product_id) \
     .join(Order, OrderItem.order_id == Order.id) \
     .join(Category, Product.category_id == Category.id) \
     .filter(Order.status == OrderStatus.COMPLETED)

    if start_dt:
        query = query.filter(Order.created_at >= start_dt)
    if end_dt:
        query = query.filter(Order.created_at <= end_dt)

    results = query.group_by(Product.id, Product.name, Category.name) \
        .order_by(func.sum(OrderItem.subtotal).desc()) \
        .limit(limit).all()

    return [
        {
            "product_id": r.product_id,
            "product_name": r.product_name,
            "category_name": r.category_name,
            "total_quantity_sold": r.total_quantity,
            "total_revenue": r.total_revenue or Decimal("0"),
            "order_count": r.order_count,
        }
        for r in results
    ]


def get_category_performance(
    db: Session,
    days: int | None = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    """Get sales performance by category."""
    start_dt, end_dt = _get_date_range(days, start_date, end_date)

    query = db.query(
        Category.id.label("category_id"),
        Category.name.label("category_name"),
        func.sum(OrderItem.subtotal).label("total_revenue"),
        func.sum(OrderItem.quantity).label("total_quantity"),
        func.count(Order.id).label("order_count"),
    ).join(Product, Category.id == Product.category_id) \
     .join(OrderItem, Product.id == OrderItem.product_id) \
     .join(Order, OrderItem.order_id == Order.id) \
     .filter(Order.status == OrderStatus.COMPLETED)

    if start_dt:
        query = query.filter(Order.created_at >= start_dt)
    if end_dt:
        query = query.filter(Order.created_at <= end_dt)

    results = query.group_by(Category.id, Category.name) \
        .order_by(func.sum(OrderItem.subtotal).desc()).all()

    return [
        {
            "category_id": r.category_id,
            "category_name": r.category_name,
            "total_revenue": r.total_revenue or Decimal("0"),
            "total_quantity": r.total_quantity or 0,
            "order_count": r.order_count,
        }
        for r in results
    ]


def get_inventory_snapshot(db: Session) -> list[dict]:
    """Get current inventory valuation."""
    products = db.query(Product).filter(Product.is_active == True).all()

    result = []
    for p in products:
        retail_value = p.price * p.stock_quantity
        cost_value = None
        if p.cost_price is not None:
            cost_value = p.cost_price * p.stock_quantity

        result.append({
            "product_id": p.id,
            "product_name": p.name,
            "category_name": p.category.name if p.category else None,
            "stock_quantity": p.stock_quantity,
            "selling_price": p.price,
            "cost_price": p.cost_price,
            "retail_value": retail_value,
            "cost_value": cost_value,
        })

    return result


def get_expense_summary(
    db: Session,
    days: int | None = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """Get expense summary by category."""
    start_dt, end_dt = _get_date_range(days, start_date, end_date)

    query = db.query(
        Expense.category,
        func.sum(Expense.amount).label("total_amount"),
    )

    if start_dt:
        query = query.filter(Expense.created_at >= start_dt)
    if end_dt:
        query = query.filter(Expense.created_at <= end_dt)

    results = query.group_by(Expense.category).order_by(func.sum(Expense.amount).desc()).all()

    by_category = {r.category: r.total_amount or Decimal("0") for r in results}
    total = sum(by_category.values(), Decimal("0"))

    return {
        "by_category": by_category,
        "total": total,
        "period_start": start_dt.date() if start_dt else None,
        "period_end": end_dt.date() if end_dt else None,
    }


def get_order_status_distribution(db: Session) -> list[dict]:
    """Get order count and total amount by status."""
    results = db.query(
        Order.status,
        func.count(Order.id).label("count"),
        func.sum(Order.total_amount).label("total_amount"),
    ).group_by(Order.status).all()

    return [
        {
            "status": r.status.value if hasattr(r.status, 'value') else str(r.status),
            "count": r.count,
            "total_amount": r.total_amount or Decimal("0"),
        }
        for r in results
    ]


def get_payment_distribution(
    db: Session,
    days: int | None = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    """Get payment distribution by method and status."""
    start_dt, end_dt = _get_date_range(days, start_date, end_date)

    query = db.query(
        Payment.payment_method,
        Payment.status,
        func.count(Payment.id).label("count"),
        func.sum(Payment.amount).label("total_amount"),
    )

    if start_dt:
        query = query.filter(Payment.created_at >= start_dt)
    if end_dt:
        query = query.filter(Payment.created_at <= end_dt)

    results = query.group_by(Payment.payment_method, Payment.status).all()

    # Aggregate by payment method
    method_stats = {}
    for r in results:
        method = r.payment_method
        if method not in method_stats:
            method_stats[method] = {
                "method": method,
                "total_count": 0,
                "total_amount": Decimal("0"),
                "success_count": 0,
                "failed_count": 0,
                "refunded_count": 0,
                "pending_count": 0,
            }
        method_stats[method]["total_count"] += r.count
        method_stats[method]["total_amount"] += r.total_amount or Decimal("0")
        if r.status == PaymentStatus.SUCCESS:
            method_stats[method]["success_count"] += r.count
        elif r.status == PaymentStatus.FAILED:
            method_stats[method]["failed_count"] += r.count
        elif r.status == PaymentStatus.REFUNDED:
            method_stats[method]["refunded_count"] += r.count
        elif r.status == PaymentStatus.PENDING:
            method_stats[method]["pending_count"] += r.count

    output = []
    for method, stats in method_stats.items():
        total = stats["total_count"]
        success_rate = (stats["success_count"] / total * 100) if total > 0 else 0.0
        output.append({
            "method": method,
            "count": total,
            "total_amount": stats["total_amount"],
            "success_rate": success_rate,
        })

    return output


def get_cogs(
    db: Session,
    days: int | None = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> Decimal:
    """Calculate Cost of Goods Sold for completed orders with known cost_price."""
    start_dt, end_dt = _get_date_range(days, start_date, end_date)

    query = db.query(
        func.sum(OrderItem.quantity * Product.cost_price).label("cogs")
    ).join(Product, OrderItem.product_id == Product.id) \
     .join(Order, OrderItem.order_id == Order.id) \
     .filter(Order.status == OrderStatus.COMPLETED) \
     .filter(Product.cost_price.isnot(None))

    if start_dt:
        query = query.filter(Order.created_at >= start_dt)
    if end_dt:
        query = query.filter(Order.created_at <= end_dt)

    result = query.scalar()
    return result or Decimal("0")


def get_gross_profit(
    db: Session,
    days: int | None = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """Calculate gross profit where cost_price data exists."""
    kpi = get_kpi_summary(db, days, start_date, end_date)
    cogs = get_cogs(db, days, start_date, end_date)
    gross_profit = kpi["net_revenue"] - cogs

    return {
        "net_revenue": kpi["net_revenue"],
        "cogs": cogs,
        "gross_profit": gross_profit,
        "gross_margin_percent": float((gross_profit / kpi["net_revenue"] * 100)) if kpi["net_revenue"] > 0 else 0.0,
    }