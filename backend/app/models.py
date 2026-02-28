from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func

from app.database import Base


class Case(Base):
    __tablename__ = "cases"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Bom(Base):
    __tablename__ = "bom"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    bom_id = Column(String(255), nullable=False)
    parent_id = Column(String(255), nullable=False)
    child_id = Column(String(255), nullable=False)
    elem_ix = Column(Integer)
    alt_group = Column(String(255))
    rate = Column(Float)


class Customer(Base):
    __tablename__ = "customer"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    customer = Column(String(255), nullable=False)
    description = Column(String(512))


class Location(Base):
    __tablename__ = "location"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    location_id = Column(String(255), nullable=False)
    location_description = Column(String(512))


class Product(Base):
    __tablename__ = "product"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(String(255), nullable=False)
    description = Column(String(512))


class Vendor(Base):
    __tablename__ = "vendor"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    vendor_id = Column(String(255), nullable=False)


class Demand(Base):
    __tablename__ = "demand"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    demand_id = Column(String(255), nullable=False)  # ID from CSV
    description = Column(String(512))
    customer_id = Column(String(255), nullable=False)
    priority = Column(Integer)
    request_due_time = Column(String(64))
    product_id = Column(String(255), nullable=False)
    location_id = Column(String(255))  # e.g. VIRTUAL; demand as fully-fledged inventory (product|location)
    quantity = Column(Float, nullable=False)


class MethodBuy(Base):
    __tablename__ = "method_buy"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(String(255), nullable=False)
    location_id = Column(String(255), nullable=False)
    preference = Column(Integer)
    lead_days_supply = Column(Integer)
    cycle_days_supply = Column(Integer)
    vendor_id = Column(String(255))


class MethodMake(Base):
    __tablename__ = "method_make"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    bom_id = Column(String(255), nullable=False)
    product_id = Column(String(255), nullable=False)
    location_id = Column(String(255), nullable=False)
    preference = Column(Integer)
    lead_time = Column(Integer)  # days (0 for virtual products)


class ProductLocation(Base):
    __tablename__ = "productlocation"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(String(255), nullable=False)
    description = Column(String(512))
    location_id = Column(String(255), nullable=False)
    max_lot_size = Column(Float)
    prod_area = Column(String(255))


class Supply(Base):
    __tablename__ = "supply"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    supply_id = Column(String(255), nullable=False)
    description = Column(String(512))
    vendor_id = Column(String(255))
    location_id = Column(String(255))
    product_id = Column(String(255), nullable=False)
    supply_date = Column(String(64))
    qty = Column(Float, nullable=False)


class MethodMove(Base):
    __tablename__ = "method_move"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(String(255), nullable=False)
    from_location_id = Column(String(255), nullable=False)
    to_location_id = Column(String(255), nullable=False)
    transit_time = Column(Float)
    transit_time_uom = Column(String(32))
    preference = Column(Integer)


class AllocationRun(Base):
    __tablename__ = "allocation_run"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    status = Column(String(64), nullable=False)  # success, failed, partial
    config = Column(JSON)  # e.g. customer weights


class AllocationAction(Base):
    __tablename__ = "allocation_action"
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("allocation_run.id", ondelete="CASCADE"), nullable=False)
    variant_key = Column(String(512), nullable=False)  # e.g. product_id|location_id
    req_component_ids = Column(JSON, nullable=False)  # list of component keys (product_id|location_id)
    req_rates = Column(JSON)  # list of BOM rates (same order as req_component_ids); need_i = qty/rate_i
    qty = Column(Float, nullable=False)
    demand_id = Column(String(255))  # demand line id if this serves a demand
    target_product_id = Column(String(255))
    target_location_id = Column(String(255))
    output_period = Column(Integer)  # period when this output becomes available (0 = preexisting)
    edge_type = Column(String(32))  # "make" or "move" (inventory graph edge)
    scarcity_rank = Column(Integer)  # order of component in scarcity_order when this action was emitted (for replay)


class ManualOverride(Base):
    __tablename__ = "manual_override"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    entity_type = Column(String(64), nullable=False)  # supply, demand, allocation
    entity_key = Column(String(512), nullable=False)
    payload = Column(JSON, nullable=False)  # e.g. {"quantity": 100}, {"assigned_demand_id": "..."}
