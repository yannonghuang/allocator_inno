"""UI views: supply view, allocation view, suggested revised demands."""
from collections import OrderedDict, defaultdict
from copy import copy
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AllocationRun, AllocationAction, Bom, Case, Demand, ManualOverride, MethodMake, MethodMove, Supply
from app.services.case_loader import load_case_data
from app.services.allocation_engine import run_allocation
from app.services.time_utils import build_period_index, period_minus_days, period_to_date, supply_period
from app.utils.sku_patterns import RAW_MATERIAL_PATTERNS, raw_material_pattern

router = APIRouter(prefix="/cases", tags=["Views"])


def _norm_comp_key(comp_key: str) -> str:
    """Normalize product_id|location_id for consistent lookup (supply view vs action req_component_ids)."""
    if not comp_key:
        return ""
    parts = (comp_key or "").split("|", 1)
    p0 = str(parts[0]).strip() if parts else ""
    p1 = str(parts[1]).strip() if len(parts) > 1 else ""
    return f"{p0}|{p1}"


def _consume_from_component_fifo(
    available_by_node: dict[str, float],
    consumed_by_node: dict[str, float],
    comp_key: str,
    need: float,
    comp_periods: dict[str, list[int]],
) -> float:
    """Consume up to `need` from component comp_key (product|location) in period order (FIFO). Records in consumed_by_node. Returns amount taken."""
    if need <= 0:
        return 0.0
    periods = comp_periods.get(comp_key, [])
    taken = 0.0
    for p in sorted(periods):
        if taken >= need:
            break
        node = f"{comp_key}|{p}"
        avail = available_by_node.get(node, 0)
        if avail <= 0:
            continue
        take = min(avail, need - taken)
        if take > 0:
            available_by_node[node] = avail - take
            consumed_by_node[node] = consumed_by_node.get(node, 0) + take
            taken += take
    return taken


@router.get("/{case_id}/runs/{run_id}/supply-view")
def get_supply_view(
    case_id: int,
    run_id: int,
    db: Session = Depends(get_db),
    debug_component_key: str | None = Query(None, description="If set (e.g. 280-0845-030|1000), response includes _debug for this component"),
):
    """Supply view: one row per supply; consumed/residual per inventory (product|location|period), not aggregated by product."""
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    supplies = db.query(Supply).filter(Supply.case_id == case_id).order_by(Supply.supply_id).all()
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).all()
    # Replay in same order as engine (scarcity_rank) so FIFO consumption matches
    actions = sorted(actions, key=lambda a: (getattr(a, "scarcity_rank") if getattr(a, "scarcity_rank") is not None else 999999, a.id or 0))
    supply_list = [{"supply_date": getattr(s, "supply_date", None)} for s in supplies]
    demand_list = [{"request_due_time": getattr(d, "request_due_time", None)} for d in demands]
    date_to_period, _ = build_period_index(supply_list, demand_list)

    # Inventory node = product|location|period; use normalized comp_key so lookup matches action req_component_ids
    initial_by_node: dict[str, float] = defaultdict(float)
    comp_periods: dict[str, list[int]] = defaultdict(list)  # comp_key -> list of periods with supply
    for s in supplies:
        period = supply_period(getattr(s, "supply_date", None), date_to_period)
        comp_key = _norm_comp_key(f"{s.product_id}|{s.location_id or ''}")
        node = f"{comp_key}|{period}"
        q = float(s.qty or 0)
        initial_by_node[node] += q
        if period not in comp_periods[comp_key]:
            comp_periods[comp_key].append(period)

    # Build node -> supply rows for distribution (FIFO by period, supply_id)
    supplies_with_node = []
    for i, s in enumerate(supplies):
        period = supply_period(getattr(s, "supply_date", None), date_to_period)
        comp_key = _norm_comp_key(f"{s.product_id}|{s.location_id or ''}")
        node = f"{comp_key}|{period}"
        init_row = float(s.qty or 0)
        supply_id = getattr(s, "supply_id", None) or ""
        supplies_with_node.append((i, node, period, supply_id, init_row))
    node_to_supplies: dict[str, list[tuple[int, float, int, str]]] = defaultdict(list)
    for i, node, period, sid, init_row in supplies_with_node:
        node_to_supplies[node].append((i, init_row, period, sid))
    for node in node_to_supplies:
        node_to_supplies[node].sort(key=lambda x: (x[2], x[3]))  # period, supply_id

    # Single source of truth: always replay actions for consumption so supply view matches raw material usage / production trace.
    consumed_by_node: dict[str, float] = defaultdict(float)
    debug_comp = (debug_component_key or "").strip() or None
    debug_actions: list[dict] = []
    bom_rate_supply: dict[tuple[str, str], float] = {}
    for b in db.query(Bom).filter(Bom.case_id == case_id).all():
        key = (b.parent_id, b.child_id)
        if key not in bom_rate_supply:
            bom_rate_supply[key] = float(b.rate or 1.0)
    available_by_node = copy(initial_by_node)
    for a in actions:
        output_qty = float(a.qty or 0)
        req_keys = a.req_component_ids or []
        req_rates = getattr(a, "req_rates", None)
        if not req_rates or len(req_rates) != len(req_keys):
            req_rates = None
        target_pid = getattr(a, "target_product_id", None) or ""
        edge_type = getattr(a, "edge_type", None) or "make"
        for i, ckey in enumerate(req_keys):
            ckey_norm = _norm_comp_key(ckey)
            if req_rates is not None and i < len(req_rates) and req_rates[i] is not None and float(req_rates[i]) > 0:
                rate = float(req_rates[i])
            else:
                comp_product = (ckey_norm or ckey).split("|", 1)[0] if (ckey_norm or ckey) else ""
                rate = float(bom_rate_supply.get((target_pid, comp_product), 1.0)) if edge_type == "make" else 1.0
            need = output_qty / float(rate)
            taken = _consume_from_component_fifo(available_by_node, consumed_by_node, ckey_norm, need, comp_periods)
            if debug_comp and (_norm_comp_key(debug_comp) == ckey_norm or ckey == debug_comp):
                debug_actions.append({
                    "action_id": a.id,
                    "scarcity_rank": getattr(a, "scarcity_rank", None),
                    "variant_key": getattr(a, "variant_key", None),
                    "target_product_id": target_pid,
                    "edge_type": edge_type,
                    "output_qty": output_qty,
                    "rate_used": rate,
                    "need_computed": need,
                    "taken": taken,
                    "req_rates_from_action": req_rates is not None,
                })

    consumed_by_supply_idx: dict[int, float] = {}
    for node, consumed_in_node in consumed_by_node.items():
        if consumed_in_node <= 0:
            continue
        remaining = consumed_in_node
        for (idx, init_row, _p, _sid) in node_to_supplies.get(node, []):
            take = min(init_row, remaining)
            if take > 0:
                consumed_by_supply_idx[idx] = consumed_by_supply_idx.get(idx, 0) + take
            remaining -= take
            if remaining <= 0:
                break

    result = []
    for i, s in enumerate(supplies):
        period = supply_period(getattr(s, "supply_date", None), date_to_period)
        node = f"{s.product_id}|{s.location_id or ''}|{period}"
        comp_key = f"{s.product_id}|{s.location_id or ''}"
        init_row = float(s.qty or 0)
        consumed_qty_row = consumed_by_supply_idx.get(i, 0.0)
        residual_qty_row = max(0.0, init_row - consumed_qty_row)
        utilization_rate = (consumed_qty_row / init_row) if init_row > 0 else None
        result.append({
            "id": s.id,
            "component_key": comp_key,
            "supply_id": s.supply_id,
            "supply_date": getattr(s, "supply_date", None),
            "product_id": s.product_id,
            "location_id": s.location_id or "",
            "initial_qty": round(init_row, 4),
            "consumed_qty": round(consumed_qty_row, 4),
            "residual_qty": round(residual_qty_row, 4),
            "utilization_rate": round(utilization_rate, 4) if utilization_rate is not None else None,
        })

    out: dict = {"run_id": run_id, "supply_view": result}
    if debug_comp:
        debug_comp_norm = _norm_comp_key(debug_comp)
        prefix = f"{debug_comp_norm}|"
        nodes_initial = {k: v for k, v in initial_by_node.items() if k == debug_comp_norm or k.startswith(prefix)}
        nodes_consumed = {k: v for k, v in consumed_by_node.items() if k == debug_comp_norm or k.startswith(prefix)}
        supplies_for_comp = []
        for s in supplies:
            loc = getattr(s, "location_id", None) or ""
            if _norm_comp_key(f"{s.product_id}|{loc}") != debug_comp_norm:
                continue
            ck = _norm_comp_key(f"{s.product_id}|{loc}")
            node = f"{ck}|{supply_period(getattr(s, 'supply_date', None), date_to_period)}"
            supplies_for_comp.append({
                "supply_id": getattr(s, "supply_id", None),
                "product_id": s.product_id,
                "location_id": loc,
                "supply_date": getattr(s, "supply_date", None),
                "qty": float(s.qty or 0),
                "node": node,
            })
        out["_debug"] = {
            "component_key": debug_comp,
            "actions_using_this_component": debug_actions,
            "actions_count": len(debug_actions),
            "nodes_initial": nodes_initial,
            "nodes_consumed": nodes_consumed,
            "comp_periods": comp_periods.get(debug_comp_norm, []),
            "supplies_for_this_component": supplies_for_comp,
            "bom_rates_for_parents": {str(k): v for k, v in bom_rate_supply.items() if k[1] == (debug_comp_norm.split("|", 1)[0] if "|" in debug_comp_norm else debug_comp_norm)},
        }
    return out


@router.get("/{case_id}/runs/{run_id}/production-trace")
def get_production_trace(
    case_id: int,
    run_id: int,
    db: Session = Depends(get_db),
):
    """
    Trace whether any supplies of products matching SKU patterns 1xx-xxxx, 2xx-xxxx, 3xx-xxxx
    were used in this run (real production). Returns which patterns had consumption and total qty per pattern.
    """
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    supplies = db.query(Supply).filter(Supply.case_id == case_id).order_by(Supply.supply_id).all()
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).all()
    actions = sorted(actions, key=lambda a: (getattr(a, "scarcity_rank") if getattr(a, "scarcity_rank") is not None else 999999, a.id or 0))

    supply_list = [{"supply_date": getattr(s, "supply_date", None)} for s in supplies]
    demand_list = [{"request_due_time": getattr(d, "request_due_time", None)} for d in demands]
    date_to_period, _ = build_period_index(supply_list, demand_list)

    initial_by_node: dict[str, float] = defaultdict(float)
    comp_periods: dict[str, list[int]] = defaultdict(list)
    for s in supplies:
        period = supply_period(getattr(s, "supply_date", None), date_to_period)
        comp_key = _norm_comp_key(f"{s.product_id}|{s.location_id or ''}")
        node = f"{comp_key}|{period}"
        initial_by_node[node] += float(s.qty or 0)
        if period not in comp_periods[comp_key]:
            comp_periods[comp_key].append(period)

    bom_rate_supply: dict[tuple[str, str], float] = {}
    for b in db.query(Bom).filter(Bom.case_id == case_id).all():
        key = (b.parent_id, b.child_id)
        if key not in bom_rate_supply:
            bom_rate_supply[key] = float(b.rate or 1.0)

    available_by_node = copy(initial_by_node)
    consumed_by_node: dict[str, float] = defaultdict(float)

    for a in actions:
        output_qty = float(a.qty or 0)
        req_keys = a.req_component_ids or []
        req_rates = getattr(a, "req_rates", None)
        if not req_rates or len(req_rates) != len(req_keys):
            req_rates = None
        target_pid = getattr(a, "target_product_id", None) or ""
        edge_type = getattr(a, "edge_type", None) or "make"
        for i, ckey in enumerate(req_keys):
            ckey_norm = _norm_comp_key(ckey)
            if req_rates is not None and i < len(req_rates) and req_rates[i] is not None and float(req_rates[i]) > 0:
                rate = float(req_rates[i])
            else:
                comp_product = ckey_norm.split("|", 1)[0] if "|" in ckey_norm else ckey_norm
                rate = float(bom_rate_supply.get((target_pid, comp_product), 1.0)) if edge_type == "make" else 1.0
            need = output_qty / float(rate)
            _consume_from_component_fifo(available_by_node, consumed_by_node, ckey_norm, need, comp_periods)

    by_pattern: dict[str, dict] = {name: {"used": False, "total_consumed_qty": 0.0, "nodes_consumed": []} for name, _ in RAW_MATERIAL_PATTERNS}
    for node, qty in consumed_by_node.items():
        if qty <= 0:
            continue
        product_id = node.split("|", 1)[0] if "|" in node else node
        pattern = raw_material_pattern(product_id)
        if pattern is None:
            continue
        by_pattern[pattern]["used"] = True
        by_pattern[pattern]["total_consumed_qty"] = by_pattern[pattern]["total_consumed_qty"] + qty
        by_pattern[pattern]["nodes_consumed"].append({"node": node, "consumed_qty": round(qty, 4)})

    supply_patterns_used = [name for name in by_pattern if by_pattern[name]["used"]]
    for name in by_pattern:
        by_pattern[name]["total_consumed_qty"] = round(by_pattern[name]["total_consumed_qty"], 4)

    return {
        "run_id": run_id,
        "supply_patterns_used": supply_patterns_used,
        "by_pattern": by_pattern,
    }


@router.get("/{case_id}/runs/{run_id}/raw-material-usage")
def get_raw_material_usage_report(
    case_id: int,
    run_id: int,
    db: Session = Depends(get_db),
):
    """
    Raw material usage report for this run: which 1xx-xxxx, 2xx-xxxx, 3xx-xxxx supplies were consumed,
    and involvement trace (critical/companion, allocated or only considered). Trace is from last allocation run.
    """
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    raw_material_trace_raw = list((run.config or {}).get("raw_material_trace", []))
    # Component keys that appear in Supply (supply view): product_id|location_id (normalize for match)
    supplies = db.query(Supply).filter(Supply.case_id == case_id).all()
    supply_comp_keys = {
        f"{str(s.product_id or '').strip()}|{str(s.location_id or '').strip()}"
        for s in supplies
    }

    def _norm_comp_key(k: str) -> str:
        if not k:
            return ""
        parts = (k or "").split("|", 1)
        p0 = str(parts[0]).strip() if parts else ""
        p1 = str(parts[1]).strip() if len(parts) > 1 else ""
        return f"{p0}|{p1}"

    raw_material_trace = [
        {**e, "in_supply_view": _norm_comp_key(e.get("comp_key") or "") in supply_comp_keys}
        for e in raw_material_trace_raw
    ]
    data = get_production_trace(case_id, run_id, db)
    by_pattern = data.get("by_pattern", {})
    summary: dict[str, dict] = {}
    details: list[dict] = []
    for pattern_name, info in by_pattern.items():
        total = float(info.get("total_consumed_qty") or 0)
        nodes_consumed = info.get("nodes_consumed") or []
        summary[pattern_name] = {"total_consumed_qty": round(total, 4), "node_count": len(nodes_consumed)}
        for item in nodes_consumed:
            node = item.get("node") or ""
            consumed_qty = float(item.get("consumed_qty") or 0)
            parts = node.split("|", 2)
            product_id = parts[0] if parts else ""
            location_id = parts[1] if len(parts) > 1 else ""
            details.append({
                "pattern": pattern_name,
                "product_id": product_id,
                "location_id": location_id,
                "node": node,
                "consumed_qty": round(consumed_qty, 4),
            })
    details.sort(key=lambda r: (r["pattern"], r["product_id"], r["location_id"], r["node"]))
    return {
        "run_id": data.get("run_id"),
        "supply_patterns_used": data.get("supply_patterns_used", []),
        "summary": summary,
        "details": details,
        "involvement_trace": raw_material_trace,
    }


@router.get("/{case_id}/trace-component")
def trace_component(
    case_id: int,
    db: Session = Depends(get_db),
    component_key: str = Query(..., description="Component key to trace, e.g. 280-0845-030|1000"),
):
    """
    Run the allocation engine in memory with trace_component_key set; return _trace explaining
    why this component was or wasn't consumed (as critical or companion). Does not persist any run.
    """
    c = db.query(Case).filter(Case.id == case_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    data = load_case_data(db, case_id)
    if not data.get("supply") or not data.get("demand"):
        raise HTTPException(status_code=400, detail="Case has no supply or demand data")
    result = run_allocation(data, trace_component_key=component_key.strip())
    trace = result.get("_trace", [])
    return {"component_key": component_key, "_trace": trace, "trace_event_count": len(trace)}


def _component_key_to_at(ck: str) -> str:
    """Format component key as product@location (e.g. 502-2119@1000)."""
    if "|" in ck:
        a, b = ck.split("|", 1)
        return f"{a}@{b}"
    return ck


def _inventory_node_display(node_id: str, sorted_dates: list[str]) -> str:
    """Format inventory node id (product|location|period) or demand|id for display."""
    if node_id.startswith("demand|"):
        return f"demand {node_id.split('|', 1)[1]}"
    parts = node_id.rsplit("|", 1)
    if len(parts) == 2:
        comp, period_str = parts[0], parts[1]
        try:
            period = int(period_str)
            date_str = period_to_date(period, sorted_dates)
            return f"{_component_key_to_at(comp)} ({date_str or period_str})"
        except ValueError:
            pass
    return _component_key_to_at(node_id) if "|" in node_id else node_id


def _consume_from_basket_nodes_fifo(basket: dict[str, float], comp_key: str, need: float) -> None:
    """Consume up to `need` from basket nodes matching comp_key (product|location) in period order (FIFO)."""
    if need <= 0:
        return
    prefix = f"{comp_key}|"
    nodes = [n for n in basket if n.startswith(prefix) and basket[n] > 0]
    nodes.sort(key=lambda n: (int(n.split("|")[-1]) if n.split("|")[-1].isdigit() else 0, n))
    remaining = need
    for node in nodes:
        if remaining <= 0:
            break
        take = min(basket[node], remaining)
        if take > 0:
            basket[node] -= take
            remaining -= take
            if basket[node] <= 0:
                del basket[node]


def _consume_from_basket_nodes_fifo_return_delta(
    basket: dict[str, float], comp_key: str, need: float
) -> list[tuple[str, float]]:
    """Consume up to `need` from basket (FIFO); return list of (node, qty_taken) for delta tracking."""
    if need <= 0:
        return []
    prefix = f"{comp_key}|"
    nodes = [n for n in basket if n.startswith(prefix) and basket[n] > 0]
    nodes.sort(key=lambda n: (int(n.split("|")[-1]) if n.split("|")[-1].isdigit() else 0, n))
    remaining = need
    taken: list[tuple[str, float]] = []
    for node in nodes:
        if remaining <= 0:
            break
        take = min(basket[node], remaining)
        if take > 0:
            basket[node] -= take
            remaining -= take
            taken.append((node, take))
            if basket[node] <= 0:
                del basket[node]
    return taken


def _replay_basket_snapshots(
    supplies: list,
    actions_sorted: list,
    bom_rate: dict,
    lead_time_by_variant: dict,
    move_transit: dict,
    date_to_period: dict,
    sorted_dates: list[str],
    supply_adj: dict[str, float] | None = None,
    max_deltas_to_keep: int | None = None,
) -> tuple[list[dict], list[dict], dict[str, int], dict[str, int], list[dict]]:
    """
    Replay allocation in scarcity order; record only incremental deltas per step (purged/added).
    Basket is initialized with the initial set of supplies only (per spec); supply_adj applies
    case overrides so the view matches the run's initial state.
    If max_deltas_to_keep is set, only that many deltas are appended (to limit payload); all steps are still replayed.
    Returns (initial_basket_items, basket_deltas_per_step, from_inventory_id_to_step, demand_id_to_step, basket_final).
    """
    supply_adj = supply_adj or {}
    # Initial basket: from initial supplies only (spec: Init basket_alloc with initial supplies)
    basket: dict[str, float] = defaultdict(float)
    for s in supplies:
        comp_key = f"{getattr(s, 'product_id')}|{getattr(s, 'location_id') or ''}"
        qty = float(getattr(s, "qty", None) or 0) + supply_adj.get(comp_key, 0)
        if qty <= 0:
            continue
        period = supply_period(getattr(s, "supply_date", None), date_to_period)
        node_id = f"{comp_key}|{period}"
        basket[node_id] = basket.get(node_id, 0) + qty

    # Same scarcity as engine: quantity-only. Total qty per component (product|location); scarcest = smallest.
    def _comp_from_node(node_id: str) -> str:
        return node_id.rsplit("|", 1)[0] if "|" in node_id else node_id

    comp_totals: dict[str, float] = defaultdict(float)
    for node_id, qty in basket.items():
        comp_totals[_comp_from_node(node_id)] += qty

    def _scarcity_sort_key(item: tuple[str, float]) -> tuple[float, str]:
        node_id, qty = item[0], item[1]
        comp = _comp_from_node(node_id)
        return (comp_totals[comp], node_id)

    # Initial basket as list for transfer (once), sorted by same scarcity as engine: smallest total qty first
    initial_items = [
        {"key": k, "display": _inventory_node_display(k, sorted_dates), "qty": round(v, 4)}
        for k, v in sorted(basket.items(), key=_scarcity_sort_key) if v > 0
    ]

    scarcity_order: list[str] = []  # order components first appear as critical (for from_inventory_id_to_step)
    scarcity_seen: set[str] = set()

    basket_deltas: list[dict] = []  # per action: { "purged": [...], "added": [...] }
    from_inventory_id_to_step: dict[str, int] = {}
    demand_id_to_step: dict[str, int] = {}

    for step_index, a in enumerate(actions_sorted):
        req_keys = a.req_component_ids or []
        variant_key = a.variant_key or ""
        target_pid = a.target_product_id or ""
        target_loc = a.target_location_id or ""
        qty = float(a.qty or 0)
        edge_type = getattr(a, "edge_type", None) or "make"
        out_per = getattr(a, "output_period", None) or 0

        critical_component_key = None
        if req_keys:
            by_avail = []
            for i, ck in enumerate(req_keys):
                prefix = f"{ck}|"
                avail = sum(v for n, v in basket.items() if n.startswith(prefix) or n == ck)
                by_avail.append((avail, i, ck))
            by_avail.sort(key=lambda x: (x[0], x[1], x[2]))
            critical_component_key = by_avail[0][2] if by_avail else None
            if critical_component_key and critical_component_key not in scarcity_seen:
                scarcity_seen.add(critical_component_key)
                scarcity_order.append(critical_component_key)

        period_from = out_per
        if edge_type == "move" and req_keys and variant_key:
            parts = req_keys[0].split("|", 1)
            from_loc = parts[1] if len(parts) > 1 else ""
            transit = move_transit.get((target_pid, from_loc, target_loc), 0)
            period_from = period_minus_days(out_per, transit, date_to_period, sorted_dates)
        elif edge_type == "make":
            lead = lead_time_by_variant.get((target_pid, target_loc), 0)
            period_from = period_minus_days(out_per, lead, date_to_period, sorted_dates)
        from_comp = critical_component_key or (req_keys[0] if req_keys else "")
        from_inventory_id = f"{from_comp}|{period_from}" if from_comp else ""

        purged: list[dict] = []
        req_rates = getattr(a, "req_rates", None)
        if not req_rates or len(req_rates) != len(req_keys):
            req_rates = None
        for i, ck in enumerate(req_keys):
            if req_rates is not None and i < len(req_rates) and req_rates[i] is not None and float(req_rates[i]) > 0:
                rate = float(req_rates[i])
            else:
                comp_product = ck.split("|", 1)[0] if "|" in ck else ck
                rate = float(bom_rate.get((target_pid, comp_product), 1.0)) if edge_type == "make" else 1.0
            need = qty / rate if rate > 0 else 0.0
            for node, take in _consume_from_basket_nodes_fifo_return_delta(basket, ck, need):
                purged.append({"key": node, "display": _inventory_node_display(node, sorted_dates), "qty": round(take, 4)})
        added: list[dict] = []
        if variant_key:
            out_node = f"{variant_key}|{out_per}"
            basket[out_node] = basket.get(out_node, 0) + qty
            added.append({"key": out_node, "display": _inventory_node_display(out_node, sorted_dates), "qty": round(qty, 4)})

        if max_deltas_to_keep is None or step_index < max_deltas_to_keep:
            basket_deltas.append({"purged": purged, "added": added})
        from_inventory_id_to_step[from_inventory_id] = step_index
        demand_id = getattr(a, "demand_id", None)
        if demand_id:
            demand_id_to_step[demand_id] = step_index

    # Final basket state after all steps (so "basket at last step" is correct even when we cap deltas)
    comp_totals_final: dict[str, float] = defaultdict(float)
    for node_id, qty in basket.items():
        if qty > 0:
            comp_totals_final[_comp_from_node(node_id)] += qty
    basket_final = [
        {"key": k, "display": _inventory_node_display(k, sorted_dates), "qty": round(v, 4)}
        for k, v in basket.items() if v > 0
    ]
    basket_final.sort(key=lambda x: (comp_totals_final.get(_comp_from_node(x["key"]), 0), x["key"]))
    return (initial_items, basket_deltas, from_inventory_id_to_step, demand_id_to_step, basket_final)


def _build_allocation_view_flat(
    actions: list,
    supplies: list,
    demands: list,
    date_to_period: dict,
    sorted_dates: list[str],
    key_to_supply_id: dict,
    lead_time_by_variant: dict,
    move_transit: dict,
    product_to_demands: dict,
) -> tuple[list[dict], list[dict]]:
    """Build flat inv→inv rows (with scarcity_rank) and inv→demand rows. Returns (inv_rows, demand_rows).
    Actions must be in scarcity order (same as engine) so 'available' and critical component match the run."""
    available: dict[str, float] = {}
    for s in supplies:
        key = _norm_comp_key(f"{s.product_id}|{s.location_id or ''}")
        available[key] = available.get(key, 0) + float(s.qty or 0)

    inv_rows: list[dict] = []
    demand_rows: list[dict] = []
    for a in actions:
        qty = round(float(a.qty or 0), 4)
        if qty <= 0:
            continue
        target_pid = a.target_product_id or ""
        target_loc = a.target_location_id or ""
        variant_key = a.variant_key or ""
        demand_ids = product_to_demands.get(target_pid, [])
        req_keys = a.req_component_ids or []
        req_keys_norm = [_norm_comp_key(ck) for ck in req_keys]
        edge_type = getattr(a, "edge_type", None) or "make"
        out_per = getattr(a, "output_period", None) or 0
        scarcity_rank = getattr(a, "scarcity_rank", None)

        critical_component_key: str | None = None
        critical_component_index: int | None = None
        if req_keys_norm:
            by_avail = [(available.get(ck, 0), i, ck) for i, ck in enumerate(req_keys_norm)]
            by_avail.sort(key=lambda x: (x[0], x[1], x[2]))
            critical_component_key = by_avail[0][2] if by_avail else None
            critical_component_index = by_avail[0][1] if by_avail else None
        req_rates_flat = getattr(a, "req_rates", None) or []
        for i, ck in enumerate(req_keys_norm):
            rate = 1.0
            if i < len(req_rates_flat) and req_rates_flat[i] is not None and float(req_rates_flat[i]) > 0:
                rate = float(req_rates_flat[i])
            need = qty / rate
            available[ck] = available.get(ck, 0) - need
        if variant_key:
            available[variant_key] = available.get(variant_key, 0) + qty

        period_from = out_per
        if edge_type == "move" and req_keys and variant_key:
            parts = req_keys[0].split("|", 1)
            from_loc = parts[1] if len(parts) > 1 else ""
            transit = move_transit.get((target_pid, from_loc, target_loc), 0)
            period_from = period_minus_days(out_per, transit, date_to_period, sorted_dates)
        elif edge_type == "make":
            lead = lead_time_by_variant.get((target_pid, target_loc), 0)
            period_from = period_minus_days(out_per, lead, date_to_period, sorted_dates)
        from_comp = critical_component_key or (req_keys_norm[0] if req_keys_norm else "")
        from_inventory_id = f"{from_comp}|{period_from}" if from_comp else ""
        to_inventory_id = f"{variant_key}|{out_per}" if variant_key else ""

        inv_rows.append({
            "edge_type": edge_type,
            "scarcity_rank": scarcity_rank,
            "from_inventory_id": from_inventory_id,
            "from_inventory_display": _inventory_node_display(from_inventory_id, sorted_dates),
            "to_inventory_id": to_inventory_id,
            "to_inventory_display": _inventory_node_display(to_inventory_id, sorted_dates),
            "from_components": req_keys,
            "critical_component_key": critical_component_key,
            "critical_component_index": critical_component_index,
            "to_variant_key": variant_key,
            "to_variant_key_display": _component_key_to_at(variant_key) if variant_key else "",
            "output_period": out_per,
            "output_date": period_to_date(out_per, sorted_dates),
            "qty": qty,
            "demand_ids": demand_ids,
            "supply_id": key_to_supply_id.get(req_keys_norm[0], "") if req_keys_norm else "",
        })

        if getattr(a, "demand_id", None) and variant_key:
            demand_rows.append({
                "edge_type": "make",
                "from_inventory_id": to_inventory_id,
                "from_inventory_display": _inventory_node_display(to_inventory_id, sorted_dates),
                "to_inventory_id": f"demand|{a.demand_id}",
                "to_inventory_display": _inventory_node_display(f"demand|{a.demand_id}", sorted_dates),
                "to_variant_key": variant_key,
                "output_period": out_per,
                "output_date": period_to_date(out_per, sorted_dates),
                "qty": qty,
                "demand_ids": [a.demand_id],
            })
    return inv_rows, demand_rows


MAX_ACTIONS_ALLOCATION_VIEW = 5_000
DEFAULT_MAX_ACTIONS_FIRST_LOAD = 300
ALLOCATION_VIEW_CACHE_MAX = 8
_allocation_view_cache: OrderedDict[tuple[int, int], list] = OrderedDict()


@router.get("/{case_id}/runs/{run_id}/allocation-actions")
def get_allocation_actions(
    case_id: int,
    run_id: int,
    db: Session = Depends(get_db),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
):
    """
    Lightweight: paginated raw allocation actions only (no view build, no basket).
    Use when allocation-view is too slow; scalable for large runs.
    """
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    total = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).count()
    rows = (
        db.query(AllocationAction)
        .filter(AllocationAction.run_id == run_id)
        .order_by(AllocationAction.scarcity_rank.asc().nulls_last(), AllocationAction.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    actions = [
        {
            "id": a.id,
            "run_id": a.run_id,
            "variant_key": a.variant_key,
            "req_component_ids": a.req_component_ids,
            "qty": a.qty,
            "demand_id": a.demand_id,
            "target_product_id": a.target_product_id,
            "target_location_id": a.target_location_id,
            "output_period": a.output_period,
            "edge_type": a.edge_type,
            "scarcity_rank": a.scarcity_rank,
        }
        for a in rows
    ]
    return {"actions": actions, "total_count": total, "offset": offset, "limit": limit}


@router.get("/{case_id}/runs/{run_id}/allocation-view")
def get_allocation_view(
    case_id: int,
    run_id: int,
    db: Session = Depends(get_db),
    max_actions: int | None = Query(None, description="Max actions to load (default 300 for faster response)"),
    from_step: int | None = Query(None, ge=1, description="Return only view rows from this step (1-based)"),
    to_step: int | None = Query(None, ge=1, description="Return only view rows up to this step (1-based inclusive)"),
    skip_basket: bool = Query(False, description="If True, skip basket replay for faster response (basket_after will be empty)"),
):
    """
    Allocation view: critical-component-centric, scarcity order. One row per critical component
    (inventory node); multiple target candidates grouped in same row with split explanation.
    Plus inv→demand rows for fulfillment traceability.
    Limited to first MAX_ACTIONS_ALLOCATION_VIEW actions to avoid timeouts/OOM on large runs.
    """
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    total_actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).count()
    limit = min(
        max_actions if max_actions is not None else DEFAULT_MAX_ACTIONS_FIRST_LOAD,
        MAX_ACTIONS_ALLOCATION_VIEW,
    )
    actions = (
        db.query(AllocationAction)
        .filter(AllocationAction.run_id == run_id)
        .order_by(AllocationAction.scarcity_rank.asc().nulls_last(), AllocationAction.id)
        .limit(limit)
        .all()
    )
    # Reuse the same actions for basket replay (no second query). Step indices always needed for view rows.
    actions_for_basket = list(actions) if actions else []
    supplies = db.query(Supply).filter(Supply.case_id == case_id).all()
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    date_to_period, sorted_dates = build_period_index(
        [{"supply_date": getattr(s, "supply_date", None)} for s in supplies],
        [{"request_due_time": getattr(d, "request_due_time", None)} for d in demands],
    )
    key_to_supply_id = {}
    for s in supplies:
        key = _norm_comp_key(f"{s.product_id}|{s.location_id or ''}")
        if key not in key_to_supply_id:
            key_to_supply_id[key] = s.supply_id
    lead_time_by_variant = {}
    for m in db.query(MethodMake).filter(MethodMake.case_id == case_id).all():
        lead_time_by_variant[(m.product_id, m.location_id or "")] = float(m.lead_time or 0)
    move_transit = {}
    for mv in db.query(MethodMove).filter(MethodMove.case_id == case_id).all():
        fl, tl = mv.from_location_id or "", mv.to_location_id or ""
        if fl != tl:
            move_transit[(mv.product_id, fl, tl)] = float(mv.transit_time or 0)
    product_to_demands = defaultdict(list)
    for d in demands:
        product_to_demands[d.product_id].append(d.demand_id)

    bom_rate: dict[tuple[str, str], float] = {}
    for b in db.query(Bom).filter(Bom.case_id == case_id).all():
        key = (b.parent_id, b.child_id)
        if key not in bom_rate:
            bom_rate[key] = float(b.rate or 1.0)
    actions_sorted = sorted(actions, key=lambda a: (getattr(a, "scarcity_rank") if getattr(a, "scarcity_rank") is not None else 999999, a.id or 0))
    actions_for_basket_sorted = (
        sorted(actions_for_basket, key=lambda a: (getattr(a, "scarcity_rank") if getattr(a, "scarcity_rank") is not None else 999999, a.id or 0))
        if actions_for_basket else []
    )

    # Same supply overrides as run (basket = initial supplies per spec; overrides define initial state)
    supply_adj: dict[str, float] = {}
    for o in db.query(ManualOverride).filter(ManualOverride.case_id == case_id).all():
        payload = (o.payload or {}) if hasattr(o, "payload") else {}
        if getattr(o, "entity_type", None) == "supply" and "quantity" in payload:
            supply_adj[getattr(o, "entity_key", "") or ""] = float(payload.get("quantity", 0))

    basket_initial: list[dict] = []
    basket_deltas: list[dict] = []
    from_inventory_id_to_step: dict[str, int] = {}
    demand_id_to_step: dict[str, int] = {}
    basket_final: list[dict] = []
    if actions_for_basket_sorted:
        basket_initial, basket_deltas, from_inventory_id_to_step, demand_id_to_step, basket_final = _replay_basket_snapshots(
            supplies, actions_for_basket_sorted, bom_rate, lead_time_by_variant, move_transit, date_to_period, sorted_dates,
            supply_adj=supply_adj,
            max_deltas_to_keep=MAX_ACTIONS_ALLOCATION_VIEW,
        )

    inv_rows, demand_rows = _build_allocation_view_flat(
        actions_sorted, supplies, demands, date_to_period, sorted_dates,
        key_to_supply_id, lead_time_by_variant, move_transit, product_to_demands,
    )

    # Group inv rows by critical component node (from_inventory_id); scarcity order
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in inv_rows:
        fid = r.get("from_inventory_id") or ""
        if fid:
            groups[fid].append(r)

    component_rows = []
    for from_inventory_id, rows in groups.items():
        if not rows:
            continue
        r0 = rows[0]
        scarcity_rank = min(
            (r.get("scarcity_rank") for r in rows if r.get("scarcity_rank") is not None),
            default=999999,
        )
        # Aggregate by (to_inventory_id, edge_type) to avoid duplicate candidate lines
        agg: dict[tuple[str, str], float] = defaultdict(float)
        for r in rows:
            q = r.get("qty") or 0
            if q <= 0:
                continue
            key = (r.get("to_inventory_id") or "", r.get("edge_type") or "")
            agg[key] += q
        candidates = [
            {
                "to_inventory_id": to_id,
                "to_inventory_display": _inventory_node_display(to_id, sorted_dates) if to_id else "",
                "qty": round(qty, 4),
                "edge_type": edge_type,
                "to_variant_key": to_id.rsplit("|", 1)[0] if to_id and "|" in to_id else (to_id or ""),
            }
            for (to_id, edge_type), qty in sorted(agg.items(), key=lambda x: (-x[1], x[0]))
            if qty > 0
        ]
        if not candidates:
            continue
        total_qty = sum(c["qty"] for c in candidates)
        pct_parts = []
        for c in candidates:
            pct = (c["qty"] / total_qty * 100) if total_qty else 0
            pct_parts.append(f"{c['to_inventory_display']}: {c['qty']} ({pct:.0f}%)")
        split_summary = " | ".join(pct_parts)
        split_explanation = (
            "Split by target weight (demand-priority): allocation = (target_weight / total_target_weight) × available. "
            "This critical component limited the step; quantities above are the resulting output per candidate. "
            f"Details: {split_summary}. Click component for full formula."
        )
        all_demand_ids = []
        for r in rows:
            all_demand_ids.extend(r.get("demand_ids") or [])
        component_rows.append({
            "row_type": "component",
            "scarcity_rank": scarcity_rank,
            "edge_type": r0.get("edge_type"),  # primary; row can mix make/move
            "from_inventory_id": from_inventory_id,
            "from_inventory_display": r0["from_inventory_display"],
            "critical_component_key": r0.get("critical_component_key"),
            "to_inventory_id": None,
            "to_inventory_display": None,
            "candidates": candidates,
            "total_qty": round(total_qty, 4),
            "split_explanation": split_explanation,
            "output_date": r0.get("output_date"),
            "output_period": r0.get("output_period"),
            "demand_ids": list(dict.fromkeys(all_demand_ids)),
            "supply_id": r0.get("supply_id"),
            "basket_step_index": from_inventory_id_to_step.get(from_inventory_id, 0),
        })

    component_rows.sort(key=lambda r: (r["scarcity_rank"], r["from_inventory_id"]))

    for d in demand_rows:
        d["row_type"] = "demand"
        d["candidates"] = []
        d["total_qty"] = d.get("qty", 0)
        d["split_explanation"] = None
        d["critical_component_key"] = None
        d["supply_id"] = ""
        did = (d.get("demand_ids") or [None])[0]
        d["basket_step_index"] = demand_id_to_step.get(did, 0) if did else 0

    full_result = component_rows + demand_rows
    total_steps = len(full_result)
    for i, row in enumerate(full_result):
        row["step"] = i + 1

    # Cache full view by (run_id, num_actions) for delta/pagination (same run, same scope = cache hit)
    cache_key = (run_id, len(actions))
    if cache_key in _allocation_view_cache:
        _allocation_view_cache.move_to_end(cache_key)
        full_result = _allocation_view_cache[cache_key]
    else:
        _allocation_view_cache[cache_key] = full_result
        _allocation_view_cache.move_to_end(cache_key)
        while len(_allocation_view_cache) > ALLOCATION_VIEW_CACHE_MAX:
            _allocation_view_cache.popitem(last=False)

    # Return slice for delta/pagination
    step_from = (from_step - 1) if from_step is not None else 0
    step_to = to_step if to_step is not None else total_steps
    step_to = min(step_to, total_steps)
    result = full_result[step_from:step_to]
    basket_prunes = (run.config or {}).get("prunes", []) if run else []
    # When skip_basket, omit basket payload so client can load it on-demand when opening the basket slide-in.
    if skip_basket:
        basket_initial, basket_deltas, basket_final, basket_prunes = [], [], None, []
    return {
        "run_id": run_id,
        "allocation_view": result,
        "truncated": total_actions > len(actions),
        "total_actions": total_actions,
        "limit": limit,
        "total_steps": total_steps,
        "from_step": step_from + 1 if result else None,
        "to_step": step_to if result else None,
        "basket_initial": basket_initial,
        "basket_deltas": basket_deltas,
        "basket_final": basket_final,
        "basket_prunes": basket_prunes,
    }


@router.get("/{case_id}/runs/{run_id}/allocation-view-basket")
def get_allocation_view_basket(
    case_id: int,
    run_id: int,
    db: Session = Depends(get_db),
    max_actions: int | None = Query(None, description="Max actions to replay (default same as allocation-view)"),
):
    """
    Return only basket state for the run (initial, deltas, final, prunes).
    Use when the allocation view was loaded with skip_basket=True and the user opens the basket slide-in.
    """
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    limit = min(
        max_actions if max_actions is not None else DEFAULT_MAX_ACTIONS_FIRST_LOAD,
        MAX_ACTIONS_ALLOCATION_VIEW,
    )
    actions = (
        db.query(AllocationAction)
        .filter(AllocationAction.run_id == run_id)
        .order_by(AllocationAction.scarcity_rank.asc().nulls_last(), AllocationAction.id)
        .limit(limit)
        .all()
    )
    supplies = db.query(Supply).filter(Supply.case_id == case_id).all()
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    date_to_period, sorted_dates = build_period_index(
        [{"supply_date": getattr(s, "supply_date", None)} for s in supplies],
        [{"request_due_time": getattr(d, "request_due_time", None)} for d in demands],
    )
    lead_time_by_variant = {}
    for m in db.query(MethodMake).filter(MethodMake.case_id == case_id).all():
        lead_time_by_variant[(m.product_id, m.location_id or "")] = float(m.lead_time or 0)
    move_transit = {}
    for mv in db.query(MethodMove).filter(MethodMove.case_id == case_id).all():
        fl, tl = mv.from_location_id or "", mv.to_location_id or ""
        if fl != tl:
            move_transit[(mv.product_id, fl, tl)] = float(mv.transit_time or 0)
    bom_rate: dict[tuple[str, str], float] = {}
    for b in db.query(Bom).filter(Bom.case_id == case_id).all():
        key = (b.parent_id, b.child_id)
        if key not in bom_rate:
            bom_rate[key] = float(b.rate or 1.0)
    supply_adj: dict[str, float] = {}
    for o in db.query(ManualOverride).filter(ManualOverride.case_id == case_id).all():
        payload = (o.payload or {}) if hasattr(o, "payload") else {}
        if getattr(o, "entity_type", None) == "supply" and "quantity" in payload:
            supply_adj[getattr(o, "entity_key", "") or ""] = float(payload.get("quantity", 0))
    actions_for_basket_sorted = sorted(
        actions,
        key=lambda a: (getattr(a, "scarcity_rank") if getattr(a, "scarcity_rank") is not None else 999999, a.id or 0),
    )
    basket_initial: list[dict] = []
    basket_deltas: list[dict] = []
    basket_final: list[dict] = []
    if actions_for_basket_sorted:
        basket_initial, basket_deltas, _, _, basket_final = _replay_basket_snapshots(
            supplies, actions_for_basket_sorted, bom_rate, lead_time_by_variant, move_transit, date_to_period, sorted_dates,
            supply_adj=supply_adj,
            max_deltas_to_keep=MAX_ACTIONS_ALLOCATION_VIEW,
        )
    basket_prunes = (run.config or {}).get("prunes", []) if run else []
    return {
        "basket_initial": basket_initial,
        "basket_deltas": basket_deltas,
        "basket_final": basket_final,
        "basket_prunes": basket_prunes,
    }
