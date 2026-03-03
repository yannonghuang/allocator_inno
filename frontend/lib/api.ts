const API = typeof window !== 'undefined' ? '/api' : 'http://localhost:8000';

export type Case = { id: number; name: string; created_at: string; demand_count?: number; supply_count?: number; run_count?: number };
export type AllocationRun = { id: number; case_id: number; created_at: string; status: string; config?: Record<string, unknown> };
export type AllocationAction = { id: number; run_id: number; variant_key: string; req_component_ids: string[]; qty: number; demand_id?: string; target_product_id?: string; target_location_id?: string };
export type FeasibleDemand = { demand_id: string; customer_id?: string | null; customer?: string | null; product_id: string; requested_qty: number; allocated_qty: number; fulfillment_rate?: number | null; status: string; suggested_revision?: string; request_due_time?: string | null; revised_time?: string | null };
export type SupplyViewRow = { id?: number; component_key: string; supply_id: string; supply_date?: string | null; product_id: string; location_id: string; initial_qty: number; consumed_qty: number; residual_qty: number; utilization_rate?: number | null };
export type AllocationViewCandidate = {
  to_inventory_id: string;
  to_inventory_display: string;
  qty: number;
  edge_type: string;
  to_variant_key: string;
};

export type BasketItem = { key: string; display: string; qty: number };

export type AllocationViewRow = {
  row_type: 'component' | 'demand';
  step?: number;
  scarcity_rank?: number;
  edge_type: string;
  from_inventory_id: string | null;
  from_inventory_display: string;
  critical_component_key: string | null;
  to_inventory_id: string | null;
  to_inventory_display: string | null;
  candidates: AllocationViewCandidate[];
  total_qty: number;
  split_explanation: string | null;
  basket_after?: BasketItem[];
  output_period?: number | null;
  output_date?: string | null;
  qty?: number;
  demand_ids: string[];
  supply_id?: string;
  to_variant_key?: string;
  from_components?: string[];
  critical_component_index?: number | null;
  to_variant_key_display?: string;
  to_product_id?: string;
  to_location_id?: string;
  supply_component_key?: string;
  supply_product_id?: string;
  supply_location_id?: string;
  target_variant_key?: string;
  target_product_id?: string;
  target_location_id?: string;
  basket_step_index?: number;
  /** Step index after this row (sometimes provided by backend). */
  after_step?: number | null;
};
export type ManualOverride = { id: number; case_id: number; entity_type: string; entity_key: string; payload: Record<string, unknown> };

export async function listCases(): Promise<Case[]> {
  const r = await fetch(`${API}/cases`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getCase(id: number): Promise<Case> {
  const r = await fetch(`${API}/cases/${id}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function createCase(name: string): Promise<Case> {
  const r = await fetch(`${API}/cases`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function updateCase(id: number, name: string): Promise<Case> {
  const r = await fetch(`${API}/cases/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function deleteCase(id: number): Promise<void> {
  const r = await fetch(`${API}/cases/${id}`, { method: 'DELETE' });
  if (!r.ok) throw new Error(await r.text());
}

export async function importCsv(caseId: number, folderPath?: string): Promise<{ status: string }> {
  const q = folderPath ? `?folder_path=${encodeURIComponent(folderPath)}` : '';
  const r = await fetch(`${API}/cases/${caseId}/import-csv${q}`, { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** Allocate starts a background job; request returns quickly with 202 and run_id. */
const ALLOCATE_TIMEOUT_MS = 3_600_000; //30_000;

export async function runAllocate(caseId: number): Promise<AllocationRun> {
  const r = await fetchWithTimeout(`${API}/cases/${caseId}/allocate`, { method: 'POST' }, ALLOCATE_TIMEOUT_MS);
  if (r.status !== 202 && !r.ok) throw new Error(await r.text());
  return r.json();
}

const POLL_INTERVAL_MS = 2000;
const POLL_MAX_WAIT_MS = 3_600_000 * 10; //1_200_000; // 20 min for large cases

export type AllocationProgress = {
  steps: number;
  max_steps: number;
  basket_total_qty: number;
  basket_keys: number;
  initial_basket_total_qty: number;
  initial_basket_keys: number;
};

/** Poll run until status is not 'running'. Optionally report progress on each poll. Returns final run. Throws on timeout. */
export async function pollRunUntilComplete(
  caseId: number,
  runId: number,
  opts?: {
    intervalMs?: number;
    maxWaitMs?: number;
    onProgress?: (progress: AllocationProgress) => void;
  }
): Promise<{ id: number; status: string; config?: Record<string, unknown> }> {
  const intervalMs = opts?.intervalMs ?? POLL_INTERVAL_MS;
  const maxWaitMs = opts?.maxWaitMs ?? POLL_MAX_WAIT_MS;
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    const statusRes = await getRunStatus(caseId, runId);
    if (statusRes.status !== 'running') return statusRes;
    const progress = statusRes.config?.progress as AllocationProgress | undefined;
    if (progress && opts?.onProgress) opts.onProgress(progress);
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error('ALLOCATION_POLL_TIMEOUT');
}

export async function listRuns(caseId: number): Promise<AllocationRun[]> {
  const r = await fetch(`${API}/cases/${caseId}/runs`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getRun(caseId: number, runId: number): Promise<{ run: AllocationRun; actions: AllocationAction[]; feasible_demands: FeasibleDemand[] }> {
  const r = await fetch(`${API}/cases/${caseId}/runs/${runId}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** Lightweight status for polling (no actions). */
export async function getRunStatus(caseId: number, runId: number): Promise<{ id: number; status: string; config?: Record<string, unknown> }> {
  const r = await fetch(`${API}/cases/${caseId}/runs/${runId}/status`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

const FETCH_TIMEOUT_MS = 90_000;

function fetchWithTimeout(url: string, options?: RequestInit, timeoutMs = FETCH_TIMEOUT_MS): Promise<Response> {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), timeoutMs);
  return fetch(url, { ...options, signal: ctrl.signal }).finally(() => clearTimeout(id));
}

export async function getFeasibleDemands(caseId: number, runId: number): Promise<{ feasible_demands: FeasibleDemand[] }> {
  const r = await fetchWithTimeout(`${API}/cases/${caseId}/runs/${runId}/feasible-demands`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** Demand-to-supply planning: returns committed demands (with commit_time), work orders, and planning pegging trees. */
export type CommittedDemand = { demand_id?: string | null; customer_id?: string | null; customer?: string | null; product_id: string; location_id: string; quantity: number; request_time?: string | null; commit_time: string | null; commit_reason?: string | null };
export type WorkOrder = {
  product_id: string;
  location_id: string;
  quantity: number;
  start_time: string | null;
  end_time: string | null;
  method: string;
  location_source?: string | null;
  demand_id?: string | null;
  prod_area?: string | null;
  /** True if this WO's pegging (supplies that fulfill it) includes a real make (non-virtual BOM). */
  pegging_includes_real_make?: boolean;
  /** True if this WO's pegging includes a buy (purchase). */
  pegging_includes_buy?: boolean;
  /** True if this WO's pegging includes a real move (TRANSIT_TIME > 0). */
  pegging_includes_real_move?: boolean;
};

/** Planning pegging tree node: demand (root) -> work_order -> ... -> supply | purchase (leaves). */
export type PlanningPeggingNode = {
  type: 'demand' | 'work_order' | 'supply' | 'purchase';
  demand_id?: string | null;
  product_id?: string;
  location_id?: string;
  quantity?: number;
  request_time?: string | null;
  commit_time?: string | null;
  commit_reason?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  method?: string;
  location_source?: string | null;
  /** Why this method was chosen when multiple alternatives exist. */
  method_choice_explanation?: string | null;
  /** Why this BOM/variant was chosen when multiple ALT_GROUP variants exist (make only). */
  variant_choice_explanation?: string | null;
  /** How this node's children relate logically, when known. 'or' is used when single-component variants are alternatives. */
  children_relation?: 'and' | 'or';
  /** For make work orders: how many production lots were created and the max lot size used. */
  lot_count?: number | null;
  max_lot_size?: number | null;
  children: PlanningPeggingNode[];
};

export type PlanningPeggingEntry = { demand_id: string; tree: PlanningPeggingNode };

/** Plan KPI dashboard: delivery, inventory, procurement, manufacturing, logistics. */
export type PlanKpis = {
  delivery: {
    total_requested: number;
    total_committed: number;
    fill_rate_pct: number | null;
    demand_count: number;
    on_time_count: number;
    fulfilled_with_tree_count?: number;
    fulfilled_by_real_make_count?: number;
    fulfilled_by_inventory_only_count?: number;
  };
  inventory: {
    initial_total: number;
    consumed_total: number;
    consumption_rate: number | null;
  };
  procurement: { order_count: number; total_quantity: number };
  manufacturing: { order_count: number; total_quantity: number };
  logistics: { order_count: number; total_quantity: number };
};

/** Config for planning (e.g. get_preferred_variants). Sent in POST body to /plan. */
export type PlanningConfig = {
  variant_selection?: {
    multiple?: boolean | null;
    top_n?: number;
    score_weights?: { commit_time?: number; inventory_consumed?: number; purchase?: number };
  };
  /** When true, score methods by commit_time/inventory/purchase (slower; run is async with progress). */
  method_selection?: { elaborate?: boolean; multiple?: boolean };
};

export type PlanResult = { committed_demands: CommittedDemand[]; work_orders: WorkOrder[]; planning_pegging: PlanningPeggingEntry[] };

export async function runPlan(
  caseId: number,
  config?: PlanningConfig | null
): Promise<PlanResult> {
  const body = JSON.stringify({ config: config ?? undefined, async: false });
  const r = await fetchWithTimeout(`${API}/cases/${caseId}/plan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** BOM (PARENT_ID, CHILD_ID) pairs that are "real" (VIRTUAL <> 'Y' in bom.csv). */
export async function getBomRealPairs(caseId: number): Promise<{ pairs: [string, string][] }> {
  const r = await fetch(`${API}/cases/${caseId}/plan/products-with-real-bom`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** Move methods with TRANSIT_TIME > 0 (real moves). Each tuple: [product_id, from_location_id, to_location_id]. */
export async function getMovesWithTransit(caseId: number): Promise<{ moves: [string, string, string][] }> {
  const r = await fetch(`${API}/cases/${caseId}/plan/moves-with-transit`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** Fetch work-order pegging on demand: how this WO is fulfilled by its supplies (all levels). Requires a prior plan run. */
export async function getWorkOrderPegging(
  caseId: number,
  params: { demand_id: string; product_id: string; location_id: string; method: string }
): Promise<{ tree: PlanningPeggingNode }> {
  const sp = new URLSearchParams({
    demand_id: params.demand_id,
    product_id: params.product_id,
    location_id: params.location_id,
    method: params.method,
  });
  const r = await fetch(`${API}/cases/${caseId}/plan/work-order-pegging?${sp.toString()}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

/** Start async plan; returns job_id. Poll getPlanStatus(caseId, job_id) for progress and result. */
export async function runPlanAsync(caseId: number, config?: PlanningConfig | null): Promise<{ job_id: string }> {
  const r = await fetch(`${API}/cases/${caseId}/plan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config: config ?? undefined, async: true }),
  });
  if (r.status !== 202) {
    const text = await r.text();
    throw new Error(r.ok ? text : `Plan start failed: ${text}`);
  }
  const data = await r.json();
  if (!data?.job_id) throw new Error('No job_id in response');
  return { job_id: data.job_id };
}

export type PlanStatusResponse = {
  status: 'running' | 'completed' | 'failed';
  progress?: { current: number; total: number };
  result?: PlanResult;
  error?: string;
};

export async function getPlanStatus(caseId: number, jobId: string): Promise<PlanStatusResponse> {
  const r = await fetch(`${API}/cases/${caseId}/plan/status/${jobId}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type PlanningCopilotMessage = { role: 'user' | 'assistant'; text: string };

export type PlanningCopilotResponse = { reply: string; config_update: PlanningConfig | null };

export async function planningCopilot(
  caseId: number,
  message: string,
  currentConfig: PlanningConfig,
  history: PlanningCopilotMessage[]
): Promise<PlanningCopilotResponse> {
  const r = await fetch(`${API}/cases/${caseId}/planning-copilot`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, current_config: currentConfig, history }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function listOverrides(caseId: number): Promise<ManualOverride[]> {
  const r = await fetch(`${API}/cases/${caseId}/overrides`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function addOverride(caseId: number, entityType: string, entityKey: string, payload: Record<string, unknown>): Promise<ManualOverride> {
  const r = await fetch(`${API}/cases/${caseId}/overrides`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ entity_type: entityType, entity_key: entityKey, payload }) });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function deleteOverride(caseId: number, overrideId: number): Promise<void> {
  const r = await fetch(`${API}/cases/${caseId}/overrides/${overrideId}`, { method: 'DELETE' });
  if (!r.ok) throw new Error(await r.text());
}

export async function getExplanations(caseId: number, runId: number, supplyId: string): Promise<{ supply_id: string; split: { demand_id: string; quantity: number; reason: string }[] }> {
  const r = await fetch(`${API}/cases/${caseId}/runs/${runId}/explanations?supply_id=${encodeURIComponent(supplyId)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type AllocationExplanation = {
  component_key: string;
  component_display: string;
  weight_formula?: string;
  candidate_targets: { variant_key: string; variant_display: string; edge_type: string; target_weight?: number; weight_calculation?: string }[];
  total_candidate_weight?: number;
  steps: { to_variant_key: string; to_variant_display: string; qty: number; edge_type: string; available_before: number }[];
  total_supply_for_component: number;
  available_during_run?: number;
  reason: string;
  supply_note?: string;
};

export async function getAllocationExplanation(
  caseId: number,
  runId: number,
  componentKey: string,
  toVariantKey?: string
): Promise<AllocationExplanation> {
  const params = new URLSearchParams({ component_key: componentKey });
  if (toVariantKey) params.set('to_variant_key', toVariantKey);
  const r = await fetch(`${API}/cases/${caseId}/runs/${runId}/allocation-explanation?${params}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

const PEGGING_PARSE_TIMEOUT_MS = 15000;

export async function getPegging(
  caseId: number,
  runId: number,
  direction: 'demand-to-supply' | 'supply-to-demand',
  demandId?: string,
  supplyId?: string,
  verify = false
): Promise<{ direction: string; nodes: { id: string; label: string; type: string }[]; edges: { from: string; to: string; qty: number }[]; critical_path?: unknown; critical_paths_by_demand?: unknown; _verify?: { node_id: string; node_qty: number; demand_edge_sum: number; ok: boolean }[] }> {
  const params = new URLSearchParams({ direction });
  if (demandId) params.set('demand_id', demandId);
  if (supplyId) params.set('supply_id', supplyId);
  if (verify) params.set('verify', '1');
  const url = `${API}/cases/${caseId}/runs/${runId}/pegging?${params}`;
  const r = await fetchWithTimeout(url);
  if (!r.ok) throw new Error(await r.text());
  const json = await Promise.race([
    r.json(),
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error('Pegging response timeout (body/parse)')), PEGGING_PARSE_TIMEOUT_MS)
    ),
  ]);
  return json as { direction: string; nodes: { id: string; label: string; type: string }[]; edges: { from: string; to: string; qty: number }[]; critical_path?: unknown; critical_paths_by_demand?: unknown; _verify?: { node_id: string; node_qty: number; demand_edge_sum: number; ok: boolean }[] };
}

export async function getSupplyView(
  caseId: number,
  runId: number,
  opts?: { debug_component_key?: string },
): Promise<{ supply_view: SupplyViewRow[]; _debug?: Record<string, unknown> }> {
  const params = new URLSearchParams();
  if (opts?.debug_component_key) params.set('debug_component_key', opts.debug_component_key);
  const qs = params.toString();
  const r = await fetchWithTimeout(`${API}/cases/${caseId}/runs/${runId}/supply-view${qs ? `?${qs}` : ''}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type RawMaterialInvolvementEntry = {
  role: 'critical' | 'companion';
  step: number;
  comp_key: string;
  pattern: string;
  /** True if this comp_key (product|location) has at least one Supply row; false if allocation came from move/production. */
  in_supply_view?: boolean;
  avail?: number;
  allocated?: boolean;
  breakdown?: { variant_key: string; output_qty: number }[];
  considered_but_skipped?: boolean;
  variant_key?: string;
  reason?: string;
  output_cap?: number;
  critical_component?: string;
  taken?: number;
  need_actual?: number;
  output_qty_actual?: number;
};

export type RawMaterialUsageReport = {
  run_id: number;
  supply_patterns_used: string[];
  summary: Record<string, { total_consumed_qty: number; node_count: number }>;
  details: { pattern: string; product_id: string; location_id: string; node: string; consumed_qty: number }[];
  involvement_trace?: RawMaterialInvolvementEntry[];
};

export async function getRawMaterialUsage(
  caseId: number,
  runId: number,
): Promise<RawMaterialUsageReport> {
  const r = await fetchWithTimeout(`${API}/cases/${caseId}/runs/${runId}/raw-material-usage`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getAllocationView(
  caseId: number,
  runId: number,
  opts?: { max_actions?: number; from_step?: number; to_step?: number; skip_basket?: boolean },
): Promise<{
  allocation_view: AllocationViewRow[];
  truncated?: boolean;
  total_actions?: number;
  limit?: number;
  total_steps?: number;
  from_step?: number | null;
  to_step?: number | null;
  basket_initial?: { key: string; display: string; qty: number }[];
  basket_deltas?: { purged: { key: string; display: string; qty: number }[]; added: { key: string; display: string; qty: number }[] }[];
  basket_final?: { key: string; display: string; qty: number }[] | null;
  basket_prunes?: { after_step: number; comp_keys: string[] }[];
}> {
  const params = new URLSearchParams();
  if (opts?.max_actions != null) params.set('max_actions', String(opts.max_actions));
  if (opts?.from_step != null) params.set('from_step', String(opts.from_step));
  if (opts?.to_step != null) params.set('to_step', String(opts.to_step));
  if (opts?.skip_basket != null) params.set('skip_basket', String(opts.skip_basket));
  const qs = params.toString();
  const url = `${API}/cases/${caseId}/runs/${runId}/allocation-view${qs ? `?${qs}` : ''}`;
  const r = await fetchWithTimeout(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getAllocationViewBasket(
  caseId: number,
  runId: number,
  opts?: { max_actions?: number },
): Promise<{
  basket_initial: { key: string; display: string; qty: number }[];
  basket_deltas: { purged: { key: string; display: string; qty: number }[]; added: { key: string; display: string; qty: number }[] }[];
  basket_final: { key: string; display: string; qty: number }[] | null;
  basket_prunes: { after_step: number; comp_keys: string[] }[];
}> {
  const params = new URLSearchParams();
  if (opts?.max_actions != null) params.set('max_actions', String(opts.max_actions));
  const qs = params.toString();
  const url = `${API}/cases/${caseId}/runs/${runId}/allocation-view-basket${qs ? `?${qs}` : ''}`;
  const r = await fetchWithTimeout(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type AllocationActionRow = {
  id: number;
  run_id: number;
  variant_key: string;
  req_component_ids: string[];
  qty: number;
  demand_id?: string;
  target_product_id?: string;
  target_location_id?: string;
  output_period?: number;
  edge_type?: string;
  scarcity_rank?: number;
};

export async function getAllocationActions(
  caseId: number,
  runId: number,
  opts?: { offset?: number; limit?: number },
): Promise<{ actions: AllocationActionRow[]; total_count: number; offset: number; limit: number }> {
  const params = new URLSearchParams();
  if (opts?.offset != null) params.set('offset', String(opts.offset));
  if (opts?.limit != null) params.set('limit', String(opts.limit));
  const qs = params.toString();
  const r = await fetchWithTimeout(`${API}/cases/${caseId}/runs/${runId}/allocation-actions${qs ? `?${qs}` : ''}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
