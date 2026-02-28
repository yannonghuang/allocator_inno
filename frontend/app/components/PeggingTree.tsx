'use client';

import React from 'react';

/** Unique key for a node at a specific path in the tree (so same nodeId at different positions don't share state). */
export const PATH_KEY_SEP = '\u001e';

export function pathKeyFromPath(pathFromRoot: string[]): string {
  return pathFromRoot.join(PATH_KEY_SEP);
}

export type PeggingGraph = {
  nodeById: Record<string, { id: string; label: string; type: string; qty?: number; demand_qty?: number; demand_count?: number }>;
  pathSet: Set<string>;
  rootId: string;
  getChildren: (nodeId: string) => { nextId: string; qty: number }[];
  nodes: { id: string; label: string; type: string; qty?: number; demand_qty?: number; demand_count?: number }[];
  edges: { from: string; to: string; qty: number }[];
};

type Props = {
  graph: PeggingGraph;
  expanded: Set<string>;
  onExpand: (pathKey: string, isExpanding: boolean, childCount?: number) => void;
  childrenAllowedFor: Set<string>;
  direction?: string;
  servedDemandIds?: string[];
};

function PeggingNodeRow({
  graph,
  nodeId,
  depth,
  edgeQty,
  pathFromRoot,
  expanded,
  onExpand,
  childrenAllowedFor,
  direction,
  ancestorIds = new Set<string>(),
}: {
  graph: PeggingGraph;
  nodeId: string;
  depth: number;
  edgeQty?: number;
  pathFromRoot: string[];
  expanded: Set<string>;
  onExpand: (pathKey: string, isExpanding: boolean, childCount?: number) => void;
  childrenAllowedFor: Set<string>;
  direction?: string;
  ancestorIds?: Set<string>;
}) {
  const { nodeById, pathSet, getChildren } = graph;
  const node = nodeById[nodeId];
  if (!node) return null;
  const pathKey = pathKeyFromPath(pathFromRoot);
  const rawChildren = getChildren(nodeId);
  // Break cycles (e.g. move 1000→2000 and 2000→1000): don't show a child that is already an ancestor
  const children = rawChildren.filter((c) => !ancestorIds.has(c.nextId));
  const hasChildren = children.length > 0;
  const isExpanded = expanded.has(pathKey);
  const onPath = pathSet.has(nodeId);
  const parentId = pathFromRoot.length >= 2 ? pathFromRoot[pathFromRoot.length - 2] : null;
  // Node id may be product|location|period (inventory) or product|location
  const nodeParts = nodeId.split('|');
  const nodeProduct = nodeParts[0] ?? '';
  const nodeLoc = nodeParts[1] ?? '';
  const isSupplyToDemand = direction === 'supply-to-demand';
  let typeLabel: string | null = null;
  if (node.type === 'demand') typeLabel = 'demand';
  else if (isSupplyToDemand) {
    // Supply pegging: root = supply (raw material); children = made (make) or moved (same product, different loc)
    if (parentId) {
      const parentParts = parentId.split('|');
      const parentProduct = parentParts[0] ?? '';
      const parentLoc = parentParts[1] ?? '';
      if (nodeProduct !== parentProduct) typeLabel = 'made';
      else if ((nodeLoc ?? '') !== (parentLoc ?? '')) typeLabel = 'moved';
    }
    if (typeLabel == null) typeLabel = node.type === 'component' ? 'supply' : node.type === 'variant' ? 'made' : null;
  } else {
    // Demand pegging: root = made; leaves = supply (raw inputs); intermediaries = made or moved, never supply
    if (!parentId) typeLabel = 'made';
    else {
      const parentParts = parentId.split('|');
      const parentProduct = parentParts[0] ?? '';
      const parentLoc = parentParts[1] ?? '';
      const sameProductDiffLoc = nodeProduct === parentProduct && (nodeLoc ?? '') !== (parentLoc ?? '');
      if (hasChildren) typeLabel = sameProductDiffLoc ? 'moved' : 'made';
      else typeLabel = sameProductDiffLoc ? 'moved' : 'supply';
    }
  }
  const summary = (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span style={{ color: onPath ? '#a78bfa' : '#e4e4e7', fontWeight: onPath ? 600 : 400 }}>
        {node.label.replace('|', '@')}
      </span>
      {typeLabel != null && (
        <span style={{ fontSize: '0.75em', color: '#71717a', background: '#2d2d30', padding: '1px 6px', borderRadius: 4 }}>
          {typeLabel}
        </span>
      )}
      {/* edgeQty = flow from this node to its parent (to demand or to next level up) */}
      {edgeQty != null && node.type !== 'demand' && (
        <span style={{ color: '#71717a', fontSize: '0.85em' }} title="Flow from this node to parent">to parent: {edgeQty}</span>
      )}
      {edgeQty != null && node.type === 'demand' && (
        <span style={{ color: '#71717a', fontSize: '0.85em' }}>allocated: {edgeQty}{node.demand_qty != null && Number(node.demand_qty) > 0 ? ` (requested: ${node.demand_qty})` : ''}</span>
      )}
      {/* Sum of flows from children into this node; so "from below" matches subordinates */}
      {hasChildren && (
        <span style={{ color: '#a1a1aa', fontSize: '0.85em' }} title="Sum of edge qtys from rows below (flow into this node)">from below: {children.reduce((s, c) => s + c.qty, 0).toLocaleString()}</span>
      )}
      {depth === 0 && node.qty != null && node.qty > 0 && node.type !== 'demand' && (
        <span style={{ color: '#71717a', fontSize: '0.85em' }} title="Total inventory at this node (not just for this demand)">inventory: {node.qty}</span>
      )}
      {depth === 0 && node.demand_qty != null && node.demand_qty > 0 && (
        <span style={{ color: '#71717a', fontSize: '0.85em' }} title="Sum of requested qty across all demands for this product@location">
          total demand (all): {node.demand_qty}{node.demand_count != null && node.demand_count > 0 ? ` (${node.demand_count} demands)` : ''}
        </span>
      )}
    </span>
  );
  const childrenAllowed = childrenAllowedFor.has(pathKey);
  const showChildren = hasChildren && isExpanded && childrenAllowed;
  const ancestorIdsForChildren = new Set([...Array.from(ancestorIds), nodeId]);

  const rowStyle: React.CSSProperties = {
    cursor: 'pointer',
    padding: '6px 8px',
    margin: 0,
    marginTop: 4,
    border: 'none',
    borderRadius: 4,
    background: 'transparent',
    color: 'inherit',
    fontSize: 'inherit',
    font: 'inherit',
    textAlign: 'left',
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    minHeight: 28,
    width: '100%',
    boxSizing: 'border-box',
    pointerEvents: 'auto',
  };

  const indentPx = 16;
  // One level of indent relative to container (so total indent = depth * indentPx without stacking)
  const levelIndent = depth === 0 ? 0 : indentPx;
  return (
    <div style={{ marginLeft: levelIndent, width: '100%', boxSizing: 'border-box', minWidth: 0 }}>
      <div
        data-pegging-path-key={pathKey}
        data-pegging-child-count={hasChildren ? children.length : 0}
        role="button"
        tabIndex={0}
        style={rowStyle}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onExpand(pathKey, !isExpanded, hasChildren ? children.length : undefined);
          }
        }}
      >
        <span style={{ userSelect: 'none', flexShrink: 0, width: 14 }}>{hasChildren ? (isExpanded ? '▾' : '▸') : '·'}</span>
        {summary}
        {hasChildren && <span style={{ color: '#71717a', fontSize: '0.8em' }}>({children.length})</span>}
      </div>
      {showChildren && children.length > 0 && (
        <div style={{ borderLeft: '1px solid #3d3d40', marginLeft: 0, paddingLeft: 0, width: '100%', boxSizing: 'border-box' }}>
          {children.map(({ nextId, qty }) => {
            const childPath = [...pathFromRoot, nextId];
            return (
              <div key={pathKeyFromPath(childPath)} style={{ marginTop: 2, width: '100%' }}>
                <PeggingNodeRow
                  graph={graph}
                  nodeId={nextId}
                  depth={depth + 1}
                  edgeQty={qty}
                  pathFromRoot={childPath}
                  expanded={expanded}
                  onExpand={onExpand}
                  childrenAllowedFor={childrenAllowedFor}
                  direction={direction}
                  ancestorIds={ancestorIdsForChildren}
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function PeggingTree({
  graph,
  expanded,
  onExpand,
  childrenAllowedFor,
  direction,
  servedDemandIds = [],
}: Props) {
  const { rootId, nodes, edges, getChildren } = graph;

  const handleTreeClick = (e: React.MouseEvent) => {
    const el = (e.target as HTMLElement).closest('[data-pegging-path-key]');
    if (!el) return;
    e.stopPropagation();
    const pathKey = el.getAttribute('data-pegging-path-key');
    if (!pathKey) return;
    const isExpanded = expanded.has(pathKey);
    const childCountStr = el.getAttribute('data-pegging-child-count');
    const childCount = childCountStr != null ? parseInt(childCountStr, 10) : undefined;
    onExpand(pathKey, !isExpanded, childCount != null && childCount > 0 ? childCount : undefined);
  };

  return (
    <div style={{ fontSize: '0.9rem' }}>
      <p><strong>Nodes:</strong> {nodes.length} &nbsp; <strong>Edges:</strong> {edges.length}</p>
      {direction === 'supply-to-demand' && servedDemandIds.length > 0 && (
        <p style={{ marginTop: '0.5rem', color: '#a1a1aa', fontSize: '0.85em' }}>
          <strong>Critical path serves demands:</strong> {servedDemandIds.join(', ')}
        </p>
      )}
      {rootId && (
        <div style={{ marginTop: '0.75rem' }}>
          <strong>Tree</strong> <span style={{ color: '#71717a', fontSize: '0.85em' }}>(▸ expandable, · leaf. to parent = flow to level above; from below = sum of subordinates; they can differ when flow serves other demands or stays as inventory.)</span>
          <div
            style={{ marginTop: 6, padding: '0.5rem', background: '#252528', borderRadius: 8, maxHeight: '60vh', minHeight: 200, overflow: 'auto', minWidth: 0 }}
            onClick={handleTreeClick}
          >
            <PeggingNodeRow
              graph={graph}
              nodeId={rootId}
              depth={0}
              pathFromRoot={[rootId]}
              expanded={expanded}
              onExpand={onExpand}
              childrenAllowedFor={childrenAllowedFor}
              direction={direction}
            />
          </div>
        </div>
      )}
    </div>
  );
}
