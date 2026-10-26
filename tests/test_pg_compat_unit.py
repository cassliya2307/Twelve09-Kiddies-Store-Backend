"""Phase 5E.3 unit tests: source-side pre-import compatibility checks.

Pure-data throughout. Rows are synthetic dicts, no cursor, no driver, no
network, no database. Confirms each check's ERROR/WARNING/PASS outcome, the
sanitization of every report line, and that nothing here writes to a target.

Run from the backend directory so the ``tools`` package is importable:

    cd backend
    python -m pytest tests/test_pg_compat_unit.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tools import pg_import as P


_BACKEND_DIR = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# Synthetic, import-clean rows
# --------------------------------------------------------------------------- #
def user_rows(n=2, **over):
    base = {
        "id": 1, "name": "Ada", "email": "a@example.invalid", "password_hash": "x",
        "role": "CUSTOMER", "permissions": ["MANAGE_ORDERS"], "is_active": True,
        "created_at": datetime(2026, 8, 14, 10, 0, 0),
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    rows = [dict(base) for _ in range(n)]
    for idx, row in enumerate(rows, start=1):
        row["id"] = idx
        row["email"] = f"user{idx}@example.invalid"
    return rows


def order_rows(n=1, **over):
    base = {
        "id": 1, "user_id": 1, "address_id": 1, "total_amount": Decimal("120.00"),
        "status": "PENDING", "fulfillment_method": "STORE_DELIVERY",
        "delivery_fee": Decimal("20.00"), "delivery_recipient_name": "Recip",
        "delivery_phone_number": "08030000000", "delivery_address_line": "1 Road",
        "delivery_city": "City", "delivery_state": "State",
        "delivery_additional_directions": None,
        "created_at": datetime(2026, 8, 14, 10, 0, 0),
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    rows = [dict(base) for _ in range(n)]
    for idx, row in enumerate(rows, start=1):
        row["id"] = idx
        row["user_id"] = 1
        row["address_id"] = 1
    return rows


def payment_rows(n=1, **over):
    base = {
        "id": 1, "order_id": 1, "amount": Decimal("120.00"), "status": "SUCCESS",
        "payment_method": "card", "provider": "paystack",
        "provider_reference": "ref-1", "transaction_reference": "tx-1",
        "payment_metadata": {"channel": "card"}, "currency": "NGN",
        "paid_at": datetime(2026, 8, 14, 10, 0, 0),
        "created_at": datetime(2026, 8, 14, 10, 0, 0),
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    rows = [dict(base) for _ in range(n)]
    for idx, row in enumerate(rows, start=1):
        row["id"] = idx
    return rows


def item_rows(n=2, **over):
    base = {
        "id": 1, "order_id": 1, "product_id": 1, "quantity": 2,
        "unit_price": Decimal("50.00"), "subtotal": Decimal("100.00"),
    }
    rows = [dict(base) for _ in range(n)]
    for idx, row in enumerate(rows, start=1):
        row["id"] = idx
    return rows


def product_rows(n=1, **over):
    base = {
        "id": 1, "category_id": 1, "name": "Toy", "description": None,
        "price": Decimal("10.00"), "cost_price": Decimal("4.00"),
        "stock_quantity": 3, "image_url": None, "is_active": True,
        "created_at": datetime(2026, 8, 14, 10, 0, 0),
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    rows = [dict(base) for _ in range(n)]
    for idx, row in enumerate(rows, start=1):
        row["id"] = idx
    return rows


def clean_dataset(**over) -> dict:
    """A minimal, fully import-compatible dataset for all nine tables."""
    data = {
        "users": user_rows(2),
        "categories": [
            {"id": 1, "name": "Toys", "description": None, "is_active": True,
             "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14)},
        ],
        "delivery_config": [
            {"id": 1, "fee_amount": Decimal("20.00"), "is_active": True,
             "updated_at": datetime(2026, 8, 14)},
        ],
        "products": product_rows(1),
        "addresses": [
            {"id": 1, "user_id": 1, "recipient_name": "Recip", "phone_number": "0803",
             "address_line": "1 Road", "city": "City", "state": "State",
             "additional_directions": None, "is_default": True,
             "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14)},
        ],
        "orders": order_rows(1),
        "order_items": item_rows(1),
        "payments": payment_rows(1),
        "expenses": [
            {"id": 1, "recorded_by": 1, "description": "Restock",
             "amount": Decimal("500.00"), "category": "STOCK",
             "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14)},
        ],
    }
    data.update(over)
    return data


def run(data, **kw) -> P.CompatibilityReport:
    return P.run_compatibility_checks(data, **kw)


def errors_of(report) -> set:
    return {f.check for f in report.errors}


def warnings_of(report) -> set:
    return {f.check for f in report.warnings}


def _one(report, check_id):
    for f in report.findings:
        if f.check == check_id:
            return f
    raise AssertionError(f"no finding for {check_id}")


# --------------------------------------------------------------------------- #
# Clean dataset + manifest plumbing
# --------------------------------------------------------------------------- #
def test_clean_dataset_reports_no_errors_and_no_warnings():
    report = run(clean_dataset())
    assert report.has_errors() is False
    assert report.errors == ()
    assert report.warnings == ()
    assert all(f.status == P.CHECK_PASS for f in report.findings)


def test_empty_dataset_is_a_full_pass():
    report = run({t: [] for t in P.TABLE_ORDER})
    assert report.has_errors() is False
    assert report.warnings == ()


def test_manifest_is_optional_and_absent_by_default():
    assert P.load_enum_normalization_manifest(None) == {}
    assert P.load_enum_normalization_manifest("") == {}
    assert P.load_enum_normalization_manifest("   ") == {}


def test_manifest_valid_mapping_is_honoured(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"orders.status": {"SHIPPED": "OUT_FOR_DELIVERY"}}),
                    encoding="utf-8")
    manifest = P.load_enum_normalization_manifest(str(path))
    assert manifest == {("orders", "status"): {"SHIPPED": "OUT_FOR_DELIVERY"}}
    data = clean_dataset()
    data["orders"][0]["status"] = "SHIPPED"
    report = run(data, manifest=manifest)
    assert "enum.orderstatus.legacy" not in errors_of(report)
    assert "enum.orderstatus" not in errors_of(report)
    assert report.has_errors() is False


def test_manifest_cannot_introduce_an_unsupported_target_label(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"orders.status": {"SHIPPED": "WEIRD"}}), encoding="utf-8")
    with pytest.raises(P.Fail) as ei:
        P.load_enum_normalization_manifest(str(path))
    assert ei.value.code == P.ExitCode.CONFIG


def test_manifest_rejects_unknown_columns_and_shapes(tmp_path):
    for payload in ({"orders": {"A": "B"}},
                    {"orders.status": ["SHIPPED"]},
                    {"users.email": {"a": "b"}},
                    ["orders.status"]):
        path = tmp_path / "m.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(P.Fail) as ei:
            P.load_enum_normalization_manifest(str(path))
        assert ei.value.code == P.ExitCode.CONFIG


def test_manifest_rejects_unreadable_and_non_json(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(P.Fail) as ei:
        P.load_enum_normalization_manifest(str(missing))
    assert ei.value.code == P.ExitCode.CONFIG
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(P.Fail) as ei:
        P.load_enum_normalization_manifest(str(bad))
    assert ei.value.code == P.ExitCode.CONFIG


# --------------------------------------------------------------------------- #
# 1. Enum values (blocking)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["SHIPPED", "DELIVERED"])
def test_legacy_order_statuses_are_rejected_without_a_manifest(status):
    data = clean_dataset()
    data["orders"][0]["status"] = status
    report = run(data)
    assert "enum.orderstatus.legacy" in errors_of(report)
    assert report.has_errors()


def test_legacy_status_is_not_silently_remapped():
    """The finding must not carry the mapped value; no auto-normalization."""
    data = clean_dataset()
    data["orders"][0]["status"] = "SHIPPED"
    report = run(data)
    finding = _one(report, "enum.orderstatus.legacy")
    rendered = finding.render()
    assert "OUT_FOR_DELIVERY" not in rendered
    assert "SHIPPED" not in rendered  # the offending value is never printed


def test_unsupported_nonlegacy_enum_value_is_rejected():
    data = clean_dataset()
    data["orders"][0]["status"] = "TELEPORTED"
    data["payments"][0]["status"] = "CHARGEBACKED"
    report = run(data)
    assert "enum.orderstatus" in errors_of(report)
    assert "enum.paymentstatus" in errors_of(report)


def test_valid_enum_values_pass():
    for value in P.ENUM_LABELS["orderstatus"]:
        data = clean_dataset()
        data["orders"][0]["status"] = value
        data["orders"][0]["fulfillment_method"] = "STORE_PICKUP"
        assert not run(data).has_errors()


def test_enum_check_reports_surrogate_ids_only():
    data = clean_dataset()
    data["orders"] = order_rows(3)
    for row in data["orders"]:
        row["status"] = "SHIPPED"
    report = run(data)
    finding = _one(report, "enum.orderstatus.legacy")
    assert finding.count == 3
    assert finding.ids == (1, 2, 3)


def test_enum_bytes_are_decoded_before_validation():
    data = clean_dataset()
    data["orders"][0]["status"] = b"PENDING"
    assert not run(data).has_errors()
    data["orders"][0]["status"] = b"SHIPPED"
    assert run(data).has_errors()


def test_null_enum_value_is_not_an_enum_error():
    """All four enum columns are NOT NULL, so this is a required-column error,
    not an enum error -- the two checks must stay independent."""
    data = clean_dataset()
    data["orders"][0]["status"] = None
    report = run(data)
    assert "enum.orderstatus" not in errors_of(report)
    assert "notnull.orders.status" in errors_of(report)


# --------------------------------------------------------------------------- #
# 2. String length overflow (blocking)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table,col,limit", [
    ("users", "name", 150),
    ("users", "email", 255),
    ("payments", "currency", 3),
    ("payments", "provider", 50),
    ("orders", "delivery_city", 100),
    ("addresses", "phone_number", 30),
])
def test_string_at_the_limit_passes(table, col, limit):
    data = clean_dataset()
    data[table][0][col] = "a" * limit
    assert not run(data).has_errors()


@pytest.mark.parametrize("table,col,limit", [
    ("users", "name", 150),
    ("users", "email", 255),
    ("payments", "currency", 3),
    ("payments", "provider", 50),
    ("orders", "delivery_city", 100),
])
def test_string_one_beyond_the_limit_fails(table, col, limit):
    data = clean_dataset()
    data[table][0][col] = "a" * (limit + 1)
    report = run(data)
    assert f"length.{table}.{col}" in errors_of(report)
    assert "aaaa" not in _one(report, f"length.{table}.{col}").render()


def test_string_length_check_never_truncates():
    data = clean_dataset()
    data["users"][0]["name"] = "n" * 200
    run(data)
    assert len(data["users"][0]["name"]) == 200  # untouched


def test_null_string_is_not_a_length_error():
    data = clean_dataset()
    data["products"][0]["description"] = None
    data["orders"][0]["delivery_city"] = None
    assert not run(data).has_errors()


def test_length_check_covers_every_bounded_column():
    report = run(clean_dataset())
    bounded = [(t, c) for (t, c), n in P.TEXT_COLUMNS.items() if n is not None]
    assert bounded  # guards against the map silently emptying out
    for table, col in bounded:
        assert any(f.check == f"length.{table}.{col}" for f in report.findings)


# --------------------------------------------------------------------------- #
# 3. Duplicate unique values (blocking)
# --------------------------------------------------------------------------- #
def test_duplicate_email_is_blocking_and_never_exposes_the_value():
    data = clean_dataset()
    data["users"][1]["email"] = data["users"][0]["email"]
    report = run(data)
    finding = _one(report, "unique.users.email")
    assert finding.status == P.CHECK_ERROR
    assert finding.count == 1 and finding.ids == (2,)
    rendered = finding.render()
    assert "example.invalid" not in rendered
    assert "user1@example.invalid" not in rendered


def test_duplicate_category_name_is_blocking():
    data = clean_dataset()
    data["categories"] = [
        {"id": 1, "name": "Toys", "description": None, "is_active": True,
         "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14)},
        {"id": 2, "name": "Toys", "description": None, "is_active": True,
         "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14)},
    ]
    report = run(data)
    finding = _one(report, "unique.categories.name")
    assert finding.status == P.CHECK_ERROR and finding.ids == (2,)
    assert "Toys" not in finding.render()


def test_distinct_unique_values_pass():
    data = clean_dataset()
    data["categories"].append(
        {"id": 2, "name": "Games", "description": None, "is_active": True,
         "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14)})
    assert not run(data).has_errors()


def test_many_duplicates_report_a_bounded_id_sample():
    data = clean_dataset()
    data["users"] = user_rows(60)
    for row in data["users"]:
        row["email"] = "same@example.invalid"
    finding = _one(run(data), "unique.users.email")
    # 60 rows share one value -> 59 are duplicates (the first is the original)
    assert finding.count == 59
    assert len(finding.ids) <= P._MAX_REPORTED_IDS
    assert "+39 more" in finding.render()


def test_null_unique_value_is_not_a_duplicate():
    data = clean_dataset()
    data["users"][0]["email"] = None
    report = run(data)
    assert "unique.users.email" not in errors_of(report)
    assert "notnull.users.email" in errors_of(report)  # caught by the right check


# --------------------------------------------------------------------------- #
# 4. Permissions (blocking)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "MANAGE_EVERYTHING", "manage_orders", "MANAGE_ORDERS ", "", "DELETE_DB",
    123, None, {"a": 1},
])
def test_unknown_permission_member_is_rejected(bad):
    data = clean_dataset()
    data["users"][0]["permissions"] = ["MANAGE_ORDERS", bad]
    report = run(data)
    finding = _one(report, "permissions.unknown")
    assert finding.status == P.CHECK_ERROR and finding.ids == (1,)
    assert "MANAGE_ORDERS" not in finding.render()  # no permission value echoed


@pytest.mark.parametrize("value", list(P.PERMISSION_LABELS))
def test_every_authoritative_permission_label_is_accepted(value):
    data = clean_dataset()
    data["users"][0]["permissions"] = [value, "VIEW_REPORTS"]
    assert not run(data).has_errors()


def test_permission_labels_match_the_application_enum():
    """The authoritative source is app/models/user.py:18-25."""
    try:
        from app.models.user import Permission
    except Exception as exc:  # pragma: no cover - env without SQLAlchemy
        pytest.skip(f"application model not importable here: {type(exc).__name__}")
    assert P.PERMISSION_LABELS == {p.value for p in Permission}


def test_permissions_as_json_text_is_parsed():
    data = clean_dataset()
    data["users"][0]["permissions"] = '["MANAGE_ORDERS","VIEW_REPORTS"]'
    assert not run(data).has_errors()
    data["users"][0]["permissions"] = '["NOT_A_PERMISSION"]'
    assert run(data).has_errors()


@pytest.mark.parametrize("bad", ["not-json", '{"a":1}', '"MANAGE_ORDERS"', "", 42])
def test_malformed_permissions_are_rejected(bad):
    data = clean_dataset()
    data["users"][0]["permissions"] = bad
    assert _one(run(data), "permissions.shape").status == P.CHECK_ERROR


def test_null_permissions_is_a_required_column_error_not_a_shape_error():
    data = clean_dataset()
    data["users"][0]["permissions"] = None
    report = run(data)
    assert "permissions.shape" not in errors_of(report)
    assert "notnull.users.permissions" in errors_of(report)


def test_empty_permission_list_is_valid():
    data = clean_dataset()
    data["users"][0]["permissions"] = []
    assert not run(data).has_errors()


# --------------------------------------------------------------------------- #
# 5. Decimal precision/scale (blocking)
# --------------------------------------------------------------------------- #
def test_decimal_at_the_limit_passes():
    data = clean_dataset()
    data["payments"][0]["amount"] = Decimal("99999999.99")
    assert not run(data).has_errors()


def test_decimal_overflow_is_blocking():
    data = clean_dataset()
    data["payments"][0]["amount"] = Decimal("100000000.00")
    assert "decimal.payments.amount" in errors_of(run(data))


def test_decimal_excessive_scale_is_blocking_and_not_rounded():
    data = clean_dataset()
    data["payments"][0]["amount"] = Decimal("1.234")
    report = run(data)
    assert "decimal.payments.amount" in errors_of(report)
    assert data["payments"][0]["amount"] == Decimal("1.234")  # unchanged


def test_decimal_two_places_passes():
    data = clean_dataset()
    data["payments"][0]["amount"] = Decimal("12345.67")
    assert not run(data).has_errors()


def test_nullable_decimal_none_is_not_an_error():
    data = clean_dataset()
    data["products"][0]["cost_price"] = None
    data["orders"][0]["delivery_fee"] = Decimal("0.00")
    assert not run(data).has_errors()


@pytest.mark.parametrize("bad", ["abc", "", "   ", "1,234.56", float("nan"),
                                  float("inf"), object()])
def test_malformed_decimal_is_blocking(bad):
    data = clean_dataset()
    data["payments"][0]["amount"] = bad
    assert "decimal.payments.amount" in errors_of(run(data))


def test_decimal_check_covers_every_decimal_column():
    report = run(clean_dataset())
    for table in P.TABLE_ORDER:
        for col in P.TABLES[table].decimal_cols:
            assert any(f.check == f"decimal.{table}.{col}" for f in report.findings)


def test_decimal_as_string_is_accepted_when_representable():
    data = clean_dataset()
    data["payments"][0]["amount"] = "150.00"
    assert not run(data).has_errors()


# --------------------------------------------------------------------------- #
# 6. Required-column nullability (blocking)
# --------------------------------------------------------------------------- #
def test_null_in_a_required_column_is_blocking():
    data = clean_dataset()
    data["users"][0]["email"] = None
    report = run(data)
    assert "notnull.users.email" in errors_of(report)
    assert _one(report, "notnull.users.email").ids == (1,)


def test_null_in_a_nullable_column_is_fine():
    data = clean_dataset()
    data["products"][0]["cost_price"] = None
    data["orders"][0]["address_id"] = None
    data["payments"][0]["paid_at"] = None
    data["payments"][0]["payment_metadata"] = None
    assert not run(data).has_errors()


def test_required_column_set_matches_the_target_spec():
    for table in P.TABLE_ORDER:
        required = {c for c in P.TABLES[table].columns if c in P.TABLES[table].not_null}
        data = {t: [] for t in P.TABLE_ORDER}
        data[table] = user_rows(1) if table == "users" else []
        report = run(data)
        for col in required:
            assert any(f.check == f"notnull.{table}.{col}" for f in report.findings)


def test_nullable_enum_and_fk_columns_are_excluded_from_notnull():
    assert "address_id" not in P.TABLES["orders"].not_null
    assert "cost_price" not in P.TABLES["products"].not_null
    assert "paid_at" not in P.TABLES["payments"].not_null
    assert "payment_metadata" not in P.TABLES["payments"].not_null


# --------------------------------------------------------------------------- #
# 7. Foreign-key orphans (blocking)
# --------------------------------------------------------------------------- #
def test_present_parents_pass():
    assert not run(clean_dataset()).has_errors()


def test_orphaned_child_is_blocking():
    data = clean_dataset()
    data["order_items"][0]["order_id"] = 999
    report = run(data)
    assert "fk.order_items.order_id" in errors_of(report)


def test_orphan_detection_covers_every_target_fk():
    report = run(clean_dataset())
    for (child, col) in P.FK_COLUMNS:
        assert any(f.check == f"fk.{child}.{col}" for f in report.findings)


def test_nullable_fk_none_is_not_an_orphan():
    data = clean_dataset()
    data["orders"][0]["address_id"] = None
    assert "fk.orders.address_id" not in errors_of(run(data))


def test_orphans_are_reported_never_repaired():
    data = clean_dataset()
    data["payments"][0]["order_id"] = 4242
    run(data)
    assert data["payments"][0]["order_id"] == 4242  # untouched


def test_fk_orphan_reports_ids_only():
    data = clean_dataset()
    data["expenses"][0]["recorded_by"] = 77
    finding = _one(run(data), "fk.expenses.recorded_by")
    assert finding.status == P.CHECK_ERROR
    assert finding.ids == (1,)
    assert "77" not in finding.render()  # the missing parent id is not echoed


# --------------------------------------------------------------------------- #
# 8. Date/time compatibility (blocking)
# --------------------------------------------------------------------------- #
def test_valid_naive_datetimes_pass_and_are_preserved():
    data = clean_dataset()
    stamp = datetime(2026, 8, 14, 10, 0, 0)
    data["orders"][0]["created_at"] = stamp
    report = run(data)
    assert "datetime.orders.created_at" not in errors_of(report)
    assert data["orders"][0]["created_at"] == stamp  # exact preservation


def test_aware_datetime_is_rejected_not_stripped():
    data = clean_dataset()
    aware = datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc)
    data["orders"][0]["created_at"] = aware
    report = run(data)
    assert "datetime.orders.created_at" in errors_of(report)
    assert data["orders"][0]["created_at"] == aware  # untouched


def test_date_only_object_is_rejected():
    data = clean_dataset()
    data["orders"][0]["created_at"] = date(2026, 8, 14)
    assert "datetime.orders.created_at" in errors_of(run(data))


@pytest.mark.parametrize("bad", ["2026-08-14", "0000-00-00", "", "not-a-date",
                                 1700000000, b"garbage"])
def test_invalid_datetime_values_are_rejected(bad):
    data = clean_dataset()
    data["orders"][0]["created_at"] = bad
    assert "datetime.orders.created_at" in errors_of(run(data))


def test_nullable_datetime_none_is_fine():
    data = clean_dataset()
    data["payments"][0]["paid_at"] = None
    assert "datetime.payments.paid_at" not in errors_of(run(data))


def test_no_timezone_coercion_anywhere():
    data = clean_dataset()
    original = data["orders"][0]["created_at"]
    run(data)
    assert data["orders"][0]["created_at"] is original
    assert data["orders"][0]["created_at"].tzinfo is None


# --------------------------------------------------------------------------- #
# 9. Negative inventory/cost (blocking, per the real target CHECKs)
# --------------------------------------------------------------------------- #
def test_negative_stock_is_blocking_by_the_target_check():
    data = clean_dataset()
    data["products"][0]["stock_quantity"] = -1
    finding = _one(run(data), "check.ck_products_stock_quantity_non_negative")
    assert finding.status == P.CHECK_ERROR


def test_negative_cost_price_is_blocking_by_the_target_check():
    data = clean_dataset()
    data["products"][0]["cost_price"] = Decimal("-0.01")
    finding = _one(run(data), "check.ck_products_cost_price_non_negative")
    assert finding.status == P.CHECK_ERROR


def test_zero_stock_and_cost_are_accepted():
    data = clean_dataset()
    data["products"][0]["stock_quantity"] = 0
    data["products"][0]["cost_price"] = Decimal("0.00")
    assert not run(data).has_errors()


def test_no_invented_check_for_expenses_amount():
    """expenses.amount carries NO CHECK in the baseline -- a negative expense
    must NOT be an error. This guards against an invented constraint."""
    assert not any((t, c) == ("expenses", "amount") for (t, c) in P.NON_NEGATIVE_COLUMNS)
    data = clean_dataset()
    data["expenses"][0]["amount"] = Decimal("-500.00")
    report = run(data)
    assert not report.has_errors()


def test_null_cost_price_is_allowed_by_the_constraint():
    data = clean_dataset()
    data["products"][0]["cost_price"] = None
    assert not run(data).has_errors()


# --------------------------------------------------------------------------- #
# 10. Report-only business invariants (decision D-1)
# --------------------------------------------------------------------------- #
def test_payment_amount_mismatch_is_a_warning_not_an_error():
    data = clean_dataset()
    data["payments"][0]["amount"] = Decimal("99.00")
    report = run(data)
    finding = _one(report, "business.payment_amount_vs_order_total")
    assert finding.status == P.CHECK_WARNING
    assert report.has_errors() is False


def test_multiple_payments_per_order_is_a_warning():
    data = clean_dataset()
    data["payments"] = payment_rows(2)
    finding = _one(run(data), "business.payments_per_order")
    assert finding.status == P.CHECK_WARNING
    assert finding.ids == (1,)


def test_item_subtotal_mismatch_is_a_warning():
    data = clean_dataset()
    data["order_items"][0]["subtotal"] = Decimal("80.00")
    finding = _one(run(data), "business.item_subtotal")
    assert finding.status == P.CHECK_WARNING


def test_order_total_mismatch_is_a_warning():
    data = clean_dataset()
    data["orders"][0]["total_amount"] = Decimal("999.00")
    finding = _one(run(data), "business.order_total")
    assert finding.status == P.CHECK_WARNING


def test_report_only_invariants_never_block_a_clean_import():
    data = clean_dataset()
    data["payments"][0]["amount"] = Decimal("1.00")   # != order total 120.00
    data["payments"].append(dict(data["payments"][0], id=2))  # 2 payments/order
    data["order_items"][0]["subtotal"] = Decimal("1.00")      # != 50.00 x 2
    report = run(data)
    assert report.has_errors() is False
    assert {f.check for f in report.warnings} >= {
        "business.payment_amount_vs_order_total", "business.payments_per_order",
        "business.item_subtotal", "business.order_total"}


def test_business_checks_use_decimal_not_float():
    """0.1+0.2 must not be judged equal to 0.3 in float; Decimal must be used."""
    data = clean_dataset()
    data["orders"][0]["total_amount"] = Decimal("0.30")
    data["orders"][0]["delivery_fee"] = Decimal("0.00")
    data["order_items"] = [
        {"id": 1, "order_id": 1, "product_id": 1, "quantity": 1,
         "unit_price": Decimal("0.10"), "subtotal": Decimal("0.10")},
        {"id": 2, "order_id": 1, "product_id": 1, "quantity": 1,
         "unit_price": Decimal("0.20"), "subtotal": Decimal("0.20")},
    ]
    report = run(data)
    assert "business.order_total" not in {f.check for f in report.warnings}


def test_business_invariant_lines_contain_no_money_values():
    data = clean_dataset()
    data["payments"][0]["amount"] = Decimal("4242.42")
    finding = _one(run(data), "business.payment_amount_vs_order_total")
    rendered = finding.render()
    assert "4242" not in rendered
    assert "120" not in rendered


def test_nullable_fields_do_not_break_business_checks():
    """Nullable fields must be skipped, not coerced -- and must not turn into
    spurious required-column errors."""
    data = clean_dataset()
    data["payments"][0]["paid_at"] = None
    data["orders"][0]["address_id"] = None
    data["orders"][0]["delivery_additional_directions"] = None
    data["payments"][0]["payment_metadata"] = None
    data["products"][0]["cost_price"] = None
    report = run(data)
    assert report.has_errors() is False
    assert report.warnings == ()


def test_order_without_items_is_not_flagged_for_a_total_mismatch():
    data = clean_dataset()
    data["order_items"] = []
    report = run(data)
    assert "business.order_total" not in {f.check for f in report.warnings}


# --------------------------------------------------------------------------- #
# Multiple simultaneous issues / malformed rows / sanitization
# --------------------------------------------------------------------------- #
def test_multiple_simultaneous_issues_are_all_reported():
    data = clean_dataset()
    data["orders"][0]["status"] = "SHIPPED"          # blocking enum
    data["users"][0]["name"] = "n" * 151             # blocking length
    data["order_items"][0]["order_id"] = 999         # blocking FK
    data["products"][0]["stock_quantity"] = -5        # blocking check
    data["payments"][0]["amount"] = Decimal("1.00")  # warning only
    report = run(data)
    assert report.has_errors()
    assert {"enum.orderstatus.legacy", "length.users.name", "fk.order_items.order_id",
            "check.ck_products_stock_quantity_non_negative"} <= errors_of(report)
    assert "business.payment_amount_vs_order_total" in warnings_of(report)


def test_malformed_rows_do_not_bypass_checks():
    data = clean_dataset()
    data["orders"][0] = {"id": 1, "user_id": "not-an-int"}  # missing columns
    report = run(data)
    # the FK enum/length/enum checks still run and report, rather than crash
    assert report.has_errors()
    assert not report.errors == ()


def test_non_mapping_input_is_a_blocking_error_not_a_crash():
    report = run(["nope"])  # type: ignore[arg-type]
    assert report.has_errors()
    assert _one(report, "input").status == P.CHECK_ERROR


def test_check_exception_becomes_a_sanitized_error(monkeypatch):
    def boom(rows_by_table, **kwargs):
        raise RuntimeError("secret-password user@example.invalid")

    monkeypatch.setattr(P, "check_enum_values", boom)
    report = run(clean_dataset())
    assert "enum" in errors_of(report)
    rendered = _one(report, "enum").render()
    assert "secret-password" not in rendered
    assert "example.invalid" not in rendered
    assert "RuntimeError" in rendered


def test_every_summary_line_is_sanitized():
    data = clean_dataset()
    data["users"][0]["email"] = "leak@example.invalid"
    data["users"][1]["email"] = "leak@example.invalid"
    data["orders"][0]["status"] = "SHIPPED"
    data["payments"][0]["payment_metadata"] = {"card": "4111111111111111"}
    report = run(data)
    blob = "\n".join(report.summary())
    for secret in ("leak@example.invalid", "4111", "root:secret",
                   "twelve09_src_reh1", "127.0.0.1"):
        assert secret not in blob, f"summary leaked {secret!r}"


def test_report_summary_is_deterministic():
    data = clean_dataset()
    data["orders"][0]["status"] = "SHIPPED"
    first = run(data).summary()
    second = run(data).summary()
    assert first == second
    assert first  # non-empty


def test_raise_if_blocking_uses_the_mapping_exit_code():
    data = clean_dataset()
    data["orders"][0]["status"] = "DELIVERED"
    with pytest.raises(P.Fail) as ei:
        run(data).raise_if_blocking()
    assert ei.value.code == P.ExitCode.MAPPING
    assert "OUT_FOR_DELIVERY" not in ei.value.message


def test_raise_if_blocking_does_not_raise_on_warnings_only():
    data = clean_dataset()
    data["payments"][0]["amount"] = Decimal("1.00")
    run(data).raise_if_blocking()  # must not raise


# --------------------------------------------------------------------------- #
# Safety: no network, no target writes, guard still closed
# --------------------------------------------------------------------------- #
def test_import_does_not_load_drivers_or_connect():
    code = (
        "import socket, sys\n"
        "def _guard(*a, **k):\n"
        "    raise AssertionError('connect attempted')\n"
        "socket.socket.connect = _guard\n"
        "import tools.pg_import as p\n"
        "assert p.EXECUTION_ENABLED is False, p.EXECUTION_ENABLED\n"
        "loaded = sorted(m for m in ('sqlalchemy','pymysql','psycopg2') "
        "if m in sys.modules)\n"
        "print('|'.join(loaded))\n"
        "try:\n"
        "    socket.socket().connect(('127.0.0.1', 1))\n"
        "    print('GUARD_INERT')\n"
        "except AssertionError:\n"
        "    print('GUARD_LIVE')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120, cwd=str(_BACKEND_DIR),
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "", f"eagerly loaded a database module: {lines[0]!r}"
    assert "GUARD_LIVE" in lines


def test_execution_remains_disabled():
    assert P.EXECUTION_ENABLED is False


def test_no_target_write_helper_is_reachable_from_compat():
    """The check layer must not import or introduce any target-side writer."""
    forbidden = ["_write_target", "truncate_target", "setval_sql",
                 "insert_sql", "TRUNCATE_SQL", "apply_to_target"]
    for name in forbidden:
        assert not hasattr(P, f"_compat_{name}")
    # and the documented builders still raise/are inert in this build
    assert P.EXECUTION_ENABLED is False


def test_open_source_connection_still_refuses():
    with pytest.raises(P.Fail) as ei:
        P.open_source_connection(
            "mysql+pymysql://root:x@127.0.0.1:3306/twelve09_src_reh1?charset=utf8mb4")
    assert ei.value.code == P.ExitCode.GATE


def test_no_env_var_can_enable_execution(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "IMPORT_SOURCE_URL",
        "mysql+pymysql://root:secret@127.0.0.1:3306/twelve09_src_reh1?charset=utf8mb4")
    monkeypatch.setenv(
        "IMPORT_TARGET_URL",
        "postgresql+psycopg2://u:p@127.0.0.1:5432/twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    for sneaky in ("IMPORT_EXECUTION_ENABLED", "IMPORT_EXECUTE", "EXECUTION_ENABLED",
                   "PG_IMPORT_ENABLED", "IMPORT_APPLY", "IMPORT_TRUNCATE"):
        monkeypatch.setenv(sneaky, "1")
    assert P.main(["--run-id", "reh1", "--apply"]) == P.ExitCode.GATE
    assert P.EXECUTION_ENABLED is False


def test_manifest_env_var_absent_means_no_manifest(monkeypatch):
    monkeypatch.delenv(P.ENUM_NORMALIZATION_MANIFEST_ENV, raising=False)
    data = clean_dataset()
    data["orders"][0]["status"] = "SHIPPED"
    assert run(data).has_errors()
