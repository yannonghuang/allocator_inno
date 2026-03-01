'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import {
  getCase,
  getFeasibleDemands,
  listRuns,
  runAllocate,
  pollRunUntilComplete,
  listOverrides,
  addOverride,
  deleteOverride,
  getSupplyView,
  getRawMaterialUsage,
  type RawMaterialUsageReport,
  getAllocationView,
  getAllocationViewBasket,
  getAllocationActions,
  runPlan,
  planningCopilot,
  type PlanningConfig,
  type PlanningCopilotMessage,
  type CommittedDemand,
  type WorkOrder,
  type PlanningPeggingNode,
  type PlanningPeggingEntry,
  type AllocationActionRow,
  getAllocationExplanation,
  getPegging,
  type Case as CaseType,
  type AllocationRun as RunType,
  type FeasibleDemand,
  type ManualOverride as OverrideType,
  type SupplyViewRow,
  type AllocationViewRow,
  type AllocationExplanation,
  type AllocationProgress,
} from '@/lib/api';
import { SortFilterTable } from '@/app/components/SortFilterTable';
import { PeggingTree, pathKeyFromPath, type PeggingGraph } from '@/app/components/PeggingTree';

export default function CaseDetail() {
  const params = useParams();
  const id = Number(params.id);
  const [c, setC] = useState<CaseType | null>(null);
  const [runs, setRuns] = useState<RunType[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [runDetail, setRunDetail] = useState<{ run: RunType; actions: unknown[]; feasible_demands: FeasibleDemand[] } | null>(null);
  const [feasibleDemands, setFeasibleDemands] = useState<FeasibleDemand[] | null>(null);
  const [demandsLoadError, setDemandsLoadError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<OverrideType[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [allocating, setAllocating] = useState(false);
  const [allocationProgress, setAllocationProgress] = useState<AllocationProgress | null>(null);
  const [etaRemainingSeconds, setEtaRemainingSeconds] = useState<number | null>(null);
  const allocationProgressRef = useRef<{ steps: number; timestamp: number } | null>(null);
  const [overrideForm, setOverrideForm] = useState({ entity_type: 'supply', entity_key: '', payload: '{}' });
  const [supplyView, setSupplyView] = useState<SupplyViewRow[]>([]);
  const [supplyViewLoading, setSupplyViewLoading] = useState(false);
  const [allocationView, setAllocationView] = useState<AllocationViewRow[]>([]);
  const [allocationViewTruncated, setAllocationViewTruncated] = useState<{ total_actions: number; limit: number; total_steps: number } | null>(null);
  const ALLOCATION_VIEW_PAGE_SIZE = 1000;
  const [allocationViewLoading, setAllocationViewLoading] = useState(false);
  const [allocationViewError, setAllocationViewError] = useState<string | null>(null);
  const [allocationActions, setAllocationActions] = useState<AllocationActionRow[]>([]);
  const [allocationActionsTotal, setAllocationActionsTotal] = useState(0);
  const [allocationActionsLoading, setAllocationActionsLoading] = useState(false);
  const [allocationActionsOffset, setAllocationActionsOffset] = useState(0);
  const [basketInitial, setBasketInitial] = useState<{ key: string; display: string; qty: number }[]>([]);
  const [basketDeltas, setBasketDeltas] = useState<{ purged: { key: string; display: string; qty: number }[]; added: { key: string; display: string; qty: number }[] }[]>([]);
  const [basketFinal, setBasketFinal] = useState<{ key: string; display: string; qty: number }[] | null>(null);
  const [basketPrunes, setBasketPrunes] = useState<{ after_step: number; comp_keys: string[] }[]>([]);
  const [basketShowingFinal, setBasketShowingFinal] = useState(false);
  const [basketLoading, setBasketLoading] = useState(false);
  const allocationViewRunIdRef = useRef<number | null>(null);
  const allocationViewFetchingRef = useRef<boolean>(false);
  const activeViewRef = useRef<'supply' | 'allocation' | 'suggested' | 'raw-material'>('supply');
  const selectedRunIdRef = useRef<number | null>(null);
  const [caseSection, setCaseSection] = useState<'allocation' | 'planning'>('allocation');
  const [rawMaterialReport, setRawMaterialReport] = useState<RawMaterialUsageReport | null>(null);
  const [rawMaterialReportLoading, setRawMaterialReportLoading] = useState(false);
  const [planResult, setPlanResult] = useState<{ committed_demands: CommittedDemand[]; work_orders: WorkOrder[]; planning_pegging: PlanningPeggingEntry[] } | null>(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [planPeggingOpen, setPlanPeggingOpen] = useState(false);
  const [planPeggingContext, setPlanPeggingContext] = useState<{ type: 'demand'; row: CommittedDemand } | { type: 'work_order'; row: WorkOrder } | null>(null);
  const [planPeggingExpanded, setPlanPeggingExpanded] = useState<Set<string>>(new Set(['0']));
  const [planExplanationExpanded, setPlanExplanationExpanded] = useState<Set<string>>(new Set());
  const [planPeggingPanelWidth, setPlanPeggingPanelWidth] = useState(420);
  const planPeggingResizeRef = useRef<{ startX: number; startW: number } | null>(null);
  const [planPeggingResizing, setPlanPeggingResizing] = useState(false);
  const [planResultTab, setPlanResultTab] = useState<'demands' | 'work_orders'>('demands');
  const [planningConfig, setPlanningConfig] = useState<PlanningConfig>({});
  const [copilotOpen, setCopilotOpen] = useState(false);
  const [copilotMessages, setCopilotMessages] = useState<PlanningCopilotMessage[]>([]);
  const [copilotInput, setCopilotInput] = useState('');
  const [copilotLoading, setCopilotLoading] = useState(false);
  const [copilotPanelWidth, setCopilotPanelWidth] = useState(440);
  const copilotResizeRef = useRef<{ startX: number; startW: number } | null>(null);
  const [copilotResizing, setCopilotResizing] = useState(false);
  const copilotMessagesEndRef = useRef<HTMLDivElement | null>(null);

  /** Rule-based intent: map user message to config updates and a reply for get_preferred_variants() behavior. */
  function parseCopilotIntent(message: string, currentConfig: PlanningConfig): { reply: string; configUpdate?: PlanningConfig } {
    const t = message.trim().toLowerCase();
    const vs = currentConfig.variant_selection ?? {};
    const multi = vs.multiple;

    if (!t) return { reply: 'You can ask to use a single best variant, or to split demand across all feasible variants. Say "show config" to see current settings.' };

    if (/show|current|what('s| is)? (my )?config|settings|config/.test(t)) {
      const mode = multi === false ? 'single best variant' : 'all feasible variants (equal split)';
      return { reply: `Current variant selection: **${mode}**. Change it by saying e.g. "use single variant" or "split across all variants".` };
    }

    if (/single|one variant|only one|best variant|use one/.test(t)) {
      return {
        reply: 'Set variant selection to **single best variant**. The planner will pick one best BOM/variant per demand (by score: earliest commit, most inventory consumed, least purchase). Re-run plan to apply.',
        configUpdate: { variant_selection: { ...vs, multiple: false } },
      };
    }

    if (/all variants|split|multiple variants|every variant|equal split|divide (across|among)/.test(t)) {
      return {
        reply: 'Set variant selection to **all feasible variants** with equal split. Demand will be divided among all feasible BOM/variants (integer quantities when demand is integer). Re-run plan to apply.',
        configUpdate: { variant_selection: { ...vs, multiple: true } },
      };
    }

    if (/reset|default|clear/.test(t)) {
      return {
        reply: 'Reset to default: **all feasible variants** (equal split). Re-run plan to apply.',
        configUpdate: { variant_selection: { multiple: true } },
      };
    }

    return {
      reply: 'I only handle variant selection for planning. Try: "use single variant", "split across all variants", or "show config".',
    };
  }

  useEffect(() => {
    copilotMessagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [copilotMessages]);

  const basketInitialKeys = new Set(basketInitial.map((b) => b.key));

  function computeBasketAfterStep(stepIndex: number): { key: string; display: string; qty: number; fromInitialSupply: boolean }[] {
    const map = new Map<string, { display: string; qty: number }>();
    for (const b of basketInitial) {
      map.set(b.key, { display: b.display, qty: b.qty });
    }
    for (let i = 0; i < basketDeltas.length && i <= stepIndex; i++) {
      const d = basketDeltas[i];
      for (const p of d.purged ?? []) {
        const cur = map.get(p.key);
        if (cur) {
          cur.qty -= p.qty;
          if (cur.qty <= 0) map.delete(p.key);
        }
      }
      for (const a of d.added ?? []) {
        const cur = map.get(a.key);
        if (cur) cur.qty += a.qty;
        else map.set(a.key, { display: a.display, qty: a.qty });
      }
    }
    // Apply engine prunes: remove components unrelated to remaining allocation targets
    const keysToDelete: string[] = [];
    for (const prune of basketPrunes) {
      if (prune.after_step > stepIndex) continue;
      for (const compKey of prune.comp_keys ?? []) {
        for (const key of map.keys()) {
          const comp = key.includes('|') ? key.replace(/\|[^|]*$/, '') : key;
          if (comp === compKey) keysToDelete.push(key);
        }
      }
    }
    for (const key of keysToDelete) map.delete(key);
    const items = Array.from(map.entries())
      .filter(([, v]) => v.qty > 0)
      .map(([key, v]) => ({ key, display: v.display, qty: v.qty, fromInitialSupply: basketInitialKeys.has(key) }));
    // Same scarcity as engine: sort by total qty per component (ascending), then key
    const compTotal = new Map<string, number>();
    for (const { key, qty } of items) {
      const comp = key.includes('|') ? key.replace(/\|[^|]*$/, '') : key;
      compTotal.set(comp, (compTotal.get(comp) ?? 0) + qty);
    }
    return items.sort((a, b) => {
      const compA = a.key.includes('|') ? a.key.replace(/\|[^|]*$/, '') : a.key;
      const compB = b.key.includes('|') ? b.key.replace(/\|[^|]*$/, '') : b.key;
      const totA = compTotal.get(compA) ?? 0;
      const totB = compTotal.get(compB) ?? 0;
      if (totA !== totB) return totA - totB;
      return a.key.localeCompare(b.key);
    });
  }
  const [activeView, setActiveView] = useState<'supply' | 'allocation' | 'suggested' | 'raw-material'>('supply');
  activeViewRef.current = activeView;
  selectedRunIdRef.current = selectedRunId;
  const [explanationOpen, setExplanationOpen] = useState(false);
  const [explanationLoading, setExplanationLoading] = useState(false);
  const [explanationData, setExplanationData] = useState<AllocationExplanation | null>(null);
  const [peggingOpen, setPeggingOpen] = useState(false);
  const [peggingLoading, setPeggingLoading] = useState(false);
  const [peggingTitle, setPeggingTitle] = useState('');
  const [peggingData, setPeggingData] = useState<{ direction: string; nodes: { id: string; label: string; type: string }[]; edges: { from: string; to: string; qty: number }[]; critical_path?: { path: string[] }; critical_paths_by_demand?: { component_key?: string; paths_by_demand: { demand_id: string; path: string[] }[] }; demand_id?: string; demand_root_id?: string; demand_allocated_qty?: number; demand_requested_qty?: number } | null>(null);
  const [peggingTreeReady, setPeggingTreeReady] = useState<boolean>(false);
  const [peggingExpanded, setPeggingExpanded] = useState<Set<string>>(new Set());
  const [peggingExpandingNodeId, setPeggingExpandingNodeId] = useState<string | null>(null);
  /** Node ids for which we have allowed rendering children (so first paint can show spinner before heavy subtree) */
  const [peggingChildrenAllowedFor, setPeggingChildrenAllowedFor] = useState<Set<string>>(new Set());
  /** For expanded nodes with many children: render only this many per frame so UI stays responsive */
  const [peggingPanelWidth, setPeggingPanelWidth] = useState(520);
  const peggingResizeRef = useRef<{ startX: number; startW: number } | null>(null);
  const [peggingResizing, setPeggingResizing] = useState(false);
  const [supplyFilterConsumedOnly, setSupplyFilterConsumedOnly] = useState(false);
  const [basketSlideInRow, setBasketSlideInRow] = useState<AllocationViewRow | null>(null);

  const loadCase = async () => {
    try {
      const data = await getCase(id);
      setC(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load case');
    }
  };

  const loadRuns = async (allowAutoSelect = true) => {
    try {
      const data = await listRuns(id);
      setRuns(data);
      if (allowAutoSelect && data.length > 0 && !selectedRunId) {
        const completed = data.find((r) => r.status !== 'running');
        if (completed) setSelectedRunId(completed.id);
      }
    } catch {
      setRuns([]);
    }
  };

  const loadRunDetail = (runId: number) => {
    setRunDetail(null);
    setFeasibleDemands(null);
    setDemandsLoadError(null);
    setSupplyView([]);
    // Only clear allocation view when switching to a different run; keep current view while refreshing same run
    if (allocationViewRunIdRef.current !== null && allocationViewRunIdRef.current !== runId) {
      setAllocationView([]);
      setAllocationViewTruncated(null);
      setAllocationViewError(null);
      setBasketInitial([]);
      setBasketDeltas([]);
      setBasketFinal(null);
      setBasketPrunes([]);
      setAllocationActions([]);
      setAllocationActionsTotal(0);
      allocationViewFetchingRef.current = false;
    } else {
      setAllocationViewError(null);
    }
    allocationViewRunIdRef.current = runId;
    setSupplyViewLoading(true);
    getSupplyView(id, runId)
      .then((s) => setSupplyView(s.supply_view))
      .catch(() => setSupplyView([]))
      .finally(() => setSupplyViewLoading(false));
    getFeasibleDemands(id, runId)
      .then((d) => {
        setFeasibleDemands(d.feasible_demands);
        setDemandsLoadError(null);
      })
      .catch((e) => {
        setFeasibleDemands([]);
        setDemandsLoadError(e instanceof Error ? e.message : 'Failed to load demands');
      });
  };

  const loadOverrides = async () => {
    try {
      const data = await listOverrides(id);
      setOverrides(data);
    } catch {
      setOverrides([]);
    }
  };

  useEffect(() => {
    setLoading(true);
    setError(null);
    const timeoutId = setTimeout(() => setLoading(false), 20000);
    Promise.all([loadCase(), loadRuns(), loadOverrides()]).finally(() => {
      clearTimeout(timeoutId);
      setLoading(false);
    });
  }, [id]);

  useEffect(() => {
    if (selectedRunId) loadRunDetail(selectedRunId);
    else {
      setRunDetail(null);
      setFeasibleDemands(null);
      setDemandsLoadError(null);
      setAllocationView([]);
      setAllocationViewTruncated(null);
      setAllocationViewError(null);
      setBasketInitial([]);
      setBasketDeltas([]);
      setBasketFinal(null);
      setBasketPrunes([]);
      setAllocationActions([]);
      setAllocationActionsTotal(0);
      allocationViewRunIdRef.current = null;
      allocationViewFetchingRef.current = false;
    }
  }, [selectedRunId, id]);

  // Lazy-load allocation view only when user opens the Allocation tab (scalable: no heavy request on run select)
  useEffect(() => {
    if (
      activeView !== 'allocation'
      || !selectedRunId
      || allocationViewRunIdRef.current !== selectedRunId
      || allocationView.length > 0
      || allocationViewLoading
      || allocationViewFetchingRef.current
    ) return;
    allocationViewFetchingRef.current = true;
    setAllocationViewLoading(true);
    // Load in one go: full view up to backend cap (5000 actions). Basket loaded on-demand when user opens basket slide-in.
    getAllocationView(id, selectedRunId, {
      max_actions: 5000,
      from_step: 1,
      to_step: 100000,
      skip_basket: true,
    })
      .then((a) => {
        if (allocationViewRunIdRef.current === selectedRunId) {
          setAllocationView(a.allocation_view);
          // Basket is loaded on-demand when user opens the basket slide-in (skip_basket: true)
          setBasketInitial(a.basket_initial ?? []);
          setBasketDeltas(a.basket_deltas ?? []);
          setBasketFinal(a.basket_final ?? null);
          setBasketPrunes(a.basket_prunes ?? []);
          const totalSteps = a.total_steps ?? a.allocation_view.length;
          setAllocationViewTruncated(
            totalSteps > 0
              ? { total_actions: a.total_actions ?? 0, limit: a.limit ?? 5000, total_steps: totalSteps }
              : null,
          );
          setAllocationViewError(null);
        }
      })
      .catch(() => {
        if (allocationViewRunIdRef.current === selectedRunId) {
          setAllocationViewError('Full allocation view unavailable for this run (timeout or too large).');
        }
      })
      .finally(() => {
        allocationViewFetchingRef.current = false;
        setAllocationViewLoading(false);
      });
  }, [activeView, selectedRunId, id, allocationView.length, allocationViewLoading]);

  // Load basket on-demand when user opens the basket slide-in and we don't have basket data yet
  useEffect(() => {
    const slideInOpen = basketSlideInRow != null || basketShowingFinal;
    if (!slideInOpen || basketDeltas.length > 0 || !id || !selectedRunId || basketLoading) return;
    setBasketLoading(true);
    getAllocationViewBasket(id, selectedRunId, { max_actions: 5000 })
      .then((a) => {
        if (allocationViewRunIdRef.current === selectedRunId) {
          setBasketInitial(a.basket_initial ?? []);
          setBasketDeltas(a.basket_deltas ?? []);
          setBasketFinal(a.basket_final ?? null);
          setBasketPrunes(a.basket_prunes ?? []);
        }
      })
      .catch(() => {})
      .finally(() => setBasketLoading(false));
  }, [basketSlideInRow, basketShowingFinal, basketDeltas.length, id, selectedRunId, basketLoading]);

  // When allocation view fails, auto-load lightweight raw steps so user still sees data
  useEffect(() => {
    if (
      !allocationViewError
      || activeView !== 'allocation'
      || !selectedRunId
      || allocationActions.length > 0
      || allocationActionsLoading
    ) return;
    setAllocationActionsLoading(true);
    getAllocationActions(id, selectedRunId, { offset: 0, limit: 500 })
      .then((d) => {
        if (allocationViewRunIdRef.current === selectedRunId) {
          setAllocationActions(d.actions);
          setAllocationActionsTotal(d.total_count);
          setAllocationActionsOffset(0);
        }
      })
      .catch(() => {})
      .finally(() => setAllocationActionsLoading(false));
  }, [allocationViewError, activeView, selectedRunId, id, allocationActions.length, allocationActionsLoading]);

  // Fetch raw material usage report when Raw material usage view is active
  useEffect(() => {
    if (activeView !== 'raw-material' || !selectedRunId) {
      return;
    }
    setRawMaterialReportLoading(true);
    setRawMaterialReport(null);
    getRawMaterialUsage(id, selectedRunId)
      .then(setRawMaterialReport)
      .catch(() => setRawMaterialReport(null))
      .finally(() => setRawMaterialReportLoading(false));
  }, [activeView, selectedRunId, id]);

  useEffect(() => {
    if (!peggingResizing) return;
    const onMove = (e: MouseEvent) => {
      const r = peggingResizeRef.current;
      if (!r) return;
      const delta = r.startX - e.clientX;
      setPeggingPanelWidth(Math.min(window.innerWidth * 0.9, Math.max(320, r.startW + delta)));
    };
    const onUp = () => {
      peggingResizeRef.current = null;
      setPeggingResizing(false);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [peggingResizing]);

  useEffect(() => {
    if (!planPeggingResizing) return;
    const onMove = (e: MouseEvent) => {
      const r = planPeggingResizeRef.current;
      if (!r) return;
      const delta = r.startX - e.clientX;
      setPlanPeggingPanelWidth(Math.min(window.innerWidth * 0.9, Math.max(320, r.startW + delta)));
    };
    const onUp = () => {
      planPeggingResizeRef.current = null;
      setPlanPeggingResizing(false);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [planPeggingResizing]);

  useEffect(() => {
    if (!copilotResizing) return;
    const onMove = (e: MouseEvent) => {
      const r = copilotResizeRef.current;
      if (!r) return;
      const delta = r.startX - e.clientX;
      setCopilotPanelWidth(Math.min(window.innerWidth * 0.9, Math.max(320, r.startW + delta)));
    };
    const onUp = () => {
      copilotResizeRef.current = null;
      setCopilotResizing(false);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [copilotResizing]);

  // Defer tree building so first paint shows counts and "Building tree…" instead of blocking on huge graph
  useEffect(() => {
    if (!peggingData) {
      setPeggingTreeReady(false);
      return;
    }
    const nodes = Array.isArray(peggingData.nodes) ? peggingData.nodes : [];
    const edges = Array.isArray(peggingData.edges) ? peggingData.edges : [];
    const isSupplyToDemand = peggingData.direction === 'supply-to-demand';
    const rootId = isSupplyToDemand
      ? (peggingData.critical_paths_by_demand?.component_key ?? peggingData.critical_paths_by_demand?.paths_by_demand?.[0]?.path?.[0] ?? edges[0]?.from)
      : (peggingData.critical_path?.path?.[0] ?? edges[0]?.to);
    const rootPathKey = rootId ? pathKeyFromPath([rootId]) : '';
    const t = setTimeout(() => {
      setPeggingTreeReady(true);
      setPeggingExpanded(rootPathKey ? new Set([rootPathKey]) : new Set());
      setPeggingChildrenAllowedFor(rootPathKey ? new Set([rootPathKey]) : new Set());
    }, 0);
    return () => clearTimeout(t);
  }, [peggingData]);

  // Pegging data comes from API (getPegging): nodes and edges (each edge has from, to, qty).
  // For "allocated" on demand children we use edge.qty (inv→demand = share from that parent node).
  const peggingGraph = useMemo(() => {
    if (!peggingData) return null;
    const nodes = Array.isArray(peggingData.nodes) ? peggingData.nodes : [];
    const edges = Array.isArray(peggingData.edges) ? peggingData.edges : [];
    const nodeById = Object.fromEntries(nodes.map((n) => [n.id, n]));
    const pathSet = new Set<string>([
      ...(peggingData.critical_path?.path ?? []),
      ...(peggingData.critical_paths_by_demand?.paths_by_demand?.flatMap((p) => p.path) ?? []),
    ]);
    const outEdges: Record<string, { to: string; qty: number }[]> = {};
    const inEdges: Record<string, { from: string; qty: number }[]> = {};
    edges.forEach((e) => {
      if (!outEdges[e.from]) outEdges[e.from] = [];
      outEdges[e.from].push({ to: e.to, qty: e.qty });
      if (!inEdges[e.to]) inEdges[e.to] = [];
      inEdges[e.to].push({ from: e.from, qty: e.qty });
    });
    const isSupplyToDemand = peggingData.direction === 'supply-to-demand';
    const demandRootId = (peggingData as { demand_root_id?: string }).demand_root_id ?? ((peggingData as { demand_id?: string }).demand_id != null ? `demand|${(peggingData as { demand_id: string }).demand_id}` : undefined);
    const rootId = isSupplyToDemand
      ? (peggingData.critical_paths_by_demand?.component_key ?? peggingData.critical_paths_by_demand?.paths_by_demand?.[0]?.path?.[0] ?? edges[0]?.from)
      : (demandRootId ?? peggingData.critical_path?.path?.[0] ?? edges[0]?.to);
    // Traverse by edge structure only (aligned with backend: from=component, to=variant).
    // supply-to-demand: children = outEdges (toward demand). demand-to-supply: root = demand node so all contributors (inv→demand edges) appear as children.
    function getChildren(nodeId: string): { nextId: string; qty: number }[] {
      if (isSupplyToDemand) {
        return (outEdges[nodeId] ?? []).map((item) => ({ nextId: item.to, qty: item.qty }));
      }
      return (inEdges[nodeId] ?? []).map((item) => ({ nextId: item.from, qty: item.qty }));
    }
    return { nodeById, pathSet, rootId, getChildren, nodes, edges };
  }, [peggingData]);

  const handleAllocate = async () => {
    setAllocating(true);
    setError(null);
    setAllocationProgress(null);
    setEtaRemainingSeconds(null);
    allocationProgressRef.current = null;
    try {
      const run = await runAllocate(id);
      setSelectedRunId(run.id);
      if (run.status === 'running') {
        const finalRun = await pollRunUntilComplete(id, run.id, {
          onProgress: (p) => {
            setAllocationProgress(p);
            const now = Date.now();
            const prev = allocationProgressRef.current;
            if (prev != null && now > prev.timestamp) {
              const elapsedSec = (now - prev.timestamp) / 1000;
              const stepDelta = p.steps - prev.steps;
              if (elapsedSec > 0 && stepDelta >= 0) {
                const rate = stepDelta / elapsedSec;
                if (rate > 0.1) {
                  const remaining = p.max_steps - p.steps;
                  if (remaining > 0) setEtaRemainingSeconds(remaining / rate);
                }
              }
            }
            allocationProgressRef.current = { steps: p.steps, timestamp: now };
            // Load once: no allocation-view refresh during run; only refresh run list/detail for progress
            if (p.steps > 0 && p.steps % 1000 === 0) {
              loadRuns(false);
              loadRunDetail(run.id);
            }
          },
        });
        if (finalRun.status === 'failed') {
          const msg = (finalRun.config as { error?: string } | null)?.error ?? 'Allocation failed';
          setError(msg);
        }
        await loadRuns(false);
        if (finalRun.status === 'success') {
          setSelectedRunId(run.id);
          loadRunDetail(run.id);
        } else {
          const data = await listRuns(id);
          if (data.length > 0) setSelectedRunId(data[0].id);
        }
      } else {
        await loadRuns();
        const data = await listRuns(id);
        if (data.length > 0) setSelectedRunId(data[0].id);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Allocation failed';
      if (msg === 'ALLOCATION_POLL_TIMEOUT') {
        setError('Stopped waiting for allocation (20 min). The run may still be in progress—refresh the page or check the Run list.');
        await loadRuns();
      } else {
        setError(msg);
      }
    } finally {
      setAllocating(false);
      setAllocationProgress(null);
      setEtaRemainingSeconds(null);
      allocationProgressRef.current = null;
    }
  };

  const handleAddOverride = async () => {
    try {
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(overrideForm.payload || '{}');
      } catch {
        setError('Invalid JSON payload');
        return;
      }
      await addOverride(id, overrideForm.entity_type, overrideForm.entity_key, payload);
      await loadOverrides();
      setOverrideForm({ ...overrideForm, entity_key: '', payload: '{}' });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to add override');
    }
  };

  const handleCriticalClick = async (componentKey: string, toVariantKey: string) => {
    if (!selectedRunId) return;
    setExplanationOpen(true);
    setExplanationLoading(true);
    setExplanationData(null);
    try {
      const data = await getAllocationExplanation(id, selectedRunId, componentKey, toVariantKey);
      setExplanationData(data);
    } catch {
      setExplanationData(null);
    } finally {
      setExplanationLoading(false);
    }
  };

  const handleSupplyPeggingClick = async (row: SupplyViewRow) => {
    if (!selectedRunId) return;
    setPeggingOpen(true);
    setPeggingLoading(true);
    setPeggingTitle(`Pegging: Supply ${row.component_key.replace('|', '@')}`);
    setPeggingData(null);
    setPeggingTreeReady(false);
    setPeggingExpanded(new Set());
    setPeggingExpandingNodeId(null);
    setPeggingChildrenAllowedFor(new Set());
    const timeoutId = setTimeout(() => {
      setPeggingLoading(false);
      setPeggingData(null);
    }, 15000);
    try {
      const supplyId = row.supply_id || row.component_key;
      const data = await getPegging(id, selectedRunId, 'supply-to-demand', undefined, supplyId, true);
      if (data._verify && typeof window !== 'undefined') {
        console.log('[Pegging verify] inv→demand edge sum vs node qty:', data._verify);
        const bad = data._verify.filter((v) => !v.ok);
        if (bad.length) console.warn('[Pegging verify] nodes with sum > qty:', bad);
      }
      setPeggingData({
        direction: data.direction ?? 'supply-to-demand',
        nodes: Array.isArray(data.nodes) ? data.nodes : [],
        edges: Array.isArray(data.edges) ? data.edges : [],
        critical_path: data.critical_path as { path: string[] } | undefined,
        critical_paths_by_demand: data.critical_paths_by_demand as { paths_by_demand: { demand_id: string; path: string[] }[] } | undefined,
      });
    } catch {
      setPeggingData(null);
    } finally {
      clearTimeout(timeoutId);
      setPeggingLoading(false);
    }
  };

  const handleDemandPeggingClick = async (row: FeasibleDemand) => {
    if (!selectedRunId) return;
    setPeggingOpen(true);
    setPeggingLoading(true);
    setPeggingTitle(`Pegging: Demand ${row.demand_id}`);
    setPeggingData(null);
    setPeggingTreeReady(false);
    setPeggingExpanded(new Set());
    setPeggingExpandingNodeId(null);
    setPeggingChildrenAllowedFor(new Set());
    const timeoutId = setTimeout(() => {
      setPeggingLoading(false);
      setPeggingData(null);
    }, 15000);
    try {
      const data = await getPegging(id, selectedRunId, 'demand-to-supply', String(row.demand_id), undefined);
      setPeggingData({
        direction: data.direction ?? 'demand-to-supply',
        nodes: Array.isArray(data.nodes) ? data.nodes : [],
        edges: Array.isArray(data.edges) ? data.edges : [],
        critical_path: data.critical_path as { path: string[] } | undefined,
        critical_paths_by_demand: data.critical_paths_by_demand as { paths_by_demand: { demand_id: string; path: string[] }[] } | undefined,
        demand_id: (data as { demand_id?: string }).demand_id,
        demand_root_id: (data as { demand_root_id?: string }).demand_root_id,
        demand_allocated_qty: (data as { demand_allocated_qty?: number }).demand_allocated_qty,
        demand_requested_qty: (data as { demand_requested_qty?: number }).demand_requested_qty,
      });
    } catch {
      setPeggingData(null);
    } finally {
      clearTimeout(timeoutId);
      setPeggingLoading(false);
    }
  };

  const handleDeleteOverride = async (overrideId: number) => {
    try {
      await deleteOverride(id, overrideId);
      await loadOverrides();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to delete');
    }
  };

  if (loading || !c) {
    return <div><Link href="/">← Cases</Link>{loading ? <p>Loading…</p> : <p>Not found</p>}</div>;
  }

  return (
    <div>
      <p><Link href="/">← Cases</Link></p>
      <h1>{c.name}</h1>
      {error && <p style={{ color: '#f87171' }}>{error}</p>}
      <nav style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem', borderBottom: '1px solid #e4e4e7', paddingBottom: '0.5rem' }}>
        <button
          type="button"
          className={caseSection === 'allocation' ? '' : 'secondary'}
          onClick={() => setCaseSection('allocation')}
        >
          Allocation
        </button>
        <button
          type="button"
          className={caseSection === 'planning' ? '' : 'secondary'}
          onClick={() => setCaseSection('planning')}
        >
          Planning
        </button>
      </nav>
      {caseSection === 'allocation' && (
      <section>
        <h2>Allocation</h2>
        <button onClick={handleAllocate} disabled={allocating}>{allocating ? 'Running…' : 'Run allocation'}</button>
        {allocating && allocationProgress != null && (
          <div style={{ marginTop: '0.75rem', maxWidth: 420 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.875rem', marginBottom: '0.25rem', color: '#64748b' }}>
              <span>Steps: {(allocationProgress.steps ?? 0).toLocaleString()} / {(allocationProgress.max_steps ?? 0).toLocaleString()}</span>
              <span>Basket: {allocationProgress.basket_keys ?? 0} items, {(Number(allocationProgress.basket_total_qty) ?? 0).toLocaleString()} qty</span>
            </div>
            <div style={{ height: 8, backgroundColor: '#e2e8f0', borderRadius: 4, overflow: 'hidden' }}>
              <div
                style={{
                  height: '100%',
                  width: (Number(allocationProgress.initial_basket_total_qty) ?? 0) > 0
                    ? `${Math.min(100, 100 * (1 - (Number(allocationProgress.basket_total_qty) ?? 0) / (Number(allocationProgress.initial_basket_total_qty) ?? 1)))}%`
                    : `${Math.min(100, 100 * ((Number(allocationProgress.steps) ?? 0) / ((Number(allocationProgress.max_steps) ?? 1) || 1)))}%`,
                  backgroundColor: '#0ea5e9',
                  borderRadius: 4,
                  transition: 'width 0.3s ease',
                }}
              />
            </div>
            <div style={{ fontSize: '0.75rem', color: '#94a3b8', marginTop: '0.25rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span>
                {(Number(allocationProgress.initial_basket_total_qty) ?? 0) > 0
                  ? `${((1 - (Number(allocationProgress.basket_total_qty) ?? 0) / (Number(allocationProgress.initial_basket_total_qty) ?? 1)) * 100).toFixed(1)}% of basket allocated`
                  : `${(100 * (Number(allocationProgress.steps) ?? 0) / (Number(allocationProgress.max_steps) ?? 1)).toFixed(1)}% of steps`}
              </span>
              {etaRemainingSeconds != null && etaRemainingSeconds > 0 && (
                <span style={{ fontWeight: 500, color: '#64748b' }}>
                  ~{etaRemainingSeconds >= 3600
                    ? `${Math.floor(etaRemainingSeconds / 3600)} h ${Math.floor((etaRemainingSeconds % 3600) / 60)} min`
                    : etaRemainingSeconds >= 60
                      ? `${Math.floor(etaRemainingSeconds / 60)} min ${Math.round(etaRemainingSeconds % 60)} s`
                      : `${Math.round(etaRemainingSeconds)} s`} left
                </span>
              )}
            </div>
          </div>
        )}
        {runs.length > 0 && (
          <div style={{ marginTop: '1rem' }}>
            <label>Run: </label>
            <select value={selectedRunId ?? ''} onChange={(e) => setSelectedRunId(Number(e.target.value))}>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>{new Date(r.created_at).toLocaleString()} – {r.status}</option>
              ))}
            </select>
            {' '}
            <Link href={`/cases/${id}/runs/${selectedRunId}`} className="btn">Explainability & Pegging</Link>
          </div>
        )}
        {selectedRunId && (
          <>
            <h3 style={{ marginTop: '1.5rem' }}>Views</h3>
            <div style={{ marginBottom: '0.5rem' }}>
              <button
                type="button"
                className={activeView === 'supply' ? '' : 'secondary'}
                onClick={() => setActiveView('supply')}
              >
                Supply view
              </button>
              {' '}
              <button
                type="button"
                className={activeView === 'allocation' ? '' : 'secondary'}
                onClick={() => setActiveView('allocation')}
              >
                Allocation view
              </button>
              {' '}
              <button
                type="button"
                className={activeView === 'suggested' ? '' : 'secondary'}
                onClick={() => setActiveView('suggested')}
              >
                Demand view
              </button>
              {' '}
              <button
                type="button"
                className={activeView === 'raw-material' ? '' : 'secondary'}
                onClick={() => setActiveView('raw-material')}
              >
                Raw material usage
              </button>
            </div>
            {activeView === 'supply' && (
              <>
                {supplyViewLoading && supplyView.length === 0 && (
                  <p style={{ color: '#71717a' }}>Loading supply…</p>
                )}
                {!supplyViewLoading && supplyView.length <= 1 && supplyView.length > 0 && (
                  <p style={{ fontSize: '0.875rem', color: '#71717a', marginBottom: '0.5rem' }}>
                    One row per supply record. If you expect more rows, re-import supply CSV for this case.
                  </p>
                )}
                {!supplyViewLoading && (
                <>
                {supplyView.length > 0 && (() => {
                  const totalInitial = supplyView.reduce((s, r) => s + (Number(r.initial_qty) || 0), 0);
                  const totalConsumed = supplyView.reduce((s, r) => s + (Number(r.consumed_qty) || 0), 0);
                  const overallUtil = totalInitial > 0 ? (totalConsumed / totalInitial) * 100 : 0;
                  return (
                    <p style={{ fontSize: '0.875rem', color: '#a1a1aa', marginBottom: '0.5rem' }}>
                      Overall utilization: <strong>{totalConsumed.toLocaleString()}</strong> / <strong>{totalInitial.toLocaleString()}</strong> initial = <strong>{overallUtil.toFixed(1)}%</strong>
                    </p>
                  );
                })()}
                <label style={{ display: 'inline-flex', alignItems: 'center', gap: 8, marginBottom: '0.5rem' }} title="Uncheck to see all supplies, including those with 0 consumed">
                  <input
                    type="checkbox"
                    checked={supplyFilterConsumedOnly}
                    onChange={(e) => setSupplyFilterConsumedOnly(e.target.checked)}
                  />
                  Only consumed (consumed qty &gt; 0) — uncheck to see all supplies
                </label>
                <SortFilterTable<SupplyViewRow & { _rowKey?: string }>
                idKey="_rowKey"
                rows={supplyView
                  .filter((r) => !supplyFilterConsumedOnly || (Number(r.consumed_qty) || 0) > 0)
                  .map((r, i) => ({ ...r, _rowKey: r.id != null ? String(r.id) : `supply-${r.supply_id}-${i}` }))}
                rowId={(r) => (r.id != null ? `supply-${r.id}` : r.supply_id ? `supply-${r.supply_id}` : undefined)}
                onRowClick={handleSupplyPeggingClick}
                filterKeys={['supply_id', 'product_id', 'location_id', 'component_key', 'supply_date', 'consumed_qty', 'utilization_rate']}
                defaultSortKey="utilization_rate"
                columns={[
                  { key: 'supply_id', label: 'Supply ID', sortable: true },
                  { key: 'supply_date', label: 'Time', sortable: true, render: (r) => r.supply_date ?? '–' },
                  { key: 'product_id', label: 'Product', sortable: true },
                  { key: 'location_id', label: 'Location', sortable: true },
                  { key: 'initial_qty', label: 'Initial qty', sortable: true },
                  { key: 'consumed_qty', label: 'Consumed qty', sortable: true },
                  { key: 'residual_qty', label: 'Residual qty', sortable: true },
                  { key: 'utilization_rate', label: 'Utilization', sortable: true, render: (r) => r.utilization_rate != null ? `${(Number(r.utilization_rate) * 100).toFixed(1)}%` : '–' },
                  { key: '_pegging', label: 'Pegging', sortable: false, render: (r) => <button type="button" className="secondary" onClick={() => handleSupplyPeggingClick(r)}>Show</button> },
                ]}
              />
                </>
                )}
              </>
            )}
            {activeView === 'allocation' && (
              <>
                {allocationViewLoading && allocationView.length === 0 && (
                  <p style={{ color: '#71717a' }}>Loading allocation…</p>
                )}
                {allocationViewLoading && allocationView.length > 0 && (
                  <p style={{ fontSize: '0.875rem', color: '#71717a', marginBottom: '0.25rem' }}>Loading more…</p>
                )}
                {allocationViewError && (
                  <p style={{ fontSize: '0.875rem', color: '#dc2626', marginBottom: '0.5rem' }}>{allocationViewError}</p>
                )}
                {allocationViewError && (allocationActions.length > 0 || allocationActionsLoading) && (
                  <>
                    <p style={{ fontSize: '0.875rem', color: '#71717a', marginBottom: '0.5rem' }}>
                      Showing raw allocation steps (lightweight, paginated). {allocationActionsTotal > 0 && `Total: ${allocationActionsTotal.toLocaleString()} steps.`}
                    </p>
                    {allocationActionsLoading && allocationActions.length === 0 && <p style={{ color: '#71717a' }}>Loading raw steps…</p>}
                    {allocationActions.length > 0 && (
                      <>
                        <div style={{ marginBottom: '0.5rem', display: 'flex', gap: 8, alignItems: 'center' }}>
                          <button
                            type="button"
                            className="secondary"
                            disabled={allocationActionsOffset <= 0 || allocationActionsLoading}
                            onClick={() => {
                              const nextOffset = Math.max(0, allocationActionsOffset - 500);
                              setAllocationActionsLoading(true);
                              getAllocationActions(id, selectedRunId!, { offset: nextOffset, limit: 500 })
                                .then((d) => {
                                  setAllocationActions(d.actions);
                                  setAllocationActionsOffset(nextOffset);
                                })
                                .finally(() => setAllocationActionsLoading(false));
                            }}
                          >
                            Previous
                          </button>
                          <span style={{ fontSize: '0.875rem', color: '#71717a' }}>
                            {allocationActionsOffset + 1}–{allocationActionsOffset + allocationActions.length} of {allocationActionsTotal.toLocaleString()}
                          </span>
                          <button
                            type="button"
                            className="secondary"
                            disabled={allocationActionsOffset + allocationActions.length >= allocationActionsTotal || allocationActionsLoading}
                            onClick={() => {
                              const nextOffset = allocationActionsOffset + 500;
                              setAllocationActionsLoading(true);
                              getAllocationActions(id, selectedRunId!, { offset: nextOffset, limit: 500 })
                                .then((d) => {
                                  setAllocationActions(d.actions);
                                  setAllocationActionsOffset(nextOffset);
                                })
                                .finally(() => setAllocationActionsLoading(false));
                            }}
                          >
                            Next
                          </button>
                        </div>
                        <SortFilterTable<AllocationActionRow & { _key?: string }>
                          idKey="_key"
                          rows={allocationActions.map((a, i) => ({ ...a, _key: `action-${a.id}-${i}` }))}
                          filterKeys={['variant_key', 'edge_type', 'demand_id', 'target_product_id']}
                          defaultSortKey="scarcity_rank"
                          columns={[
                            { key: 'id', label: 'ID', sortable: true },
                            { key: 'scarcity_rank', label: 'Rank', sortable: true, render: (r) => (r.scarcity_rank != null ? String(r.scarcity_rank) : '–') },
                            { key: 'variant_key', label: 'Variant', sortable: true },
                            { key: 'edge_type', label: 'Edge', sortable: true, render: (r) => r.edge_type || '–' },
                            { key: 'qty', label: 'Qty', sortable: true },
                            { key: 'demand_id', label: 'Demand', sortable: true, render: (r) => r.demand_id ?? '–' },
                            { key: 'target_product_id', label: 'Target product', sortable: true, render: (r) => r.target_product_id ?? '–' },
                            { key: 'target_location_id', label: 'Target location', sortable: true, render: (r) => r.target_location_id ?? '–' },
                            { key: 'output_period', label: 'Output period', sortable: true, render: (r) => (r.output_period != null ? String(r.output_period) : '–') },
                          ]}
                        />
                      </>
                    )}
                  </>
                )}
                {(allocationView.length > 0 || !allocationViewLoading) && (
                <>
                {allocationViewTruncated && (
                  <p style={{ fontSize: '0.875rem', color: '#b45309', marginBottom: '0.5rem', fontWeight: 600 }}>
                    Showing {allocationView.length.toLocaleString()} of {allocationViewTruncated.total_steps.toLocaleString()} rows
                    {allocationViewTruncated.total_actions > allocationViewTruncated.limit && ` (${allocationViewTruncated.total_actions.toLocaleString()} steps)`}.
                    {' '}
                    {selectedRunId && allocationView.length < allocationViewTruncated.total_steps && (
                      <button
                        type="button"
                        className="secondary"
                        disabled={allocationViewLoading}
                        onClick={() => {
                          setAllocationViewLoading(true);
                          const nextFrom = allocationView.length + 1;
                          const nextTo = Math.min(allocationView.length + ALLOCATION_VIEW_PAGE_SIZE, allocationViewTruncated.total_steps);
                          // Incremental: only view rows, no basket (stack on existing state)
                          getAllocationView(id, selectedRunId, {
                            max_actions: 300,
                            from_step: nextFrom,
                            to_step: nextTo,
                            skip_basket: true,
                          })
                            .then((a) => {
                              if (allocationViewRunIdRef.current === selectedRunId) {
                                setAllocationView((prev) => [...prev, ...a.allocation_view]);
                                const totalSteps = a.total_steps ?? allocationViewTruncated.total_steps;
                                setAllocationViewTruncated((prev) => (prev ? { ...prev, total_steps: totalSteps } : null));
                              }
                            })
                            .finally(() => setAllocationViewLoading(false));
                        }}
                      >
                        Load more
                      </button>
                    )}
                  </p>
                )}
                {allocating && allocationProgress != null && (
                  <p style={{ fontSize: '0.875rem', color: '#64748b', marginBottom: '0.5rem' }}>
                    Run progress: <strong>{(allocationProgress.steps ?? 0).toLocaleString()} steps</strong>, basket <strong>{allocationProgress.basket_keys ?? 0} items</strong>. Table is ordered by scarcity; each row’s basket is after the step in <strong>After step</strong> (view may show fewer steps until it refreshes).
                  </p>
                )}
                {(basketDeltas.length > 0 || allocationView.length > 0) && (
                  <p style={{ fontSize: '0.875rem', color: '#64748b', marginBottom: '0.5rem' }}>
                    View has data through step <strong>{(basketDeltas.length || allocationView.length).toLocaleString()}</strong>
                    {allocationViewTruncated && allocationView.length < allocationViewTruncated.total_steps ? (
                      <> (first chunk loaded; use Load more for rest).</>
                    ) : (
                      <>.</>
                    )}{' '}
                    {basketDeltas.length > 0 ? (
                      <>
                        Basket after last loaded step: <strong>{(basketFinal?.length ?? computeBasketAfterStep(basketDeltas.length - 1).length).toLocaleString()} items</strong>
                        {basketFinal != null && (
                          <>
                            {' '}
                            <button
                              type="button"
                              className="secondary"
                              style={{ marginLeft: 4 }}
                              onClick={() => { setBasketSlideInRow(null); setBasketShowingFinal(true); }}
                            >
                              View basket at end of loaded view
                            </button>
                          </>
                        )}.
                      </>
                    ) : (
                      <>Open a row’s <strong>View</strong> in the Basket column to load basket state.</>
                    )}
                    {basketDeltas.length > 0 && allocating && allocationProgress != null && allocationProgress.steps > basketDeltas.length && (
                      <>
                        {' '}View is behind run (refresh runs every 1,000 steps).
                        {selectedRunId && (
                          <button
                            type="button"
                            className="secondary"
                            style={{ marginLeft: 8 }}
                            disabled={allocationViewLoading}
                            onClick={() => {
                              setAllocationViewLoading(true);
                              const maxActions = Math.min(allocationProgress!.steps, 5000);
                              getAllocationView(id, selectedRunId!, { max_actions: maxActions, from_step: 1, to_step: 5000, skip_basket: false })
                                .then((a) => {
                                  if (allocationViewRunIdRef.current === selectedRunId) {
                                    setAllocationView(a.allocation_view);
          setBasketInitial(a.basket_initial ?? []);
          setBasketDeltas(a.basket_deltas ?? []);
          setBasketFinal(a.basket_final ?? null);
          setBasketPrunes(a.basket_prunes ?? []);
                                    setAllocationViewTruncated(
                                      (a.total_steps ?? 0) > 0
                                        ? { total_actions: a.total_actions ?? 0, limit: a.limit ?? 300, total_steps: a.total_steps ?? a.allocation_view.length }
                                        : null,
                                    );
                                    setAllocationViewError(null);
                                  }
                                })
                                .finally(() => setAllocationViewLoading(false));
                            }}
                          >
                            Refresh view now
                          </button>
                        )}
                      </>
                    )}
                  </p>
                )}
                <p style={{ fontSize: '0.875rem', color: '#71717a', marginBottom: '0.5rem' }}>
                  Critical-component-centric (scarcity = total quantity; scarcest = smallest). <strong>After step</strong> = allocation step after which the basket is shown (same scale as progress bar). One row per critical component; multiple target candidates grouped with split explanation. <strong>Time</strong> = when that edge’s output becomes available. From/To are true inventories; To can be a customer demand. <strong>Demands</strong> = customer demand IDs that request this row’s output product (empty “–” for intermediate steps whose output feeds later steps, not a final demand). Edge types: make, move, demand.
                </p>
                <SortFilterTable<AllocationViewRow & { _id?: string }>
                idKey="_id"
                rows={allocationView.map((row, i) => ({
                  ...row,
                  _id: row.row_type === 'component'
                    ? `comp-${row.step ?? i}-${row.from_inventory_id ?? i}`
                    : `demand-${row.from_inventory_id}-${row.to_inventory_id}-${i}`,
                  after_step: (row.basket_step_index ?? 0) + 1,
                }))}
                filterKeys={['edge_type', 'from_inventory_display', 'to_inventory_display', 'split_explanation', 'demand_ids']}
                defaultSortKey="step"
                columns={[
                  { key: 'step', label: 'Row', sortable: true, render: (r) => (r.step != null ? String(r.step) : '–') },
                  { key: 'after_step', label: 'After step', sortable: true, render: (r) => (r.after_step != null ? String(r.after_step) : (r.basket_step_index != null ? String((r.basket_step_index as number) + 1) : '–')) },
                  { key: 'edge_type', label: 'Edge', sortable: true, render: (r) => r.edge_type || '–' },
                  { key: 'from_inventory_display', label: 'From (critical component)', sortable: true, render: (r) => {
                    const ck = r.critical_component_key ?? r.from_inventory_id?.split('|').slice(0, 2).join('|');
                    if (r.row_type === 'component' && ck) {
                      return (
                        <button type="button" onClick={() => handleCriticalClick(ck, '')} style={{ background: 'rgba(251,191,36,0.4)', padding: '2px 5px', borderRadius: 3, fontWeight: 600, border: 'none', font: 'inherit', cursor: 'pointer', textAlign: 'left' }} title="Explain split formula">
                          {r.from_inventory_display || '–'}
                        </button>
                      );
                    }
                    return <code>{r.from_inventory_display || '–'}</code>;
                  } },
                  { key: 'candidates', label: 'To (candidates)', sortable: false, render: (r) => {
                    if (r.row_type === 'demand') {
                      return <code style={{ color: '#a78bfa' }}>{r.to_inventory_display ?? r.to_inventory_id ?? '–'}</code>;
                    }
                    if (!r.candidates?.length) return '–';
                    return (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                        {r.candidates.map((c, j) => (
                          <span key={j}>
                            <code>{c.to_inventory_display}</code>
                            <span style={{ color: '#71717a', marginLeft: 4 }}>qty: {c.qty}</span>
                            {c.edge_type && <span style={{ fontSize: '0.75em', color: '#52525b', marginLeft: 4 }}>({c.edge_type})</span>}
                          </span>
                        ))}
                      </div>
                    );
                  } },
                  { key: 'total_qty', label: 'Total qty', sortable: true, render: (r) => r.row_type === 'component' ? r.total_qty : (r.qty ?? '–') },
                  { key: 'split_explanation', label: 'Split (how & why)', sortable: false, render: (r) => (
                    r.split_explanation ? <span style={{ fontSize: '0.85em', color: '#a1a1aa', maxWidth: 420, display: 'inline-block' }}>{r.split_explanation}</span> : '–'
                  ) },
                  { key: 'basket_after', label: 'Basket', sortable: false, render: (r) => (
                      <button
                        type="button"
                        className="secondary"
                        onClick={() => { setBasketSlideInRow(r); setBasketShowingFinal(false); }}
                        style={{ padding: '2px 8px', fontSize: '0.85em' }}
                        title={`View basket state after step ${(r.basket_step_index ?? 0) + 1}`}
                      >
                        View
                      </button>
                    ) },
                  { key: 'output_date', label: 'Time', sortable: true, render: (r) => r.output_date ?? (r.output_period != null ? String(r.output_period) : '–') },
                  { key: 'demand_ids', label: 'Demands', render: (r) => (
                    <span title={r.demand_ids?.length ? `Demands that request this row's output product` : `Intermediate: output feeds later steps, not a final customer demand`}>
                      {r.demand_ids?.length ? r.demand_ids.join(', ') : '–'}
                    </span>
                  ) },
                ]}
              />
                </>
                )}
              </>
            )}
            {activeView === 'raw-material' && (
              <>
                {!selectedRunId && (
                  <p style={{ color: '#71717a' }}>Select a run above to see raw material usage (1xx-xxxx, 2xx-xxxx, 3xx-xxxx).</p>
                )}
                {selectedRunId && rawMaterialReportLoading && (
                  <p style={{ color: '#71717a' }}>Loading raw material usage report…</p>
                )}
                {selectedRunId && !rawMaterialReportLoading && rawMaterialReport && (
                  <>
                    <p style={{ fontSize: '0.875rem', color: '#a1a1aa', marginBottom: '0.75rem' }}>
                      Supplies of products matching 1xx-xxxx, 2xx-xxxx, 3xx-xxxx: consumption and involvement (critical or companion, allocated or only considered).
                    </p>
                    {rawMaterialReport.supply_patterns_used.length === 0 && (!rawMaterialReport.involvement_trace || rawMaterialReport.involvement_trace.length === 0) && (
                      <p style={{ color: '#71717a' }}>No raw material (1xx-xxxx, 2xx-xxxx, 3xx-xxxx) was consumed or involved in this run. Re-run allocation to record involvement trace.</p>
                    )}
                    {rawMaterialReport.supply_patterns_used.length > 0 && (
                      <>
                        <h4 style={{ marginTop: '1rem', marginBottom: '0.5rem', fontSize: '0.95rem' }}>Consumption</h4>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem', marginBottom: '1rem' }}>
                          {rawMaterialReport.supply_patterns_used.map((pattern) => {
                            const s = rawMaterialReport.summary[pattern];
                            if (!s) return null;
                            return (
                              <div
                                key={pattern}
                                style={{
                                  background: '#252528',
                                  borderRadius: 8,
                                  padding: '0.75rem 1rem',
                                  minWidth: 140,
                                }}
                              >
                                <div style={{ fontSize: '0.75rem', color: '#71717a', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{pattern}</div>
                                <div style={{ fontSize: '1.25rem', fontWeight: 600, color: '#fafafa' }}>
                                  {Number(s.total_consumed_qty).toLocaleString()}
                                </div>
                                <div style={{ fontSize: '0.8rem', color: '#a1a1aa' }}>{s.node_count} node(s) consumed</div>
                              </div>
                            );
                          })}
                        </div>
                        <SortFilterTable
                          idKey="node"
                          rows={rawMaterialReport.details}
                          filterKeys={['pattern', 'product_id', 'location_id', 'node', 'consumed_qty']}
                          defaultSortKey="consumed_qty"
                          columns={[
                            { key: 'pattern', label: 'Pattern', sortable: true },
                            { key: 'product_id', label: 'Product', sortable: true },
                            { key: 'location_id', label: 'Location', sortable: true },
                            { key: 'node', label: 'Node', sortable: true },
                            { key: 'consumed_qty', label: 'Consumed qty', sortable: true, render: (r) => Number(r.consumed_qty).toLocaleString() },
                          ]}
                        />
                      </>
                    )}
                    {rawMaterialReport.involvement_trace && rawMaterialReport.involvement_trace.length > 0 && (
                      <>
                        <h4 style={{ marginTop: '1.5rem', marginBottom: '0.5rem', fontSize: '0.95rem' }}>Involvement trace (critical / companion, allocated or considered)</h4>
                        <p style={{ fontSize: '0.8rem', color: '#71717a', marginBottom: '0.5rem' }}>
                          Every step where a raw material was critical or companion; allocated = actually used, considered_but_skipped = recipe used it but output was capped or skipped.
                        </p>
                        <div style={{ background: '#252528', borderRadius: 8, padding: '0.5rem', maxHeight: '40vh', overflow: 'auto' }}>
                          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                            <thead>
                              <tr style={{ textAlign: 'left', borderBottom: '1px solid #3d3d40' }}>
                                <th style={{ padding: '6px 8px' }}>Step</th>
                                <th style={{ padding: '6px 8px' }}>Role</th>
                                <th style={{ padding: '6px 8px' }}>Comp</th>
                                <th style={{ padding: '6px 8px' }}>Pattern</th>
                                <th style={{ padding: '6px 8px' }}>Status</th>
                                <th style={{ padding: '6px 8px' }}>In supply view</th>
                                <th style={{ padding: '6px 8px' }}>Details</th>
                              </tr>
                            </thead>
                            <tbody>
                              {rawMaterialReport.involvement_trace.map((e, i) => (
                                <tr key={i} style={{ borderBottom: '1px solid #2d2d30' }}>
                                  <td style={{ padding: '6px 8px' }}>{e.step}</td>
                                  <td style={{ padding: '6px 8px' }}>{e.role}</td>
                                  <td style={{ padding: '6px 8px' }}>{(e.comp_key || '').replace('|', '@')}</td>
                                  <td style={{ padding: '6px 8px' }}>{e.pattern}</td>
                                  <td style={{ padding: '6px 8px' }}>
                                    {e.considered_but_skipped ? 'considered, skipped' : e.allocated ? 'allocated' : '—'}
                                  </td>
                                  <td style={{ padding: '6px 8px' }}>
                                    {e.in_supply_view === false ? (
                                      <span style={{ color: '#f59e0b' }} title="This product|location has no Supply row; allocation came from move or production output.">No</span>
                                    ) : e.in_supply_view === true ? (
                                      <span style={{ color: '#22c55e' }}>Yes</span>
                                    ) : (
                                      '—'
                                    )}
                                  </td>
                                  <td style={{ padding: '6px 8px', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                    {e.breakdown && e.breakdown.length > 0 && (
                                      <span title={e.breakdown.map((b) => `${b.variant_key}=${b.output_qty}`).join('; ')}>
                                        {e.breakdown.map((b) => `${b.variant_key}=${b.output_qty}`).join('; ')}
                                      </span>
                                    )}
                                    {e.considered_but_skipped && e.reason && <span>{e.reason}</span>}
                                    {e.role === 'companion' && e.allocated && e.taken != null && (
                                      <span>taken={e.taken} → {e.variant_key} (critical: {e.critical_component?.replace('|', '@')})</span>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </>
                    )}
                    {selectedRunId && rawMaterialReport.supply_patterns_used.length === 0 && rawMaterialReport.involvement_trace && rawMaterialReport.involvement_trace.length > 0 && (
                      <p style={{ marginTop: '0.75rem', color: '#71717a', fontSize: '0.875rem' }}>No raw material was consumed in this run; trace above shows involvement as critical or companion (considered but skipped).</p>
                    )}
                  </>
                )}
                {selectedRunId && !rawMaterialReportLoading && !rawMaterialReport && (
                  <p style={{ color: '#71717a' }}>Could not load raw material usage report.</p>
                )}
              </>
            )}
            {activeView === 'suggested' && (
              feasibleDemands === null ? (
                <p style={{ color: '#71717a' }}>Loading demands…</p>
              ) : demandsLoadError ? (
                <p style={{ color: '#f87171' }}>{demandsLoadError}. Switch run or refresh to retry.</p>
              ) : (
              <>
                {feasibleDemands.length > 0 && (() => {
                  const totalRequested = feasibleDemands.reduce((s, f) => s + (Number(f.requested_qty) || 0), 0);
                  const totalAllocated = feasibleDemands.reduce((s, f) => s + (Number(f.allocated_qty) || 0), 0);
                  const overallRate = totalRequested > 0 ? (totalAllocated / totalRequested) * 100 : 0;
                  const byCustomer = feasibleDemands.reduce<Record<string, { requested: number; allocated: number }>>((acc, f) => {
                    const c = (f.customer ?? f.customer_id ?? '–') as string;
                    if (!acc[c]) acc[c] = { requested: 0, allocated: 0 };
                    acc[c].requested += Number(f.requested_qty) || 0;
                    acc[c].allocated += Number(f.allocated_qty) || 0;
                    return acc;
                  }, {});
                  return (
                    <>
                      <p style={{ fontSize: '0.875rem', color: '#a1a1aa', marginBottom: '0.5rem' }}>
                        Overall fulfillment: <strong>{totalAllocated.toLocaleString()}</strong> / <strong>{totalRequested.toLocaleString()}</strong> requested = <strong>{overallRate.toFixed(1)}%</strong>
                      </p>
                      {Object.keys(byCustomer).length > 1 && (
                        <details style={{ fontSize: '0.875rem', color: '#a1a1aa', marginBottom: '0.5rem' }}>
                          <summary style={{ cursor: 'pointer' }}>Rollup by customer</summary>
                          <ul style={{ marginTop: '0.25rem', paddingLeft: '1.25rem' }}>
                            {Object.entries(byCustomer).map(([cust, { requested, allocated }]) => {
                              const rate = requested > 0 ? (allocated / requested) * 100 : 0;
                              return (
                                <li key={cust}>
                                  <strong>{cust}</strong>: {allocated.toLocaleString()} / {requested.toLocaleString()} = {rate.toFixed(1)}%
                                </li>
                              );
                            })}
                          </ul>
                        </details>
                      )}
                    </>
                  );
                })()}
                <SortFilterTable<FeasibleDemand & { suggested_revision?: string }>
                idKey="demand_id"
                rows={feasibleDemands.map((f) => ({
                  ...f,
                  suggested_revision: f.suggested_revision ?? (f.status === 'fulfilled' ? 'Fulfilled' : f.allocated_qty > 0 ? `Reduce to ${f.allocated_qty}` : 'Unfulfilled (0 allocated)'),
                }))}
                rowId={(r) => `demand-${r.demand_id}`}
                onRowClick={handleDemandPeggingClick}
                filterKeys={['demand_id', 'customer', 'customer_id', 'product_id', 'status', 'suggested_revision', 'request_due_time', 'revised_time', 'fulfillment_rate']}
                defaultSortKey="fulfillment_rate"
                columns={[
                  { key: 'demand_id', label: 'Demand ID', sortable: true },
                  { key: 'customer', label: 'Customer', sortable: true, render: (r) => r.customer ?? r.customer_id ?? '–' },
                  { key: 'request_due_time', label: 'Time', sortable: true, render: (r) => r.request_due_time ?? '–' },
                  { key: 'revised_time', label: 'Revised time', sortable: true, render: (r) => r.revised_time ?? '–' },
                  { key: 'product_id', label: 'Product', sortable: true },
                  { key: 'requested_qty', label: 'Requested', sortable: true },
                  { key: 'allocated_qty', label: 'Allocated', sortable: true },
                  { key: 'fulfillment_rate', label: 'Fulfillment', sortable: true, render: (r) => r.fulfillment_rate != null ? `${(Number(r.fulfillment_rate) * 100).toFixed(1)}%` : '–' },
                  { key: 'status', label: 'Status', sortable: true },
                  { key: 'suggested_revision', label: 'Suggested revision', sortable: true },
                  { key: '_pegging', label: 'Pegging', sortable: false, render: (r) => <button type="button" className="secondary" onClick={() => handleDemandPeggingClick(r)}>Show</button> },
                ]}
              />
              </>
              )
            )}
          </>
        )}
      </section>
      )}
      {caseSection === 'planning' && (
      <section>
        <h2>Planning</h2>
        <p style={{ fontSize: '0.875rem', color: '#a1a1aa', marginBottom: '0.5rem' }}>
          Demand-to-supply planning: takes customer demands and outputs committed demands (with commit_time) and planned work orders.
          Use <strong>Configure planning (copilot)</strong> to set how variants are chosen (single best vs. split across all).
        </p>
        <div style={{ marginBottom: '0.75rem' }}>
          <button
            type="button"
            disabled={planLoading}
            onClick={() => {
              setPlanError(null);
              setPlanLoading(true);
              runPlan(id, Object.keys(planningConfig).length ? planningConfig : undefined)
                .then(setPlanResult)
                .catch((e) => setPlanError(e instanceof Error ? e.message : 'Plan failed'))
                .finally(() => setPlanLoading(false));
            }}
          >
            {planLoading ? 'Running plan…' : 'Run plan'}
          </button>
          <span style={{ marginLeft: '1rem' }} />
          <button
            type="button"
            onClick={() => setCopilotOpen((o) => !o)}
            aria-expanded={copilotOpen}
            style={{
              padding: '6px 12px',
              background: copilotOpen ? '#3b82f6' : 'transparent',
              color: copilotOpen ? '#fff' : '#3b82f6',
              border: '1px solid #3b82f6',
              borderRadius: 6,
              cursor: 'pointer',
              fontWeight: 500,
            }}
          >
            {copilotOpen ? 'Hide copilot' : 'Configure planning (copilot)'}
          </button>
        </div>
        {planError && <p style={{ color: '#f87171', marginTop: '0.5rem' }}>{planError}</p>}
        {planResult && !planLoading && (
          <>
            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1.5rem', marginBottom: '0.5rem' }}>
              <button
                type="button"
                className={planResultTab === 'demands' ? '' : 'secondary'}
                onClick={() => setPlanResultTab('demands')}
              >
                Committed demands
              </button>
              <button
                type="button"
                className={planResultTab === 'work_orders' ? '' : 'secondary'}
                onClick={() => setPlanResultTab('work_orders')}
              >
                Work orders
              </button>
            </div>
            <div style={{ border: '1px solid #3d3d40', borderRadius: 6 }}>
              {planResultTab === 'demands' && (
                <div style={{ padding: '0.75rem 1rem' }}>
                  <h4 style={{ marginTop: 0, marginBottom: '0.5rem' }}>Committed demands</h4>
                  {planResult.committed_demands.length > 0 && (() => {
                    const byCustomer = planResult.committed_demands.reduce<Record<string, number>>((acc, r) => {
                      const c = (r.customer ?? r.customer_id ?? '–') as string;
                      acc[c] = (acc[c] ?? 0) + (Number(r.quantity) || 0);
                      return acc;
                    }, {});
                    const hasMultipleCustomers = Object.keys(byCustomer).length > 1;
                    return hasMultipleCustomers ? (
                      <details style={{ fontSize: '0.875rem', color: '#a1a1aa', marginBottom: '0.5rem' }}>
                        <summary style={{ cursor: 'pointer' }}>Rollup by customer</summary>
                        <ul style={{ marginTop: '0.25rem', paddingLeft: '1.25rem' }}>
                          {Object.entries(byCustomer).map(([cust, qty]) => (
                            <li key={cust}><strong>{cust}</strong>: {Number(qty).toLocaleString()} committed</li>
                          ))}
                        </ul>
                      </details>
                    ) : null;
                  })()}
                  <SortFilterTable<CommittedDemand & { _key?: string; _customer?: string }>
                    idKey="_key"
                    rows={planResult.committed_demands.map((r, i) => ({
                      ...r,
                      _key: `cd-${r.demand_id ?? ''}-${r.product_id}-${r.location_id}-${i}`,
                      _customer: String(r.customer ?? r.customer_id ?? ''),
                    }))}
                    filterKeys={['demand_id', '_customer', 'product_id', 'location_id', 'commit_time', 'commit_reason']}
                    filterPlaceholder="Filter by demand ID, customer, product, location…"
                    defaultSortKey="commit_time"
                    stickyHeader
                    columns={[
                      { key: 'demand_id', label: 'Demand ID', sortable: true, render: (r) => r.demand_id ?? '–' },
                      { key: '_customer', label: 'Customer', sortable: true, render: (r) => (r as { _customer?: string })._customer || (r.customer ?? r.customer_id ?? '–') },
                      { key: 'product_id', label: 'Product', sortable: true },
                      { key: 'location_id', label: 'Location', sortable: true },
                      { key: 'quantity', label: 'Quantity', sortable: true },
                      { key: 'request_time', label: 'Request time', sortable: true, render: (r) => r.request_time ?? '–' },
                      { key: 'commit_time', label: 'Commit time', sortable: true, render: (r) => r.commit_time ?? '–' },
                      { key: 'commit_reason', label: 'Commit reason', sortable: true, render: (r) => r.commit_reason ?? '–' },
                      { key: '_pegging', label: 'Pegging', sortable: false, render: (r) => (
                        <button type="button" className="secondary" onClick={() => { setPlanPeggingContext({ type: 'demand', row: r }); setPlanPeggingOpen(true); }}>Show</button>
                      ) },
                    ]}
                  />
                </div>
              )}
              {planResultTab === 'work_orders' && (
                <div style={{ padding: '0.75rem 1rem' }}>
                  <h4 style={{ marginTop: 0, marginBottom: '0.5rem' }}>Work orders</h4>
                  {planResult.work_orders.length > 0 && (() => {
                    const byProdArea = planResult.work_orders.reduce<Record<string, number>>((acc, r) => {
                      const pa = (r.prod_area ?? '–') as string;
                      acc[pa] = (acc[pa] ?? 0) + (Number(r.quantity) || 0);
                      return acc;
                    }, {});
                    const hasMultipleAreas = Object.keys(byProdArea).length > 1;
                    return hasMultipleAreas ? (
                      <details style={{ fontSize: '0.875rem', color: '#a1a1aa', marginBottom: '0.5rem' }}>
                        <summary style={{ cursor: 'pointer' }}>Rollup by PROD_AREA</summary>
                        <ul style={{ marginTop: '0.25rem', paddingLeft: '1.25rem' }}>
                          {Object.entries(byProdArea).map(([area, qty]) => (
                            <li key={area}><strong>{area}</strong>: {Number(qty).toLocaleString()} quantity</li>
                          ))}
                        </ul>
                      </details>
                    ) : null;
                  })()}
                  <SortFilterTable<WorkOrder & { _key?: string; _prod_area?: string }>
                    idKey="_key"
                    rows={planResult.work_orders.map((r, i) => ({
                      ...r,
                      _key: `wo-${i}-${r.product_id}-${r.location_id}`,
                      _prod_area: String(r.prod_area ?? ''),
                    }))}
                    filterKeys={['product_id', 'location_id', '_prod_area', 'method', 'start_time', 'end_time']}
                    filterPlaceholder="Filter by product, location, PROD_AREA, method…"
                    defaultSortKey="start_time"
                    stickyHeader
                    columns={[
                      { key: 'product_id', label: 'Product', sortable: true },
                      { key: 'location_id', label: 'Location', sortable: true },
                      { key: '_prod_area', label: 'PROD_AREA', sortable: true, render: (r) => (r as { _prod_area?: string })._prod_area || r.prod_area ?? '–' },
                      { key: 'quantity', label: 'Quantity', sortable: true },
                      { key: 'start_time', label: 'Start time', sortable: true, render: (r) => r.start_time ?? '–' },
                      { key: 'end_time', label: 'End time', sortable: true, render: (r) => r.end_time ?? '–' },
                      { key: 'method', label: 'Method', sortable: true },
                      { key: 'location_source', label: 'Location source', sortable: true, render: (r) => r.location_source ?? '–' },
                      { key: '_pegging', label: 'Pegging', sortable: false, render: (r) => (
                        <button type="button" className="secondary" onClick={() => { setPlanPeggingContext({ type: 'work_order', row: r }); setPlanPeggingOpen(true); }}>Show</button>
                      ) },
                    ]}
                  />
                </div>
              )}
            </div>
          </>
        )}
      </section>
      )}
      {copilotOpen && typeof document !== 'undefined' && createPortal(
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 9998,
            display: 'flex',
            justifyContent: 'flex-end',
            pointerEvents: 'auto',
          }}
          role="dialog"
          aria-label="Planning copilot"
        >
          <div
            style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 0, pointerEvents: 'auto' }}
            onClick={() => setCopilotOpen(false)}
            aria-hidden
          />
          <div
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => e.stopPropagation()}
            style={{
              position: 'relative',
              zIndex: 10,
              width: copilotPanelWidth,
              maxWidth: '90vw',
              minWidth: 320,
              height: '100vh',
              display: 'flex',
              flexDirection: 'column',
              background: '#1c1c1e',
              color: '#e4e4e7',
              boxShadow: '-4px 0 24px rgba(0,0,0,0.4)',
              pointerEvents: 'auto',
            }}
          >
            <div
              role="separator"
              aria-label="Resize copilot panel"
              onMouseDown={(e) => {
                e.preventDefault();
                e.stopPropagation();
                copilotResizeRef.current = { startX: e.clientX, startW: copilotPanelWidth };
                setCopilotResizing(true);
              }}
              style={{
                position: 'absolute',
                left: 0,
                top: 0,
                bottom: 0,
                width: 6,
                cursor: 'col-resize',
                zIndex: 11,
              }}
            />
            <div style={{ padding: '1rem 1.25rem', borderBottom: '1px solid #3d3d40', flexShrink: 0 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                <h3 style={{ margin: 0, color: '#fafafa' }}>Planning copilot</h3>
                <button type="button" onClick={() => setCopilotOpen(false)} style={{ padding: '6px 12px', background: '#2d2d30', color: '#e4e4e7', border: '1px solid #3d3d40', borderRadius: 6, cursor: 'pointer' }}>Close</button>
              </div>
              <p style={{ margin: 0, fontSize: '0.8rem', color: '#a1a1aa' }}>
                Variant selection: <strong>{planningConfig.variant_selection?.multiple === false ? 'single best' : 'all feasible (equal split)'}</strong>. Express your requirements in natural language; the system may ask follow-up questions to clarify.
              </p>
            </div>
            <div style={{ flex: 1, overflowY: 'auto', padding: '1rem 1.25rem' }}>
              {copilotMessages.length === 0 && (
                <p style={{ margin: 0, fontSize: '0.875rem', color: '#71717a' }}>
                  Ask how to configure <code style={{ background: '#27272a', padding: '2px 6px', borderRadius: 4 }}>get_preferred_variants()</code>. Examples: &quot;use only one variant per demand&quot;, &quot;split demand across all feasible variants&quot;, &quot;show current config&quot;. With an LLM enabled, you can use natural language and the assistant may ask clarifying questions.
                </p>
              )}
              {copilotMessages.map((m, i) => (
                <div key={i} style={{ marginBottom: '0.75rem' }}>
                  <span style={{ fontWeight: 600, color: m.role === 'user' ? '#a78bfa' : '#67e8f9', fontSize: '0.8rem' }}>{m.role === 'user' ? 'You' : 'Copilot'}: </span>
                  <span style={{ whiteSpace: 'pre-wrap', fontSize: '0.875rem' }}>{m.text.replace(/\*\*(.*?)\*\*/g, '$1')}</span>
                </div>
              ))}
              {copilotLoading && <p style={{ margin: 0, fontSize: '0.875rem', color: '#a1a1aa' }}>Thinking…</p>}
              <div ref={copilotMessagesEndRef} />
            </div>
            <form
              style={{ padding: '1rem 1.25rem', borderTop: '1px solid #3d3d40', flexShrink: 0 }}
              onSubmit={async (e) => {
                e.preventDefault();
                const text = copilotInput.trim();
                if (!text || copilotLoading) return;
                setCopilotMessages((prev) => [...prev, { role: 'user', text }]);
                setCopilotInput('');
                setCopilotLoading(true);
                try {
                  const res = await planningCopilot(id, text, planningConfig, copilotMessages);
                  if (res.config_update) setPlanningConfig((prev) => ({ ...prev, ...res.config_update! }));
                  setCopilotMessages((prev) => [...prev, { role: 'assistant', text: res.reply }]);
                } catch {
                  const { reply, configUpdate } = parseCopilotIntent(text, planningConfig);
                  if (configUpdate) setPlanningConfig((prev) => ({ ...prev, ...configUpdate }));
                  setCopilotMessages((prev) => [...prev, { role: 'assistant', text: reply }]);
                } finally {
                  setCopilotLoading(false);
                }
              }}
            >
              <input
                type="text"
                value={copilotInput}
                onChange={(e) => setCopilotInput(e.target.value)}
                placeholder="e.g. I want to use a single best variant"
                disabled={copilotLoading}
                style={{ width: '100%', padding: '8px 12px', background: '#27272a', border: '1px solid #3d3d40', borderRadius: 6, color: '#fafafa' }}
                aria-label="Chat with planning copilot"
              />
              <button type="submit" disabled={copilotLoading} className="secondary" style={{ marginTop: '0.5rem' }}>Send</button>
            </form>
          </div>
        </div>,
        document.body
      )}
      {planPeggingOpen && planPeggingContext && planResult && typeof document !== 'undefined' && createPortal(
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 9998,
            display: 'flex',
            justifyContent: 'flex-end',
            pointerEvents: 'auto',
          }}
          role="dialog"
          aria-label="Planning pegging"
        >
          <div
            style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 0, pointerEvents: 'auto' }}
            onClick={() => { setPlanPeggingOpen(false); setPlanPeggingContext(null); }}
            aria-hidden
          />
          <div
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => e.stopPropagation()}
            style={{
              position: 'relative',
              zIndex: 10,
              width: planPeggingPanelWidth,
              maxWidth: '90vw',
              minWidth: 320,
              maxHeight: '100vh',
              overflow: 'auto',
              background: '#1c1c1e',
              color: '#e4e4e7',
              boxShadow: '-4px 0 24px rgba(0,0,0,0.4)',
              padding: '1.25rem',
              pointerEvents: 'auto',
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <div
              role="separator"
              aria-label="Resize planning pegging panel"
              onMouseDown={(e) => {
                e.preventDefault();
                e.stopPropagation();
                planPeggingResizeRef.current = { startX: e.clientX, startW: planPeggingPanelWidth };
                setPlanPeggingResizing(true);
              }}
              style={{
                position: 'absolute',
                left: 0,
                top: 0,
                bottom: 0,
                width: 6,
                cursor: 'col-resize',
                zIndex: 11,
              }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
              <h3 style={{ margin: 0, color: '#fafafa' }}>
                {planPeggingContext.type === 'demand'
                  ? `Pegging: ${planPeggingContext.row.demand_id ?? planPeggingContext.row.product_id}`
                  : `Pegging: ${planPeggingContext.row.product_id} @ ${planPeggingContext.row.location_id}`}
              </h3>
              <button type="button" onClick={() => { setPlanPeggingOpen(false); setPlanPeggingContext(null); }} style={{ padding: '6px 12px', background: '#2d2d30', color: '#e4e4e7', border: '1px solid #3d3d40', borderRadius: 6, cursor: 'pointer' }}>Close</button>
            </div>
            <p style={{ margin: 0, marginBottom: '0.5rem', fontSize: '0.8rem', color: '#71717a' }}>
              ▢ Inventory (static state) and ⚙ Work order (transformation). Root = target inventory; work orders transform between states; leaves = supply or purchase inventory.
            </p>
            {(() => {
              const demandId = planPeggingContext.type === 'demand'
                ? planPeggingContext.row.demand_id
                : (planPeggingContext.row as WorkOrder).demand_id;
              const entry = planResult.planning_pegging?.find((e) => e.demand_id === demandId);
              const tree = entry?.tree;
              if (!tree) {
                return <p style={{ color: '#a1a1aa', fontSize: '0.9rem' }}>No pegging tree for this demand. Re-run plan to get planning_pegging.</p>;
              }
              function renderNode(node: PlanningPeggingNode, path: string, depth: number) {
                const childrenList = node.children ?? [];
                const hasChildren = childrenList.length > 0;
                const isRoot = path === '0';
                const expandable = hasChildren || isRoot;
                const isExpanded = planPeggingExpanded.has(path);
                const toggle = () => setPlanPeggingExpanded((prev) => {
                  const next = new Set(prev);
                  if (next.has(path)) next.delete(path);
                  else next.add(path);
                  return next;
                });
                const isInventory = node.type === 'demand' || node.type === 'supply' || node.type === 'purchase';
                const icon = isInventory ? '▢' : '⚙';
                const typeLabel = isInventory ? 'Inventory' : 'Work order';
                const label = node.type === 'demand'
                  ? `${node.demand_id ?? node.product_id} · ${Number(node.quantity ?? 0).toLocaleString()} @ ${node.location_id ?? '–'}`
                  : node.type === 'work_order'
                    ? `${node.method} ${node.product_id} @ ${node.location_id ?? '–'} · ${Number(node.quantity ?? 0).toLocaleString()}${node.end_time ? ` · end ${node.end_time}` : ''}`
                    : node.type === 'supply'
                      ? `${node.product_id} @ ${node.location_id ?? '–'} · ${Number(node.quantity ?? 0).toLocaleString()} (supply)`
                      : `${node.product_id} @ ${node.location_id ?? '–'} · ${Number(node.quantity ?? 0).toLocaleString()} (purchase)`;
                const indentPx = 12;
                return (
                  <div key={path} style={{ marginBottom: 4 }}>
                    <button
                      type="button"
                      onClick={toggle}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                        width: '100%',
                        textAlign: 'left',
                        padding: '4px 6px',
                        background: depth % 2 === 0 ? 'rgba(255,255,255,0.04)' : 'transparent',
                        border: 'none',
                        borderRadius: 4,
                        color: '#e4e4e7',
                        cursor: expandable ? 'pointer' : 'default',
                        fontSize: '0.85rem',
                      }}
                    >
                      <span style={{ width: 14, flexShrink: 0 }}>{expandable ? (isExpanded ? '▼' : '▶') : '·'}</span>
                      <span style={{ width: 18, flexShrink: 0, fontSize: '0.9em' }} title={isInventory ? 'Inventory (state)' : 'Work order (transformation)'}>{icon}</span>
                      <span style={{ flex: 1 }}>
                        <span style={{ color: isInventory ? '#a78bfa' : '#34d399', fontWeight: 600 }}>{typeLabel}</span>
                        {' '}
                        {label}
                      </span>
                    </button>
                    {node.type === 'work_order' && (node.method_choice_explanation || node.variant_choice_explanation) && (() => {
                      const explanationPath = `explain-${path}`;
                      const isExplanationOpen = planExplanationExpanded.has(explanationPath);
                      const toggleExplanation = () => setPlanExplanationExpanded((prev) => {
                        const next = new Set(prev);
                        if (next.has(explanationPath)) next.delete(explanationPath);
                        else next.add(explanationPath);
                        return next;
                      });
                      return (
                        <div style={{ marginTop: 4, marginLeft: 4, fontSize: '0.75rem', color: '#a1a1aa' }}>
                          <button
                            type="button"
                            onClick={toggleExplanation}
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: 4,
                              padding: '2px 0',
                              background: 'none',
                              border: 'none',
                              color: '#71717a',
                              cursor: 'pointer',
                              fontSize: '0.75rem',
                            }}
                          >
                            {isExplanationOpen ? '▼' : '▶'}
                            Why (method / variant)
                          </button>
                          {isExplanationOpen && (
                            <div style={{ paddingLeft: 8, borderLeft: '2px solid #3d3d40', marginTop: 2 }}>
                              {node.method_choice_explanation && (
                                <p style={{ margin: '0 0 4px', lineHeight: 1.35 }}><strong>Method:</strong> {node.method_choice_explanation}</p>
                              )}
                              {node.variant_choice_explanation && (
                                <p style={{ margin: 0, lineHeight: 1.35 }}><strong>Variant:</strong> {node.variant_choice_explanation}</p>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })()}
                    {expandable && isExpanded && (
                      <div style={{ marginTop: 2, paddingLeft: indentPx }}>
                        {hasChildren
                          ? childrenList.map((child, i) => renderNode(child, `${path}-${i}`, depth + 1))
                          : <p style={{ margin: 0, fontSize: '0.8rem', color: '#71717a' }}>No work orders — planning failed or no method for this demand.</p>}
                      </div>
                    )}
                  </div>
                );
              }
              return <div style={{ marginTop: '0.5rem' }}>{renderNode(tree, '0', 0)}</div>;
            })()}
          </div>
        </div>,
        document.body
      )}
      {(basketSlideInRow != null || basketShowingFinal) && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 50,
            display: 'flex',
            justifyContent: 'flex-end',
          }}
          role="dialog"
          aria-label={basketShowingFinal ? 'Basket at end of loaded view' : 'Basket after step'}
        >
          <div
            style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.55)' }}
            onClick={() => { setBasketSlideInRow(null); setBasketShowingFinal(false); }}
          />
          <div
            style={{
              width: 'min(380px, 100vw)',
              maxHeight: '100vh',
              overflow: 'auto',
              background: '#1c1c1e',
              color: '#e4e4e7',
              boxShadow: '-4px 0 24px rgba(0,0,0,0.4)',
              padding: '1.25rem',
              position: 'relative',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
              <h3 style={{ margin: 0, color: '#fafafa' }}>
                {basketShowingFinal ? 'Basket at end of loaded view' : `Basket after step ${(basketSlideInRow?.basket_step_index ?? 0) + 1}`}
              </h3>
              <button type="button" onClick={() => { setBasketSlideInRow(null); setBasketShowingFinal(false); }} style={{ padding: '6px 12px', background: '#2d2d30', color: '#e4e4e7', border: '1px solid #3d3d40', borderRadius: 6, cursor: 'pointer' }}>Close</button>
            </div>
            {!basketShowingFinal && basketSlideInRow != null && (
              <>
                <p style={{ margin: 0, color: '#a1a1aa', fontSize: '0.9rem' }}>
                  Row {basketSlideInRow.step ?? '–'} · From {basketSlideInRow.from_inventory_display ?? '–'}
                </p>
                {(basketSlideInRow.candidates?.length ?? 0) > 0 && (
                  <>
                    <h4 style={{ margin: '1rem 0 0.5rem', fontSize: '0.9rem', color: '#a78bfa' }}>Target candidates</h4>
                    <ul style={{ marginTop: 0, paddingLeft: '1.25rem', listStyle: 'disc' }}>
                      {basketSlideInRow.candidates!.map((c, j) => (
                        <li key={j} style={{ marginBottom: 4 }}>
                          <code style={{ background: '#2d2d30', padding: '2px 6px', borderRadius: 4 }}>{c.to_inventory_display}</code>
                          <span style={{ marginLeft: 8, fontWeight: 600 }}>{c.qty}</span>
                          {c.edge_type ? <span style={{ marginLeft: 6, fontSize: '0.8em', color: '#71717a' }}>({c.edge_type})</span> : null}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </>
            )}
            <h4 style={{ margin: '1rem 0 0.5rem', fontSize: '0.9rem', color: '#a1a1aa' }}>Basket (scarcity order)</h4>
            <p style={{ margin: '0 0 0.5rem', fontSize: '0.8rem', color: '#71717a' }}>
              {basketShowingFinal ? 'Final basket after all allocation steps. Consumed supplies are removed.' : 'Scarcity = total quantity per component; scarcest first. Basket = initial supplies (minus consumed) + produced in this run. Produced output is added after its step, so it appears at the beginning of the next step.'}
            </p>
            {basketDeltas.length === 0 ? (
              <p style={{ color: '#a1a1aa', fontSize: '0.9rem' }}>{basketLoading ? 'Loading basket…' : 'Basket not loaded yet.'}</p>
            ) : (
            <ul style={{ marginTop: 0, paddingLeft: '1.25rem', listStyle: 'disc' }}>
              {(() => {
                const items = basketShowingFinal && basketFinal
                  ? basketFinal.map((b) => ({ ...b, fromInitialSupply: basketInitialKeys.has(b.key) }))
                  : computeBasketAfterStep(basketSlideInRow?.basket_step_index ?? 0);
                return items.length === 0 ? (
                  <li style={{ color: '#71717a' }}>No inventory in basket</li>
                ) : (
                  items.map((b, i) => (
                    <li key={i} style={{ marginBottom: 6 }}>
                      <span style={{ marginRight: 6, color: '#a1a1aa' }}>{i + 1}.</span>
                      <code style={{ background: '#2d2d30', padding: '2px 6px', borderRadius: 4 }}>{b.display}</code>
                      <span style={{ marginLeft: 8, fontWeight: 600 }}>{b.qty}</span>
                      {b.fromInitialSupply ? (
                        <span style={{ marginLeft: 6, fontSize: '0.75rem', color: '#71717a' }}>(initial supply)</span>
                      ) : (
                        <span style={{ marginLeft: 6, fontSize: '0.75rem', color: '#a78bfa' }}>(produced this run)</span>
                      )}
                    </li>
                  ))
                );
              })()}
            </ul>
            )}
          </div>
        </div>
      )}
      {explanationOpen && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 50,
            display: 'flex',
            justifyContent: 'flex-end',
          }}
          role="dialog"
          aria-label="Split explanation"
        >
          <div
            style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.55)' }}
            onClick={() => setExplanationOpen(false)}
          />
          <div
            style={{
              width: 'min(420px, 100vw)',
              maxHeight: '100vh',
              overflow: 'auto',
              background: '#1c1c1e',
              color: '#e4e4e7',
              boxShadow: '-4px 0 24px rgba(0,0,0,0.4)',
              padding: '1.25rem',
              position: 'relative',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
              <h3 style={{ margin: 0, color: '#fafafa' }}>Split explanation</h3>
              <button type="button" onClick={() => setExplanationOpen(false)} style={{ padding: '6px 12px', background: '#2d2d30', color: '#e4e4e7', border: '1px solid #3d3d40', borderRadius: 6, cursor: 'pointer' }}>Close</button>
            </div>
            {explanationLoading && <p style={{ color: '#a1a1aa' }}>Loading…</p>}
            {!explanationLoading && explanationData && (
              <div style={{ fontSize: '0.9rem', color: '#e4e4e7' }}>
                <p><strong>Critical component</strong>: <code style={{ background: '#2d2d30', padding: '2px 6px', borderRadius: 4 }}>{explanationData.component_display}</code></p>
                <p style={{ marginTop: '0.75rem' }}><strong>Total supply for this component</strong> (initial): {explanationData.total_supply_for_component}</p>
                {explanationData.available_during_run != null && explanationData.available_during_run !== explanationData.total_supply_for_component && (
                  <p style={{ marginTop: '0.25rem', color: '#a1a1aa' }}><strong>Available during run</strong> (initial + produced): {explanationData.available_during_run}</p>
                )}
                {explanationData.supply_note && (
                  <p style={{ marginTop: '0.5rem', padding: '0.5rem', background: '#3d3d40', color: '#a1a1aa', borderRadius: 6, fontSize: '0.85rem' }}>
                    {explanationData.supply_note}
                  </p>
                )}
                {explanationData.weight_formula && (
                  <p style={{ marginTop: '0.75rem', padding: '0.5rem', background: '#2d2d30', color: '#a1a1aa', borderRadius: 6, border: '1px solid #3d3d40', fontSize: '0.85rem' }}>
                    <strong>How weights are calculated</strong>: {explanationData.weight_formula}
                  </p>
                )}
                <p style={{ marginTop: '0.75rem' }}><strong>Candidate targets</strong> (edges that consume this component){explanationData.total_candidate_weight != null ? ` · total weight = ${explanationData.total_candidate_weight}` : ''}:</p>
                <ul style={{ margin: '0.25rem 0', paddingLeft: '1.25rem' }}>
                  {explanationData.candidate_targets.map((t) => (
                    <li key={t.variant_key}>
                      <code style={{ background: '#2d2d30', padding: '2px 6px', borderRadius: 4 }}>{t.variant_display}</code> ({t.edge_type})
                      {t.target_weight != null && <span style={{ color: '#a1a1aa', marginLeft: 6 }}>weight = {t.target_weight}</span>}
                      {t.weight_calculation && <div style={{ fontSize: '0.8rem', color: '#71717a', marginTop: 2 }}>{t.weight_calculation}</div>}
                    </li>
                  ))}
                </ul>
                {explanationData.candidate_targets.length === 0 && <p style={{ margin: 0, color: '#71717a' }}>None</p>}
                <p style={{ marginTop: '0.75rem' }}><strong>Usage in this run</strong> (available before → qty allocated):</p>
                <ul style={{ margin: '0.25rem 0', paddingLeft: '1.25rem' }}>
                  {explanationData.steps.map((s, i) => (
                    <li key={i}>
                      <code style={{ background: '#2d2d30', padding: '2px 6px', borderRadius: 4 }}>{s.to_variant_display}</code> ({s.edge_type}): available before = {s.available_before} → qty = {s.qty}
                    </li>
                  ))}
                </ul>
                {explanationData.steps.length === 0 && <p style={{ margin: 0, color: '#71717a' }}>None</p>}
                <p style={{ marginTop: '1rem', padding: '0.5rem', background: '#2d2d30', color: '#a1a1aa', borderRadius: 6, border: '1px solid #3d3d40' }}>{explanationData.reason}</p>
              </div>
            )}
            {!explanationLoading && !explanationData && <p style={{ color: '#a1a1aa' }}>Could not load explanation.</p>}
          </div>
        </div>
      )}
      {peggingOpen && typeof document !== 'undefined' && createPortal(
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 9999,
            display: 'flex',
            justifyContent: 'flex-end',
            pointerEvents: 'auto',
          }}
          role="dialog"
          aria-label="Pegging"
        >
          <div
            style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 0, pointerEvents: 'auto' }}
            onClick={() => setPeggingOpen(false)}
            aria-hidden
          />
          <div
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => e.stopPropagation()}
            style={{
              position: 'relative',
              zIndex: 10,
              width: peggingPanelWidth,
              maxWidth: '90vw',
              minWidth: 320,
              maxHeight: '100vh',
              overflow: 'auto',
              background: '#1c1c1e',
              color: '#e4e4e7',
              boxShadow: '-4px 0 24px rgba(0,0,0,0.4)',
              padding: '1.25rem',
              pointerEvents: 'auto',
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <div
              role="separator"
              aria-label="Resize pegging panel"
              onMouseDown={(e) => {
                e.preventDefault();
                e.stopPropagation();
                peggingResizeRef.current = { startX: e.clientX, startW: peggingPanelWidth };
                setPeggingResizing(true);
              }}
              style={{
                position: 'absolute',
                left: 0,
                top: 0,
                bottom: 0,
                width: 6,
                cursor: 'col-resize',
                zIndex: 11,
              }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
              <h3 style={{ margin: 0, color: '#fafafa' }}>{peggingTitle}</h3>
              <button type="button" onClick={() => setPeggingOpen(false)} style={{ padding: '6px 12px', background: '#2d2d30', color: '#e4e4e7', border: '1px solid #3d3d40', borderRadius: 6, cursor: 'pointer' }}>Close</button>
            </div>
            {peggingData?.direction === 'demand-to-supply' && (peggingData.demand_allocated_qty != null || peggingData.demand_requested_qty != null) && (
              <>
                <p style={{ margin: 0, marginBottom: '0.25rem', color: '#a1a1aa', fontSize: '0.9rem' }}>
                  Allocated: <strong style={{ color: '#fafafa' }}>{Number(peggingData.demand_allocated_qty ?? 0).toLocaleString()}</strong>
                  {peggingData.demand_requested_qty != null && (
                    <> (requested: {Number(peggingData.demand_requested_qty).toLocaleString()})</>
                  )}
                </p>
                <p style={{ margin: 0, marginBottom: '1rem', color: '#71717a', fontSize: '0.8rem' }}>
                  First level below = direct contributors; their edge qtys sum to Allocated above. Deeper levels: qty = flow along that edge (· = leaf, no further expansion).
                </p>
              </>
            )}
            {peggingLoading && <p style={{ color: '#a1a1aa' }}>Loading…</p>}
            {!peggingLoading && peggingData && !peggingTreeReady && (
              <div style={{ fontSize: '0.9rem' }}>
                <p><strong>Nodes:</strong> {(Array.isArray(peggingData.nodes) ? peggingData.nodes : []).length} &nbsp; <strong>Edges:</strong> {(Array.isArray(peggingData.edges) ? peggingData.edges : []).length}</p>
                <p style={{ color: '#a1a1aa' }}>Building tree…</p>
              </div>
            )}
            {!peggingLoading && peggingData && peggingTreeReady && peggingGraph && (
              <PeggingTree
                graph={peggingGraph}
                expanded={peggingExpanded}
                onExpand={(pathKey, isExpanding) => {
                  setPeggingExpanded((prev) => {
                    const next = new Set(prev);
                    if (next.has(pathKey)) next.delete(pathKey);
                    else next.add(pathKey);
                    return next;
                  });
                  if (isExpanding) {
                    setPeggingChildrenAllowedFor((prev) => new Set(prev).add(pathKey));
                  }
                }}
                childrenAllowedFor={peggingChildrenAllowedFor}
                direction={peggingData.direction}
                servedDemandIds={peggingData.direction === 'supply-to-demand' && peggingData.critical_paths_by_demand?.paths_by_demand
                  ? peggingData.critical_paths_by_demand.paths_by_demand.filter((p: { path?: string[] }) => p.path?.length).map((p: { demand_id: string }) => p.demand_id)
                  : []}
              />
            )}
            {!peggingLoading && !peggingData && <p style={{ color: '#a1a1aa' }}>Could not load pegging.</p>}
          </div>
        </div>,
        document.body
      )}
      <section style={{ marginTop: '2rem' }}>
        <h2>Manual overrides</h2>
        <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center', marginBottom: '1rem' }}>
          <select value={overrideForm.entity_type} onChange={(e) => setOverrideForm({ ...overrideForm, entity_type: e.target.value })}>
            <option value="supply">supply</option>
            <option value="demand">demand</option>
            <option value="allocation">allocation</option>
          </select>
          <input placeholder="Entity key" value={overrideForm.entity_key} onChange={(e) => setOverrideForm({ ...overrideForm, entity_key: e.target.value })} />
          <input placeholder='{"quantity": 100}' value={overrideForm.payload} onChange={(e) => setOverrideForm({ ...overrideForm, payload: e.target.value })} style={{ minWidth: 160 }} />
          <button onClick={handleAddOverride}>Add override</button>
        </div>
        <table>
          <thead>
            <tr><th>Type</th><th>Key</th><th>Payload</th><th></th></tr>
          </thead>
          <tbody>
            {overrides.map((o) => (
              <tr key={o.id}>
                <td>{o.entity_type}</td>
                <td>{o.entity_key}</td>
                <td><code>{JSON.stringify(o.payload)}</code></td>
                <td><button className="danger" onClick={() => handleDeleteOverride(o.id)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {overrides.length === 0 && <p>No overrides. Add one to fix supply/demand quantities or assignments.</p>}
      </section>
    </div>
  );
}
