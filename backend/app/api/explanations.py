"""Explainability: why a supply is split among multiple demands; allocation split logic for critical component."""
from collections import defaultdict
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Case, AllocationRun, AllocationAction, Demand, Supply
from app.services.case_loader import load_case_data
from app.services.time_utils import build_period_index, demand_due_period

router = APIRouter(prefix="/cases", tags=["Explainability"])

# Variant = (product_id, location_id)
Variant = tuple[str, str]


def _comp_key_to_at(ck: str) -> str:
    if "|" in ck:
        a, b = ck.split("|", 1)
        return f"{a}@{b}"
    return ck


@router.get("/{case_id}/runs/{run_id}/explanations")
def get_explanations(
    case_id: int,
    run_id: int,
    supply_id: str = Query(..., description="Supply ID or component key (product_id|location_id)"),
    db: Session = Depends(get_db),
):
    """Return why the given supply is split across demands: list of demand ids, quantities, and reason."""
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).all()
    # If supply_id looks like product_id|location_id, use as component key; else resolve supply to (product_id, location_id)
    if "|" in supply_id:
        comp_key = supply_id
    else:
        from app.models import Supply
        s = db.query(Supply).filter(Supply.case_id == case_id, Supply.supply_id == supply_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Supply not found")
        comp_key = f"{s.product_id}|{s.location_id or ''}"
    # Find actions that consume this component (comp_key in req_component_ids)
    demand_qty: dict[str, float] = {}
    for a in actions:
        if comp_key not in (a.req_component_ids or []):
            continue
        # This action used this supply; it serves target product -> map to demands for that product
        pid = a.target_product_id
        if not pid:
            continue
        demands = db.query(Demand).filter(Demand.case_id == case_id, Demand.product_id == pid).order_by(Demand.priority).all()
        qty = float(a.qty or 0)
        for d in demands:
            if qty <= 0:
                break
            req = float(d.quantity or 0)
            give = min(qty, req)
            if give > 0:
                demand_qty[d.demand_id] = demand_qty.get(d.demand_id, 0) + give
                qty -= give
    items = [
        {
            "demand_id": did,
            "quantity": round(qty, 4),
            "reason": "Proportional allocation by scarcity order; this supply was consumed by variant(s) serving this demand.",
        }
        for did, qty in demand_qty.items()
    ]
    return {"supply_id": supply_id, "component_key": comp_key, "split": items}


@router.get("/{case_id}/runs/{run_id}/allocation-explanation")
def get_allocation_explanation(
    case_id: int,
    run_id: int,
    component_key: str = Query(..., description="Component key (product_id|location_id)"),
    to_variant_key: str = Query(None, description="Optional: filter to this produced variant"),
    db: Session = Depends(get_db),
):
    """Explain split logic for a critical component: candidate targets, available at step, and how qty was split."""
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    data = load_case_data(db, case_id)
    # Candidate targets = variants (edges) that consume this component
    method_make_list = data.get("method_make", [])
    method_move_list = data.get("method_move", [])
    bom_list = data.get("bom", [])
    bom_by_parent = defaultdict(list)
    for b in bom_list:
        bom_by_parent[b["parent_id"]].append(b)
    comp_pid = component_key.split("|", 1)[0] if component_key else ""
    comp_loc = component_key.split("|", 1)[1] if "|" in component_key else ""
    candidate_targets = []
    for m in method_make_list:
        pid, loc = m["product_id"], m["location_id"]
        children = bom_by_parent.get(pid, [])
        req = [(c["child_id"], loc) for c in children]
        if (comp_pid, comp_loc) in req:
            candidate_targets.append({"variant_key": f"{pid}|{loc}", "variant_display": _comp_key_to_at(f"{pid}|{loc}"), "edge_type": "make"})
    # Build full variants_req for target weight computation
    variants_req: dict[Variant, list[tuple[str, str]]] = {}
    for m in method_make_list:
        pid, loc = m["product_id"], m["location_id"]
        children = bom_by_parent.get(pid, [])
        req = [(c["child_id"], loc) for c in children]
        if req:
            variants_req[(pid, loc)] = req
    for mv in method_move_list:
        pid = mv["product_id"]
        from_loc = mv.get("from_location_id") or ""
        to_loc = mv.get("to_location_id") or ""
        if from_loc == to_loc:
            continue
        key_from = f"{pid}|{from_loc}"
        if key_from == component_key:
            candidate_targets.append({"variant_key": f"{pid}|{to_loc}", "variant_display": _comp_key_to_at(f"{pid}|{to_loc}"), "edge_type": "move"})
        if (pid, to_loc) not in variants_req:
            variants_req[(pid, to_loc)] = [(pid, from_loc)]
    # Deduplicate candidate targets by (variant_key, edge_type)
    seen_target: set[tuple[str, str]] = set()
    unique_targets: list[dict] = []
    for ct in candidate_targets:
        key = (ct["variant_key"], ct["edge_type"])
        if key not in seen_target:
            seen_target.add(key)
            unique_targets.append(ct)
    candidate_targets = unique_targets
    all_targets = set(variants_req.keys())
    demand_product_to_location = {}
    for m in method_make_list:
        pid, loc = m["product_id"], m["location_id"]
        if pid not in demand_product_to_location:
            demand_product_to_location[pid] = loc
    # Demand weights: unmet per (variant, customer), then target_weight = sum(unmet * customer_weight)
    supply_list = data.get("supply", [])
    demand_list_raw = data.get("demand", [])
    date_to_period, _ = build_period_index(supply_list, demand_list_raw)
    demand_list = []
    for d in demand_list_raw:
        due = demand_due_period(d.get("request_due_time"), date_to_period)
        demand_list.append({**d, "due_period": due})
    overrides = data.get("overrides", [])
    demand_adj = {}
    for o in overrides:
        if o.get("entity_type") == "demand" and "quantity" in (o.get("payload") or {}):
            key = o.get("entity_key", "")
            if not key.startswith("demand|"):
                key = f"demand|{key}"
            demand_adj[key] = float((o.get("payload") or {}).get("quantity", 0))
    unmet: dict[tuple[Variant, str], float] = defaultdict(float)
    for d in demand_list:
        pid, cust = d["product_id"], d["customer_id"]
        qty = float(d.get("quantity") or 0)
        loc = d.get("location_id") or demand_product_to_location.get(pid)
        if loc is None:
            continue
        adj = demand_adj.get(f"demand|{d['demand_id']}", 0)
        qty = max(0, qty + adj)
        v = (pid, loc)
        unmet[(v, cust)] += qty
    customer_weights = (run.config or {}) if isinstance(run.config, dict) else {}
    for d in demand_list:
        cid = d["customer_id"]
        if cid not in customer_weights:
            customer_weights[cid] = 1.0
    target_weight: dict[Variant, float] = {}
    for (v, cust), q in unmet.items():
        if q > 0:
            target_weight[v] = target_weight.get(v, 0) + q * customer_weights.get(cust, 1.0)
    downstream: dict[Variant, set[Variant]] = defaultdict(set)
    for v, req in variants_req.items():
        for c in req:
            if c in all_targets:
                downstream[c].add(v)
    order = []
    seen = set()
    def visit(t: Variant):
        if t in seen:
            return
        seen.add(t)
        for t2 in downstream.get(t, set()):
            visit(t2)
        order.append(t)
    for t in all_targets:
        visit(t)
    order.reverse()
    for t in order:
        if t in target_weight:
            continue
        target_weight[t] = sum(target_weight.get(t2, 0) for t2 in downstream.get(t, set()))
    # Attach target_weight and weight_calculation to each candidate
    demanded_variants = {v for (v, _), q in unmet.items() if q > 0}
    for ct in candidate_targets:
        vk = ct["variant_key"]
        v = (vk.split("|", 1)[0], vk.split("|", 1)[1] if "|" in vk else "")
        w = round(target_weight.get(v, 0), 4)
        ct["target_weight"] = w
        if v in demanded_variants:
            ct["weight_calculation"] = "Demanded variant: Σ(unmet quantity × customer weight) over customers with demand for this product."
        else:
            ct["weight_calculation"] = "Non-demanded: downstream_value = Σ target_weight of demanded variants reachable from this variant (via BOM/move edges)."
    total_w = sum(ct.get("target_weight", 0) for ct in candidate_targets)
    weight_formula = (
        "Target weight (demanded variant) = Σ unmet(customer) × customer_weight. "
        "Target weight (non-demanded variant) = downstream_value = Σ target_weight of demanded targets reachable from it. "
        "Split of component = (target_weight / total_target_weight) × available."
    )
    # Supply overrides (same keying as engine): comp_key -> quantity adjustment
    supply_adj: dict[str, float] = {}
    for o in overrides:
        if o.get("entity_type") == "supply" and "quantity" in (o.get("payload") or {}):
            key = o.get("entity_key", "")
            supply_adj[key] = float((o.get("payload") or {}).get("quantity", 0))
    # Simulate available and collect steps (each action that used this component)
    supplies = db.query(Supply).filter(Supply.case_id == case_id).all()
    available: dict[str, float] = {}
    for s in supplies:
        key = f"{s.product_id}|{s.location_id or ''}"
        available[key] = available.get(key, 0) + float(s.qty or 0) + supply_adj.get(key, 0)
    actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).all()
    # Replay in scarcity order (same as engine); old runs without scarcity_rank sort by id
    def _action_order(a) -> tuple:
        rank = getattr(a, "scarcity_rank", None)
        return (rank if rank is not None else 999999, a.id or 0)
    actions = sorted(actions, key=_action_order)
    steps = []
    for a in actions:
        req_keys = a.req_component_ids or []
        if component_key not in req_keys:
            if a.variant_key:
                available[a.variant_key] = available.get(a.variant_key, 0) + float(a.qty or 0)
            continue
        available_before = round(available.get(component_key, 0), 4)
        qty = round(float(a.qty or 0), 4)
        to_key = a.variant_key or ""
        if to_variant_key and to_key != to_variant_key:
            for ck in req_keys:
                available[ck] = available.get(ck, 0) - qty
            if a.variant_key:
                available[a.variant_key] = available.get(a.variant_key, 0) + qty
            continue
        steps.append({
            "to_variant_key": to_key,
            "to_variant_display": _comp_key_to_at(to_key),
            "qty": qty,
            "edge_type": getattr(a, "edge_type", None) or "make",
            "available_before": available_before,
        })
        for ck in req_keys:
            available[ck] = available.get(ck, 0) - qty
        if a.variant_key:
            available[a.variant_key] = available.get(a.variant_key, 0) + qty
    total_available = sum(
        float(s.qty or 0) + supply_adj.get(f"{s.product_id}|{s.location_id or ''}", 0)
        for s in supplies
        if f"{s.product_id}|{s.location_id or ''}" == component_key
    )
    # Total that became available during run = initial + produced (actions that output this component)
    available_during_run = round(total_available, 4) + sum(
        round(float(a.qty or 0), 4)
        for a in actions
        if getattr(a, "variant_key", None) == component_key
    )
    reason = (
        "Allocation is proportional to target weight (demand pressure). "
        "Components are processed in scarcity order (total quantity; scarcest = smallest). "
        "The critical (limiting) component had the smallest available quantity among requirements for this edge."
    )
    supply_note: str | None = None
    if round(total_available, 4) == 0 and any((s.get("qty") or 0) > 0 for s in steps):
        supply_note = (
            "Initial supply for this component is 0. Allocated quantities come from production of this component "
            "earlier in the run (make/move that outputs this product@location), then consumed by the steps below."
        )
    return {
        "component_key": component_key,
        "component_display": _comp_key_to_at(component_key),
        "weight_formula": weight_formula,
        "candidate_targets": candidate_targets,
        "total_candidate_weight": round(total_w, 4),
        "steps": steps,
        "total_supply_for_component": round(total_available, 4),
        "available_during_run": round(available_during_run, 4),
        "reason": reason,
        "supply_note": supply_note,
    }
