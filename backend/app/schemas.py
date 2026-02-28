from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel


class CaseCreate(BaseModel):
    name: str


class CaseUpdate(BaseModel):
    name: Optional[str] = None


class CaseResponse(BaseModel):
    id: int
    name: str
    created_at: datetime

    class Config:
        from_attributes = True


class CaseDetailResponse(CaseResponse):
    demand_count: Optional[int] = None
    supply_count: Optional[int] = None
    run_count: Optional[int] = None


class AllocationRunResponse(BaseModel):
    id: int
    case_id: int
    created_at: datetime
    status: str
    config: Optional[dict] = None

    class Config:
        from_attributes = True


class AllocationActionResponse(BaseModel):
    id: int
    run_id: int
    variant_key: str
    req_component_ids: list
    req_rates: Optional[list] = None  # BOM rates (same order as req_component_ids); need_i = qty/rate_i
    qty: float
    demand_id: Optional[str] = None
    target_product_id: Optional[str] = None
    target_location_id: Optional[str] = None
    output_period: Optional[int] = None  # period when output is available (0 = preexisting)
    edge_type: Optional[str] = None  # "make" | "move" (inventory graph edge)

    class Config:
        from_attributes = True


class ManualOverrideCreate(BaseModel):
    entity_type: str
    entity_key: str
    payload: dict


class ManualOverrideResponse(BaseModel):
    id: int
    case_id: int
    entity_type: str
    entity_key: str
    payload: dict

    class Config:
        from_attributes = True


class FeasibleDemandSummary(BaseModel):
    demand_id: str
    product_id: str
    requested_qty: float
    allocated_qty: float
    status: str  # fulfilled, partial, unfulfilled
