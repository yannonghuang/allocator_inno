"""
Scarcity-based allocation engine per spec (inventory graph DAG).
Node = inventory(product, location, quantity, time). Edges: method_make (consume components → produce parent)
and method_move (consume at from_loc → produce at to_loc). Bottom-up from supplies to demands.
"""
import logging
import math
from collections import defaultdict
from copy import deepcopy
from typing import Any, Callable

from app.services.time_utils import (
    build_period_index,
    supply_period,
    demand_due_period,
    period_plus_days,
)
from app.utils.sku_patterns import raw_material_pattern

logger = logging.getLogger(__name__)

# Minimum output qty to emit as its own action; smaller shares are merged into the largest (use up without crumbs)
MIN_ALLOCATION = 0.001

# Component = (product_id, location_id)
Comp = tuple[str, str]
# Variant = (product_id, location_id)
# ReqWithRate = (comp, rate): for make, need qty_output/rate of comp; for move, rate=1
Variant = tuple[str, str]
ReqWithRate = tuple[Comp, float]

# Basket with time: comp -> list of (qty, period) sorted by period
BasketByTime = dict[Comp, list[tuple[float, int]]]


def _comp_key(c: Comp) -> str:
    return f"{c[0]}|{c[1]}"


def _norm_node_key(comp_key: str, period: int) -> str:
    """Normalized node key for consumed_by_node so supply view can match (same as views._norm_comp_key + period)."""
    parts = (comp_key or "").split("|", 1)
    p0 = str(parts[0]).strip() if parts else ""
    p1 = str(parts[1]).strip() if len(parts) > 1 else ""
    return f"{p0}|{p1}|{period}"


def _variant_key(v: Variant) -> str:
    return f"{v[0]}|{v[1]}"


def _basket_add(basket: BasketByTime, comp: Comp, qty: float, period: int) -> None:
    if qty <= 0:
        return
    if comp not in basket:
        basket[comp] = []
    # Merge with existing same period
    for i, (q, p) in enumerate(basket[comp]):
        if p == period:
            basket[comp][i] = (q + qty, p)
            return
    basket[comp].append((qty, period))
    basket[comp].sort(key=lambda x: x[1])


def _basket_consume(
    basket: BasketByTime,
    comp: Comp,
    need: float,
    record_consumed: dict[str, float] | None = None,
) -> tuple[float, int]:
    """Consume up to `need` from component, earliest period first. Returns (taken, max_period_consumed).
    If record_consumed is provided, records consumption per node (comp_key|period) for supply view."""
    if comp not in basket or need <= 0:
        return 0.0, 0
    taken = 0.0
    max_period = 0
    comp_key = _comp_key(comp)
    i = 0
    while i < len(basket[comp]) and taken < need:
        q, p = basket[comp][i]
        take = min(q, need - taken)
        if take > 0:
            taken += take
            max_period = max(max_period, p)
            if record_consumed is not None:
                node = _norm_node_key(comp_key, p)
                record_consumed[node] = record_consumed.get(node, 0.0) + take
            if take >= q:
                basket[comp].pop(i)
                continue
            basket[comp][i] = (q - take, p)
        i += 1
    return taken, max_period


def _basket_total(basket: BasketByTime, comp: Comp) -> float:
    if comp not in basket:
        return 0.0
    return sum(q for q, _ in basket[comp])


def _basket_total_qty(basket: BasketByTime) -> float:
    """Sum of all quantities in the basket (all components)."""
    return sum(_basket_total(basket, c) for c in basket)


def run_allocation(
    data: dict[str, Any],
    customer_weights: dict[str, float] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    trace_component_key: str | None = None,
) -> dict[str, Any]:
    """
    Run allocation and production passes with timing.
    Supply: null supply_date = period 0 (preexisting). Demand: only allocation with output_period <= due_period counts.
    If trace_component_key is set (e.g. "280-0845-030|1000"), result["_trace"] will list why that component was or wasn't consumed as critical/companion.
    """
    trace_comp: Comp | None = None
    trace_events: list[dict] = []
    raw_material_trace: list[dict] = []  # involvement as critical or companion, allocated or only considered
    if trace_component_key and "|" in trace_component_key:
        a, b = trace_component_key.strip().split("|", 1)
        trace_comp = (a, b)

    supply_adj, demand_adj = _apply_overrides(data.get("overrides", []))

    supply_list = data.get("supply", [])
    demand_list_raw = data.get("demand", [])

    # Build period index from all supply_date and request_due_time
    date_to_period, sorted_dates = build_period_index(supply_list, demand_list_raw)

    # Basket: init with initial supplies only (spec: Init basket_alloc with initial supplies). Overrides adjust per comp.
    basket_alloc: BasketByTime = defaultdict(list)
    for s in supply_list:
        key = (s["product_id"], s.get("location_id") or "")
        qty = float(s.get("qty") or 0) + supply_adj.get(_comp_key(key), 0)
        if qty > 0:
            period = supply_period(s.get("supply_date"), date_to_period)
            _basket_add(basket_alloc, key, qty, period)
    initial_basket_total_qty = _basket_total_qty(basket_alloc)
    initial_basket_keys = len([c for c in basket_alloc if _basket_total(basket_alloc, c) > 0])
    basket_prod: BasketByTime = deepcopy(basket_alloc)

    # Enrich demand with due_period
    demand_list = []
    for d in demand_list_raw:
        due = demand_due_period(d.get("request_due_time"), date_to_period)
        demand_list.append({**d, "due_period": due})

    # Build variants: each (variant, alternatives, lead_days, edge_type).
    # BOM: requirement set (AND) = same alt_group or all null; alternative groups (OR) = different alt_group.
    # Each alternative = one list[ReqWithRate]; to produce variant we choose ONE alternative and need ALL components in it.
    method_make_list = data.get("method_make", [])
    method_move_list = data.get("method_move", [])
    bom_list = data.get("bom", [])
    # parent_id -> list of (alt_group_key, [(child_id, rate), ...]); same alt_key = one requirement set (AND), different = OR
    bom_by_parent_alt: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(lambda: defaultdict(list))
    for b in bom_list:
        pid, child_id = b["parent_id"], b["child_id"]
        rate = float(b.get("rate") or 1.0)
        alt_key = b.get("alt_group") if b.get("alt_group") is not None and b.get("alt_group") != "" else "__null__"
        bom_by_parent_alt[pid][alt_key].append((child_id, rate))
    # (variant, list of requirement_sets, lead_days, edge_type). Each requirement_set = list[ReqWithRate] (AND).
    variants_list: list[tuple[Variant, list[list[ReqWithRate]], float, str]] = []
    for m in method_make_list:
        pid, loc = m["product_id"], m["location_id"]
        v = (pid, loc)
        alt_groups = bom_by_parent_alt.get(pid, {})
        if not alt_groups:
            continue
        alternatives: list[list[ReqWithRate]] = [
            [((child_id, loc), rate) for child_id, rate in req_set]
            for req_set in alt_groups.values()
        ]
        if not alternatives:
            continue
        lead_days = float(m.get("lead_time") or 0)
        variants_list.append((v, alternatives, lead_days, "make"))
    for mv in method_move_list:
        pid = mv["product_id"]
        from_loc = mv.get("from_location_id") or ""
        to_loc = mv.get("to_location_id") or ""
        if from_loc == to_loc:
            continue
        v = (pid, to_loc)
        req_with_rate = [((pid, from_loc), 1.0)]
        transit_days = float(mv.get("transit_time") or 0)
        variants_list.append((v, [req_with_rate], transit_days, "move"))

    # Union of components across all alternatives (for downstream, total_cap, pruning)
    variants_req: dict[Variant, list[Comp]] = {}
    for v, alternatives, _, _ in variants_list:
        comps = set()
        for req in alternatives:
            comps.update(c for c, _ in req)
        variants_req[v] = list(comps)
    all_targets = set(variants_req.keys())

    # Trace: which variants have trace_comp in any alternative?
    if trace_comp:
        variants_using_trace: list[dict] = []
        for v, alternatives, _, edge_type in variants_list:
            for alt_ix, req_wr in enumerate(alternatives):
                if trace_comp in [c for c, _ in req_wr]:
                    variants_using_trace.append({
                        "variant_key": _variant_key(v),
                        "edge_type": edge_type,
                        "alt_index": alt_ix,
                        "req_component_keys": [_comp_key(c) for c, _ in req_wr],
                        "rates": [r for _, r in req_wr],
                    })
                    break
        trace_events.append({"event": "variants_using_trace_component", "trace_component_key": _comp_key(trace_comp), "variants": variants_using_trace})
    demand_product_to_location: dict[str, str] = {}
    for m in method_make_list:
        pid, loc = m["product_id"], m["location_id"]
        if pid not in demand_product_to_location:
            demand_product_to_location[pid] = loc

    unmet: dict[tuple[Variant, str], float] = defaultdict(float)
    for d in demand_list:
        pid, cust, qty = d["product_id"], d["customer_id"], float(d.get("quantity") or 0)
        loc = d.get("location_id") or demand_product_to_location.get(pid)
        if loc is None:
            continue
        adj = demand_adj.get(f"demand|{d['demand_id']}", 0)
        qty = max(0, qty + adj)
        v = (pid, loc)
        unmet[(v, cust)] += qty

    demanded_targets = {v for (v, _), q in unmet.items() if q > 0}
    w_c = customer_weights or {}
    for d in demand_list:
        cid = d["customer_id"]
        if cid not in w_c:
            w_c[cid] = 1.0

    target_weight: dict[Variant, float] = {}
    for (v, cust), q in unmet.items():
        if q > 0:
            target_weight[v] = target_weight.get(v, 0) + q * w_c.get(cust, 1.0)

    downstream: dict[Variant, set[Variant]] = defaultdict(set)
    for v, req in variants_req.items():
        for c in req:
            if c in all_targets:
                downstream[c].add(v)

    order: list[Variant] = []
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

    # Total available per component (for scarcity). Single definition: scarcity = total quantity; scarcest = smallest.
    def total_avail(c: Comp) -> float:
        return _basket_total(basket_alloc, c)

    def scarcity(c: Comp) -> tuple[float, str]:
        """Quantity-only scarcity: (total_avail, comp_key) so we process smallest quantity first; tie-break by key."""
        return (total_avail(c), _comp_key(c))

    allocation: list[dict] = []
    reserved_by_material: dict[Comp, float] = defaultdict(float)
    variant_allocated_qty: dict[Variant, float] = defaultdict(float)
    variant_output_period: dict[Variant, int] = {}  # period when variant output becomes available
    # Spec: targets_to_be_allocated = all non-leaf variants; targets_to_be_produced = same (exclude demand nodes)
    targets_to_be_allocated = set(variants_req.keys())
    targets_to_be_produced = set(variants_req.keys()) - demanded_targets

    def allocatable_recipes(comp: Comp) -> list[tuple[Variant, list[ReqWithRate], float, str]]:
        # Spec: allocatable_variants = [(v, req_variant)] where component in req_variant; one alternative per variant.
        # Choice: first alternative that contains the component (AND set for that variant).
        out: list[tuple[Variant, list[ReqWithRate], float, str]] = []
        for (v, alternatives, lead_days, edge_type) in variants_list:
            if v not in targets_to_be_allocated:
                continue
            for req_wr in alternatives:
                if comp in [c for c, _ in req_wr]:
                    out.append((v, req_wr, lead_days, edge_type))
                    break  # one alternative per variant
        return out

    # Track reserved per (variant, comp) so we can apply deductions when adding variant to basket
    reserved_for_variant: dict[Variant, dict[Comp, float]] = defaultdict(lambda: defaultdict(float))

    # Allocation pass (spec: quantity-focused only). Process components in scarcity order; never overcommit:
    # compute output_actual from current availability, then consume only that amount; reserve remainder.
    # Multi-pass: produced inventory is added to the basket and processed in later passes until no progress.
    # Cap total steps to avoid runaway / socket hang up on large cases.
    MAX_ALLOCATION_STEPS = 100_000
    PROGRESS_INTERVAL = 200  # report progress every N steps
    PERSIST_SLICE_INTERVAL = 1000  # pass allocation_slice every N steps for incremental persist
    step_counter = 0
    last_persist_boundary = 0
    if progress_callback:
        progress_callback({
            "steps": 0,
            "max_steps": MAX_ALLOCATION_STEPS,
            "basket_total_qty": round(initial_basket_total_qty, 2),
            "basket_keys": initial_basket_keys,
            "initial_basket_total_qty": round(initial_basket_total_qty, 2),
            "initial_basket_keys": initial_basket_keys,
        })
    # Single source of truth for supply view: consumption per node (comp_key|period), recorded as we consume
    consumed_by_node: dict[str, float] = {}
    while True:
        if step_counter >= MAX_ALLOCATION_STEPS:
            break
        components_in_basket = [c for c in basket_alloc if total_avail(c) > 0]
        scarcity_order = sorted(components_in_basket, key=scarcity)  # ascending: smallest total quantity first
        if not scarcity_order:
            break
        allocated_this_pass = False
        for component in scarcity_order:
            if step_counter >= MAX_ALLOCATION_STEPS:
                break
            avail = total_avail(component)
            if avail <= 0:
                continue
            recipes = allocatable_recipes(component)
            if not recipes:
                continue
            total_w = sum(target_weight.get(v, 0) for (v, _, _, _) in recipes)
            if total_w <= 0:
                continue

            # Trace: critical component this step
            if trace_comp and component == trace_comp:
                trace_events.append({
                    "event": "trace_component_as_critical",
                    "step_before": step_counter,
                    "critical_avail": avail,
                    "recipes_count": len(recipes),
                    "variant_keys": [_variant_key(v) for v, _, _, _ in recipes],
                })
            # Trace: is trace_comp a companion in any of these recipes? (critical component = component)
            if trace_comp and component != trace_comp:
                companion_recipes = [(v, req_wr, edge_type) for (v, req_wr, _, edge_type) in recipes if trace_comp in [c for c, _ in req_wr]]
                if companion_recipes:
                    trace_avail = total_avail(trace_comp)
                    trace_events.append({
                        "event": "companion_opportunity",
                        "step_before": step_counter,
                        "critical_component": _comp_key(component),
                        "critical_avail": avail,
                        "trace_component_avail": trace_avail,
                        "variants_with_trace_as_companion": [_variant_key(v) for v, _, _ in companion_recipes],
                    })

            # Proportional split of available component by target_weight. Split is in component units.
            split_qtys: list[tuple[Variant, list[ReqWithRate], float, str, float]] = []
            for (v, req_wr, lead_days, edge_type) in recipes:
                share_component = (target_weight.get(v, 0) / total_w) * avail
                rate_c = next((r for c, r in req_wr if c == component), 1.0)
                output_qty = share_component / rate_c if rate_c > 0 else 0.0
                split_qtys.append((v, req_wr, lead_days, edge_type, output_qty))
            # Use up without crumbs: merge tiny shares into the largest so we don't emit crumb-sized actions.
            if split_qtys:
                crumb_total = sum(q for (_, _, _, _, q) in split_qtys if 0 < q < MIN_ALLOCATION)
                if crumb_total > 0:
                    dominant_idx = max(range(len(split_qtys)), key=lambda i: split_qtys[i][4])
                    consolidated: list[tuple[Variant, list[ReqWithRate], float, str, float]] = []
                    for i, (v, req_wr, lead_days, edge_type, q) in enumerate(split_qtys):
                        if i == dominant_idx:
                            consolidated.append((v, req_wr, lead_days, edge_type, q + crumb_total))
                        elif q >= MIN_ALLOCATION:
                            consolidated.append((v, req_wr, lead_days, edge_type, q))
                        # else 0 < q < MIN_ALLOCATION: merged into dominant, skip (q becomes 0)
                    split_qtys = consolidated

            # Raw material trace: critical component involvement (allocated breakdown; skipped events added below)
            raw_pattern = raw_material_pattern(component[0])
            if raw_pattern:
                # Aggregate by variant so we show each recipient once with total qty (no duplicate lines)
                breakdown_by_variant: dict[str, float] = defaultdict(float)
                for (v, _, _, _, qty) in split_qtys:
                    if qty > 0:
                        breakdown_by_variant[_variant_key(v)] += qty
                allocation_breakdown = [f"{vk}={round(q, 4)}" for vk, q in breakdown_by_variant.items()]
                logger.info(
                    "allocation step=%s critical_raw_material comp=%s pattern=%s avail=%s → %s",
                    step_counter,
                    _comp_key(component),
                    raw_pattern,
                    avail,
                    "; ".join(allocation_breakdown) or "(none)",
                )
                raw_material_trace.append({
                    "role": "critical",
                    "step": step_counter,
                    "comp_key": _comp_key(component),
                    "pattern": raw_pattern,
                    "avail": round(avail, 4),
                    "allocated": True,
                    "breakdown": [
                        {"variant_key": vk, "output_qty": round(q, 4)}
                        for vk, q in breakdown_by_variant.items()
                    ],
                })

            for (variant, req_wr, lead_days, edge_type, output_qty) in split_qtys:
                if output_qty <= 0:
                    continue
                # Spec: consume min(need, available); reserve remainder. No overcommit: determine output_actual
                # from current availability first, then consume only that amount.
                output_cap = min(
                    (total_avail(c) * rate for c, rate in req_wr if rate and rate > 0),
                    default=0.0,
                )
                output_qty_actual = min(output_qty, output_cap)
                # Integer BOM rates => discrete: no fractional allocation; output must be multiple of LCM(integer rates).
                # Use tolerance so 1.0, 1.0000000000000002 (from JSON/float) all count as integer.
                integer_rates = [
                    int(round(r))
                    for (_, r) in req_wr
                    if r is not None and r > 0 and abs(r - round(r)) < 1e-9
                ]
                if integer_rates:
                    lcm = math.lcm(*integer_rates)
                    output_qty_actual = int(output_qty_actual // lcm) * lcm
                if output_qty_actual <= 0 or output_qty_actual < 1e-9:
                    if trace_comp and trace_comp in [c for c, _ in req_wr]:
                        cap_per_comp = [(c, total_avail(c), float(r or 0), total_avail(c) * float(r or 0)) for c, r in req_wr if r and r > 0]
                        avail_by_comp = {_comp_key(c): {"avail": a, "rate": r, "cap_contribution": cap} for c, a, r, cap in cap_per_comp}
                        bottleneck_comp = min(cap_per_comp, key=lambda x: x[3]) if cap_per_comp else None
                        trace_events.append({
                            "event": "skip_action_using_trace_component",
                            "step": step_counter,
                            "variant_key": _variant_key(variant),
                            "reason": "output_qty_actual <= 0 or < 1e-9",
                            "output_qty": output_qty,
                            "output_cap": output_cap,
                            "output_qty_actual": output_qty_actual,
                            "req_component_keys": [_comp_key(c) for c, _ in req_wr],
                            "avail_by_component": avail_by_comp,
                            "bottleneck_component": _comp_key(bottleneck_comp[0]) if bottleneck_comp else None,
                            "bottleneck_avail": bottleneck_comp[1] if bottleneck_comp else None,
                        })
                    # Raw material trace: companion considered but skipped (output capped or integer LCM).
                    # Do not add a second "considered, skipped" line for the critical component in the same step—
                    # the "allocated" entry above already summarizes the step; a zero-output variant share is internal.
                    skip_reason = "output_qty_actual <= 0 (cap or integer BOM)"
                    for c, _ in req_wr:
                        if c != component and raw_material_pattern(c[0]):
                            raw_material_trace.append({
                                "role": "companion",
                                "step": step_counter,
                                "comp_key": _comp_key(c),
                                "pattern": raw_material_pattern(c[0]),
                                "considered_but_skipped": True,
                                "variant_key": _variant_key(variant),
                                "critical_component": _comp_key(component),
                                "reason": skip_reason,
                            })
                    continue
                need_actual_by_c = [
                    (output_qty_actual / rate if rate and rate > 0 else 0.0)
                    for _, rate in req_wr
                ]
                max_period_consumed = 0
                for (c, rate), need_actual in zip(req_wr, need_actual_by_c):
                    taken, max_p = _basket_consume(basket_alloc, c, need_actual, record_consumed=consumed_by_node)
                    max_period_consumed = max(max_period_consumed, max_p)
                    # Raw material trace: companion involvement (allocated or only considered)
                    if c != component and raw_material_pattern(c[0]):
                        comp_pattern = raw_material_pattern(c[0])
                        logger.info(
                            "allocation step=%s companion_raw_material comp=%s pattern=%s allocated=%s "
                            "because critical=%s making variant=%s output_qty=%s",
                            step_counter,
                            _comp_key(c),
                            comp_pattern,
                            round(taken, 4),
                            _comp_key(component),
                            _variant_key(variant),
                            output_qty_actual,
                        )
                        raw_material_trace.append({
                            "role": "companion",
                            "step": step_counter,
                            "comp_key": _comp_key(c),
                            "pattern": comp_pattern,
                            "allocated": taken > 0,
                            "taken": round(taken, 4),
                            "need_actual": round(need_actual, 4),
                            "critical_component": _comp_key(component),
                            "variant_key": _variant_key(variant),
                            "output_qty_actual": round(output_qty_actual, 4),
                        })
                    # Reserve remainder vs target output_qty: short = output_qty/rate - output_actual/rate
                    need_target = output_qty / rate if rate and rate > 0 else 0.0
                    short = need_target - need_actual
                    if short > 0:
                        reserved_by_material[c] += short
                        reserved_for_variant[variant][c] += short
                output_period = period_plus_days(
                    max_period_consumed, lead_days, date_to_period, sorted_dates
                ) if (max_period_consumed > 0 or lead_days > 0) else max_period_consumed
                variant_output_period[variant] = max(
                    variant_output_period.get(variant, 0), output_period
                )
                req_comp_list = [c for c, _ in req_wr]
                req_rates = [r for _, r in req_wr]
                allocation.append({
                    "variant_key": _variant_key(variant),
                    "target_product_id": variant[0],
                    "target_location_id": variant[1],
                    "req_component_ids": [_comp_key(c) for c in req_comp_list],
                    "req_rates": req_rates,
                    "qty": output_qty_actual,
                    "demand_id": None,
                    "output_period": output_period,
                    "edge_type": edge_type,
                    "scarcity_rank": step_counter,
                })
                if trace_comp and trace_comp in req_comp_list:
                    trace_events.append({
                        "event": "action_emitted_with_trace_component",
                        "step": step_counter,
                        "variant_key": _variant_key(variant),
                        "qty": output_qty_actual,
                        "req_component_ids": [_comp_key(c) for c in req_comp_list],
                        "as_critical": component == trace_comp,
                        "critical_component": _comp_key(component),
                    })
                step_counter += 1
                allocated_this_pass = True
                if progress_callback and step_counter % PROGRESS_INTERVAL == 0:
                    basket_total_qty = _basket_total_qty(basket_alloc)
                    basket_keys = len([c for c in basket_alloc if total_avail(c) > 0])
                    payload: dict = {
                        "steps": step_counter,
                        "max_steps": MAX_ALLOCATION_STEPS,
                        "basket_total_qty": round(basket_total_qty, 2),
                        "basket_keys": basket_keys,
                        "initial_basket_total_qty": round(initial_basket_total_qty, 2),
                        "initial_basket_keys": initial_basket_keys,
                    }
                    if step_counter > 0 and step_counter % PERSIST_SLICE_INTERVAL == 0:
                        payload["allocation_slice"] = allocation[last_persist_boundary:step_counter]
                        last_persist_boundary = step_counter
                    progress_callback(payload)
                variant_allocated_qty[variant] += output_qty_actual
                # Do not discard variant: it can receive allocation again when we process other components
                # in scarcity order (same variant is driven by different components in different steps).
                _basket_add(basket_alloc, variant, output_qty_actual, output_period)
                for c in req_comp_list:
                    rv = reserved_for_variant[variant].get(c, 0)
                    if rv > 0:
                        reserved_by_material[c] -= rv
                        reserved_for_variant[variant][c] = 0
        # Spec: prune basket_alloc of resources unrelated to remaining allocation targets.
        # "Remaining" = only targets reachable from demand (backwards): demanded variants + transitive requirements.
        # So we purge any component that is not on a path from some demand (no variant we need uses it).
        related_components: set[Comp] = set()
        queue: list[Comp] = list(demanded_targets)
        while queue:
            v = queue.pop()
            if v in related_components:
                continue
            related_components.add(v)
            for c in variants_req.get(v, []):
                related_components.add(c)
                if c in all_targets:
                    queue.append(c)
        comps_removed = [c for c in basket_alloc if c not in related_components]
        for c in comps_removed:
            del basket_alloc[c]
        if comps_removed and progress_callback:
            progress_callback({
                "prune_after_step": step_counter,
                "prune_components": [_comp_key(c) for c in comps_removed],
            })
        if not allocated_this_pass:
            break

    # Demand-fulfillment: assign produced qty to demands by priority (and due date). Split each production
    # into one action per (demand_id, qty) so total allocated to demands <= available and demand_id is set.
    produced: dict[tuple[Variant, int], float] = defaultdict(float)
    template_by_key: dict[tuple[Variant, int], dict] = {}
    for a in allocation:
        variant = (a["target_product_id"], a["target_location_id"])
        out_per = int(a.get("output_period", 0))
        qty = float(a.get("qty", 0))
        if qty <= 0:
            continue
        key = (variant, out_per)
        produced[key] += qty
        if key not in template_by_key:
            template_by_key[key] = dict(a)

    expanded: list[dict] = []
    for (variant, out_per), total_qty in produced.items():
        pid, loc = variant[0], variant[1]
        # All demands for this variant (revised plan: we allocate by priority; Revised time shows when supply is available)
        matching = [
            d for d in demand_list
            if d["product_id"] == pid
            and (d.get("location_id") or demand_product_to_location.get(pid)) == loc
        ]
        remaining = total_qty
        unmet = {d["demand_id"]: float(d.get("quantity") or 0) for d in matching}
        for d in matching:
            if remaining <= 0:
                break
            did = d["demand_id"]
            give = min(unmet.get(did, 0), remaining)
            if give <= 0:
                continue
            remaining -= give
            unmet[did] = unmet.get(did, 0) - give
            t = template_by_key.get((variant, out_per), {})
            expanded.append({
                "variant_key": t.get("variant_key"),
                "target_product_id": pid,
                "target_location_id": loc,
                "req_component_ids": t.get("req_component_ids", []),
                "req_rates": t.get("req_rates", []),
                "qty": give,
                "demand_id": did,
                "output_period": out_per,
                "edge_type": t.get("edge_type"),
                "scarcity_rank": t.get("scarcity_rank"),
            })
        # Emit one action for unallocated qty so production pass still consumes/adds full amount
        if remaining > 0:
            t = template_by_key.get((variant, out_per), {})
            expanded.append({
                "variant_key": t.get("variant_key"),
                "target_product_id": pid,
                "target_location_id": loc,
                "req_component_ids": t.get("req_component_ids", []),
                "req_rates": t.get("req_rates", []),
                "qty": remaining,
                "demand_id": None,
                "output_period": out_per,
                "edge_type": t.get("edge_type"),
                "scarcity_rank": t.get("scarcity_rank"),
            })
    allocation = expanded

    # Production pass (spec: timing-focused). Replay allocation list: when all req components available
    # in basket_prod, consume need_i = qty/rate_i per component and add output to basket_prod.
    for a in allocation:
        variant = (a["target_product_id"], a["target_location_id"])
        req_keys = a.get("req_component_ids", [])
        req_rates = a.get("req_rates") or [1.0] * len(req_keys)
        req_comps: list[Comp] = []
        for k in req_keys:
            parts = k.split("|", 1)
            req_comps.append((parts[0], parts[1] if len(parts) > 1 else ""))
        output_qty = float(a.get("qty", 0))
        out_per = int(a.get("output_period", 0))
        if output_qty <= 0 or not req_comps:
            continue
        # Need per component: output_qty / rate_i (spec: quantity_i >= quantity/rate_i)
        needs = [
            output_qty / (req_rates[i] if i < len(req_rates) and req_rates[i] > 0 else 1.0)
            for i in range(len(req_comps))
        ]
        can_fire = all(
            _basket_total(basket_prod, req_comps[i]) >= needs[i]
            for i in range(len(req_comps))
        )
        if not can_fire:
            continue
        for i, c in enumerate(req_comps):
            _basket_consume(basket_prod, c, needs[i])
        _basket_add(basket_prod, variant, output_qty, out_per)

    # Feasible demands: sum allocated qty per demand (each action now has demand_id set)
    demand_allocated: dict[str, float] = defaultdict(float)
    for a in allocation:
        did = a.get("demand_id")
        if did is not None:
            demand_allocated[did] += float(a.get("qty", 0))

    feasible = []
    for d in demand_list:
        did, pid, req_qty = d["demand_id"], d["product_id"], float(d.get("quantity") or 0)
        alloc_qty = min(demand_allocated.get(did, 0), req_qty)
        status = "fulfilled" if alloc_qty >= req_qty else ("partial" if alloc_qty > 0 else "unfulfilled")
        feasible.append({
            "demand_id": did,
            "product_id": pid,
            "requested_qty": req_qty,
            "allocated_qty": alloc_qty,
            "status": status,
        })

    # Flatten basket for output (legacy: comp_key -> total qty)
    def basket_totals(b: BasketByTime) -> dict[str, float]:
        out = {}
        for comp, buckets in b.items():
            total = sum(q for q, _ in buckets)
            if total > 0:
                out[_comp_key(comp)] = total
        return out

    out: dict[str, Any] = {
        "allocation": allocation,
        "feasible_demands": feasible,
        "basket_alloc": basket_totals(basket_alloc),
        "basket_prod": basket_totals(basket_prod),
        "raw_material_trace": raw_material_trace,
        "consumed_by_node": {k: round(v, 4) for k, v in consumed_by_node.items()},
    }
    if trace_comp:
        out["_trace"] = trace_events
    return out


def _apply_overrides(overrides: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    supply_adj: dict[str, float] = {}
    demand_adj: dict[str, float] = {}
    for o in overrides:
        et, key, payload = o.get("entity_type"), o.get("entity_key"), o.get("payload") or {}
        if et == "supply" and "quantity" in payload:
            supply_adj[key] = float(payload["quantity"])
        if et == "demand" and "quantity" in payload:
            demand_adj[key] = float(payload["quantity"])
    return supply_adj, demand_adj
