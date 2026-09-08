from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_permission
from app.core.database import get_db
from app.models import Permission, User
from app.schemas.dashboard import (
    CategoryPerformance,
    DashboardResponse,
    ExpenseSummary,
    GrossProfit,
    InventorySnapshot,
    KPISummary,
    OrderStatusDistribution,
    PaymentMethodDistribution,
    SalesTimeSeries,
    TopProduct,
)
from app.services import analytics


router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])


def _require_reports_permission(current_user: User = Depends(get_current_user)):
    if not require_permission(current_user, Permission.VIEW_REPORTS):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="VIEW_REPORTS permission required",
        )
    return current_user


def _parse_date_range(
    days: Optional[int] = Query(None, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
) -> tuple[Optional[int], Optional[date], Optional[date]]:
    if start_date and end_date and start_date > end_date:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date must be before or equal to end_date",
        )
    return days, start_date, end_date


@router.get("", response_model=DashboardResponse)
def get_dashboard(
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Get full dashboard with all analytics."""
    kpis = analytics.get_kpi_summary(db, days, start_date, end_date)
    sales_ts = analytics.get_sales_timeseries(db, "day", days, start_date, end_date)
    top_products = analytics.get_top_products(db, 10, days, start_date, end_date)
    categories = analytics.get_category_performance(db, days, start_date, end_date)
    inventory = analytics.get_inventory_snapshot(db)
    expenses = analytics.get_expense_summary(db, days, start_date, end_date)
    order_status = analytics.get_order_status_distribution(db)
    payments = analytics.get_payment_distribution(db, days, start_date, end_date)
    gross_profit = analytics.get_gross_profit(db, days, start_date, end_date)

    # Check if we have sufficient cost data for gross profit
    gross_profit_obj = None
    if gross_profit["cogs"] > 0:
        gross_profit_obj = GrossProfit(**gross_profit)

    return DashboardResponse(
        kpis=KPISummary(**kpis),
        sales_timeseries=SalesTimeSeries(**sales_ts),
        top_products=[TopProduct(**p) for p in top_products],
        category_performance=[CategoryPerformance(**c) for c in categories],
        inventory_snapshot=[InventorySnapshot(**i) for i in inventory],
        expense_summary=ExpenseSummary(**expenses),
        order_status_distribution=[OrderStatusDistribution(**s) for s in order_status],
        payment_distribution=[PaymentMethodDistribution(**p) for p in payments],
        gross_profit=gross_profit_obj,
        generated_at=datetime.utcnow(),
    )


@router.get("/kpis", response_model=KPISummary)
def get_kpis(
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> KPISummary:
    """Get KPI summary only."""
    kpis = analytics.get_kpi_summary(db, days, start_date, end_date)
    return KPISummary(**kpis)


@router.get("/sales", response_model=SalesTimeSeries)
def get_sales(
    granularity: str = Query("day", pattern="^(day|week|month)$"),
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> SalesTimeSeries:
    """Get sales time series."""
    sales_ts = analytics.get_sales_timeseries(db, granularity, days, start_date, end_date)
    return SalesTimeSeries(**sales_ts)


@router.get("/top-products", response_model=list[TopProduct])
def get_top_products(
    limit: int = Query(10, ge=1, le=100),
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> list[TopProduct]:
    """Get top selling products."""
    products = analytics.get_top_products(db, limit, days, start_date, end_date)
    return [TopProduct(**p) for p in products]


@router.get("/categories", response_model=list[CategoryPerformance])
def get_categories(
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> list[CategoryPerformance]:
    """Get category performance."""
    categories = analytics.get_category_performance(db, days, start_date, end_date)
    return [CategoryPerformance(**c) for c in categories]


@router.get("/inventory", response_model=list[InventorySnapshot])
def get_inventory(
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> list[InventorySnapshot]:
    """Get inventory snapshot (current, no date filtering)."""
    inventory = analytics.get_inventory_snapshot(db)
    return [InventorySnapshot(**i) for i in inventory]


@router.get("/expenses", response_model=ExpenseSummary)
def get_expenses(
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> ExpenseSummary:
    """Get expense summary."""
    expenses = analytics.get_expense_summary(db, days, start_date, end_date)
    return ExpenseSummary(**expenses)


@router.get("/orders/status", response_model=list[OrderStatusDistribution])
def get_order_status(
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> list[OrderStatusDistribution]:
    """Get order status distribution."""
    statuses = analytics.get_order_status_distribution(db)
    return [OrderStatusDistribution(**s) for s in statuses]


@router.get("/payments", response_model=list[PaymentMethodDistribution])
def get_payments(
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> list[PaymentMethodDistribution]:
    """Get payment method distribution."""
    payments = analytics.get_payment_distribution(db, days, start_date, end_date)
    return [PaymentMethodDistribution(**p) for p in payments]


@router.get("/gross-profit", response_model=GrossProfit)
def get_gross_profit(
    days: Optional[int] = Query(30, ge=1, le=365),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: User = Depends(_require_reports_permission),
    db: Session = Depends(get_db),
) -> GrossProfit:
    """Get gross profit (only when cost_price data exists)."""
    profit = analytics.get_gross_profit(db, days, start_date, end_date)
    return GrossProfit(**profit)