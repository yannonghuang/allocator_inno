from typing import Any, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import Case, AllocationRun, AllocationAction, Demand, Supply
from app.schemas import AllocationRunResponse, AllocationActionResponse
from app.services.case_loader import load_case_data
from app.services.allocation_engine import run_allocation
from app.services.time_utils import build_period_index, demand_due_period, period_to_date

router = APIRouter(tags=["Allocation"])


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
