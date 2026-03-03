"""
Demand-to-supply planning: takes customer demands and outputs committed demands (with commit_time)
and planned work orders. See spec.md (planning algorithm).
"""
from __future__ import annotations

import logging
import csv
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _load_real_bom_pairs_from_csv() -> frozenset[tuple[str, str]]:
    """
    Load (parent_id, child_id) pairs from csv/bom.csv where VIRTUAL column exists and VIRTUAL <> 'Y'.
    This is used only for logging which BOM links are considered 'real' during pegging construction.
    """
    try:
        root = Path(__file__).resolve().parents[3]
    except IndexError:
        root = Path(__file__).resolve().parent.parent.parent
    bom_csv = root / "csv" / "bom.csv"
    if not bom_csv.exists():
        return frozenset()
    pairs: set[tuple[str, str]] = set()
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
                    # Real BOM row: VIRTUAL not equal to 'Y'
                    if v == "Y":
                        continue
                pairs.add((parent, child))
    except Exception:
        return frozenset()
    return frozenset(pairs)


REAL_BOM_PAIRS: frozenset[tuple[str, str]] = _load_real_bom_pairs_from_csv()

# Date format used in demand.request_due_time and supply.supply_date
DATE_FMT = "%Y-%m-%d"
# Limit recursion depth to avoid RecursionError on deep or cyclic BOMs
MAX_PLAN_DEPTH = 500
# Removed MAX_METHOD_SCORING_DEPTH: elaborate method scoring now simulates full planning depth (depth - 1) to allow observing performance impact.

# Pegging tree node: demand (root) -> work_order -> ... -> work_order | supply | purchase (leaves)
# type 'demand' | 'work_order' | 'supply' | 'purchase'
# All nodes have type, children (list, empty for leaves). Attrs vary by type.
# Optional field children_relation:
# - 'and': children are jointly required (e.g. BOM components under a variant)
# - 'or': children are alternatives (e.g. single-component variants chosen for the same make)
PeggingNode = dict[str, Any]


def _parse_date(s: str | None) -> datetime | None:
    if not s or not str(s).strip():
        return None
    raw = str(s).strip()[:10]
    if len(raw) < 10:
        return None
    try:
        # Parse without relying on locale (use fromisoformat for YYYY-MM-DD)
        if raw[4] == "-" and raw[7] == "-":
            return datetime(int(raw[:4]), int(raw[5:7]), int(raw[8:10]))
        return datetime.strptime(raw, DATE_FMT)
    except (ValueError, TypeError, RecursionError):
        return None


def _date_add_days(d: datetime | None, days: float) -> datetime | None:
    if d is None:
        return None
    return d + timedelta(days=days)


def _format_date(d: datetime | None) -> str | None:
    if d is None:
        return None
    return d.strftime(DATE_FMT)


def get_methods(
    product_id: str,
    location_id: str,
    data: dict[str, Any],
) -> list[dict]:
    """Return all methods that can fulfill (product, location): buy, make, move.
    When location is VIRTUAL or empty, also match methods by product_id only (make/buy)
    so demand at VIRTUAL can be planned via production at 1000/2000 etc.
    """
    methods: list[dict] = []
    loc = location_id or ""
    pid = product_id or ""
    virtual_fallback = loc in ("", "VIRTUAL")

    for m in data.get("method_buy") or []:
        if (m.get("product_id") or "") != pid:
            continue
        if (m.get("location_id") or "") == loc or virtual_fallback:
            methods.append({"type": "purchase", **m})

    for m in data.get("method_make") or []:
        if (m.get("product_id") or "") != pid:
            continue
        if (m.get("location_id") or "") == loc or virtual_fallback:
            methods.append({"type": "make", **m})

    for m in data.get("method_move") or []:
        if (m.get("product_id") or "") == pid and (m.get("to_location_id") or "") == loc:
            methods.append({"type": "move", **m})

    return methods


def get_preferred_method(methods: list[dict]) -> tuple[dict | None, str]:
    """
    Pick best method by preference (smaller number = higher preference). No simulation to avoid
    blow-up when many methods/levels exist. Returns (chosen_method, explanation).
    """
    if not methods:
        return None, "No methods available."
    chosen = min(methods, key=lambda m: (m.get("preference") or 0))
    pref = chosen.get("preference") or 0
    if len(methods) <= 1:
        return chosen, f"Only option: {chosen.get('type', '')} @ {chosen.get('location_id', '') or chosen.get('to_location_id', '')} (preference {pref})."
    alternatives = [
        f"{m.get('type', '')} @ {m.get('location_id') or m.get('to_location_id') or ''} (preference {m.get('preference') or 0})"
        for m in methods
        if m is not chosen
    ]
    return chosen, (
        f"Chosen: {chosen.get('type', '')} @ {chosen.get('location_id') or chosen.get('to_location_id') or ''} "
        f"(preference {pref}, best = lowest number). Alternatives: {'; '.join(alternatives)}."
    )


def get_preferred_method_elaborate(
    methods: list[dict],
    demand: dict,
    inventory: list[dict],
    data: dict[str, Any],
    request_time_dt: datetime | None,
    config: dict[str, Any] | None,
    depth: int,
    planning_path: frozenset[tuple[str, str]],
) -> tuple[dict | None, str]:
    """
    Score each method by simulating one level of plan() (with limited depth) and aggregating
    commit_time, inventory_consumed, purchase. Uses same score_weights as variant_selection if present.
    Only safe to call when depth is high (top-level); otherwise falls back to preference-only.
    Returns (chosen_method, explanation).
    """
    if not methods:
        return None, "No methods available."
    # Only use elaborate scoring at the very first call (depth == MAX_PLAN_DEPTH); simulated child plans use simple method selection
    if depth < MAX_PLAN_DEPTH:
        return get_preferred_method(methods)
    # Single method: no need to score, decision is trivial
    if len(methods) <= 1:
        return get_preferred_method(methods)
    logger.debug("Elaborate method scoring: %s methods for demand %s", len(methods), demand.get("demand_id") or demand.get("product_id"))
    variant_selection = (config or {}).get("variant_selection") or {}
    score_weights = variant_selection.get("score_weights")
    w_commit, w_inv, w_purchase = _normalize_score_weights(score_weights)
    _LATE = datetime(9999, 12, 31)
    _BENIGN = frozenset({"cycle_stopped", "cycle_detected"})
    product_id = demand.get("product_id") or ""
    location_id = demand.get("location_id") or ""
    quantity = float(demand.get("quantity") or 0)
    req_time_str = demand.get("request_due_time") or demand.get("request_time")
    scored: list[tuple[float, dict, float, float, float, bool]] = []  # (combined_score, method, max_commit_ts, consumed, purchase, failed)
    for m in methods:
        inv_copy = _copy_inventory(inventory)
        production_location = (m.get("location_id") or m.get("to_location_id") or location_id or "")
        req_dt = _parse_date(req_time_str) or request_time_dt
        lead_days = 0.0
        if m.get("type") == "make":
            lead_days = float(m.get("lead_time") or 0)
        elif m.get("type") == "move":
            lead_days = float(m.get("transit_time") or 0)
        elif m.get("type") == "purchase":
            lead_days = float(m.get("lead_days_supply") or 0)
        c_req_dt = _date_add_days(req_dt, -lead_days) if req_dt else None
        if m.get("type") == "make":
            variants = _variants_for_make(product_id, production_location, quantity, m, data)
            if not variants:
                scored.append((-1e9, m, _LATE.timestamp(), 0.0, 0.0, True))
                continue
            variant_list, _ = get_preferred_variants(
                variants, inv_copy, data, req_dt, lead_days, planning_path,
                depth - 1,
                quantity,
                multiple=False, score_weights=score_weights, top_n=None,
            )
            child_materials = [c for (cm, _ak, _qty) in variant_list for c in cm]
        elif m.get("type") == "move":
            child_materials = _child_materials_for_move(m, quantity)
        else:
            child_materials = []
        before_qty = sum(float(b.get("qty") or 0) for b in inv_copy)
        max_commit = None
        purchase_qty = 0.0
        any_failed = False
        sim_depth = depth - 1  # full depth: no cap (was MAX_METHOD_SCORING_DEPTH)
        for c in child_materials:
            c_demand = {
                "demand_id": None,
                "product_id": c["product_id"],
                "location_id": c["location_id"],
                "quantity": c["quantity"],
                "request_due_time": _format_date(c_req_dt) if c_req_dt else None,
                "request_time": _format_date(c_req_dt) if c_req_dt else None,
            }
            solved_list, c_wos, _ = plan(
                c_demand, inv_copy, data, c_req_dt, depth=sim_depth, planning_path=planning_path, config=config
            )
            for s in solved_list:
                if (s.get("quantity") or 0) <= 0:
                    continue
                ct = s.get("commit_time")
                reason = s.get("commit_reason") or ""
                if ct is None or (reason and reason not in _BENIGN):
                    any_failed = True
                if ct:
                    dt = _parse_date(ct)
                    if dt and (max_commit is None or dt > max_commit):
                        max_commit = dt
            for wo in c_wos:
                if (wo.get("method") or "") == "purchase":
                    purchase_qty += float(wo.get("quantity") or 0)
        after_qty = sum(float(b.get("qty") or 0) for b in inv_copy)
        consumed = before_qty - after_qty
        if any_failed:
            max_commit = None
        ts = max_commit.timestamp() if max_commit else _LATE.timestamp()
        scored.append((0.0, m, ts, consumed, purchase_qty, any_failed))
    # Normalize and compute combined score (higher = better). Same as variant scoring.
    ts_list = [x[2] for x in scored if not x[5]]
    cons_list = [x[3] for x in scored]
    purch_list = [x[4] for x in scored]
    span_ts = (max(ts_list) - min(ts_list)) if len(ts_list) >= 2 else 1.0
    span_c = max(cons_list) - min(cons_list) if cons_list else 1.0
    span_p = max(purch_list) - min(purch_list) if purch_list else 1.0
    if span_c <= 0:
        span_c = 1.0
    if span_p <= 0:
        span_p = 1.0
    ts_max = max(ts_list) if ts_list else 0.0
    for i, row in enumerate(scored):
        _, m, ts, consumed, purchase, failed = row
        if failed:
            scored[i] = (-1e9, m, ts, consumed, purchase, failed)
            continue
        norm_commit = (ts_max - ts) / span_ts if span_ts > 0 else 1.0
        norm_commit = max(0.0, min(1.0, norm_commit))
        norm_inv = (consumed - min(cons_list)) / span_c if span_c > 0 else 1.0
        norm_inv = max(0.0, min(1.0, norm_inv))
        norm_p = (max(purch_list) - purchase) / span_p if span_p > 0 else 1.0
        norm_p = max(0.0, min(1.0, norm_p))
        combined = w_commit * norm_commit + w_inv * norm_inv + w_purchase * norm_p
        scored[i] = (combined, m, ts, consumed, purchase, failed)
    scored.sort(key=lambda x: -x[0])
    best = scored[0]
    if best[5]:
        return get_preferred_method(methods)
    chosen = best[1]
    expl = (
        f"Chosen (elaborate score): {chosen.get('type', '')} @ {chosen.get('location_id') or chosen.get('to_location_id') or ''} "
        f"(commit_time, inventory_consumed={best[3]:.0f}, purchase={best[4]:.0f})."
    )
    return chosen, expl


def _inventory_key(product_id: str, location_id: str) -> tuple[str, str]:
    return (product_id or "", location_id or "")


def _consume_from_inventory(
    inventory: list[dict],
    product_id: str,
    location_id: str,
    need: float,
) -> tuple[float, str | None]:
    """
    Consume up to `need` from inventory for (product_id, location_id), FIFO by supply_date.
    inventory items: { product_id, location_id, supply_date, qty } (qty is mutated).
    Returns (taken, commit_time from first supply used, or None if preexisting).
    """
    key = _inventory_key(product_id, location_id)
    taken = 0.0
    commit_time: str | None = None
    # Sort so preexisting (null supply_date) and earlier dates first
    buckets = [b for b in inventory if _inventory_key(b["product_id"], b["location_id"]) == key and (b.get("qty") or 0) > 0]
    buckets.sort(key=lambda b: (_parse_date(b.get("supply_date")) or datetime.min, b.get("supply_id", "")))

    for b in buckets:
        if taken >= need:
            break
        avail = float(b.get("qty") or 0)
        if avail <= 0:
            continue
        take = min(avail, need - taken)
        b["qty"] = avail - take
        taken += take
        if commit_time is None and take > 0:
            sd = b.get("supply_date")
            commit_time = _format_date(_parse_date(sd)) if sd else None
    return taken, commit_time


def _variants_for_make(
    product_id: str,
    location_id: str,
    quantity: float,
    method: dict,
    data: dict[str, Any],
) -> list[tuple[str, list[dict]]]:
    """
    Group BOM rows by ALT_GROUP (same BOM_ID, PARENT_ID). Each group = one variant (requirement set).
    Returns list of (alt_group_key, child_materials) where child_materials = [{ product_id, location_id, quantity }, ...].
    If no rows match the method's bom_id, fall back to any BOM rows with parent_id = product_id (so method_make
    rows whose bom_id is not in bom.csv still get a component tree from the parent's BOM).
    """
    bom_id = (method.get("bom_id") or "").strip()
    if not bom_id:
        return []
    product_id = (product_id or "").strip()
    by_alt: dict[str, list[dict]] = {}
    bom_list = data.get("bom") or []
    for b in bom_list:
        if (b.get("parent_id") or "").strip() != product_id:
            continue
        if (b.get("bom_id") or "").strip() != bom_id:
            continue
        rate = float(b.get("rate") or 1.0)
        if rate <= 0:
            continue
        ag = b.get("alt_group")
        alt_key = str(ag).strip() if ag is not None and str(ag).strip() else "__null__"
        child_qty = quantity * rate
        entry = {
            "product_id": b.get("child_id") or "",
            "location_id": location_id or "",
            "quantity": child_qty,
        }
        if alt_key not in by_alt:
            by_alt[alt_key] = []
        by_alt[alt_key].append(entry)
    if by_alt:
        return [(k, v) for k, v in by_alt.items()]
    if bom_id:
        by_alt_fallback: dict[str, list[dict]] = {}
        for b in bom_list:
            if (b.get("parent_id") or "").strip() != product_id:
                continue
            rate = float(b.get("rate") or 1.0)
            if rate <= 0:
                continue
            ag = b.get("alt_group")
            alt_key = str(ag).strip() if ag is not None and str(ag).strip() else "__null__"
            child_qty = quantity * rate
            entry = {
                "product_id": b.get("child_id") or "",
                "location_id": location_id or "",
                "quantity": child_qty,
            }
            if alt_key not in by_alt_fallback:
                by_alt_fallback[alt_key] = []
            by_alt_fallback[alt_key].append(entry)
        if by_alt_fallback:
            logger.debug(
                "variants_for_make: no BOM rows for bom_id=%s parent=%s; using fallback by parent_id (found %s variant(s))",
                bom_id, product_id, len(by_alt_fallback),
            )
            return [(k, v) for k, v in by_alt_fallback.items()]
    return []


def _copy_inventory(inventory: list[dict]) -> list[dict]:
    """Deep copy inventory so we can mutate it when scoring variants without affecting the original."""
    return [
        {
            "product_id": b.get("product_id") or "",
            "location_id": b.get("location_id") or "",
            "supply_date": b.get("supply_date"),
            "supply_id": b.get("supply_id"),
            "qty": float(b.get("qty") or 0),
        }
        for b in inventory
    ]


def _scale_child_materials(child_list: list[dict], scale: float) -> list[dict]:
    """Return child_materials with each quantity multiplied by scale."""
    if abs(scale - 1.0) < 1e-12:
        return child_list
    return [
        {**c, "quantity": float(c.get("quantity") or 0) * scale}
        for c in child_list
    ]


# Result set: list of (child_materials, alt_key, quantity) per variant
VariantResultItem = tuple[list[dict], str, float]


# Default weights for variant scoring when not overridden by config (commit_time, inventory_consumed, purchase)
_DEFAULT_SCORE_WEIGHTS = (0.4, 0.35, 0.25)


def _normalize_score_weights(weights: dict[str, float] | None) -> tuple[float, float, float]:
    """Return (w_commit, w_inventory, w_purchase) summing to 1. Missing keys default to 0 then we normalize."""
    if not weights:
        return _DEFAULT_SCORE_WEIGHTS
    w_c = float(weights.get("commit_time") or 0)
    w_i = float(weights.get("inventory_consumed") or 0)
    w_p = float(weights.get("purchase") or 0)
    total = w_c + w_i + w_p
    if total <= 0:
        return _DEFAULT_SCORE_WEIGHTS
    return (w_c / total, w_i / total, w_p / total)


def get_preferred_variants(
    variants: list[tuple[str, list[dict]]],
    inventory: list[dict],
    data: dict[str, Any],
    req_dt: datetime | None,
    lead_days: float,
    planning_path: frozenset[tuple[str, str]],
    depth: int,
    demand_net_qty: float,
    multiple: bool | None = None,
    score_weights: dict[str, float] | None = None,
    top_n: int | None = None,
) -> tuple[list[VariantResultItem], str]:
    """
    When multiple is False: return one best variant (set of one), chosen by weighted score.
    When multiple is None (default): return variants that equally divide the demand. If top_n is set
    (e.g. 2), use only the top N variants by weighted score; otherwise use all feasible.
    score_weights: optional { commit_time, inventory_consumed, purchase } (0..1, normalized).
    Returns (list of (child_materials, alt_key, quantity), explanation).
    """
    if not variants:
        return [], "No variants."

    if len(variants) <= 1:
        alt_key, child_list = variants[0]
        return (
            [(child_list, alt_key, demand_net_qty)],
            f"Single variant (ALT_GROUP={alt_key}); {len(child_list)} component(s).",
        )

    logger.info(
        "multi-variant: %s variants alt_groups=%s",
        len(variants),
        [alt_key for alt_key, _ in variants],
    )
    _BENIGN = frozenset({"cycle_stopped", "cycle_detected"})
    _LATE = datetime(9999, 12, 31)
    w_commit, w_inv, w_purchase = _normalize_score_weights(score_weights)

    def score_variant(alt_key: str, child_list: list[dict]) -> tuple[datetime | None, float, float, bool]:
        inv_copy = _copy_inventory(inventory)
        before_qty = sum(float(b.get("qty") or 0) for b in inv_copy)
        max_commit: datetime | None = None
        purchase_qty = 0.0
        any_failed = False
        for c in child_list:
            c_req_dt = _date_add_days(req_dt, -lead_days) if req_dt else None
            c_demand = {
                "demand_id": None,
                "product_id": c["product_id"],
                "location_id": c["location_id"],
                "quantity": c["quantity"],
                "request_due_time": _format_date(c_req_dt) if c_req_dt else None,
                "request_time": _format_date(c_req_dt) if c_req_dt else None,
            }
            solved_list, c_wos, _ = plan(
                c_demand, inv_copy, data, c_req_dt, depth=depth - 1, planning_path=planning_path
            )
            for s in solved_list:
                if (s.get("quantity") or 0) <= 0:
                    continue
                ct = s.get("commit_time")
                reason = (s.get("commit_reason") or "")
                if ct is None or (reason and reason not in _BENIGN):
                    any_failed = True
                if ct:
                    dt = _parse_date(ct)
                    if dt and (max_commit is None or dt > max_commit):
                        max_commit = dt
            for wo in c_wos:
                if (wo.get("method") or "") == "purchase":
                    purchase_qty += float(wo.get("quantity") or 0)
        after_qty = sum(float(b.get("qty") or 0) for b in inv_copy)
        consumed = before_qty - after_qty
        if any_failed:
            max_commit = None
        return (max_commit or _LATE, -consumed, purchase_qty, any_failed)

    scored: list[tuple[tuple, str, list[dict], bool]] = []
    for alt_key, child_list in variants:
        sc = score_variant(alt_key, child_list)
        scored.append((sc, alt_key, child_list, sc[3]))

    # When equal split and no top_n, decision doesn't depend on scores—only feasibility. Skip score math and sort.
    need_ranking = multiple is False or (top_n is not None and top_n >= 1)
    if need_ranking:
        commits = [x[0][0] for x in scored]
        consumed_list = [-x[0][1] for x in scored]
        purchase_list = [x[0][2] for x in scored]
        valid_ts = [c.timestamp() for c in commits if c != _LATE]
        span_ts = (max(valid_ts) - min(valid_ts)) if len(valid_ts) >= 2 else 1.0
        span_c = max(consumed_list) - min(consumed_list) if consumed_list else 1.0
        span_p = max(purchase_list) - min(purchase_list) if purchase_list else 1.0
        if span_c <= 0:
            span_c = 1.0
        if span_p <= 0:
            span_p = 1.0
        ts_max_val = max(valid_ts) if valid_ts else 0.0

        def combined_score(idx: int) -> float:
            commit_ts = commits[idx]
            consumed = consumed_list[idx]
            purchase = purchase_list[idx]
            any_failed = scored[idx][3]
            if any_failed:
                return -1e9
            norm_commit = (ts_max_val - commit_ts.timestamp()) / span_ts if span_ts > 0 and commit_ts != _LATE else 1.0
            norm_commit = max(0.0, min(1.0, norm_commit))
            norm_inv = (consumed - min(consumed_list)) / span_c if span_c > 0 else 1.0
            norm_inv = max(0.0, min(1.0, norm_inv))
            norm_purchase = (max(purchase_list) - purchase) / span_p if span_p > 0 else 1.0
            norm_purchase = max(0.0, min(1.0, norm_purchase))
            return w_commit * norm_commit + w_inv * norm_inv + w_purchase * norm_purchase

        scored_with_score = [(combined_score(i), scored[i]) for i in range(len(scored))]
        scored_with_score.sort(key=lambda x: -x[0])
        scored = [x[1] for x in scored_with_score]

    if multiple is False:
        best = scored[0]
        chosen_alt = best[1]
        chosen_list = best[2]
        max_commit, neg_consumed, purchase, _ = best[0]
        others = [v[1] for v in scored[1:]]
        explanation = (
            f"Chosen variant ALT_GROUP={chosen_alt} ({len(chosen_list)} component(s)). "
            f"Score: commit_time={'—' if max_commit == _LATE else _format_date(max_commit)}, "
            f"inventory_consumed={-neg_consumed:.0f}, purchase_qty={purchase:.0f}. "
            f"Alternatives: {', '.join(others)}."
        )
        return ([(chosen_list, chosen_alt, demand_net_qty)], explanation)

    # multiple is None (default): feasible variants, equally divide demand; optionally limit to top_n
    feasible = [(alt_key, child_list) for (_, alt_key, child_list, any_failed) in scored if not any_failed]
    if not feasible:
        feasible = [(scored[0][1], scored[0][2])]
    if top_n is not None and top_n >= 1:
        feasible = feasible[: top_n]
    n = len(feasible)
    # When original demand is integer, keep per-variant quantities integer (base + remainder spread)
    demand_int = int(round(demand_net_qty))
    use_integer_split = n > 0 and abs(demand_net_qty - demand_int) < 1e-9
    if use_integer_split and n > 0:
        base = demand_int // n
        remainder = demand_int % n
        qty_per_variant = [float(base + 1)] * remainder + [float(base)] * (n - remainder)
    else:
        qty_each = demand_net_qty / n if n > 0 else demand_net_qty
        qty_per_variant = [qty_each] * n
    result: list[VariantResultItem] = []
    for i, (alt_key, child_list) in enumerate(feasible):
        qty_i = qty_per_variant[i] if i < len(qty_per_variant) else (demand_net_qty / n if n > 0 else demand_net_qty)
        scale = qty_i / demand_net_qty if demand_net_qty > 1e-12 else 1.0
        result.append((_scale_child_materials(child_list, scale), alt_key, qty_i))
    alt_keys = [alt_key for _, alt_key, _ in result]
    qty_str = ", ".join(f"{q:.0f}" if use_integer_split else f"{q:.2f}" for q in qty_per_variant)
    total_str = f"{demand_net_qty:.0f}" if use_integer_split else f"{demand_net_qty:.2f}"
    if top_n is not None and top_n >= 1:
        explanation = (
            f"Top {n} variant(s) (by score): ALT_GROUP={', '.join(alt_keys)}; "
            f"quantities: {qty_str} (total {total_str})."
        )
    else:
        explanation = (
            f"Multiple variants ({n}): ALT_GROUP={', '.join(alt_keys)}; "
            f"quantities: {qty_str} (total {total_str})."
        )
    return (result, explanation)


def get_preferred_variant(
    variants: list[tuple[str, list[dict]]],
    inventory: list[dict],
    data: dict[str, Any],
    req_dt: datetime | None,
    lead_days: float,
    planning_path: frozenset[tuple[str, str]],
    depth: int,
    demand_net_qty: float,
) -> tuple[list[dict], str, str]:
    """
    Single-variant mode: same as get_preferred_variants(..., multiple=False). Returns (child_materials, chosen_alt_key, explanation).
    """
    result_list, explanation = get_preferred_variants(
        variants, inventory, data, req_dt, lead_days, planning_path, depth, demand_net_qty, multiple=False
    )
    if not result_list:
        return [], "", explanation
    child_materials, chosen_alt, _ = result_list[0]
    return child_materials, chosen_alt, explanation


def _child_materials_for_move(method: dict, quantity: float) -> list[dict]:
    """Return single child: same product at from_location (move)."""
    from_loc = method.get("from_location_id") or ""
    product_id = method.get("product_id") or ""
    return [{"product_id": product_id, "location_id": from_loc, "quantity": quantity}]


def _max_lot_size(product_id: str, location_id: str, data: dict[str, Any]) -> float | None:
    """Return MAX_LOT_SIZE from productlocation or None."""
    for pl in data.get("productlocation") or []:
        if (pl.get("product_id") or "") == product_id and (pl.get("location_id") or "") == (location_id or ""):
            v = pl.get("max_lot_size")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    return None


def _get_prod_area(product_id: str, location_id: str, data: dict[str, Any]) -> str | None:
    """Return PROD_AREA from productlocation for (product_id, location_id) or None."""
    for pl in data.get("productlocation") or []:
        if (pl.get("product_id") or "") == product_id and (pl.get("location_id") or "") == (location_id or ""):
            v = pl.get("prod_area")
            if v is not None and str(v).strip():
                return str(v).strip()
    return None


def plan(
    demand: dict,
    inventory: list[dict],
    data: dict[str, Any],
    request_time_dt: datetime | None,
    depth: int = MAX_PLAN_DEPTH,
    planning_path: frozenset[tuple[str, str]] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[list[dict], list[dict], PeggingNode | None]:
    """
    Plan one demand. Returns (committed_demands, work_orders, pegging_tree_node).
    pegging_tree_node: demand at root, children = supply nodes + work_order node; WO children = recursive trees.
    """
    planning_path = planning_path or frozenset()
    product_id = demand.get("product_id") or ""
    location_id = demand.get("location_id") or ""
    quantity = float(demand.get("quantity") or 0)
    demand_id = demand.get("demand_id")
    req_time_str = demand.get("request_due_time") or demand.get("request_time")
    customer_id = demand.get("customer_id")
    customer = demand.get("customer")

    def _demand_node(children: list[PeggingNode], commit_time: str | None = None, commit_reason: str | None = None) -> PeggingNode:
        return {
            "type": "demand",
            "demand_id": demand_id,
            "product_id": product_id,
            "location_id": location_id,
            "quantity": quantity,
            "request_time": req_time_str,
            "commit_time": commit_time,
            "commit_reason": commit_reason,
            "children": children,
        }

    if quantity <= 0:
        return [], [], None
    if depth <= 0:
        return [{
            "demand_id": demand_id,
            "customer_id": customer_id,
            "customer": customer,
            "product_id": product_id,
            "location_id": location_id,
            "quantity": quantity,
            "request_time": req_time_str,
            "commit_time": req_time_str,
            "commit_reason": "depth_limit",
        }], [], _demand_node([], req_time_str, "depth_limit")
    key = (product_id, location_id or "")
    if key in planning_path:
        # Natural circle (e.g. movable both ways between two locations): stop before re-entering. Not a failure.
        return [{
            "demand_id": demand_id,
            "customer_id": customer_id,
            "customer": customer,
            "product_id": product_id,
            "location_id": location_id,
            "quantity": quantity,
            "request_time": req_time_str,
            "commit_time": req_time_str,
            "commit_reason": "cycle_stopped",
        }], [], _demand_node([], req_time_str, "cycle_stopped")
    path = planning_path | {key}

    # 1) Fulfill from inventory (FIFO)
    taken, fulfill_commit_time = _consume_from_inventory(inventory, product_id, location_id, quantity)
    demand_fulfilled_list: list[dict] = []
    pegging_children: list[PeggingNode] = []
    if taken > 0:
        commit_for_fulfilled = fulfill_commit_time if fulfill_commit_time is not None else req_time_str
        demand_fulfilled_list.append({
            "demand_id": demand_id,
            "customer_id": customer_id,
            "customer": customer,
            "product_id": product_id,
            "location_id": location_id,
            "quantity": taken,
            "request_time": req_time_str,
            "commit_time": commit_for_fulfilled,
        })
        pegging_children.append({
            "type": "supply",
            "product_id": product_id,
            "location_id": location_id,
            "quantity": round(taken, 4),
            "commit_time": commit_for_fulfilled,
            "children": [],
        })

    demand_net_qty = quantity - taken
    if demand_net_qty <= 0:
        return demand_fulfilled_list, [], _demand_node(pegging_children, fulfill_commit_time)

    # 2) Get methods and preferred method
    methods = get_methods(product_id, location_id, data)
    if not methods:
        demand_fulfilled_list.append({
            "demand_id": demand_id,
            "customer_id": customer_id,
            "customer": customer,
            "product_id": product_id,
            "location_id": location_id,
            "quantity": demand_net_qty,
            "request_time": req_time_str,
            "commit_time": req_time_str,
            "commit_reason": "no_methods",
        })
        return demand_fulfilled_list, [], _demand_node(pegging_children, req_time_str, "no_methods")

    # Default: elaborate off → method by preference only. When elaborate on, methods scored by same score_weights as variant_selection.
    if len(methods) > 1:
        logger.info(
            "multi-method: demand_id=%s product_id=%s location_id=%s count=%s types=%s",
            demand_id, product_id, location_id, len(methods),
            [m.get("type") for m in methods],
        )
    method_selection = (config or {}).get("method_selection") or {}
    use_elaborate_method = method_selection.get("elaborate") is True
    use_multiple_methods = method_selection.get("multiple") is True and len(methods) > 1
    variant_selection = (config or {}).get("variant_selection") or {}
    use_single_variant = variant_selection.get("multiple") is False
    score_weights = variant_selection.get("score_weights")
    top_n = variant_selection.get("top_n")
    if isinstance(top_n, (int, float)):
        top_n = max(1, int(top_n))
    else:
        top_n = None

    if use_multiple_methods:
        # Equal split: no method scoring. We plan each method branch fully (variants + children), so runtime is ~N× single-method when N methods exist.
        n = len(methods)
        demand_int = int(round(demand_net_qty))
        use_integer_split = n > 0 and abs(demand_net_qty - demand_int) < 1e-9
        if use_integer_split:
            base = demand_int // n
            remainder = demand_int % n
            qty_per_method = [float(base + 1)] * remainder + [float(base)] * (n - remainder)
        else:
            qty_each = demand_net_qty / n if n > 0 else demand_net_qty
            qty_per_method = [qty_each] * n

        all_wos: list[dict] = []
        all_pegging_wo_nodes: list[PeggingNode] = []
        latest_commit: datetime | None = None
        _BENIGN = frozenset({"cycle_stopped", "cycle_detected"})
        req_dt = _parse_date(req_time_str) or request_time_dt
        if req_dt is None:
            req_dt = datetime.now()

        for idx, m in enumerate(methods):
            method_qty = qty_per_method[idx] if idx < len(qty_per_method) else 0.0
            if method_qty <= 1e-9:
                continue
            if m.get("type") == "move":
                production_location = m.get("to_location_id") or location_id
            else:
                production_location = m.get("location_id") or location_id
            lead_days = 0.0
            if m.get("type") == "make":
                lead_days = float(m.get("lead_time") or 0)
            elif m.get("type") == "move":
                lead_days = float(m.get("transit_time") or 0)
            elif m.get("type") == "purchase":
                lead_days = float(m.get("lead_days_supply") or 0)

            wo_children_relation: str | None = None
            if m.get("type") == "make":
                variants = _variants_for_make(product_id, production_location, method_qty, m, data)
                variant_list, variant_explanation = get_preferred_variants(
                    variants, inventory, data, req_dt, lead_days, path, depth, method_qty,
                    multiple=False if use_single_variant else None,
                    score_weights=score_weights,
                    top_n=top_n,
                )
                child_materials = [c for (cm, _ak, _qty) in variant_list for c in cm]
                # Determine whether child inventories under this work order represent OR (single-component variants) or AND (BOM components).
                if len(variant_list) > 1:
                    all_single_component = all(len(cm) == 1 for (cm, _ak, _qty) in variant_list)
                    if all_single_component:
                        wo_children_relation = "or"
                    else:
                        wo_children_relation = "and"
            elif m.get("type") == "move":
                child_materials = _child_materials_for_move(m, method_qty)
                variant_explanation = ""
            else:
                child_materials = []
                variant_explanation = ""

            child_wos: list[dict] = []
            commit_times: list[datetime] = []
            child_pegging_nodes: list[PeggingNode] = []
            for c in child_materials:
                if m.get("type") == "make":
                    parent_key = (product_id or "").strip()
                    child_key = (c.get("product_id") or "").strip()
                    if (parent_key, child_key) in REAL_BOM_PAIRS:
                        logger.info(
                            "planning: real BOM used (VIRTUAL<>Y) parent_id=%s child_id=%s demand_id=%s qty=%s (equal-split methods)",
                            parent_key,
                            child_key,
                            demand_id,
                            c.get("quantity"),
                        )
                c_req_dt = _date_add_days(req_dt, -lead_days) if req_dt else None
                c_demand = {
                    "demand_id": demand_id,
                    "product_id": c["product_id"],
                    "location_id": c["location_id"],
                    "quantity": c["quantity"],
                    "request_due_time": _format_date(c_req_dt) if c_req_dt else None,
                    "request_time": _format_date(c_req_dt) if c_req_dt else None,
                }
                solved_list, c_wos, c_pegging = plan(c_demand, inventory, data, c_req_dt, depth=depth - 1, planning_path=path, config=config)
                child_wos.extend(c_wos)
                if c_pegging is not None:
                    child_pegging_nodes.append(c_pegging)
                for s in solved_list:
                    if (s.get("quantity") or 0) <= 0:
                        continue
                    reason = s.get("commit_reason") or ""
                    if s.get("commit_time") is None or (reason and reason not in _BENIGN):
                        child_reason = reason or "child_planning_failed"
                        demand_fulfilled_list.append({
                            "demand_id": demand_id,
                            "customer_id": customer_id,
                            "customer": customer,
                            "product_id": product_id,
                            "location_id": location_id,
                            "quantity": demand_net_qty,
                            "request_time": req_time_str,
                            "commit_time": req_time_str,
                            "commit_reason": f"child_failed:{s.get('product_id','')}@{s.get('location_id','')}({child_reason})",
                        })
                        return demand_fulfilled_list, [], _demand_node(pegging_children, req_time_str, f"child_failed:{child_reason}")
                for s in solved_list:
                    ct_str = s.get("commit_time")
                    if ct_str:
                        dt = _parse_date(ct_str)
                        if dt:
                            commit_times.append(dt)

            start_dt = req_dt
            if req_dt and lead_days > 0:
                start_dt = _date_add_days(req_dt, -lead_days)
            if commit_times:
                latest_child = max(commit_times)
                if start_dt is None or latest_child > start_dt:
                    start_dt = latest_child
            end_dt = _date_add_days(start_dt, lead_days) if start_dt else None

            lot_size_val = _max_lot_size(product_id, production_location, data)
            if lot_size_val is None or lot_size_val <= 0:
                lot_size_val = method_qty
            lot_size = max(1e-9, float(lot_size_val))
            prod_area = _get_prod_area(product_id, production_location, data)
            wos: list[dict] = []
            left = method_qty
            lot_start = start_dt
            last_end = end_dt
            lot_count = 0
            while left > 1e-9 and lot_start is not None:
                lot_qty = min(lot_size, left)
                lot_end = _date_add_days(lot_start, lead_days) if lot_start else None
                wos.append({
                    "product_id": product_id,
                    "location_id": production_location,
                    "quantity": round(lot_qty, 4),
                    "start_time": _format_date(lot_start),
                    "end_time": _format_date(lot_end),
                    "method": m.get("type", ""),
                    "location_source": m.get("from_location_id") if m.get("type") == "move" else None,
                    "demand_id": demand_id,
                    "prod_area": prod_area,
                })
                last_end = lot_end
                left -= lot_qty
                lot_count += 1
                if left > 1e-9:
                    lot_start = lot_end

            method_type = m.get("type", "")
            method_label = f"{method_type}@{production_location}"
            method_choice_explanation = f"Equal split: {method_label} (qty {method_qty:.0f} of {demand_net_qty:.0f})"
            if method_type == "purchase":
                wo_children = [{
                    "type": "purchase",
                    "product_id": product_id,
                    "location_id": production_location,
                    "quantity": round(method_qty, 4),
                    "children": [],
                }]
            else:
                wo_children = child_pegging_nodes
            wo_node: PeggingNode = {
                "type": "work_order",
                "product_id": product_id,
                "location_id": production_location,
                "quantity": round(method_qty, 4),
                "start_time": _format_date(start_dt),
                "end_time": _format_date(last_end) if last_end else None,
                "method": method_type,
                "location_source": m.get("from_location_id") if method_type == "move" else None,
                "method_choice_explanation": method_choice_explanation,
                "variant_choice_explanation": variant_explanation if variant_explanation else None,
                "children_relation": wo_children_relation,
                "lot_count": lot_count if lot_count > 0 else None,
                "max_lot_size": float(lot_size_val) if lot_size_val is not None else None,
                "children": wo_children,
            }
            all_pegging_wo_nodes.append(wo_node)
            all_wos.extend(wos)
            all_wos.extend(child_wos)
            if last_end and (latest_commit is None or last_end > latest_commit):
                latest_commit = last_end

        demand_fulfilled_list.append({
            "demand_id": demand_id,
            "customer_id": customer_id,
            "customer": customer,
            "product_id": product_id,
            "location_id": location_id,
            "quantity": demand_net_qty,
            "request_time": req_time_str,
            "commit_time": _format_date(latest_commit) if latest_commit else None,
        })
        return demand_fulfilled_list, all_wos, _demand_node(all_pegging_wo_nodes, _format_date(latest_commit) if latest_commit else None)

    # Single method: no need to score or run elaborate simulation
    if len(methods) == 1:
        m = methods[0]
        loc = m.get("location_id") or m.get("to_location_id") or ""
        method_choice_explanation = f"Only option: {m.get('type', '')} @ {loc}."
    elif use_elaborate_method and depth >= MAX_PLAN_DEPTH:
        m, method_choice_explanation = get_preferred_method_elaborate(
            methods, demand, inventory, data, request_time_dt, config, depth, path,
        )
    else:
        m, method_choice_explanation = get_preferred_method(methods)
    if not m:
        demand_fulfilled_list.append({
            "demand_id": demand_id,
            "customer_id": customer_id,
            "customer": customer,
            "product_id": product_id,
            "location_id": location_id,
            "quantity": demand_net_qty,
            "request_time": req_time_str,
            "commit_time": req_time_str,
            "commit_reason": "no_preferred_method",
        })
        return demand_fulfilled_list, [], _demand_node(pegging_children, req_time_str, "no_preferred_method")

    # Production location: where we make/move/buy (may differ from demand location, e.g. demand at VIRTUAL, make at 1000)
    if m.get("type") == "move":
        production_location = m.get("to_location_id") or location_id
    else:
        production_location = m.get("location_id") or location_id

    req_dt = _parse_date(req_time_str) or request_time_dt
    if req_dt is None:
        req_dt = datetime.now()
    lead_days = 0.0
    if m.get("type") == "make":
        lead_days = float(m.get("lead_time") or 0)
    elif m.get("type") == "move":
        lead_days = float(m.get("transit_time") or 0)
    elif m.get("type") == "purchase":
        lead_days = float(m.get("lead_days_supply") or 0)

    # 3) Child materials (at production location for make); use get_preferred_variants (multiple, top_n, score_weights from config)
    wo_children_relation: str | None = None
    if m.get("type") == "make":
        variants = _variants_for_make(product_id, production_location, demand_net_qty, m, data)
        variant_list, variant_explanation = get_preferred_variants(
            variants, inventory, data, req_dt, lead_days, path, depth, demand_net_qty,
            multiple=False if use_single_variant else None,
            score_weights=score_weights,
            top_n=top_n,
        )
        child_materials = [c for (cm, _ak, _qty) in variant_list for c in cm]
        if len(variant_list) > 1:
            all_single_component = all(len(cm) == 1 for (cm, _ak, _qty) in variant_list)
            if all_single_component:
                wo_children_relation = "or"
            else:
                wo_children_relation = "and"
    elif m.get("type") == "move":
        child_materials = _child_materials_for_move(m, demand_net_qty)
        variant_explanation = ""
    else:
        # purchase: no child materials (we treat as leaf; could extend with PO lead time)
        child_materials = []
        variant_explanation = ""

    # 4) Recursively plan children
    child_wos: list[dict] = []
    commit_times: list[datetime] = []
    child_pegging_nodes: list[PeggingNode] = []
    for c in child_materials:
        if m.get("type") == "make":
            parent_key = (product_id or "").strip()
            child_key = (c.get("product_id") or "").strip()
            if (parent_key, child_key) in REAL_BOM_PAIRS:
                logger.info(
                    "planning: real BOM used (VIRTUAL<>Y) parent_id=%s child_id=%s demand_id=%s qty=%s",
                    parent_key,
                    child_key,
                    demand_id,
                    c.get("quantity"),
                )
        c_req_dt = _date_add_days(req_dt, -lead_days) if req_dt else None
        c_demand = {
            "demand_id": demand_id,
            "product_id": c["product_id"],
            "location_id": c["location_id"],
            "quantity": c["quantity"],
            "request_due_time": _format_date(c_req_dt) if c_req_dt else None,
            "request_time": _format_date(c_req_dt) if c_req_dt else None,
        }
        solved_list, c_wos, c_pegging = plan(c_demand, inventory, data, c_req_dt, depth=depth - 1, planning_path=path, config=config)
        child_wos.extend(c_wos)
        if c_pegging is not None:
            child_pegging_nodes.append(c_pegging)
        # Check if any solved has commit_time None or a failure reason (child failed). cycle_stopped is NOT failure.
        _BENIGN_REASONS = frozenset({"cycle_stopped", "cycle_detected"})
        for s in solved_list:
            ct = s.get("commit_time")
            reason = s.get("commit_reason") or ""
            if (s.get("quantity") or 0) <= 0:
                continue
            if ct is None or (reason and reason not in _BENIGN_REASONS):
                child_reason = reason or "child_planning_failed"
                demand_fulfilled_list.append({
                    "demand_id": demand_id,
                    "customer_id": customer_id,
                    "customer": customer,
                    "product_id": product_id,
                    "location_id": location_id,
                    "quantity": demand_net_qty,
                    "request_time": req_time_str,
                    "commit_time": req_time_str,
                    "commit_reason": f"child_failed:{s.get('product_id','')}@{s.get('location_id','')}({child_reason})",
                })
                return demand_fulfilled_list, [], _demand_node(pegging_children, req_time_str, f"child_failed:{child_reason}")
        for s in solved_list:
            ct_str = s.get("commit_time")
            if ct_str:
                dt = _parse_date(ct_str)
                if dt:
                    commit_times.append(dt)

    # 5) Current method: start_time, end_time
    start_dt = req_dt
    if req_dt and lead_days > 0:
        start_dt = _date_add_days(req_dt, -lead_days)
    if commit_times:
        latest_child = max(commit_times)
        if start_dt is None or latest_child > start_dt:
            start_dt = latest_child
    end_dt = _date_add_days(start_dt, lead_days) if start_dt else None

    # 6) Batching by lot_size (use production location for lot size)
    lot_size_val = _max_lot_size(product_id, production_location, data)
    if lot_size_val is None or lot_size_val <= 0:
        lot_size_val = demand_net_qty
    lot_size = max(1e-9, float(lot_size_val))
    prod_area = _get_prod_area(product_id, production_location, data)

    wos: list[dict] = []
    left = demand_net_qty
    lot_start = start_dt
    last_end = end_dt
    idx = 0
    lot_count = 0
    while left > 1e-9 and lot_start is not None:
        lot_qty = min(lot_size, left)
        lot_end = _date_add_days(lot_start, lead_days) if lot_start else None
        wos.append({
            "product_id": product_id,
            "location_id": production_location,
            "quantity": round(lot_qty, 4),
            "start_time": _format_date(lot_start),
            "end_time": _format_date(lot_end),
            "method": m.get("type", ""),
            "location_source": m.get("from_location_id") if m.get("type") == "move" else None,
            "demand_id": demand_id,
            "prod_area": prod_area,
        })
        last_end = lot_end
        left -= lot_qty
        idx += 1
        lot_count += 1
        if left > 1e-9:
            lot_start = lot_end

    # Pegging: one work_order node for this level; children = child trees (make/move) or purchase leaf (buy)
    method_type = m.get("type", "")
    if method_type == "purchase":
        wo_children: list[PeggingNode] = [{
            "type": "purchase",
            "product_id": product_id,
            "location_id": production_location,
            "quantity": round(demand_net_qty, 4),
            "children": [],
        }]
    else:
        wo_children = child_pegging_nodes
    wo_node: PeggingNode = {
        "type": "work_order",
        "product_id": product_id,
        "location_id": production_location,
        "quantity": round(demand_net_qty, 4),
        "start_time": _format_date(start_dt),
        "end_time": _format_date(last_end) if last_end else None,
        "method": method_type,
        "location_source": m.get("from_location_id") if method_type == "move" else None,
        "method_choice_explanation": method_choice_explanation,
        "variant_choice_explanation": variant_explanation if variant_explanation else None,
        "children_relation": wo_children_relation,
        "lot_count": lot_count if lot_count > 0 else None,
        "max_lot_size": float(lot_size_val) if lot_size_val is not None else None,
        "children": wo_children,
    }
    pegging_children.append(wo_node)

    demand_fulfilled_list.append({
        "demand_id": demand_id,
        "customer_id": customer_id,
        "customer": customer,
        "product_id": product_id,
        "location_id": location_id,
        "quantity": demand_net_qty,
        "request_time": req_time_str,
        "commit_time": _format_date(last_end) if last_end else None,
    })
    return demand_fulfilled_list, wos + child_wos, _demand_node(pegging_children, _format_date(last_end) if last_end else None)


def run_planning(
    data: dict[str, Any],
    config: dict[str, Any] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """
    Main entry: Inventory = supply, for each demand (sorted by preference) run plan.
    config: optional { "variant_selection": {...}, "method_selection": { "elaborate": true, "multiple": true } }. multiple=true: split demand equally across all feasible methods (make/move/buy); integer split when demand is integer.
    progress_callback: optional callable(progress_dict) called after each demand with e.g. {"current": i+1, "total": n, "demand_id": "..."}.
    Returns { "committed_demands": [...], "work_orders": [...], "planning_pegging": [...] }.
    """
    # Build mutable inventory from supply (list of buckets we can deduct from)
    inventory: list[dict] = []
    for s in data.get("supply") or []:
        inventory.append({
            "product_id": s.get("product_id") or "",
            "location_id": s.get("location_id") or "",
            "supply_date": s.get("supply_date"),
            "supply_id": s.get("supply_id"),
            "qty": float(s.get("qty") or 0),
        })

    demands = list(data.get("demand") or [])
    # Sort by priority (smaller number = higher priority)
    demands.sort(key=lambda d: (d.get("priority") or 0, d.get("demand_id") or ""))

    committed_demands: list[dict] = []
    work_orders: list[dict] = []
    planning_pegging: list[dict] = []  # [{ demand_id, tree }, ...]
    total = len(demands)

    for i, d in enumerate(demands):
        req_str = d.get("request_due_time") or d.get("request_time")
        req_dt = _parse_date(req_str)
        solved_list, wos, pegging_node = plan(d, inventory, data, req_dt, config=config)
        committed_demands.extend(solved_list)
        work_orders.extend(wos)
        demand_id = d.get("demand_id")
        if pegging_node is not None and demand_id:
            planning_pegging.append({"demand_id": demand_id, "tree": pegging_node})
        if progress_callback:
            progress_callback({"current": i + 1, "total": total, "demand_id": demand_id})

    return {
        "committed_demands": committed_demands,
        "work_orders": work_orders,
        "planning_pegging": planning_pegging,
    }
