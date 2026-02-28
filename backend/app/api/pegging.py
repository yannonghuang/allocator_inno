"""
Pegging: sub-graph of the inventory graph (DAG).

Per spec, nodes are inventory(product, location, quantity, time); edges are defined by
method_move and method_make with rigorous quantity and time propagation. This API returns
a graph where each node carries product_id, location_id, quantity (and optionally period/time
when run data exists); each edge carries from, to, qty, and optionally period_from/period_to
so that edges represent inventory(product, loc, qty, time_source) -> inventory(..., time_target).
"""
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.allocate import _feasible_demands_from_actions
from app.database import get_db
from app.models import AllocationAction, AllocationRun, Bom, Demand, MethodMake, MethodMove, Supply
from app.services.time_utils import (
    build_period_index,
    demand_due_period,
    period_to_date,
    supply_period,
)

router = APIRouter(prefix="/cases", tags=["Pegging"])

# Inventory node id: product|location|period (period = time index; 0 = preexisting)
def _inv_node_id(product: str, location: str, period: int) -> str:
    return f"{product}|{location or ''}|{period}"


def _action_component_keys_fallback(
    db: Session, case_id: int, variant_key: str, edge_type: str
) -> list[str]:
    """When req_component_ids is missing on an action, derive comp keys from BOM (make) or MethodMove (move)."""
    if not variant_key or "|" not in variant_key:
        return []
    pid, to_loc = variant_key.split("|", 1)
    edge_type = (edge_type or "make").lower()
    if edge_type == "move":
        mv = (
            db.query(MethodMove)
            .filter(
                MethodMove.case_id == case_id,
                MethodMove.product_id == pid,
                MethodMove.to_location_id == to_loc,
            )
            .first()
        )
        if mv and (mv.from_location_id or "") != (mv.to_location_id or ""):
            return [f"{pid}|{mv.from_location_id or ''}"]
        return []
    # make: BOM children for this parent; component at make location
    boms = db.query(Bom).filter(Bom.case_id == case_id, Bom.parent_id == pid).all()
    return [f"{b.child_id}|{to_loc}" for b in boms]


def _comp_node(ckey: str) -> dict:
    parts = ckey.split("|", 1)
    return {
        "id": ckey,
        "label": ckey,
        "type": "component",
        "product_id": parts[0] if parts else "",
        "location_id": parts[1] if len(parts) > 1 else "",
    }


def _variant_node(vkey: str, product_id: str, location_id: str) -> dict:
    return {"id": vkey, "label": vkey, "type": "variant", "product_id": product_id, "location_id": location_id or ""}


def _build_graph_from_case_data(db: Session, case_id: int) -> tuple[dict[str, dict], list[dict], dict[str, float]]:
    """
    Build inventory graph (DAG) per spec: nodes = inventory(product, location, quantity, time);
    edges = method_make (component -> variant, qty = consumption per unit) and method_move
    (from_loc -> to_loc, qty conserved). Compact: one edge per recipe; qty on edge is per-unit
    until overlain by run consumption. Node id = product|location (time/period attached when run exists).
    """
    supplies = db.query(Supply).filter(Supply.case_id == case_id).all()
    supply_by_comp: dict[str, float] = defaultdict(float)
    for s in supplies:
        key = f"{s.product_id}|{s.location_id or ''}"
        supply_by_comp[key] += float(s.qty or 0)
    nodes: dict[str, dict] = {}
    # (from, to) -> single edge; no duplicate children per parent, qty not summed
    edge_seen: set[tuple[str, str]] = set()
    edges: list[dict] = []

    # BOM: parent_id -> list of (child_id, rate). Spec: quantity_i >= quantity/rate_i => consumption per unit output = 1/rate
    boms = db.query(Bom).filter(Bom.case_id == case_id).all()
    bom_by_parent: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for b in boms:
        rate = float(b.rate) if b.rate is not None and b.rate != 0 else 1.0
        bom_by_parent[b.parent_id].append((b.child_id, rate))

    # 1) method_make: (product_id, location) consumes (child_id, location) for each child; edge qty = 1/rate (consumption per unit output)
    method_makes = db.query(MethodMake).filter(MethodMake.case_id == case_id).all()
    product_to_location: dict[str, str] = {}
    for m in method_makes:
        pid, loc = m.product_id, (m.location_id or "")
        product_to_location[pid] = loc
        vkey = f"{pid}|{loc}"
        nodes[vkey] = _variant_node(vkey, pid, loc)
        for child_id, rate in bom_by_parent.get(pid, []):
            ckey = f"{child_id}|{loc}"
            if ckey not in nodes:
                nodes[ckey] = _comp_node(ckey)
            key = (ckey, vkey)
            if key not in edge_seen:
                edge_seen.add(key)
                qty = 1.0 / rate if rate > 0 else 1.0
                edges.append({"from": ckey, "to": vkey, "qty": qty})

    # 2) method_move: (product, from_loc) -> (product, to_loc); one edge per move recipe
    method_moves = db.query(MethodMove).filter(MethodMove.case_id == case_id).all()
    for mv in method_moves:
        pid = mv.product_id
        from_loc = mv.from_location_id or ""
        to_loc = mv.to_location_id or ""
        if from_loc == to_loc:
            continue
        vkey = f"{pid}|{to_loc}"
        ckey = f"{pid}|{from_loc}"
        if vkey not in nodes:
            nodes[vkey] = _variant_node(vkey, pid, to_loc)
        if ckey not in nodes:
            nodes[ckey] = _comp_node(ckey)
        key = (ckey, vkey)
        if key not in edge_seen:
            edge_seen.add(key)
            edges.append({"from": ckey, "to": vkey, "qty": 1.0})

    # 3) All supply (product|location) as component nodes
    for s in supplies:
        ckey = f"{s.product_id}|{s.location_id or ''}"
        if ckey not in nodes:
            nodes[ckey] = _comp_node(ckey)

    # 4) Demand targets (product, location) as variant nodes; aggregate demand quantity per variant
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    demand_by_variant: dict[str, float] = defaultdict(float)
    for d in demands:
        loc = (getattr(d, "location_id", None) or product_to_location.get(d.product_id, "") or "VIRTUAL")
        vkey = f"{d.product_id}|{loc}"
        demand_by_variant[vkey] += float(d.quantity or 0)
        if vkey not in nodes:
            nodes[vkey] = _variant_node(vkey, d.product_id, loc)

    # 5) Attach real quantities to nodes: supply qty for component nodes, demand qty for demand-target variants
    for nid, n in nodes.items():
        n["qty"] = supply_by_comp.get(nid, 0.0)
        if nid in demand_by_variant and demand_by_variant[nid] != 0:
            n["demand_qty"] = demand_by_variant[nid]

    # 6) Align node type with edge role: any node that is "from" (has outgoing edges) is component
    #    so method_make and method_move nodes are treated the same in the frontend tree.
    for e in edges:
        from_id = e["from"]
        to_id = e["to"]
        if from_id in nodes:
            nodes[from_id]["type"] = "component"
        if to_id in nodes and nodes[to_id].get("type") != "component":
            nodes[to_id]["type"] = "variant"

    return nodes, edges, dict(supply_by_comp)


def _build_inventory_graph(
    db: Session, case_id: int, run_id: int
) -> tuple[dict[str, dict], list[dict], dict[str, float], list[str]]:
    """
    Build pegging graph with nodes = inventory(product, location, quantity, time).
    Node id = product|location|period. Each node is one inventory state (date/period + qty).
    Edges from allocation actions: inventory(..., period_from) -> inventory(..., period_to) with qty.
    Returns (nodes, edges, supply_qty_by_node_id, sorted_dates).
    """
    supplies = db.query(Supply).filter(Supply.case_id == case_id).all()
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    supply_list = [{"supply_date": getattr(s, "supply_date", None)} for s in supplies]
    demand_list = [{"request_due_time": getattr(d, "request_due_time", None)} for d in demands]
    date_to_period, sorted_dates = build_period_index(supply_list, demand_list)

    # Nodes: (product, location, period) -> quantity and optional date label
    inv_qty: dict[str, float] = defaultdict(float)
    # From supplies: one inventory node per (product, location, period)
    for s in supplies:
        period = supply_period(getattr(s, "supply_date", None), date_to_period)
        nid = _inv_node_id(s.product_id, s.location_id or "", period)
        inv_qty[nid] += float(s.qty or 0)
    # From allocation: produced inventory at (target_product, target_location, output_period)
    actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).all()
    make_lead: dict[tuple[str, str], int] = {}
    for m in db.query(MethodMake).filter(MethodMake.case_id == case_id).all():
        make_lead[(m.product_id, m.location_id or "")] = int(m.lead_time or 0)
    move_transit: dict[tuple[str, str, str], int] = {}
    for mv in db.query(MethodMove).filter(MethodMove.case_id == case_id).all():
        fl, tl = mv.from_location_id or "", mv.to_location_id or ""
        if fl != tl:
            move_transit[(mv.product_id, fl, tl)] = int(mv.transit_time or 0)

    boms = db.query(Bom).filter(Bom.case_id == case_id).all()
    bom_rate: dict[tuple[str, str], float] = {}
    for b in boms:
        rate = float(b.rate) if b.rate and b.rate != 0 else 1.0
        bom_rate[(b.parent_id, b.child_id)] = rate

    for a in actions:
        variant_key = a.variant_key or ""
        out_per = int(a.output_period or 0)
        output_qty = float(a.qty or 0)
        inv_qty[_inv_node_id(variant_key.split("|", 1)[0] if "|" in variant_key else variant_key, variant_key.split("|", 1)[1] if "|" in variant_key else "", out_per)] += output_qty

    # Edges: link from inventory nodes to variants that consumed them. Assign consumption FIFO by period so edge qtys stay whole when allocation is whole.
    edge_real_qty: dict[tuple[str, str], tuple[float, int, int]] = {}  # (from, to) -> (qty, period_from, period_to)
    used_by_node: dict[str, float] = defaultdict(float)  # from_nid -> total already assigned from this node
    comp_key_prefix = "|"  # nid = product|location|period so comp_key + "|" prefixes the period part
    for a in actions:
        variant_key = a.variant_key or ""
        out_per = int(a.output_period or 0)
        output_qty = float(a.qty or 0)
        edge_type = (a.edge_type or "make").lower()
        pid = variant_key.split("|", 1)[0] if "|" in variant_key else variant_key
        to_loc = variant_key.split("|", 1)[1] if "|" in variant_key else ""
        to_nid = _inv_node_id(pid, to_loc, out_per)
        req_component_ids = a.req_component_ids
        if not req_component_ids:
            req_component_ids = _action_component_keys_fallback(db, case_id, variant_key, a.edge_type or "make")
        for comp_key in req_component_ids or []:
            comp_parts = comp_key.split("|", 1)
            comp_product = comp_parts[0] if comp_parts else ""
            if edge_type == "move":
                consumption = output_qty
            else:
                rate = bom_rate.get((pid, comp_product), 1.0)
                consumption = output_qty / rate if rate > 0 else 0.0
            prefix = comp_key + comp_key_prefix
            # Matching nodes with remaining qty, sorted by period (FIFO)
            matching = []
            for from_nid, q in inv_qty.items():
                if q <= 0 or not from_nid.startswith(prefix):
                    continue
                avail = q - used_by_node.get(from_nid, 0)
                if avail > 0:
                    parts = from_nid.split("|", 2)
                    pf = int(parts[2]) if len(parts) > 2 else 0
                    matching.append((from_nid, avail, pf))
            matching.sort(key=lambda x: (x[2], x[0]))  # period, then nid
            remaining = consumption
            for from_nid, avail, pf in matching:
                if remaining <= 0:
                    break
                take = min(avail, remaining)
                if take <= 0:
                    continue
                key = (from_nid, to_nid)
                if key not in edge_real_qty:
                    edge_real_qty[key] = (take, pf, out_per)
                else:
                    existing_q, epf, ept = edge_real_qty[key]
                    edge_real_qty[key] = (existing_q + take, epf, ept)
                used_by_node[from_nid] = used_by_node.get(from_nid, 0) + take
                remaining -= take
    edges = [{"from": f, "to": t, "qty": round(q, 4), "period_from": pf, "period_to": pt} for (f, t), (q, pf, pt) in edge_real_qty.items() if q > 0]

    product_to_location = {}
    for m in db.query(MethodMake).filter(MethodMake.case_id == case_id).all():
        product_to_location[m.product_id] = m.location_id or ""
    demand_by_variant: dict[str, float] = defaultdict(float)
    demand_count_by_variant: dict[str, int] = defaultdict(int)
    for d in demands:
        loc = (getattr(d, "location_id", None) or product_to_location.get(d.product_id, "") or "VIRTUAL")
        vk = f"{d.product_id}|{loc}"
        demand_by_variant[vk] += float(d.quantity or 0)
        demand_count_by_variant[vk] += 1
    # Build node dict: id, label (include date when known), product_id, location_id, period, qty, type
    nodes: dict[str, dict] = {}
    for nid, qty in inv_qty.items():
        parts = nid.split("|", 2)
        product_id = parts[0] if parts else ""
        location_id = parts[1] if len(parts) > 1 else ""
        period = int(parts[2]) if len(parts) > 2 else 0
        date_str = period_to_date(period, sorted_dates) if period > 0 else "preexisting"
        label = f"{product_id}|{location_id} ({date_str})"
        n = {
            "id": nid,
            "label": label,
            "product_id": product_id,
            "location_id": location_id,
            "period": period,
            "qty": qty,
            "type": "component" if nid in [e["from"] for e in edges] else "variant",
        }
        variant_key = f"{product_id}|{location_id}"
        if variant_key in demand_by_variant and demand_by_variant[variant_key] != 0:
            n["demand_qty"] = demand_by_variant[variant_key]
            n["demand_count"] = demand_count_by_variant.get(variant_key, 0)
        nodes[nid] = n
    for e in edges:
        if e["to"] in nodes and nodes[e["to"]].get("type") != "component":
            nodes[e["to"]]["type"] = "variant"

    # Raw allocated qty per (variant_key, demand_id) from actions (for total_allocated_by_variant)
    allocated: dict[tuple[str, str], float] = defaultdict(float)
    for a in actions:
        vk = a.variant_key or ""
        did = getattr(a, "demand_id", None)
        if vk and did:
            allocated[(vk, did)] += float(a.qty or 0)

    # Total allocated per variant (so we can cap each node's outflow at node qty)
    total_allocated_by_variant: dict[str, float] = defaultdict(float)
    for (vk, _did), a in allocated.items():
        if a > 0:
            total_allocated_by_variant[vk] += a

    # Capped allocation per demand (same as Suggested revised demands: allocated <= requested)
    feasible = _feasible_demands_from_actions(db, case_id, actions)
    capped_alloc_by_demand: dict[str, float] = {str(f["demand_id"]): float(f.get("allocated_qty") or 0) for f in feasible}

    # Outflow from each node already used by inv->inv edges (so we don't over-commit when adding inv->demand)
    out_flow_inv_inv: dict[str, float] = defaultdict(float)
    for e in edges:
        out_flow_inv_inv[e["from"]] += float(e.get("qty") or 0)

    # Track how much of each node we've assigned to demands (FIFO across demands so sums add up)
    used_by_node_demand: dict[str, float] = defaultdict(float)

    # Add demand nodes and inv->demand edges so sum(edges to demand) = capped alloc and node outflow <= node qty
    for d in demands:
        demand_nid = f"demand|{d.demand_id}"
        loc = (getattr(d, "location_id", None) or product_to_location.get(d.product_id, "") or "VIRTUAL")
        variant_key = f"{d.product_id}|{loc}"
        demand_qty = float(d.quantity or 0)
        alloc = capped_alloc_by_demand.get(str(d.demand_id), 0.0)
        period = demand_due_period(getattr(d, "request_due_time", None), date_to_period)
        date_str = period_to_date(period, sorted_dates) if period and period > 0 else ("preexisting" if period == 0 else "–")
        nodes[demand_nid] = {
            "id": demand_nid,
            "label": f"Demand {d.demand_id} | {loc} ({date_str})",
            "product_id": d.product_id,
            "location_id": loc,
            "period": period,
            "qty": demand_qty,
            "demand_qty": demand_qty,
            "type": "demand",
            "demand_id": d.demand_id,
        }
        prefix = variant_key + "|"
        all_inv_nids = [nid for nid in nodes if not nid.startswith("demand|") and nid.startswith(prefix)]
        all_inv_nids.sort(key=lambda nid: (int(nid.split("|", 2)[2]) if len(nid.split("|", 2)) > 2 else 0, nid))
        remaining = max(0.0, alloc)
        for nid in all_inv_nids:
            if remaining <= 0:
                break
            node_qty = inv_qty.get(nid, 0.0)
            already_inv = out_flow_inv_inv.get(nid, 0.0)
            already_demand = used_by_node_demand.get(nid, 0.0)
            available = max(0.0, node_qty - already_inv - already_demand)
            take = round(min(available, remaining), 4)
            if take > 0:
                edges.append({"from": nid, "to": demand_nid, "qty": take, "period_from": None, "period_to": None})
                used_by_node_demand[nid] = used_by_node_demand.get(nid, 0) + take
                remaining -= take

    # Guarantee: for each inventory node, sum(all edges from node) <= node qty (rescale inv->demand if needed)
    out_sum: dict[str, float] = defaultdict(float)
    inv_to_demand_edges: list[dict] = []
    other_edges: list[dict] = []
    for e in edges:
        if e["to"].startswith("demand|") and not e["from"].startswith("demand|"):
            inv_to_demand_edges.append(e)
        else:
            other_edges.append(e)
            out_sum[e["from"]] += float(e.get("qty") or 0)
    demand_sum_by_nid: dict[str, float] = defaultdict(float)
    for e in inv_to_demand_edges:
        out_sum[e["from"]] += float(e.get("qty") or 0)
        demand_sum_by_nid[e["from"]] += float(e.get("qty") or 0)
    for e in inv_to_demand_edges:
        nid = e["from"]
        node_qty = inv_qty.get(nid, 0)
        total = out_sum.get(nid, 0)
        demand_sum = demand_sum_by_nid.get(nid, 0)
        inv_inv_sum = total - demand_sum
        if total > 0 and node_qty >= 0 and demand_sum > 0:
            if total > node_qty:
                # Rescale so inv_inv_sum + scaled_demand <= node_qty
                scale = (node_qty - inv_inv_sum) / demand_sum if demand_sum > 0 else 0.0
                scale = max(0.0, min(1.0, scale))
                e["qty"] = round(float(e.get("qty") or 0) * scale, 4)
        elif demand_sum <= 0 or node_qty < 0:
            e["qty"] = 0.0
    edges = other_edges + inv_to_demand_edges

    supply_qty_by_node = {nid: float(n.get("qty") or 0) for nid, n in nodes.items() if n.get("type") != "demand"}
    for nid, n in nodes.items():
        if n.get("type") == "demand":
            supply_qty_by_node[nid] = 0.0
    return nodes, edges, supply_qty_by_node, sorted_dates


def _edge_real_qtys_from_run(
    db: Session, case_id: int, run_id: int, as_consumption: bool = False
) -> dict[tuple[str, str], float]:
    """
    From allocation actions for this run, aggregate per (from_comp, to_variant).
    If as_consumption: qty = consumption of parent (output_qty/rate for make, output_qty for move).
    Else: qty = output of child (rate * quantity).
    """
    boms = db.query(Bom).filter(Bom.case_id == case_id).all()
    bom_rate: dict[tuple[str, str], float] = {}
    for b in boms:
        rate = float(b.rate) if b.rate is not None and b.rate != 0 else 1.0
        bom_rate[(b.parent_id, b.child_id)] = rate
    actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).all()
    edge_real: dict[tuple[str, str], float] = defaultdict(float)
    for a in actions:
        variant_key = a.variant_key or ""
        output_qty = float(a.qty or 0)
        req_component_ids = a.req_component_ids or []
        edge_type = (a.edge_type or "make").lower()
        if not variant_key or output_qty <= 0:
            continue
        variant_product = variant_key.split("|", 1)[0] if "|" in variant_key else variant_key
        for comp_key in req_component_ids:
            if as_consumption:
                comp_product = comp_key.split("|", 1)[0] if "|" in comp_key else comp_key
                if edge_type == "move":
                    edge_real[(comp_key, variant_key)] += output_qty
                else:
                    rate = bom_rate.get((variant_product, comp_product), 1.0)
                    edge_real[(comp_key, variant_key)] += output_qty / rate if rate > 0 else 0.0
            else:
                edge_real[(comp_key, variant_key)] += output_qty
    return dict(edge_real)


def _attach_time_propagation(
    db: Session,
    case_id: int,
    run_id: int,
    nodes: dict[str, dict],
    edges: list[dict],
) -> None:
    """
    Attach time (period) to nodes and edges so the graph matches the inventory graph spec:
    nodes = inventory(product, location, quantity, time); edges propagate time (time_target = time_source + delta).
    """
    actions = db.query(AllocationAction).filter(AllocationAction.run_id == run_id).all()
    # (from_key, to_key) -> output_period and edge_type from an action that used this edge
    edge_period: dict[tuple[str, str], tuple[int, str]] = {}
    for a in actions:
        variant_key = a.variant_key or ""
        out_per = int(a.output_period or 0)
        edge_type = (a.edge_type or "make").lower()
        for comp_key in a.req_component_ids or []:
            key = (comp_key, variant_key)
            if key not in edge_period:
                edge_period[key] = (out_per, edge_type)
    # MethodMake: product_id, location_id -> lead_time (days)
    make_lead: dict[tuple[str, str], int] = {}
    for m in db.query(MethodMake).filter(MethodMake.case_id == case_id).all():
        make_lead[(m.product_id, m.location_id or "")] = int(m.lead_time or 0)
    # MethodMove: (product, from_loc, to_loc) -> transit (days)
    move_transit: dict[tuple[str, str, str], int] = {}
    for mv in db.query(MethodMove).filter(MethodMove.case_id == case_id).all():
        pid = mv.product_id
        fl, tl = mv.from_location_id or "", mv.to_location_id or ""
        if fl != tl:
            move_transit[(pid, fl, tl)] = int(mv.transit_time or 0)
    for e in edges:
        from_key, to_key = e["from"], e["to"]
        info = edge_period.get((from_key, to_key))
        if not info:
            continue
        period_to, edge_type = info
        e["period_to"] = period_to
        if edge_type == "move":
            parts_from = from_key.split("|", 1)
            parts_to = to_key.split("|", 1)
            pid = parts_to[0] if parts_to else ""
            from_loc = parts_from[1] if len(parts_from) > 1 else ""
            to_loc = parts_to[1] if len(parts_to) > 1 else ""
            transit = move_transit.get((pid, from_loc, to_loc), 0)
            e["period_from"] = period_to - transit
        else:
            parts_to = to_key.split("|", 1)
            pid = parts_to[0] if parts_to else ""
            loc = parts_to[1] if len(parts_to) > 1 else ""
            lead = make_lead.get((pid, loc), 0)
            e["period_from"] = period_to - lead
    # Node period: variant nodes get max output_period of actions producing them
    variant_period: dict[str, int] = defaultdict(int)
    for a in actions:
        vk = a.variant_key or ""
        if vk:
            variant_period[vk] = max(variant_period[vk], int(a.output_period or 0))
    for nid, n in nodes.items():
        if nid in variant_period:
            n["period"] = variant_period[nid]


def _inv_node_from_id(nid: str, sorted_dates: list[str], qty: float) -> dict:
    parts = nid.split("|", 2)
    product_id = parts[0] if parts else ""
    location_id = parts[1] if len(parts) > 1 else ""
    period = int(parts[2]) if len(parts) > 2 else 0
    date_str = period_to_date(period, sorted_dates) if period > 0 else "preexisting"
    return {
        "id": nid,
        "label": f"{product_id}|{location_id} ({date_str})",
        "product_id": product_id,
        "location_id": location_id,
        "period": period,
        "qty": qty,
        "type": "component",
    }


def _resolve_supply_to_inventory_node(
    db: Session, case_id: int, supply_id: str, nodes: dict, sorted_dates: list[str]
) -> str | None:
    """Resolve supply_id to an inventory node id (product|location|period). supply_id may be DB id, product|location, or product|location|period."""
    if supply_id.count("|") >= 2:
        return supply_id
    s = db.query(Supply).filter(Supply.case_id == case_id, Supply.supply_id == supply_id).first()
    if not s and "|" in supply_id:
        product_loc = supply_id.split("|", 1)
        s = db.query(Supply).filter(
            Supply.case_id == case_id,
            Supply.product_id == product_loc[0],
            Supply.location_id == (product_loc[1] if len(product_loc) > 1 else ""),
        ).first()
    if not s:
        return None
    supplies = db.query(Supply).filter(Supply.case_id == case_id).all()
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    supply_list = [{"supply_date": getattr(x, "supply_date", None)} for x in supplies]
    demand_list = [{"request_due_time": getattr(x, "request_due_time", None)} for x in demands]
    date_to_period, _ = build_period_index(supply_list, demand_list)
    period = supply_period(getattr(s, "supply_date", None), date_to_period)
    return _inv_node_id(s.product_id, s.location_id or "", period)


def _prune_supply_to_demand_inv(
    db: Session, case_id: int, nodes: dict[str, dict], edges: list[dict], root_nid: str
) -> tuple[dict[str, dict], list[dict]]:
    """Prune to nodes/edges on a path from root inventory node to at least one demand (product, location)."""
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    if not demands:
        return nodes, edges
    mm = db.query(MethodMake).filter(MethodMake.case_id == case_id).first()
    product_to_location: dict[str, str] = {}
    if mm:
        for m in db.query(MethodMake).filter(MethodMake.case_id == case_id).all():
            product_to_location[m.product_id] = m.location_id or ""
    demand_variant_keys = {
        f"{d.product_id}|{(getattr(d, 'location_id', None) or product_to_location.get(d.product_id, '') or 'VIRTUAL')}"
        for d in demands
    }
    demand_nodes = {nid for nid in nodes if "|".join(nid.split("|")[:2]) in demand_variant_keys} | {
        nid for nid in nodes if nid.startswith("demand|")
    }

    in_edges: dict[str, list[str]] = defaultdict(list)
    out_edges: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        in_edges[e["to"]].append(e["from"])
        out_edges[e["from"]].append(e["to"])
    can_reach_demand: set[str] = set(demand_nodes)
    changed = True
    while changed:
        changed = False
        for nid in list(can_reach_demand):
            for pred in in_edges.get(nid, []):
                if pred not in can_reach_demand:
                    can_reach_demand.add(pred)
                    changed = True
            for succ in out_edges.get(nid, []):
                if succ not in can_reach_demand:
                    can_reach_demand.add(succ)
                    changed = True
    reachable_from_root: set[str] = {root_nid}
    frontier = [root_nid]
    while frontier:
        nid = frontier.pop()
        for succ in out_edges.get(nid, []):
            if succ not in reachable_from_root:
                reachable_from_root.add(succ)
                frontier.append(succ)
    visible = reachable_from_root & can_reach_demand or {root_nid}
    return (
        {nid: n for nid, n in nodes.items() if nid in visible},
        [e for e in edges if e["from"] in visible and e["to"] in visible],
    )


def _critical_path_demand_to_supply_inv(
    nodes: dict, edges: list, supply_qty: dict, demand_id: str, db: Session, case_id: int
) -> dict:
    d = db.query(Demand).filter(Demand.case_id == case_id, Demand.demand_id == demand_id).first()
    if not d:
        return {"path": [], "cost": 0, "demand_id": demand_id}
    mm = db.query(MethodMake).filter(MethodMake.case_id == case_id, MethodMake.product_id == d.product_id).first()
    loc = (getattr(d, "location_id", None) or (mm.location_id if mm else None) or "VIRTUAL") or ""
    demand_key = f"{d.product_id}|{loc}"
    start_nodes = [nid for nid in nodes if "|".join(nid.split("|")[:2]) == demand_key]
    if not start_nodes:
        return {"path": [], "cost": 0, "demand_id": demand_id}
    in_edges: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        in_edges[e["to"]].append((e["from"], e["qty"]))
    best_path: list[str] = []
    best_cost = float("inf")

    def path_cost(path: list[str]) -> float:
        return sum(supply_qty.get(n, 0) for n in path)

    def dfs(node: str, path: list[str], visited: set):
        nonlocal best_path, best_cost
        if node in visited:
            return
        visited.add(node)
        path.append(node)
        preds = in_edges.get(node, [])
        if not preds:
            cost = path_cost(path)
            if cost < best_cost:
                best_cost = cost
                best_path = list(path)
        else:
            for pred, _ in preds:
                dfs(pred, path, visited)
        path.pop()
        visited.discard(node)

    for start in start_nodes:
        dfs(start, [], set())
    return {"path": best_path, "cost": best_cost if best_cost != float("inf") else 0, "demand_id": demand_id}


def _critical_path_supply_to_demand_inv(
    nodes: dict, edges: list, supply_qty: dict, root_nid: str, db: Session, case_id: int
) -> dict:
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    mm_q = db.query(MethodMake).filter(MethodMake.case_id == case_id).all()
    product_to_location = {m.product_id: m.location_id or "" for m in mm_q}
    out_edges: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        out_edges[e["from"]].append((e["to"], e["qty"]))
    results = []
    for d in demands:
        demand_nid = f"demand|{d.demand_id}"
        demand_loc = getattr(d, "location_id", None) or product_to_location.get(d.product_id, "") or "VIRTUAL"
        demand_key = f"{d.product_id}|{demand_loc}"
        end_nodes = [demand_nid] if demand_nid in nodes else [nid for nid in nodes if "|".join(nid.split("|")[:2]) == demand_key]
        best_path: list[str] = []
        best_cost = float("inf")

        def path_cost(path: list[str]) -> float:
            return sum(supply_qty.get(n, 0) for n in path)

        def dfs(node: str, path: list[str], visited: set):
            nonlocal best_path, best_cost
            if node in visited:
                return
            visited.add(node)
            path.append(node)
            if node in end_nodes:
                c = path_cost(path)
                if c < best_cost:
                    best_cost = c
                    best_path = list(path)
            for succ, _ in out_edges.get(node, []):
                dfs(succ, path, visited)
            path.pop()
            visited.discard(node)

        dfs(root_nid, [], set())
        results.append({"demand_id": d.demand_id, "path": best_path, "cost": best_cost if best_cost != float("inf") else 0})
    return {"component_key": root_nid, "paths_by_demand": results}


def _prune_supply_to_demand(
    nodes: dict[str, dict], edges: list[dict], comp_key: str
) -> tuple[dict[str, dict], list[dict]]:
    """
    For supply-to-demand pegging, keep only nodes and edges on a path from the supply root to at least one demand.
    Prune targets that do not lead to any demand.
    """
    # Nodes that have demand (demand targets)
    demand_nodes = {nid for nid, n in nodes.items() if n.get("demand_qty", 0) > 0}
    if not demand_nodes:
        return nodes, edges

    # Closure of nodes on a path to demand: add both predecessors and successors so intermediate
    # variants stay in the graph (otherwise they get pruned and intermediates appear as leaves).
    in_edges: dict[str, list[str]] = defaultdict(list)
    out_edges_prune: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        in_edges[e["to"]].append(e["from"])
        out_edges_prune[e["from"]].append(e["to"])
    can_reach_demand: set[str] = set(demand_nodes)
    changed = True
    while changed:
        changed = False
        for nid in list(can_reach_demand):
            for pred in in_edges.get(nid, []):
                if pred not in can_reach_demand:
                    can_reach_demand.add(pred)
                    changed = True
            for succ in out_edges_prune.get(nid, []):
                if succ not in can_reach_demand:
                    can_reach_demand.add(succ)
                    changed = True

    # Forward closure from root: nodes reachable from comp_key (follow edges forward: from -> to)
    out_edges: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        out_edges[e["from"]].append(e["to"])
    reachable_from_root: set[str] = {comp_key}
    frontier = [comp_key]
    while frontier:
        nid = frontier.pop()
        for succ in out_edges.get(nid, []):
            if succ not in reachable_from_root:
                reachable_from_root.add(succ)
                frontier.append(succ)

    # Visible = on a path from root to demand
    visible = reachable_from_root & can_reach_demand
    if not visible:
        visible = {comp_key}  # keep at least the root

    nodes_filtered = {nid: n for nid, n in nodes.items() if nid in visible}
    edges_filtered = [e for e in edges if e["from"] in visible and e["to"] in visible]
    return nodes_filtered, edges_filtered


def _critical_path_demand_to_supply(nodes: dict, edges: list, supply_qty: dict, demand_id: str, db: Session, case_id: int) -> dict:
    """From demand (product@location) find path to leaf with minimum sum of supplies. Return path node ids and path cost."""
    d = db.query(Demand).filter(Demand.case_id == case_id, Demand.demand_id == demand_id).first()
    if not d:
        return {"path": [], "cost": 0, "demand_id": demand_id}
    pid = d.product_id
    # Demand is for product at location from method_make (same as allocation engine)
    mm = db.query(MethodMake).filter(MethodMake.case_id == case_id, MethodMake.product_id == pid).first()
    loc = (mm.location_id or "") if mm else ""
    vkey = f"{pid}|{loc}"
    # Start from demand's (product, location) node (variant if produced, or component if supply-only)
    start_nodes = [vkey] if vkey in nodes else []
    if not start_nodes:
        return {"path": [], "cost": 0, "demand_id": demand_id}
    # BFS/DFS from start to leaves (components with no outgoing edges); path cost = sum of supply_qty along path
    # Edges go component -> variant. So we traverse variant -> (edges from component to variant) -> component
    # Build reverse: to_node -> [(from_node, qty)]
    in_edges: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        in_edges[e["to"]].append((e["from"], e["qty"]))
    # Path from demand product (variant) down to leaf = follow in_edges from variant (get components), then recurse on components that are variants
    def path_cost(path: list[str]) -> float:
        return sum(supply_qty.get(n, 0) for n in path)
    best_path: list[str] = []
    best_cost = float("inf")
    def dfs(node: str, path: list[str], visited: set):
        nonlocal best_path, best_cost
        if node in visited:
            return
        visited.add(node)
        path.append(node)
        preds = in_edges.get(node, [])
        if not preds:
            cost = path_cost(path)
            if cost < best_cost:
                best_cost = cost
                best_path = list(path)
        else:
            for pred, _ in preds:
                dfs(pred, path, visited)
        path.pop()
        visited.discard(node)
    for start in start_nodes:
        dfs(start, [], set())
    if best_cost == float("inf"):
        best_cost = 0
    return {"path": best_path, "cost": best_cost, "demand_id": demand_id}


def _critical_path_supply_to_demand(
    nodes: dict, edges: list, supply_qty: dict, comp_key: str, db: Session, case_id: int
) -> dict:
    """From supply (raw material) to each demand: path with minimum sum of supplies. Return per-demand paths."""
    demands = db.query(Demand).filter(Demand.case_id == case_id).all()
    out_edges: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        out_edges[e["from"]].append((e["to"], e["qty"]))
    results = []
    for d in demands:
        pid = d.product_id
        mm = db.query(MethodMake).filter(MethodMake.case_id == case_id, MethodMake.product_id == pid).first()
        loc = (mm.location_id or "") if mm else ""
        vkey = f"{pid}|{loc}"
        end_nodes = [vkey] if vkey in nodes else []
        best_path: list[str] = []
        best_cost = float("inf")

        def path_cost(path: list[str]) -> float:
            return sum(supply_qty.get(n, 0) for n in path)

        def dfs(node: str, path: list[str], visited: set):
            nonlocal best_path, best_cost
            if node in visited:
                return
            visited.add(node)
            path.append(node)
            if node in end_nodes:
                cost = path_cost(path)
                if cost < best_cost:
                    best_cost = cost
                    best_path = list(path)
            for succ, _ in out_edges.get(node, []):
                dfs(succ, path, visited)
            path.pop()
            visited.discard(node)

        dfs(comp_key, [], set())
        results.append({
            "demand_id": d.demand_id,
            "path": best_path,
            "cost": best_cost if best_cost != float("inf") else 0,
        })
    return {"component_key": comp_key, "paths_by_demand": results}


def _pegging_verify_inv_demand_edges(nodes: dict[str, dict], edges: list[dict]) -> list[dict]:
    """For each inv node with inv->demand edges, return node_id, node_qty, demand_edge_sum, ok (sum <= qty)."""
    out_sum: dict[str, float] = defaultdict(float)
    for e in edges:
        if e["to"].startswith("demand|") and not e["from"].startswith("demand|"):
            out_sum[e["from"]] += float(e.get("qty") or 0)
    result = []
    for nid in sorted(out_sum.keys()):
        node_qty = float(nodes.get(nid, {}).get("qty") or 0)
        total = out_sum[nid]
        result.append({
            "node_id": nid,
            "node_qty": round(node_qty, 4),
            "demand_edge_sum": round(total, 4),
            "ok": total <= node_qty + 1e-6,
        })
    return result


@router.get("/{case_id}/runs/{run_id}/pegging")
def get_pegging(
    case_id: int,
    run_id: int,
    direction: str = Query(..., description="demand-to-supply or supply-to-demand"),
    demand_id: str = Query(None),
    supply_id: str = Query(None),
    verify: bool = Query(False, description="If true, add _verify with inv node edge-sum checks"),
    db: Session = Depends(get_db),
):
    run = db.query(AllocationRun).filter(AllocationRun.id == run_id, AllocationRun.case_id == case_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    # Build graph with nodes = inventory(product, location, quantity, time); one node per (product, location, period)
    nodes, edges, supply_qty, sorted_dates = _build_inventory_graph(db, case_id, run.id)
    if direction == "demand-to-supply":
        if not demand_id:
            raise HTTPException(status_code=400, detail="demand_id required")
        critical = _critical_path_demand_to_supply_inv(nodes, edges, supply_qty, demand_id, db, case_id)
        # Use same attribution as Suggested revised demands: allocated capped at requested
        actions = db.query(AllocationAction).filter(AllocationAction.run_id == run.id).all()
        feasible = _feasible_demands_from_actions(db, case_id, actions)
        by_did = {str(f["demand_id"]): f for f in feasible}
        fd = by_did.get(str(demand_id), {})
        allocated_qty = float(fd.get("allocated_qty", 0) or 0)
        requested_qty = float(fd.get("requested_qty", 0) or 0)
        if requested_qty == 0:
            demand_row = db.query(Demand).filter(Demand.case_id == case_id, Demand.demand_id == demand_id).first()
            requested_qty = float(demand_row.quantity or 0) if demand_row else 0.0
        demand_root_id = f"demand|{str(demand_id)}"
        # Rescale inv->demand edges for this demand so they sum to capped allocated (so tree "to parent" adds up)
        inv_to_this_demand = [e for e in edges if str(e.get("to") or "").strip() == demand_root_id]
        if not inv_to_this_demand:
            # Fallback: match by suffix (e.g. DB may return demand_id in different type)
            inv_to_this_demand = [e for e in edges if (e.get("to") or "").startswith("demand|") and (e.get("to") or "").split("|", 1)[-1].strip() == str(demand_id).strip()]
        current_sum = sum(float(e.get("qty") or 0) for e in inv_to_this_demand)
        if current_sum > 0 and allocated_qty >= 0:
            scale = allocated_qty / current_sum
            for e in inv_to_this_demand:
                e["qty"] = round(float(e.get("qty") or 0) * scale, 4)
        return {
            "direction": direction,
            "nodes": list(nodes.values()),
            "edges": edges,
            "critical_path": critical,
            "sorted_dates": sorted_dates,
            "demand_id": demand_id,
            "demand_root_id": demand_root_id,
            "demand_allocated_qty": round(allocated_qty, 4),
            "demand_requested_qty": round(requested_qty, 4),
        }
    if direction == "supply-to-demand":
        if not supply_id:
            raise HTTPException(status_code=400, detail="supply_id required")
        root_nid = _resolve_supply_to_inventory_node(db, case_id, supply_id, nodes, sorted_dates)
        if not root_nid:
            raise HTTPException(status_code=404, detail="Supply not found or no inventory node for this supply")
        if root_nid not in nodes:
            nodes[root_nid] = _inv_node_from_id(root_nid, sorted_dates, supply_qty.get(root_nid, 0.0))
        nodes, edges = _prune_supply_to_demand_inv(db, case_id, nodes, edges, root_nid)
        critical = _critical_path_supply_to_demand_inv(nodes, edges, supply_qty, root_nid, db, case_id)
        out = {"direction": direction, "nodes": list(nodes.values()), "edges": edges, "critical_paths_by_demand": critical, "sorted_dates": sorted_dates}
        if verify:
            out["_verify"] = _pegging_verify_inv_demand_edges(nodes, edges)
        return out
    raise HTTPException(status_code=400, detail="direction must be demand-to-supply or supply-to-demand")
