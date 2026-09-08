from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class KPISummary(BaseModel):
    total_revenue: Decimal
    total_orders: int
    completed_orders: int
    total_refunds: Decimal
    net_revenue: Decimal
    total_expenses: Decimal
    profit_proxy: Decimal
    avg_order_value: Decimal

    model_config = ConfigDict(from_attributes=True)


class TimeSeriesPoint(BaseModel):
    period: date
    value: Decimal
    count: int

    model_config = ConfigDict(from_attributes=True)


class SalesTimeSeries(BaseModel):
    daily: list[TimeSeriesPoint]
    weekly: list[TimeSeriesPoint]
    monthly: list[TimeSeriesPoint]

    model_config = ConfigDict(from_attributes=True)


class TopProduct(BaseModel):
    product_id: int
    product_name: str
    category_name: str
    total_quantity_sold: int
    total_revenue: Decimal
    order_count: int

    model_config = ConfigDict(from_attributes=True)


class CategoryPerformance(BaseModel):
    category_id: int
    category_name: str
    total_revenue: Decimal
    total_quantity: int
    order_count: int

    model_config = ConfigDict(from_attributes=True)


class InventorySnapshot(BaseModel):
    product_id: int
    product_name: str
    category_name: Optional[str] = None
    stock_quantity: int
    selling_price: Decimal
    cost_price: Optional[Decimal] = None
    retail_value: Decimal
    cost_value: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


class ExpenseSummary(BaseModel):
    by_category: dict[str, Decimal]
    total: Decimal
    period_start: Optional[date] = None
    period_end: Optional[date] = None

    model_config = ConfigDict(from_attributes=True)


class OrderStatusDistribution(BaseModel):
    status: str
    count: int
    total_amount: Decimal

    model_config = ConfigDict(from_attributes=True)


class PaymentMethodDistribution(BaseModel):
    method: str
    count: int
    total_amount: Decimal
    success_rate: float

    model_config = ConfigDict(from_attributes=True)


class GrossProfit(BaseModel):
    net_revenue: Decimal
    cogs: Decimal
    gross_profit: Decimal
    gross_margin_percent: float

    model_config = ConfigDict(from_attributes=True)


class DashboardResponse(BaseModel):
    kpis: KPISummary
    sales_timeseries: SalesTimeSeries
    top_products: list[TopProduct]
    category_performance: list[CategoryPerformance]
    inventory_snapshot: list[InventorySnapshot]
    expense_summary: ExpenseSummary
    order_status_distribution: list[OrderStatusDistribution]
    payment_distribution: list[PaymentMethodDistribution]
    gross_profit: Optional[GrossProfit] = None
    generated_at: datetime

    model_config = ConfigDict(from_attributes=True)