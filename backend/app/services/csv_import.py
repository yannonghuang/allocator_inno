import csv
import io
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (
    Case,
    Bom,
    Customer,
    Demand,
    Location,
    MethodBuy,
    MethodMake,
    Product,
    ProductLocation,
    Supply,
    MethodMove,
    Vendor,
)


def _read_csv_path(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _read_csv_content(content: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    return list(reader)


def _float_or_none(v: str):
    if v is None or v == "" or v.upper() == "NULL":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int_or_none(v: str):
    if v is None or v == "" or v.upper() == "NULL":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def import_case_from_folder(db: Session, case_id: int, folder: Path) -> None:
    tables = [
        ("bom.csv", _import_bom, Bom),
        ("customer.csv", _import_customer, Customer),
        ("location.csv", _import_location, Location),
        ("product.csv", _import_product, Product),
        ("vendor.csv", _import_vendor, Vendor),
        ("demand.csv", _import_demand, Demand),
        ("method_buy.csv", _import_method_buy, MethodBuy),
        ("method_make.csv", _import_method_make, MethodMake),
        ("productlocation.csv", _import_productlocation, ProductLocation),
        ("supply.csv", _import_supply, Supply),
        ("method_move.csv", _import_method_move, MethodMove),
    ]
    for filename, importer, model in tables:
        path = folder / filename
        if path.exists():
            db.query(model).filter(model.case_id == case_id).delete()
            rows = _read_csv_path(path)
            importer(db, case_id, rows)
    db.commit()


def _import_bom(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(Bom(
            case_id=case_id,
            bom_id=r.get("BOM_ID", ""),
            parent_id=r.get("PARENT_ID", ""),
            child_id=r.get("CHILD_ID", ""),
            elem_ix=_int_or_none(r.get("ELEM_IX")),
            alt_group=r.get("ALT_GROUP") or None,
            rate=_float_or_none(r.get("RATE")),
        ))


def _import_customer(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(Customer(
            case_id=case_id,
            customer=r.get("CUSTOMER", ""),
            description=r.get("DESCRIPTION") or None,
        ))


def _import_location(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(Location(
            case_id=case_id,
            location_id=r.get("LOCATION_ID", ""),
            location_description=r.get("LOCATION_DESCRIPTION") or None,
        ))


def _import_product(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(Product(
            case_id=case_id,
            product_id=r.get("PRODUCT_ID", ""),
            description=r.get("DESCRIPTION") or None,
        ))


def _import_vendor(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(Vendor(
            case_id=case_id,
            vendor_id=r.get("VENDOR_ID", ""),
        ))


def _import_demand(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(Demand(
            case_id=case_id,
            demand_id=r.get("ID", ""),
            description=r.get("DESCRIPTION") or None,
            customer_id=r.get("CUSTOMER_ID", ""),
            priority=_int_or_none(r.get("PRIORITY")),
            request_due_time=r.get("REQUEST_DUE_TIME") or None,
            product_id=r.get("PRODUCT_ID", ""),
            location_id=r.get("LOCATION") or r.get("LOCATION_ID") or "VIRTUAL",
            quantity=_float_or_none(r.get("QUANTITY")) or 0,
        ))


def _import_method_buy(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(MethodBuy(
            case_id=case_id,
            product_id=r.get("PRODUCT_ID", ""),
            location_id=r.get("LOCATION_ID", ""),
            preference=_int_or_none(r.get("PREFERENCE")),
            lead_days_supply=_int_or_none(r.get("LEAD_DAYS_SUPPLY")),
            cycle_days_supply=_int_or_none(r.get("CYCLE_DAYS_SUPPLY")),
            vendor_id=r.get("VENDOR_ID") or None,
        ))


def _import_method_make(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(MethodMake(
            case_id=case_id,
            bom_id=r.get("BOM_ID", ""),
            product_id=r.get("PRODUCT_ID", ""),
            location_id=r.get("LOCATION_ID", ""),
            preference=_int_or_none(r.get("PREFERENCE")),
            lead_time=_int_or_none(r.get("LEAD_TIME")),
        ))


def _import_productlocation(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(ProductLocation(
            case_id=case_id,
            product_id=r.get("PRODUCT_ID", ""),
            description=r.get("DESCRIPTION") or None,
            location_id=r.get("LOCATION_ID", ""),
            max_lot_size=_float_or_none(r.get("MAX_LOT_SIZE")),
            prod_area=r.get("PROD_AREA") or None,
        ))


def _import_supply(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        qty = r.get("QTY")
        if qty is not None and isinstance(qty, str):
            if "e+" in qty.lower():
                qty = float(qty)
            else:
                qty = _float_or_none(qty)
        if qty is None:
            qty = 0
        db.add(Supply(
            case_id=case_id,
            supply_id=r.get("SUPPLY_ID", ""),
            description=r.get("DESCRIPTION") or None,
            vendor_id=r.get("VENDOR_ID") or None,
            location_id=r.get("LOCATION_ID") or None,
            product_id=r.get("PRODUCT_ID", ""),
            supply_date=r.get("SUPPLY_DATE") or None,
            qty=qty,
        ))


def _import_method_move(db: Session, case_id: int, rows: list[dict]) -> None:
    for r in rows:
        db.add(MethodMove(
            case_id=case_id,
            product_id=r.get("PRODUCT_ID", ""),
            from_location_id=r.get("FROM_LOCATION_ID", ""),
            to_location_id=r.get("TO_LOCATION_ID", ""),
            transit_time=_float_or_none(r.get("TRANSIT_TIME")),
            transit_time_uom=r.get("TRANSIT_TIME_UOM") or None,
            preference=_int_or_none(r.get("PREFERENCE")),
        ))
