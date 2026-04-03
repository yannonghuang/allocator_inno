import copy
import threading
import uuid
from typing import Any, List
import csv
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import Case, AllocationRun, AllocationAction, Demand, Supply, Customer, Bom, MethodMove
from app.schemas import AllocationRunResponse, AllocationActionResponse
from app.services.case_loader import load_case_data
from app.services.allocation_engine import run_allocation
from app.services.planning_engine import run_planning
from app.services.planning_copilot import planning_copilot_reply
from app.services.time_utils import build_period_index, demand_due_period, period_to_date

router = APIRouter(tags=["Allocation"])

# In-memory store for async plan jobs: job_id -> { case_id, status, progress, result, error }
_plan_jobs: dict[str, dict] = {}
_plan_jobs_lock = threading.Lock()

# Last plan result per case_id for on-demand work-order pegging (not full planning_pegging transfer)
_case_plan_results: dict[int, dict] = {}
_case_plan_results_lock = threading.Lock()


def _run_allocation_background(case_id: int, run_id: int) -> None:
    """Run allocation in a background thread; update AllocationRun and AllocationAction when done."""

    def progress_callback(progress: dict) -> None:
        progress_db = SessionLocal()
        try:
            run = progress_db.query(AllocationRun).filter(
                AllocationRun.id == run_id, AllocationRun.case_id == case_id
            ).first()
            if run and run.status == "running":
                config = dict(run.config or {})
                config["progress"] = {k: v for k, v in progress.items() if k not in ("allocation_slice", "prune_after_step", "prune_components")}
                if "prune_after_step" in progress and "prune_components" in progress:
                    config["prunes"] = config.get("prunes", []) + [{"after_step": progress["prune_after_step"], "comp_keys": progress["prune_components"]}]
                run.config = config
                progress_db.commit()
                slice_actions = progress.get("allocation_slice") or []
                for a in slice_actions:
                    act = AllocationAction(
                        run_id=run_id,
                        variant_key=a.get("variant_key", ""),
                        req_component_ids=a.get("req_component_ids") or [],
                        req_rates=a.get("req_rates"),
                        qty=float(a.get("qty", 0)),
                        demand_id=a.get("demand_id"),
                        target_product_id=a.get("target_product_id"),
                        target_location_id=a.get("target_location_id"),
                        output_period=a.get("output_period"),
                        edge_type=a.get("edge_type"),
                        scarcity_rank=a.get("scarcity_rank"),
                    )
                    progress_db.add(act)
                if slice_actions:
                    progress_db.commit()
        finally:
            progress_db.close()

    db = SessionLocal()
    try:
        run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
        if not run or run.status != "running":
            return
        data = load_case_data(db, case_id)
        if not data.get("demand") or not data.get("supply"):
            run.status = "failed"
            run.config = {"error": "No demand or supply data"}
            db.commit()
            return
        result = run_allocation(data, progress_callback=progress_callback)
        allocation_list = result.get("allocation", [])
        if not allocation_list:
            run.status = "failed"
            run.config = {"error": "Allocation returned no actions"}
            db.commit()
            return
        db.query(AllocationAction).filter(AllocationAction.run_id == run.id).delete()
        db.commit()
        BATCH_SIZE = 2000
        for i in range(0, len(allocation_list), BATCH_SIZE):
            batch = allocation_list[i : i + BATCH_SIZE]
            for a in batch:
                act = AllocationAction(
                    run_id=run.id,
                    variant_key=a["variant_key"],
                    req_component_ids=a["req_component_ids"],
                    req_rates=a.get("req_rates"),
                    qty=a["qty"],
                    demand_id=a.get("demand_id"),
                    target_product_id=a.get("target_product_id"),
                    target_location_id=a.get("target_location_id"),
                    output_period=a.get("output_period"),
                    edge_type=a.get("edge_type"),
                    scarcity_rank=a.get("scarcity_rank"),
                )
                db.add(act)
            db.commit()
        run.status = "success"
        config = dict(run.config or {})
        config["prunes"] = config.get("prunes", [])
        config["raw_material_trace"] = result.get("raw_material_trace", [])
        config["consumed_by_node"] = result.get("consumed_by_node", {})
        run.config = config
        db.commit()
    except Exception as e:
        run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
        if run:
            run.status = "failed"
            run.config = {"error": str(e)}
            db.commit()
    finally:
        db.close()


@router.post("/cases/{case_id}/allocate", response_model=AllocationRunResponse, status_code=202)
def run_allocate(case_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Start allocation asynchronously; returns 202 with run_id. Poll GET /cases/{id}/runs/{run_id} until status != 'running'."""
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    data = load_case_data(db, case_id)
    if not data.get("demand"):
        raise HTTPException(status_code=400, detail="No demand data")
    if not data.get("supply"):
        raise HTTPException(status_code=400, detail="No supply data")
    run = AllocationRun(case_id=case_id, status="running", config={})
    db.add(run)
    db.commit()
    db.refresh(run)
    background_tasks.add_task(_run_allocation_background, case_id, run.id)
    return run


@router.get("/cases/{case_id}/runs", response_model=List[AllocationRunResponse])
def list_runs(case_id: int, db: Session = Depends(get_db)):
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    return db.query(AllocationRun).filter(AllocationRun.case_id == case_id).order_by(AllocationRun.created_at.desc()).all()


@router.get("/cases/{case_id}/runs/{run_id}/status")
def get_run_status(case_id: int, run_id: int, db: Session = Depends(get_db)):
    """Lightweight poll endpoint: returns only run id and status (no actions)."""
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"id": run.id, "status": run.status, "config": run.config}


def _feasible_demands_from_actions(db: Session, case_id: int, actions: list) -> list[dict]:
    supply_list = [{"supply_date": row[0]} for row in db.query(Supply.supply_date).filter(Supply.case_id == case_id).all()]
    demands_q = db.query(Demand).filter(Demand.case_id == case_id).order_by(Demand.priority.asc()).all()
    demand_list_raw = [{"request_due_time": getattr(d, "request_due_time", None)} for d in demands_q]
    date_to_period, sorted_dates = build_period_index(supply_list, demand_list_raw)
    demands = demands_q
    customers_q = db.query(Customer).filter(Customer.case_id == case_id).all()
    cust_by_id = {c.customer: (c.description or c.customer) for c in customers_q}
    by_demand: dict[str, dict] = {}
    for d in demands:
        due_per = demand_due_period(d.request_due_time, date_to_period)
        by_demand[d.demand_id] = {
            "requested": float(d.quantity or 0),
            "allocated": 0.0,
            "product_id": d.product_id,
            "due_period": due_per,
            "request_due_time": getattr(d, "request_due_time", None),
            "revised_period": None,  # latest output_period among actions serving this demand
            "customer_id": d.customer_id,
            "customer": cust_by_id.get(d.customer_id, d.customer_id),
        }
    # demands by product_id (order preserved from demands = priority order)
    demands_by_product: dict[str, list] = {}
    for d in demands:
        demands_by_product.setdefault(d.product_id or "", []).append(d)
    # Only count action qty that is available by demand's due date (output_period <= due_period)
    for a in actions:
        pid = _action_target_product_id(a)
        out_per_raw = _action_output_period(a)
        out_per = out_per_raw if out_per_raw is not None else 0
        demand_id = _action_demand_id(a)
        qty = float(_action_qty(a) or 0)
        if demand_id and out_per_raw is not None and demand_id in by_demand:
            if by_demand[demand_id].get("revised_period") is None or out_per_raw > by_demand[demand_id]["revised_period"]:
                by_demand[demand_id]["revised_period"] = out_per_raw
        if not pid or qty <= 0:
            continue
        dmds = demands_by_product.get(pid, [])
        remaining = qty
        for d in dmds:
            if remaining <= 0:
                break
            due_per = by_demand[d.demand_id]["due_period"]
            if due_per != 0 and out_per > due_per:
                continue
            req = by_demand[d.demand_id]["requested"]
            already = by_demand[d.demand_id]["allocated"]
            give = min(req - already, remaining)
            if give > 0:
                by_demand[d.demand_id]["allocated"] += give
                remaining -= give
    result = []
    for did, v in by_demand.items():
        req, alloc = v["requested"], v["allocated"]
        status = "fulfilled" if alloc >= req else ("partial" if alloc > 0 else "unfulfilled")
        if alloc >= req:
            suggested_revision = "Fulfilled"
        elif alloc > 0:
            suggested_revision = f"Reduce to {alloc}"
        else:
            suggested_revision = "Unfulfilled (0 allocated)"
        rp = v.get("revised_period")
        revised_time = period_to_date(rp, sorted_dates) if rp is not None else None
        fulfillment_rate = (alloc / req) if req > 0 else None
        result.append({
            "demand_id": did,
            "customer_id": v.get("customer_id"),
            "customer": v.get("customer"),
            "product_id": v["product_id"],
            "requested_qty": req,
            "allocated_qty": alloc,
            "fulfillment_rate": round(fulfillment_rate, 4) if fulfillment_rate is not None else None,
            "status": status,
            "suggested_revision": suggested_revision,
            "request_due_time": v.get("request_due_time"),
            "revised_time": revised_time,
        })
    return result


def _action_target_product_id(a) -> str:
    if hasattr(a, "target_product_id"):
        return a.target_product_id or ""
    if isinstance(a, (list, tuple)) and len(a) >= 1:
        return (a[0] or "") if a[0] is not None else ""
    return ""


def _action_output_period(a) -> int | None:
    if hasattr(a, "output_period"):
        return getattr(a, "output_period", None)
    if isinstance(a, (list, tuple)) and len(a) >= 2:
        return a[1]
    return None


def _action_qty(a) -> float:
    if hasattr(a, "qty"):
        return float(a.qty or 0)
    if isinstance(a, (list, tuple)) and len(a) >= 3:
        return float(a[2] or 0)
    return 0.0


def _action_demand_id(a):
    if hasattr(a, "demand_id"):
        return getattr(a, "demand_id", None)
    if isinstance(a, (list, tuple)) and len(a) >= 4:
        return a[3]
    return None


@router.get("/cases/{case_id}/plan/products-with-real-bom", response_model=dict)
def get_products_with_real_bom(case_id: int, db: Session = Depends(get_db)):
    """
    Return BOM (PARENT_ID, CHILD_ID) pairs that have VIRTUAL <> 'Y' in bom.csv (non-virtual / real BOM row).
    Definition of real make: a make work order involving (product_parent, product_child) where there exists a BOM row
    with PARENT_ID=product_parent, CHILD_ID=product_child, and VIRTUAL is not 'Y'.

    Because the Bom table does not currently persist the VIRTUAL column, this endpoint reads csv/bom.csv directly.
    Response: { "pairs": [[parent_id, child_id], ...] } for lookup in the frontend.
    """
    # Ensure case exists (even though the BOM is global from csv)
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")

    # Resolve bom.csv relative to backend/app/api/allocate.py → project_root/csv/bom.csv
    try:
        root = Path(__file__).resolve().parents[3]
    except IndexError:
        root = Path(__file__).resolve().parent.parent.parent
    bom_csv = root / "csv" / "bom.csv"

    pairs: list[list[str]] = []
    if not bom_csv.exists():
        return {"pairs": pairs}

    try:
        with bom_csv.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            has_virtual = "VIRTUAL" in (reader.fieldnames or [])
            for row in reader:
                parent = (row.get("PARENT_ID") or "").strip()
                child = (row.get("CHILD_ID") or "").strip()
                if not parent or not child:
                    continue
                if has_virtual:
                    v = (row.get("VIRTUAL") or "").strip().upper()
                    # Real row: VIRTUAL not equal to 'Y'
                    if v == "Y":
                        continue
                pairs.append([parent, child])
    except Exception:
        # On any CSV parsing error, fall back to empty list so frontend degrades gracefully
        pairs = []

    return {"pairs": pairs}


@router.get("/cases/{case_id}/plan/moves-with-transit", response_model=dict)
def get_moves_with_transit(case_id: int, db: Session = Depends(get_db)):
    """
    Return move method rows with TRANSIT_TIME > 0 (real moves).
    Response: { "moves": [[product_id, from_location_id, to_location_id], ...] } for frontend filter.
    """
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    rows = (
        db.query(MethodMove.product_id, MethodMove.from_location_id, MethodMove.to_location_id)
        .filter(MethodMove.case_id == case_id, MethodMove.transit_time.isnot(None))
        .filter(MethodMove.transit_time > 0)
        .distinct()
        .all()
    )
    moves: list[list[str]] = [[(r[0] or "").strip(), (r[1] or "").strip(), (r[2] or "").strip()] for r in rows]
    return {"moves": moves}


def _get_bom_real_pairs_for_enrichment() -> set[tuple[str, str]]:
    """Return set of (parent_id, child_id) for real BOM rows (VIRTUAL <> Y). Reads csv/bom.csv."""
    try:
        root = Path(__file__).resolve().parents[3]
    except IndexError:
        root = Path(__file__).resolve().parent.parent.parent
    bom_csv = root / "csv" / "bom.csv"
    pairs: set[tuple[str, str]] = set()
    if not bom_csv.exists():
        return pairs
    try:
        with bom_csv.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            has_virtual = "VIRTUAL" in (reader.fieldnames or [])
            for row in reader:
                parent = (row.get("PARENT_ID") or "").strip()
                child = (row.get("CHILD_ID") or "").strip()
                if not parent or not child:
                    continue
                if has_virtual and (row.get("VIRTUAL") or "").strip().upper() == "Y":
                    continue
                pairs.add((parent, child))
    except Exception:
        pass
    return pairs


def _find_wo_node(tree: dict, product_id: str, location_id: str, method: str) -> dict | None:
    """Find first work_order node in tree matching product_id, location_id, method. Returns the node (subtree root)."""
    if tree.get("type") == "work_order":
        if (
            (tree.get("product_id") or "").strip() == product_id
            and (tree.get("location_id") or "").strip() == location_id
            and (tree.get("method") or "").strip() == method
        ):
            return tree
    for ch in tree.get("children") or []:
        found = _find_wo_node(ch, product_id, location_id, method)
        if found:
            return found
    return None


def _subtree_contains_real_make(node: dict, bom_real_pairs: set[tuple[str, str]]) -> bool:
    """True if subtree has a make WO whose (parent, child) is in bom_real_pairs."""
    if node.get("type") == "work_order" and (node.get("method") or "").strip() == "make":
        # If backend has explicitly recognized BOM links for this make (children_relation set),
        # treat it as a real make even when BOM_ID fallback was used and parent/child isn't in bom_real_pairs.
        relation = (node.get("children_relation") or "").strip().lower()
        if relation in ("and", "or"):
            return True
        parent_id = (node.get("product_id") or "").strip()
        for ch in node.get("children") or []:
            child_id = (ch.get("product_id") or "").strip()
            if (parent_id, child_id) in bom_real_pairs:
                return True
    for ch in node.get("children") or []:
        if _subtree_contains_real_make(ch, bom_real_pairs):
            return True
    return False


def _subtree_contains_buy(node: dict) -> bool:
    if node.get("type") == "purchase":
        return True
    for ch in node.get("children") or []:
        if _subtree_contains_buy(ch):
            return True
    return False


def _subtree_contains_real_move(node: dict, move_triples: set[tuple[str, str, str]]) -> bool:
    """True if subtree has a move WO with (product_id, from_location, to_location) in move_triples."""
    if node.get("type") == "work_order" and (node.get("method") or "").strip() == "move":
        pid = (node.get("product_id") or "").strip()
        from_loc = (node.get("location_source") or "").strip()
        to_loc = (node.get("location_id") or "").strip()
        if (pid, from_loc, to_loc) in move_triples:
            return True
    for ch in node.get("children") or []:
        if _subtree_contains_real_move(ch, move_triples):
            return True
    return False


def _pegging_supply_consumed(node: dict) -> float:
    """Sum quantity of all type=supply nodes in the pegging subtree (inventory consumed by plan)."""
    total = 0.0
    if node.get("type") == "supply":
        total += float(node.get("quantity") or 0)
    for ch in node.get("children") or []:
        total += _pegging_supply_consumed(ch)
    return total


def _plan_supply_summary(data: dict, result: dict) -> dict:
    """
    Compute inventory (existing/scheduled supply) consumption for the plan.
    Returns { initial_total, consumed_total, consumption_rate } for KPI dashboard.
    """
    supply_list = data.get("supply") or []
    initial_total = sum(float(s.get("qty") or 0) for s in supply_list)
    consumed_total = 0.0
    for entry in result.get("planning_pegging") or []:
        tree = entry.get("tree")
        if tree:
            consumed_total += _pegging_supply_consumed(tree)
    rate = (consumed_total / initial_total) if initial_total and initial_total > 0 else None
    return {
        "initial_total": round(initial_total, 4),
        "consumed_total": round(consumed_total, 4),
        "consumption_rate": round(rate, 4) if rate is not None else None,
    }


def _plan_kpis(data: dict, result: dict) -> dict:
    """
    Build full KPI set for plan dashboard: delivery, inventory, procurement, manufacturing, logistics.
    """
    demands = data.get("demand") or []
    committed = result.get("committed_demands") or []
    work_orders = result.get("work_orders") or []

    # Delivery (demand side)
    total_requested = sum(float(d.get("quantity") or 0) for d in demands)
    total_committed = sum(float(c.get("quantity") or 0) for c in committed)
    fill_rate_pct = (total_committed / total_requested * 100) if total_requested and total_requested > 0 else None
    demand_by_id = {str(d.get("demand_id") or "").strip(): d for d in demands}
    # Latest commit_time per demand_id (one demand can have multiple committed lines if split)
    commit_by_demand: dict[str, str | None] = {}
    for c in committed:
        did = str(c.get("demand_id") or "").strip()
        ct = c.get("commit_time")
        if did and ct and (did not in commit_by_demand or (commit_by_demand[did] or "") < ct):
            commit_by_demand[did] = ct
    on_time_count = 0
    for did, due in ((str(d.get("demand_id") or "").strip(), d.get("request_due_time") or d.get("request_time")) for d in demands):
        if not did or not due:
            continue
        ct = commit_by_demand.get(did)
        if ct and due and ct <= due:
            on_time_count += 1

    # Fulfillment split: demands whose pegging includes a real make vs inventory-only / other
    planning_pegging = result.get("planning_pegging") or []
    tree_by_demand: dict[str, dict] = {
        str(e.get("demand_id") or "").strip(): e.get("tree")
        for e in planning_pegging
        if e.get("tree")
    }
    bom_pairs = _get_bom_real_pairs_for_enrichment()
    # Unique demand_ids with committed qty > 0
    fulfilled_ids: set[str] = set()
    for c in committed:
        did = str(c.get("demand_id") or "").strip()
        qty = float(c.get("quantity") or 0)
        if did and qty > 0:
            fulfilled_ids.add(did)
    fulfilled_with_tree = [did for did in fulfilled_ids if did in tree_by_demand]
    fulfilled_by_real_make = 0
    fulfilled_by_inventory_only = 0
    for did in fulfilled_with_tree:
        tree = tree_by_demand.get(did)
        if not tree:
            continue
        if _subtree_contains_real_make(tree, bom_pairs):
            fulfilled_by_real_make += 1
        else:
            fulfilled_by_inventory_only += 1
    fulfilled_with_tree_count = len(fulfilled_with_tree)

    # Method breakdown (aggregate by method; same key = one logical WO)
    def _method_stats(method_val: str) -> dict:
        # Only count real make / real move in KPIs; for other methods, include all.
        wos = [
            wo
            for wo in work_orders
            if str(wo.get("method") or "").strip().lower() == method_val
            and (
                method_val not in ("make", "move")
                or (
                    (method_val == "make" and bool(wo.get("pegging_includes_real_make")))
                    or (method_val == "move" and bool(wo.get("pegging_includes_real_move")))
                )
            )
        ]
        # Dedupe by (demand_id, product_id, location_id, method) and sum quantity
        seen: dict[tuple, float] = {}
        for wo in wos:
            key = (
                str(wo.get("demand_id") or "").strip(),
                str(wo.get("product_id") or "").strip(),
                str(wo.get("location_id") or "").strip(),
                str(wo.get("method") or "").strip(),
            )
            q = float(wo.get("quantity") or 0)
            seen[key] = seen.get(key, 0) + q
        total_qty = sum(seen.values())
        return {"order_count": len(seen), "total_quantity": round(total_qty, 4)}

    return {
        "delivery": {
            "total_requested": round(total_requested, 4),
            "total_committed": round(total_committed, 4),
            "fill_rate_pct": round(fill_rate_pct, 2) if fill_rate_pct is not None else None,
            "demand_count": len(demands),
            "on_time_count": on_time_count,
            "fulfilled_with_tree_count": fulfilled_with_tree_count,
            "fulfilled_by_real_make_count": fulfilled_by_real_make,
            "fulfilled_by_inventory_only_count": fulfilled_by_inventory_only,
        },
        "inventory": _plan_supply_summary(data, result),
        "procurement": _method_stats("purchase"),
        "manufacturing": _method_stats("make"),
        "logistics": _method_stats("move"),
    }


def _get_move_triples_with_transit(db: Session, case_id: int) -> set[tuple[str, str, str]]:
    """Return set of (product_id, from_location_id, to_location_id) for moves with transit_time > 0."""
    rows = (
        db.query(MethodMove.product_id, MethodMove.from_location_id, MethodMove.to_location_id)
        .filter(MethodMove.case_id == case_id, MethodMove.transit_time.isnot(None))
        .filter(MethodMove.transit_time > 0)
        .distinct()
        .all()
    )
    return {((r[0] or "").strip(), (r[1] or "").strip(), (r[2] or "").strip()) for r in rows}


def _enrich_work_orders_with_pegging_flags(
    result: dict,
    bom_real_pairs: set[tuple[str, str]],
    move_triples: set[tuple[str, str, str]],
) -> None:
    """Mutate result['work_orders'] adding pegging_includes_real_make, pegging_includes_buy, pegging_includes_real_move."""
    planning_pegging = result.get("planning_pegging") or []
    by_demand = {str(e.get("demand_id") or "").strip(): e.get("tree") for e in planning_pegging if e.get("tree")}
    for wo in result.get("work_orders") or []:
        demand_id = str(wo.get("demand_id") or "").strip()
        tree = by_demand.get(demand_id) if demand_id else None
        wo["pegging_includes_real_make"] = False
        wo["pegging_includes_buy"] = False
        wo["pegging_includes_real_move"] = False
        if not tree:
            continue
        pid = (wo.get("product_id") or "").strip()
        loc = (wo.get("location_id") or "").strip()
        method = (wo.get("method") or "").strip()
        wo_node = _find_wo_node(tree, pid, loc, method)
        if not wo_node:
            continue
        wo["pegging_includes_real_make"] = _subtree_contains_real_make(wo_node, bom_real_pairs)
        wo["pegging_includes_buy"] = _subtree_contains_buy(wo_node)
        wo["pegging_includes_real_move"] = _subtree_contains_real_move(wo_node, move_triples)


def _log_wo_pegging(msg: str, **kwargs: Any) -> None:
    try:
        import logging
        logging.getLogger("app.api.allocate").info("work-order-pegging: %s %s", msg, kwargs)
    except Exception:
        pass


@router.get("/cases/{case_id}/plan/work-order-pegging", response_model=dict)
def get_work_order_pegging(
    case_id: int,
    db: Session = Depends(get_db),
    demand_id: str = Query("", alias="demand_id"),
    product_id: str = Query("", alias="product_id"),
    location_id: str = Query("", alias="location_id"),
    method: str = Query("", alias="method"),
):
    """
    Return the pegging subtree for one work order: how this WO is fulfilled by its supplies (all levels).
    Fetched on demand; requires a prior plan run for this case. Query params: demand_id, product_id, location_id, method.
    """
    demand_id = (demand_id or "").strip()
    product_id = (product_id or "").strip()
    location_id = (location_id or "").strip()
    method = (method or "").strip()
    _log_wo_pegging("request", case_id=case_id, demand_id=demand_id, product_id=product_id, location_id=location_id, method=method)
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        _log_wo_pegging("404 case not found", case_id=case_id)
        raise HTTPException(status_code=404, detail="Case not found")
    if not demand_id or not product_id or not location_id or not method:
        _log_wo_pegging("400 missing params", demand_id=bool(demand_id), product_id=bool(product_id), location_id=bool(location_id), method=bool(method))
        raise HTTPException(status_code=400, detail="demand_id, product_id, location_id, method required")
    with _case_plan_results_lock:
        result = _case_plan_results.get(case_id)
    if not result:
        _log_wo_pegging("404 no plan result for case; run plan first", case_id=case_id)
        raise HTTPException(
            status_code=404,
            detail="No plan result for this case. Run plan first; work-order pegging is loaded on demand.",
        )
    planning_pegging = result.get("planning_pegging") or []
    entry = next((e for e in planning_pegging if str(e.get("demand_id") or "").strip() == demand_id), None)
    if not entry or not entry.get("tree"):
        _log_wo_pegging("404 no pegging tree for demand", demand_id=demand_id, num_pegging=len(planning_pegging))
        raise HTTPException(status_code=404, detail="No pegging tree for this demand")
    tree = entry["tree"]
    wo_node = _find_wo_node(tree, product_id, location_id, method)
    if not wo_node:
        _log_wo_pegging("404 WO not found in tree", product_id=product_id, location_id=location_id, method=method)
        raise HTTPException(status_code=404, detail="Work order not found in pegging tree")
    # Align root quantity with work_orders list (sum of WO quantities for this key) so pegging matches the table
    work_orders = result.get("work_orders") or []
    wo_qty_sum = sum(
        float(wo.get("quantity") or 0)
        for wo in work_orders
        if (
            str(wo.get("demand_id") or "").strip() == demand_id
            and str(wo.get("product_id") or "").strip() == product_id
            and str(wo.get("location_id") or "").strip() == location_id
            and str(wo.get("method") or "").strip() == method
        )
    )
    tree_return = copy.deepcopy(wo_node)
    if wo_qty_sum > 0:
        tree_return["quantity"] = round(wo_qty_sum, 4)
    _log_wo_pegging("200 ok", product_id=product_id, location_id=location_id, method=method, wo_qty_sum=wo_qty_sum)
    return {"tree": tree_return}


@router.get("/cases/{case_id}/runs/{run_id}/feasible-demands")
def get_feasible_demands(case_id: int, run_id: int, db: Session = Depends(get_db)):
    """Lightweight endpoint: returns only feasible_demands. Loads minimal action columns to avoid heavy ORM/serialization."""
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    # Only load columns needed for feasible_demands (avoids full ORM and speeds up with many actions)
    action_rows = db.query(
        AllocationAction.target_product_id,
        AllocationAction.output_period,
        AllocationAction.qty,
        AllocationAction.demand_id,
    ).filter(AllocationAction.run_id == run_id).all()
    feasible = _feasible_demands_from_actions(db, case_id, action_rows)
    return {"feasible_demands": feasible}


def _run_planning_background(job_id: str, case_id: int, config: Any) -> None:
    """Run planning in background; update _plan_jobs with progress and result."""
    db = SessionLocal()
    try:
        data = load_case_data(db, case_id)
        if not data.get("demand") or not data.get("supply"):
            with _plan_jobs_lock:
                _plan_jobs[job_id]["status"] = "failed"
                _plan_jobs[job_id]["error"] = "No demand or supply data"
            return

        def progress_cb(progress: dict) -> None:
            with _plan_jobs_lock:
                if job_id in _plan_jobs and _plan_jobs[job_id]["status"] == "running":
                    _plan_jobs[job_id]["progress"] = dict(progress)

        result = run_planning(data, config=config, progress_callback=progress_cb)
        bom_pairs = _get_bom_real_pairs_for_enrichment()
        move_triples = _get_move_triples_with_transit(db, case_id)
        _enrich_work_orders_with_pegging_flags(result, bom_pairs, move_triples)
        result["plan_kpis"] = _plan_kpis(data, result)
        result["supply_summary"] = result["plan_kpis"]["inventory"]
        with _plan_jobs_lock:
            if job_id in _plan_jobs:
                _plan_jobs[job_id]["status"] = "completed"
                _plan_jobs[job_id]["result"] = result
                _plan_jobs[job_id]["progress"] = {"current": result["committed_demands"] and len(result["committed_demands"]) or 0, "total": result["committed_demands"] and len(result["committed_demands"]) or 0}
        with _case_plan_results_lock:
            _case_plan_results[case_id] = result
    except Exception as e:
        with _plan_jobs_lock:
            if job_id in _plan_jobs:
                _plan_jobs[job_id]["status"] = "failed"
                _plan_jobs[job_id]["error"] = str(e)
    finally:
        db.close()


@router.get("/cases/{case_id}/plan/status/{job_id}", response_model=dict)
def get_plan_status(case_id: int, job_id: str):
    """Poll for async plan job status. Returns { status, progress?: { current, total }, result?, error? }."""
    with _plan_jobs_lock:
        job = _plan_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Plan job not found")
    if job.get("case_id") != case_id:
        raise HTTPException(status_code=404, detail="Plan job not found for this case")
    out = {"status": job["status"], "progress": job.get("progress")}
    if job.get("result") is not None:
        out["result"] = job["result"]
    if job.get("error") is not None:
        out["error"] = job["error"]
    return out


def _start_plan_background(job_id: str, case_id: int, config: Any) -> None:
    """Start planning in a daemon thread so we don't need BackgroundTasks (avoids FastAPI response_model issue)."""
    t = threading.Thread(target=_run_planning_background, args=(job_id, case_id, config), daemon=True)
    t.start()


@router.post("/cases/{case_id}/plan", response_model=None)
def run_plan(
    case_id: int,
    db: Session = Depends(get_db),
    body: dict | None = Body(None),
) -> Any:
    """
    Demand-to-supply planning. Body: { "config": {...}, "async": true }.
    If async is true: returns 202 with job_id; poll GET /cases/{case_id}/plan/status/{job_id} for progress and result.
    If async is false or omitted: runs synchronously and returns the result.
    """
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    data = load_case_data(db, case_id)
    if not data.get("demand"):
        raise HTTPException(status_code=400, detail="No demand data")
    if not data.get("supply"):
        raise HTTPException(status_code=400, detail="No supply data")
    payload = body or {}
    config = payload.get("config")
    use_async = payload.get("async") is True

    if use_async:
        job_id = str(uuid.uuid4())
        with _plan_jobs_lock:
            _plan_jobs[job_id] = {
                "case_id": case_id,
                "status": "running",
                "progress": {"current": 0, "total": len(data.get("demand") or [])},
                "result": None,
                "error": None,
            }
        _start_plan_background(job_id, case_id, config)
        return JSONResponse(
            status_code=202,
            content={"job_id": job_id, "status": "running", "message": "Poll GET /cases/{case_id}/plan/status/{job_id} for progress and result."},
            headers={"Location": f"/cases/{case_id}/plan/status/{job_id}"},
        )

    result = run_planning(data, config=config)
    bom_pairs = _get_bom_real_pairs_for_enrichment()
    move_triples = _get_move_triples_with_transit(db, case_id)
    _enrich_work_orders_with_pegging_flags(result, bom_pairs, move_triples)
    result["plan_kpis"] = _plan_kpis(data, result)
    result["supply_summary"] = result["plan_kpis"]["inventory"]
    with _case_plan_results_lock:
        _case_plan_results[case_id] = result
    return result


@router.post("/cases/{case_id}/planning-copilot", response_model=dict)
def planning_copilot(case_id: int, db: Session = Depends(get_db), body: dict | None = Body(None)):
    """
    Chat endpoint for planning config: interpret natural-language message into a reply and optional config update.
    Body: { "message": str, "current_config": dict, "history": [{"role": "user"|"assistant", "text": str}] }.
    Returns: { "reply": str, "config_update": dict | null }. Uses LLM when OPENAI_API_KEY is set.
    """
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    payload = body or {}
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    current_config = payload.get("current_config") or {}
    history = payload.get("history") or []
    reply, config_update = planning_copilot_reply(message, current_config, history)
    return {"reply": reply, "config_update": config_update}


@router.get("/cases/{case_id}/runs/{run_id}", response_model=dict)
def get_run(case_id: int, run_id: int, db: Session = Depends(get_db)):
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).all()
    feasible = _feasible_demands_from_actions(db, case_id, actions)
    return {
        "run": AllocationRunResponse.model_validate(run),
        "actions": [AllocationActionResponse.model_validate(a) for a in actions],
        "feasible_demands": feasible,
    }
