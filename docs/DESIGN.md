# Data model and allocation logic (spec alignment)

This document restates the spec’s **model intuition** and **allocation algorithm**, then maps them to the current implementation so we stay aligned.

---

## 1. Model intuition (from spec)

**Mental model: inventory state transformation.**

| Concept | Spec | Meaning |
|--------|------|--------|
| **Inventory** | product + location + **time** | A “bucket” of quantity is (product_id, location_id) plus a **timing factor**. Inventory is not just what/where but also when. |
| **Demand** | inventory | Demand is for a product (and may carry a due time); we treat it as demand for (product_id, location_id) once we pick the location. |
| **Supply** | inventory | Supply rows are (supply_id, product_id, location_id, **supply_date**, qty). **When supply_date (time) is null, that supply is preexisting** — already on hand, no specific arrival date. |
| **Allocation** | inventory state transformation from supplies → demands | We move “quantity” from supply buckets through transformation steps until it satisfies demand; timing can constrain order and feasibility. |
| **One step** | method_make or method_move or method_buy | A single transformation is either make (BOM), move (transport), or buy. **Current focus: method_make and method_move only** (method_buy disregarded for allocation). |

So:

- **Inventory** = (product_id, location_id, **time**) with a quantity. **Time is part of the model**; e.g. supply can be “at this location by this date” or “preexisting” when date is null.
- **Preexisting supply**: supply with **null supply_date** = already available, no specific time.
- **Transformation steps** = method_make (consume children at a location, produce parent at that location) and method_move (move product from one location to another; transit_time gives timing).
- **Allocation** = run a process that starts from initial supplies and applies make/move steps toward demands; in a full time-aware formulation, ordering and feasibility depend on timing.

---

## 2. Data (CSV) ↔ implementation

| Spec entity | CSV / spec | Our DB / code |
|-------------|------------|----------------|
| BOM | bom(BOM_ID, PARENT_ID, CHILD_ID, ELEM_IX, ALT_GROUP, RATE) | `bom` table; engine uses parent_id → child_id, **RATE not yet used** in consumption |
| method_make | (BOM_ID, PRODUCT_ID, LOCATION_ID, PREFERENCE) | `method_make` table; we build **variants** = (product_id, location_id) with req = list of (child_id, location_id) |
| method_move | (PRODUCT_ID, FROM_LOCATION_ID, TO_LOCATION_ID, TRANSIT_TIME, UOM, PREFERENCE) | `method_move` table; **used** as graph edges: consume (product, from_loc) → produce (product, to_loc) with output_period = period + transit_time |
| method_buy | (PRODUCT_ID, LOCATION_ID, …) | `method_buy` table; **disregarded** in allocation (spec) |
| demand | (ID, PRODUCT_ID, CUSTOMER_ID, PRIORITY, REQUEST_DUE_TIME, QUANTITY) | `demand` table; demand = (product_id, customer, qty); we map product_id → (product_id, location_id) via first method_make for that product |
| supply | (SUPPLY_ID, PRODUCT_ID, LOCATION_ID, **SUPPLY_DATE**, QTY, …) | `supply` table; **SUPPLY_DATE null = preexisting (period 0)**. Supply is basket (component → list of (qty, period)); consumption FIFO by period. |

**Naming:** Spec and backend both use **method_move** (table and CSV `method_move.csv`); same columns.

---

## 3. Core notions in code

- **Node** = inventory(product_id, location_id, period) with quantity. **Period 0** = preexisting (null supply_date / request_due_time).
- **Edges / Time:** method_make: output_period = max(comp periods) + lead_time; method_move: output_period = period + transit_time. Each action has **edge_type** ("make" | "move"). Demand satisfied only if output_period ≤ due_period (0 = any).
- **Time in allocation (legacy):** Period index is built from all supply_date and request_due_time (null → period 0). Supply is stored as (component, qty, period) buckets; consumption is FIFO by period. Each allocation action has an **output_period** (when that output is available). A demand is only satisfied by allocation whose output_period ≤ the demand’s due_period (due_period 0 = any time).
- **Component** = (product_id, location_id). Key: `product_id|location_id`.
- **Variant** = (product_id, location_id) producible by **recipes**: (1) **method_make:** req = BOM children at same location; output_period = max(comp periods) + lead_time. (2) **method_move:** req = [(product, from_location)]; output_period = period + transit_time. Intermediary nodes are explicit: produced inventory is added to the basket and consumed by downstream make/move.
- **Target** = variant. “Demanded target” = variant whose product_id appears in demand (with a chosen location).
- **Basket** = component → list of (qty, period). `basket_alloc` and `basket_prod` start as initial supplies; after each allocation we add (variant, qty, output_period) to the basket.

---

## 4. Allocation algorithm (spec ↔ implementation)

### Init (spec)

- basket_alloc = initial supplies ✓  
- basket_prod = initial supplies ✓  
- targets_to_be_allocated = all non-leaf variants ✓  
- targets_to_be_produced = all non-leaf variants **excluding demand nodes** ✓  
- allocation = [] ✓  
- reserved_by_material = {} ✓  

**Implementation:** We have one variant per (product_id, location_id) from method_make. “Demand nodes” = variants that are demanded (product in demand); we set `targets_to_be_produced = variants_req.keys() - demanded_targets`.

### Scoring

- **Target weight (demanded t):**  
  `target_weight(t) = Σ_c unmet[(t,c)] * w_c`  
  We implement: unmet = (variant, customer) → remaining qty; weight = sum over customers of unmet * w_c. ✓  

- **Target weight (non-demanded t):**  
  `target_weight(t) = downstream_value(t)` = sum of target_weight of demanded targets reachable from t (via “t feeds into …”).  
  We implement: downstream(t) = variants that have t in their req; we topological-order and set weight(t) = sum(weight(t’) for t’ in downstream(t)). ✓  

- **Scarcity:**  
  `scarcity(c) = supply(c) / total_cap(c)` with `total_cap(c) = target_weight(c)` (or sum of weights of variants that use c if c is not a target).  
  We do the same; components processed in **ascending** scarcity order. ✓  

### Allocation pass (spec)

- For each component in scarcity order:
  - allocatable_variants = variants v with component in req_v ✓  
  - Proportional split over allocatable_variants ✓ (we use target_weight share × available qty)  
  - For each variant: consume/reserve components, record allocation, mark variant allocated ✓  
  - If all variants for target are allocated → add target qty to basket_alloc, apply reserved_by_material ✓  
  - Prune basket ✓  

**Implementation detail:** We treat “target” as a single variant (product_id, location_id). So “all variants for target” = that one variant. Spec could allow multiple variants per target (e.g. multiple BOMs for same product at same location); we don’t yet.

### Production pass (spec)

- For each component in scarcity order, candidates = variants that use component and are in targets_to_be_produced ✓  
- If all req allocated and all req available in basket_prod → fire variant, consume req, add target to basket_prod, prune ✓  

We do the same; “fire” = subtract req from basket_prod and add variant qty to basket_prod.

---

## 5. Gaps and simplifications

| Item | Spec | Current implementation |
|------|------|------------------------|
| **BOM RATE** | Child consumed at RATE per unit of parent | Not used; we assume 1:1 (effective rate = 1). |
| **method_move** | Component can be at FROM_LOCATION and consumed at TO_LOCATION (or vice versa) | Not used; component location = variant location for all BOM children. |
| **Multiple variants per target** | Same (product, location) could have multiple BOMs (e.g. alt groups) | Single variant per (product_id, location_id). |
| **“All variants for target”** | Might mean multiple ways to produce same target | We have one variant per target; condition reduces to “this variant allocated”. |
| **Critical component (per variant)** | critical_component = argmin supply(c) in req; critical_ratio defined | Computed in spec for insight; not yet used in our allocation or pegging logic. |
| **Time in inventory** | Inventory = product + location + time; null supply_date = preexisting | **Included:** Period index from supply_date and request_due_time (period 0 = preexisting). Supply is time-bucketed; consume FIFO by period; each allocation has output_period; feasible demand only counts allocation with output_period ≤ demand’s due_period. |

---

## 6. Summary

- **Data model:** Inventory = (product_id, location_id). Supply and demand are inventory; allocation is a process that transforms supply inventory via method_make (and optionally method_move) toward demand. We store method_move in the `method_move` table and match spec’s method_move.
- **Allocation logic:** Init baskets and targets; scarcity order; allocation pass (proportional split, consume/reserve, record actions, add target to basket when variant fully allocated, prune); production pass (fire when req allocated and available, consume, add target, prune). Implementation matches this except for the simplifications above.
- **Next steps for full alignment:** (1) Use BOM RATE when consuming components. (2) Use method_move to allow component at FROM and consumption at TO (or the reverse). (3) Optionally support multiple variants per (product, location) and clarify “all variants for target” in that case.
