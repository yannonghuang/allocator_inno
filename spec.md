----------------------------------------
Objective
----------------------------------------
allocating supplies to competing demands


----------------------------------------
Technology stack
----------------------------------------
Next App + Fast API + Postgres

----------------------------------------
Deployment
----------------------------------------
Docker compose

----------------------------------------
Requirements
----------------------------------------
1. automatic allocation of supplies to demandes, generate feasible demands
2. allow manual overrides
3. explanability: why a supply is split among multiple competing demands
4. pegging view
5. persistent case data, allocation results: load/save/purge

----------------------------------------
Model intuition and Sample case data: folder csv
----------------------------------------
Mendal model: Inventory state transformation. 
- inventory=product+location
- demand=inventory. 
- supply=inventory.
- allocation=inventory state transformation process starting with supplies towards demands (this is the current focus).
- resolution=inventory state transformation process starting with demands towards suppliues (this is NOT the current focus).
- once step in inventory state transformation process = method_make or method_move or method_buy (disregard method_buy in the current focus).

bom(BOM_ID,PARENT_ID,CHILD_ID,ELEM_IX,ALT_GROUP,RATE)

customer(CUSTOMER,DESCRIPTION)

demand(ID,DESCRIPTION,CUSTOMER_ID,PRIORITY,REQUEST_DUE_TIME,PRODUCT_ID,QUANTITY): smaller number= higher PRIORITY 

location(LOCATION_ID,LOCATION_DESCRIPTION)

method_buy(PRODUCT_ID,LOCATION_ID,PREFERENCE,LEAD_DAYS_SUPPLY,CYCLE_DAYS_SUPPLY,VENDOR_ID): smaller number= higher PREFERENCE

method_make(BOM_ID,PRODUCT_ID,LOCATION_ID,PREFERENCE): smaller number= higher PREFERENCE

product(PRODUCT_ID,DESCRIPTION)

productlocation(PRODUCT_ID,DESCRIPTION,LOCATION_ID,MAX_LOT_SIZE,PROD_AREA)

supply(SUPPLY_ID,DESCRIPTION,VENDOR_ID,LOCATION_ID,PRODUCT_ID,SUPPLY_DATE,QTY)

method_move(PRODUCT_ID,FROM_LOCATION_ID,TO_LOCATION_ID,TRANSIT_TIME,TRANSIT_TIME_UOM,PREFERENCE): smaller number= higher PREFERENCE
(a.k.a. transportation())

vendor(VENDOR_ID)

---------------------------------------
inventory graph (DAG)
---------------------------------------

Allocation consists of building/traversing/reasoning along the inventory graph (DAG), bottom-up from supplies to demands.

1. node=inventory(product, location, quantity, time)

2. edge by method_move: 
    edge from inventory(product, location_target, quantity, time_target) to inventory(product, location_source, quantity, time_source), if:

    method_move(product, location_source, location_target, TRANSIT_TIME) 
    and time_target - time_source == TRANSIT_TIME.
    and inventory(product, location_source, quantity, time_source) is available

3. edge by method_make: 
    edge from inventory(product, location, quantity, time_target) to inventory(product_i, location, quantity_i, time_i) for i in {requirements of product}, if:

    bom(BOM_ID, PARENT_ID=product, CHILD_ID=product_i, ALT_GROUP=alt_grp_i, RATE=rate_i) 
    and method_make(BOM_ID, PARENT_ID=product, LEAD_TIME)
    and all alt_grp_i are identical or null for i in {requirements of product}
    and time_target == max(time_i for i in {requirements of product}) + LEAD_TIME
    and inventory(product_i, location, quantity_i>=quantity/rate_i, time_i) for i in {requirements of product}, is available 

Note: for a given parent product, child products product_i having identical or null value in ALT_GROUP for i in {requirements of product}, form a requirement set. For example, if
    bom(PARENT_ID=product, CHILD_ID=product_a, ALT_GROUP=x) 
    bom(PARENT_ID=product, CHILD_ID=product_b, ALT_GROUP=x) 
    bom(PARENT_ID=product, CHILD_ID=product_c, ALT_GROUP=y) 
    bom(PARENT_ID=product, CHILD_ID=product_d, ALT_GROUP=y) 
    bom(PARENT_ID=product, CHILD_ID=product_e, ALT_GROUP=null) 
    bom(PARENT_ID=product, CHILD_ID=product_f, ALT_GROUP=null)

    x = {product_a, product_b} is a requirement set, the logic relationship between product_a and product_b is AND;
    y = {product_c, product_d} is a requirement set, the logic relationship between product_c and product_d is AND;
    z = {product_e, product_f} is a requirement set, the logic relationship between product_e and product_f is AND.

    x, y and z are alternative groups, the logic relationship among them is OR.

---------------------------------------
pegging
---------------------------------------
Pegging is a sub-graph of the inventory graph. The same node and edge definitions apply; quantities and times are propagated rigorously.

**Nodes**  
Every node is an inventory node: inventory(product, location, quantity, time).  
In implementations, a node may be keyed by (product, location) with quantity (and optionally time/period) attached; time-sliced views may key by (product, location, period).

**Edges (quantity and time propagation)**  
Edges are exactly those of the inventory graph:

- **method_move**  
  Edge from inventory(product, location_target, quantity, time_target) to inventory(product, location_source, quantity, time_source) when:  
  method_move(product, location_source, location_target, TRANSIT_TIME),  
  time_target − time_source = TRANSIT_TIME,  
  and inventory(product, location_source, quantity, time_source) is available.  
  Quantity is conserved along the edge (same quantity at source and target).

- **method_make**  
  Edge from inventory(product, location, quantity, time_target) to inventory(product_i, location, quantity_i, time_i) for each requirement i in the chosen requirement set, when:  
  bom(..., PARENT_ID=product, CHILD_ID=product_i, RATE=rate_i),  
  method_make(..., PARENT_ID=product, LEAD_TIME),  
  time_target = max(time_i for i) + LEAD_TIME,  
  and inventory(product_i, location, quantity_i, time_i) is available with quantity_i ≥ quantity/rate_i.  
  Quantities propagate: consumption of component i is quantity/rate_i; output quantity is quantity.

**Sub-graph selection**  
- **Demand–supply pegging**: sub-graph induced by nodes and edges that lie on paths from customer demands (inventory nodes that satisfy demand) backward to raw material supplies, following edges in the reverse direction. Paths include both AND (within a requirement set) and OR (across alternative groups) as defined in the BOM.  
- **Supply–demand pegging**: sub-graph induced by nodes and edges on paths from a given supply node (inventory(product, location, quantity, time)) forward to demands, following edges in the forward direction. Paths reflect primarily OR (which alternative/recipe consumed the supply).

---------------------------------------
allocation algorithm (AND vs OR)
---------------------------------------
BOM structure: Requirement set (AND) = children with same ALT_GROUP (or all null); all required. Alternative groups (OR) = different ALT_GROUP values; exactly one alternative chosen per production. For each variant we choose ONE alternative and consume ALL components in that set (AND). Choice is made during allocation (e.g. first alternative that contains the component being processed in scarcity order).

Init basket_alloc with initial supplies
Init basket_prod with initial supplies
Init targets_to_be_allocated with all non-leaf variants
Init targets_to_be_produced with all non-leaf variants (exclude demand nodes)
Init allocation = []
Init reserved_by_material = {}

# Scoring formulas (current implementation)
Target score (a.k.a. target_weight):
Let unmet[(material, customer)] be remaining demand quantity and w_c be customer weight.

For a demanded target t:
  target_weight(t) = Σ_{customer c} unmet[(t, c)] * w_c

For a non-demanded target t:
  downstream_value(t) = Σ_{demanded target d reachable from t} target_weight(d)
  (Reachable means d is in t’s downstream closure via req_union edges.)
  target_weight(t) = downstream_value(t)

Critical component scarcity score:
Given a requirement set req for a variant in production (one chosen alternative):
  critical_component = argmin_{c in req} supply(c)
  critical_ratio = supply(critical_component) / max_{c in req, c != critical_component} supply(c)
  (If no “other” components exist, critical_ratio is null. If max_other <= 0, critical_ratio = 0.)

Global scarcity order (basket allocation / production):
For each component c in the basket, compute
  scarcity(c) = supply(c) / total_cap(c)
where total_cap(c) = target_weight(c).
Here target_weight(c) already includes transitive demand pressure from reachable demanded targets
(via downstream propagation), so we do not sum over requiring targets to avoid double counting.
Components are processed in ascending scarcity(c) order (smaller ratio = more scarce = earlier).

# Allocation pass (quantity-focused)
for component in scarcity_order(basket_alloc):
    # allocatable = (v, req_variant) where component is in req_variant; one alternative per variant
    allocatable_variants = [(v, req_variant) : v has alternative req_variant with component in req_variant]
    if allocatable_variants is empty:
        continue

    compute proportional split over allocatable_variants (by target_weight(v))
    for (variant, req_variant) in allocatable_variants:
        qty = split_qty(variant, component)
        if qty <= 0:
            continue

        # Allocate ALL required components of the chosen alternative (AND).
        for c in req_variant:
            consume min(qty, basket_alloc[c])
            reserve remainder in reserved_by_material[c]

        record allocation action/edges (include all req components)
        allocation.append(variant, req_variant, qty)

        mark variant allocated
        if all variants for target are allocated:
            add target qty to basket_alloc
            immediately apply reserved_by_material deductions for that target

    prune basket_alloc of resources unrelated to remaining allocation targets

# Production pass (timing-focused)
for component in scarcity_order(basket_prod):
    candidates = [v: component in req_v and v in targets_to_be_produced]  # req_v = chosen alternative's set
    for variant in candidates:
        if all req_v are allocated AND all req_v available in basket_prod:
            fire variant
            consume all req_v from basket_prod
            add target to basket_prod
            prune basket_prod of resources unrelated to remaining production targets

---------------------------------------
planning algorithm (from demand to supply)
---------------------------------------
Implementation: backend/app/services/planning_engine.py. Entry: run_planning(data); API: POST /cases/{id}/plan.

Data models (as implemented):
  work_order: product_id, location_id, quantity, start_time, end_time, method (make|move|purchase), location_source (if move), demand_id, prod_area (from productlocation)
  committed_demand: demand_id, customer_id, customer, product_id, location_id, quantity, request_time, commit_time, commit_reason (if soft fail)
  demand (input): demand_id, customer_id, customer, product_id, location_id, quantity, request_due_time, priority

get_methods(product_id, location_id, data):
  Return all methods that can fulfill (product, location): method_buy, method_make, method_move.
  Match on PRODUCT_ID and LOCATION_ID (or TO_LOCATION_ID for move). When location is VIRTUAL or empty,
  also match make/buy by product_id only so demand at VIRTUAL can be planned via production at physical locations.

get_preferred_method(methods):
  Implemented as preference-only (no recursive scoring): choose method with smallest preference number.
  Returns (chosen_method, explanation). Spec originally allowed score = earliest commit + most consumed + least buy + preference;
  full scoring was removed to avoid blow-up when many methods/levels exist.

Optimizations (trivial cases): When only one method exists, plan() uses it without calling get_preferred_method or elaborate. When only one variant exists, get_preferred_variants returns it without scoring. When equal split is used with no top_n (all feasible variants), variant scoring skips weighted score and sort—only feasibility is needed.

method_selection.multiple (optional): When true and multiple methods (make/move/buy) can fulfill a demand, demand is split equally across those methods (integer split when demand quantity is integer). Same policy as variant equal split. When false or omitted, one method is chosen (by preference or by elaborate score).

get_preferred_method_elaborate (optional, config.method_selection.elaborate = true):
  Default: elaborate is off; method choice uses get_preferred_method (preference-only). When elaborate is on,
  each method is scored by simulating one level of plan() (limited depth) and aggregating commit_time, inventory_consumed, purchase.
  Policy: uses the same score_weights as variant_selection (commit_time, inventory_consumed, purchase). So configuring variant
  weights via the copilot also configures elaborate method selection when it is enabled. Only applied at top level to avoid slow runs. Ignored when method_selection.multiple is true (equal split across methods).

get_preferred_variants(variants, inventory, data, req_dt, lead_days, planning_path, depth, demand_net_qty, multiple=None):
  Variants = list of (alt_group_key, child_materials) from _variants_for_make (BOM grouped by ALT_GROUP).
  - multiple is False: return one best variant. Score each variant by running plan() for its children on a copy of inventory;
    sort by (earliest max_commit_time, most inventory_consumed, least purchase_qty). Return [(chosen_child_materials, chosen_alt_key, demand_net_qty)], explanation.
  - multiple is None (default): return all feasible variants (planning did not fail), with demand_net_qty divided equally.
    When demand quantity is integer, per-variant quantities kept integer (base + remainder spread). Each item = (scaled_child_materials, alt_key, qty).
  Failed variants (any child with commit_reason not in {cycle_stopped, cycle_detected}) sort last.
  Returns (list of (child_materials, alt_key, quantity), explanation).

get_preferred_variant(...):
  Wrapper: get_preferred_variants(..., multiple=False); returns (child_materials, chosen_alt_key, explanation).

ALT_GROUP (BOM) clarification:
  - Group BOM rows by ALT_GROUP (same BOM_ID, PARENT_ID). Null/empty ALT_GROUP → one group (e.g. "__null__").
  - Within each group: all children required (AND). Each group = one requirement set (variant).
  - Across groups: alternatives (OR). _variants_for_make builds list of (alt_group_key, child_materials).
  - Example: x = {product_a, product_b}, y = {product_c, product_d}, z = {product_e, product_f} (null) → choose one of x, y, z.

plan(demand, inventory, data, request_time_dt, depth, planning_path):
  Returns (committed_demands_list, work_orders_list, pegging_tree_node).
  - Fulfill from inventory (FIFO); demand_fulfilled gets commit_time from supply or request_time. demand_net = quantity - taken.
  - methods = get_methods(...). m, explanation = get_preferred_method(methods). Production location from method (or to_location_id for move).
  - Child materials: make → get_preferred_variants(..., multiple=None), flatten child_materials; move → one child at from_location; purchase → none.
  - Recursively plan each child; if any child fails (commit_reason not in {cycle_stopped, cycle_detected}), return with commit_reason "child_failed:...".
  - start_time = max(request_time - lead_days, latest child commit_times); batch by MAX_LOT_SIZE from productlocation; emit work_orders with prod_area.
  - Cycle: (product_id, location_id) in planning_path → return commit_reason "cycle_stopped" (benign). Depth <= 0 → "depth_limit". All set commit_time.
  - Pegging tree: root type=demand; children = supply nodes + work_order nodes (with method_choice_explanation, variant_choice_explanation).

run_planning(data):
  Inventory = supply buckets. Demands sorted by priority, demand_id. For each d: solved, wos, pegging = plan(d, ...); extend committed_demands and work_orders; append { demand_id, tree } to planning_pegging. Return { committed_demands, work_orders, planning_pegging }.

---------------------------------------
"critical path" 
---------------------------------------
lets revisit "critical component" or more precisely "critical path" logic. here is the mental model: the critical path is the one with the least sum of supplies. 
For a demand-to-supply pegging, there is one critical path starting from the demand ending at a leaf, of which the sum of the supplies is the minimum among all paths starting from the demand ending at a leaf.
For a supply-to-demand pegging, there is one critical path per desination demand starting from the raw material supply ending at a demand, of which the sum of the supplies is the minimum among all paths starting from the raw material supply ending at that demand.

Presentation/denotation: use the same color to denotate actions along a critical path. 

For the critical path on a demand-to-supply pegging, use the same color bar to denotate the produced material of each action, and the critical component of the last action. 
For supply-to-demand pegging, choose different colors for different demands. For a particular critical path, use the same color bar to denotate the produced material of each action, and the raw material node. Shared edges and nodes get multiple color bars each representing a served demand.

Lets simply coloring pattern for supply-to-demand pegging: choose different colors for different demands, multiple color bars for the raw material each representing a served demand. NO cloring for intermediary nodes. Tooltip for the raw material node showing demand list.

---------------------------------------
UI views
---------------------------------------
UI should support the following views:
- Supply view: initial/residual quantities
- Allocation view: how a supply (raw/intermediary materials) to allocated to demands (intermediary/FG demand materials)
- Demand view (suggested revised demands): fulfillment by demand, with customer and rollup by customer
- Planning (separate section): Run plan → Committed demands table (customer, commit_time, commit_reason, pegging) and Work orders table (prod_area, method, pegging). Each table in a tab with sticky headers and rollup (by customer, by PROD_AREA). Planning pegging slide-in shows tree (demand → work orders → supply/purchase) with method/variant explanations. **Planning copilot**: slide-in chat panel to configure `get_preferred_variants()`; users express requirements in natural language; the system uses an LLM (when OPENAI_API_KEY is set) to parse intents and may ask follow-up questions for clarification; fallback rule-based parsing when LLM is unavailable; config is sent in POST body when running plan.

Each view should be equipped with sort/filter.