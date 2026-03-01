"""Load case data from DB into in-memory structures for the allocation engine."""
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
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
    ManualOverride,
)


def _f(r, key, default=None):
    v = getattr(r, key, None)
    return v if v is not None else default


def load_case_data(db: Session, case_id: int) -> dict[str, Any]:
    """Load all case entities into dicts/lists keyed by case_id."""
    boms = db.query(Bom).filter(Bom.case_id == case_id).all()
    customers = db.query(Customer).filter(Customer.case_id == case_id).all()
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    locations = db.query(Location).filter(Location.case_id == case_id).all()
    method_buy = db.query(MethodBuy).filter(MethodBuy.case_id == case_id).all()
    method_make = db.query(MethodMake).filter(MethodMake.case_id == case_id).all()
    products = db.query(Product).filter(Product.case_id == case_id).all()
    productlocations = db.query(ProductLocation).filter(ProductLocation.case_id == case_id).all()
    supplies = db.query(Supply).filter(Supply.case_id == case_id).all()
    method_moves = db.query(MethodMove).filter(MethodMove.case_id == case_id).all()
    vendors = db.query(Vendor).filter(Vendor.case_id == case_id).all()
    overrides = db.query(ManualOverride).filter(ManualOverride.case_id == case_id).all()

    cust_by_id = {r.customer: (r.description or r.customer) for r in customers}
    return {
        "bom": [{"bom_id": r.bom_id, "parent_id": r.parent_id, "child_id": r.child_id, "rate": r.rate or 0, "alt_group": getattr(r, "alt_group", None)} for r in boms],
        "customer": [{"customer": r.customer, "description": r.description} for r in customers],
        "demand": [
            {
                "demand_id": r.demand_id,
                "description": r.description,
                "customer_id": r.customer_id,
                "customer": cust_by_id.get(r.customer_id, r.customer_id),
                "priority": r.priority or 0,
                "request_due_time": r.request_due_time,
                "product_id": r.product_id,
                "location_id": getattr(r, "location_id", None) or "VIRTUAL",
                "quantity": float(r.quantity or 0),
            }
            for r in demands
        ],
        "location": [{"location_id": r.location_id} for r in locations],
        "method_buy": [
            {
                "product_id": r.product_id,
                "location_id": r.location_id,
                "preference": r.preference,
                "lead_days_supply": r.lead_days_supply,
                "cycle_days_supply": r.cycle_days_supply,
                "vendor_id": r.vendor_id,
            }
            for r in method_buy
        ],
        "method_make": [
            {"bom_id": r.bom_id, "product_id": r.product_id, "location_id": r.location_id, "preference": r.preference or 0, "lead_time": getattr(r, "lead_time", None)}
            for r in method_make
        ],
        "product": [{"product_id": r.product_id, "description": r.description} for r in products],
        "productlocation": [
            {
                "product_id": r.product_id,
                "location_id": r.location_id,
                "max_lot_size": r.max_lot_size,
                "prod_area": r.prod_area,
            }
            for r in productlocations
        ],
        "supply": [
            {
                "supply_id": r.supply_id,
                "product_id": r.product_id,
                "location_id": r.location_id or "",
                "supply_date": getattr(r, "supply_date", None),
                "qty": float(r.qty or 0),
            }
            for r in supplies
        ],
        "method_move": [
            {
                "product_id": r.product_id,
                "from_location_id": r.from_location_id,
                "to_location_id": r.to_location_id,
                "transit_time": r.transit_time,
                "transit_time_uom": r.transit_time_uom,
                "preference": r.preference or 0,
            }
            for r in method_moves
        ],
        "vendor": [{"vendor_id": r.vendor_id} for r in vendors],
        "overrides": [
            {"entity_type": r.entity_type, "entity_key": r.entity_key, "payload": r.payload or {}}
            for r in overrides
        ],
    }
