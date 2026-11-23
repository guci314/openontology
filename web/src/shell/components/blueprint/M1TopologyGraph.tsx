import React, { useMemo, useState } from 'react';
import { GitBranch, RotateCcw, Search, ZoomIn, ZoomOut } from 'lucide-react';
import type { BlueprintEntityMeta, BlueprintLinkMeta } from '../../../api/client';

interface M1TopologyGraphProps {
  objectTypes: BlueprintEntityMeta[];
  links: BlueprintLinkMeta[];
  selectedType: string;
  onSelectType: (typeName: string) => void;
}

interface PositionedNode {
  meta: BlueprintEntityMeta;
  x: number;
  y: number;
}

export const M1TopologyGraph: React.FC<M1TopologyGraphProps> = ({
  objectTypes,
  links,
  selectedType,
  onSelectType,
}) => {
  const [search, setSearch] = useState('');
  const [hoveredType, setHoveredType] = useState<string | null>(null);
  const [zoom, setZoom] = useState<number>(1);

  const focusType = hoveredType || selectedType;
  const searchNeedle = search.trim().toLowerCase();

  const neighborSet = useMemo(() => {
    const s = new Set<string>();
    if (!focusType) return s;
    s.add(focusType);
    for (const l of links) {
      if (l.source === focusType) s.add(l.target);
      if (l.target === focusType) s.add(l.source);
    }
    return s;
  }, [focusType, links]);

  const { positioned, nodeW, nodeH, svgWidth, svgHeight } = useMemo(() => {
    const nW = 192;
    const nH = 74;
    const count = objectTypes.length;
    if (count === 0) {
      return { positioned: [] as PositionedNode[], nodeW: nW, nodeH: nH, svgWidth: 640, svgHeight: 240 };
    }

    if (count <= 4) {
      const gap = 92;
      const padX = 48;
      const padY = 48;
      // Arrange referenced hubs cleanly toward the right
      const inDeg = new Map<string, number>();
      const outDeg = new Map<string, number>();
      for (const ot of objectTypes) {
        inDeg.set(ot.name, 0);
        outDeg.set(ot.name, 0);
      }
      for (const l of links) {
        if (l.source !== l.target) {
          outDeg.set(l.source, (outDeg.get(l.source) || 0) + 1);
          inDeg.set(l.target, (inDeg.get(l.target) || 0) + 1);
        }
      }
      const sorted = [...objectTypes].sort((a, b) => {
        const scoreA = (inDeg.get(a.name) || 0) - (outDeg.get(a.name) || 0);
        const scoreB = (inDeg.get(b.name) || 0) - (outDeg.get(b.name) || 0);
        if (scoreA !== scoreB) return scoreA - scoreB;
        return a.name.localeCompare(b.name);
      });
      const list: PositionedNode[] = sorted.map((meta, idx) => ({
        meta,
        x: padX + idx * (nW + gap),
        y: padY,
      }));
      return {
        positioned: list,
        nodeW: nW,
        nodeH: nH,
        svgWidth: Math.max(580, padX * 2 + sorted.length * nW + Math.max(0, sorted.length - 1) * gap),
        svgHeight: padY * 2 + nH + 40,
      };
    }

    // Multi-column stratified layout based on outgoing/incoming link flow
    const outMap = new Map<string, string[]>();
    const inDeg = new Map<string, number>();
    for (const ot of objectTypes) {
      outMap.set(ot.name, []);
      inDeg.set(ot.name, 0);
    }
    for (const l of links) {
      if (l.source !== l.target && outMap.has(l.source) && inDeg.has(l.target)) {
        outMap.get(l.source)!.push(l.target);
        inDeg.set(l.target, (inDeg.get(l.target) || 0) + 1);
      }
    }

    const depth = new Map<string, number>();
    for (const ot of objectTypes) depth.set(ot.name, 0);
    for (let iter = 0; iter < Math.min(8, objectTypes.length); iter += 1) {
      let changed = false;
      for (const l of links) {
        if (l.source === l.target) continue;
        const dSrc = depth.get(l.source) ?? 0;
        const dDst = depth.get(l.target) ?? 0;
        if (dDst <= dSrc && dSrc < 4) {
          depth.set(l.target, dSrc + 1);
          changed = true;
        }
      }
      if (!changed) break;
    }

    // Sort all objects cleanly by hierarchical depth, FSM presence, and name
    const ordered = [...objectTypes].sort((a, b) => {
      const da = depth.get(a.name) ?? 0;
      const db = depth.get(b.name) ?? 0;
      if (da !== db) return da - db;
      const degA = (inDeg.get(a.name) || 0) + (outMap.get(a.name)?.length || 0);
      const degB = (inDeg.get(b.name) || 0) + (outMap.get(b.name)?.length || 0);
      if (degB !== degA) return degB - degA;
      return a.name.localeCompare(b.name);
    });

    const maxPerCol = 6;
    const numCols = Math.ceil(ordered.length / maxPerCol);
    const colGap = 72;
    const rowGap = 24;
    const padX = 36;
    const padY = 34;

    const list: PositionedNode[] = ordered.map((meta, idx) => {
      const col = Math.floor(idx / maxPerCol);
      const row = idx % maxPerCol;
      return {
        meta,
        x: padX + col * (nW + colGap),
        y: padY + row * (nH + rowGap),
      };
    });

    const maxRowsUsed = Math.min(maxPerCol, ordered.length);
    return {
      positioned: list,
      nodeW: nW,
      nodeH: nH,
      svgWidth: padX * 2 + numCols * nW + Math.max(0, numCols - 1) * colGap,
      svgHeight: padY * 2 + maxRowsUsed * nH + Math.max(0, maxRowsUsed - 1) * rowGap,
    };
  }, [objectTypes, links]);

  const posMap = useMemo(
    () => new Map(positioned.map((p) => [p.meta.name, p])),
    [positioned]
  );

  // Pair offsets to avoid overlapping parallel links between same two nodes
  const linkOffsets = useMemo(() => {
    const pairCounts = new Map<string, number>();
    const pairSeen = new Map<string, number>();
    for (const l of links) {
      const key = [l.source, l.target].sort().join('::');
      pairCounts.set(key, (pairCounts.get(key) || 0) + 1);
    }
    return links.map((l) => {
      const key = [l.source, l.target].sort().join('::');
      const total = pairCounts.get(key) || 1;
      const idx = pairSeen.get(key) || 0;
      pairSeen.set(key, idx + 1);
      if (total <= 1) return 0;
      return (idx - (total - 1) / 2) * 26;
    });
  }, [links]);

  const alwaysShowEdgeLabels = links.length <= 6;

  return (
    <div className="flex flex-col rounded-xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2.5 border-b border-slate-100 px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
            <GitBranch className="h-3.5 w-3.5" />
          </div>
          <div>
            <h2 className="text-sm font-semibold text-slate-900">
              本体模型拓扑图 ({objectTypes.length} 类 · {links.length} 条关系)
            </h2>
            <p className="text-[11px] text-slate-500">
              点击任意节点查看状态机与字段契约，悬停高亮关联邻域
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-44">
            <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
            <input
              type="text"
              data-testid="blueprint-node-search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="检索模型节点..."
              className="w-full rounded-lg border border-slate-200 bg-slate-50 py-1.5 pl-8 pr-2.5 text-xs text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none"
            />
          </div>

          <div className="inline-flex items-center rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-xs">
            <button
              type="button"
              aria-label="缩小画布"
              onClick={() => setZoom((z) => Math.max(0.6, Number((z - 0.15).toFixed(2))))}
              className="rounded-md p-1 text-slate-600 hover:bg-white hover:text-slate-900"
            >
              <ZoomOut className="h-3.5 w-3.5" />
            </button>
            <span className="px-2 font-mono text-[11px] text-slate-700">
              {Math.round(zoom * 100)}%
            </span>
            <button
              type="button"
              aria-label="放大画布"
              onClick={() => setZoom((z) => Math.min(1.45, Number((z + 0.15).toFixed(2))))}
              className="rounded-md p-1 text-slate-600 hover:bg-white hover:text-slate-900"
            >
              <ZoomIn className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              aria-label="重置视图"
              onClick={() => {
                setZoom(1);
                setSearch('');
              }}
              className="rounded-md p-1 text-slate-600 hover:bg-white hover:text-slate-900"
            >
              <RotateCcw className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>

      {/* Interactive SVG Viewport */}
      <div className="relative h-[460px] overflow-auto bg-[#F8FAFC] p-3">
        <div
          style={{
            width: `${Math.round(svgWidth * zoom)}px`,
            height: `${Math.round(svgHeight * zoom)}px`,
            minWidth: '100%',
            minHeight: '100%',
          }}
        >
          <svg
            data-testid="blueprint-topology-svg"
            viewBox={`0 0 ${svgWidth} ${svgHeight}`}
            width={Math.round(svgWidth * zoom)}
            height={Math.round(svgHeight * zoom)}
            className="mx-auto block select-none"
          >
            <defs>
              <marker
                id="bp-arrow-muted"
                viewBox="0 0 10 10"
                refX="8.5"
                refY="5"
                markerWidth="5.5"
                markerHeight="5.5"
                orient="auto-start-reverse"
              >
                <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="#94A3B8" />
              </marker>
              <marker
                id="bp-arrow-active"
                viewBox="0 0 10 10"
                refX="8.5"
                refY="5"
                markerWidth="6"
                markerHeight="6"
                orient="auto-start-reverse"
              >
                <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="#2563EB" />
              </marker>
            </defs>

            {/* Edges rendered first */}
            {links.map((link, idx) => {
              const srcNode = posMap.get(link.source);
              const dstNode = posMap.get(link.target);
              if (!srcNode || !dstNode) return null;

              const isHighlighted =
                focusType === link.source || focusType === link.target;
              const dimmed = Boolean(focusType) && !isHighlighted;
              const pairShift = linkOffsets[idx] || 0;

              if (link.source === link.target) {
                const topX = srcNode.x + nodeW - 24;
                const topY = srcNode.y;
                return (
                  <g key={`${link.name}-${idx}`} opacity={dimmed ? 0.16 : 1}>
                    <path
                      d={`M ${topX - 18} ${topY} C ${topX - 24} ${topY - 28}, ${topX + 24} ${topY - 28}, ${topX + 18} ${topY - 2}`}
                      fill="none"
                      stroke={isHighlighted ? '#2563EB' : '#94A3B8'}
                      strokeWidth={isHighlighted ? 2 : 1.2}
                      markerEnd={
                        isHighlighted ? 'url(#bp-arrow-active)' : 'url(#bp-arrow-muted)'
                      }
                    />
                    {(isHighlighted || alwaysShowEdgeLabels) && (
                      <text
                        x={topX}
                        y={topY - 24}
                        textAnchor="middle"
                        fontSize="10"
                        fontWeight="600"
                        fill="#1E40AF"
                      >
                        {link.field} ({link.cardinality_label})
                      </text>
                    )}
                  </g>
                );
              }

              const srcCx = srcNode.x + nodeW / 2;
              const srcCy = srcNode.y + nodeH / 2;
              const dstCx = dstNode.x + nodeW / 2;
              const dstCy = dstNode.y + nodeH / 2;
              const dx = dstCx - srcCx;
              const dy = dstCy - srcCy;
              const len = Math.hypot(dx, dy) || 1;
              const ux = dx / len;
              const uy = dy / len;

              // Determine clean entry/exit anchor points on node sides
              let startX = srcCx;
              let startY = srcCy;
              let endX = dstCx;
              let endY = dstCy;
              if (Math.abs(dx) >= Math.abs(dy)) {
                startX = dx >= 0 ? srcNode.x + nodeW : srcNode.x;
                endX = dx >= 0 ? dstNode.x - 3 : dstNode.x + nodeW + 3;
                startY = srcCy + pairShift * 0.25;
                endY = dstCy + pairShift * 0.25;
              } else {
                startY = dy >= 0 ? srcNode.y + nodeH : srcNode.y;
                endY = dy >= 0 ? dstNode.y - 3 : dstNode.y + nodeH + 3;
                startX = srcCx + pairShift * 0.25;
                endX = dstCx + pairShift * 0.25;
              }

              const mx = (startX + endX) / 2;
              const my = (startY + endY) / 2;
              const curveMag = (pairShift !== 0 ? pairShift : Math.abs(dx) < 8 ? 28 : 16);
              const ctrlX = mx - uy * curveMag;
              const ctrlY = my + ux * curveMag;
              const labelX = 0.25 * startX + 0.5 * ctrlX + 0.25 * endX;
              const labelY = 0.25 * startY + 0.5 * ctrlY + 0.25 * endY;

              return (
                <g key={`${link.name}-${idx}`} opacity={dimmed ? 0.14 : isHighlighted ? 1 : 0.65}>
                  <path
                    d={`M ${startX.toFixed(1)} ${startY.toFixed(1)} Q ${ctrlX.toFixed(1)} ${ctrlY.toFixed(1)} ${endX.toFixed(1)} ${endY.toFixed(1)}`}
                    fill="none"
                    stroke={isHighlighted ? '#2563EB' : '#94A3B8'}
                    strokeWidth={isHighlighted ? 2.1 : 1.15}
                    markerEnd={
                      isHighlighted ? 'url(#bp-arrow-active)' : 'url(#bp-arrow-muted)'
                    }
                  />
                  {(isHighlighted || alwaysShowEdgeLabels) && (
                    <g transform={`translate(${labelX.toFixed(1)}, ${labelY.toFixed(1)})`}>
                      <rect
                        x={-48}
                        y={-10}
                        width={96}
                        height={18}
                        rx={5}
                        fill="#FFFFFF"
                        stroke="#BFDBFE"
                        strokeWidth={1}
                      />
                      <text
                        x={0}
                        y={2.5}
                        textAnchor="middle"
                        fontSize="9.5"
                        fontWeight="600"
                        fill="#1D4ED8"
                      >
                        {link.field} · {link.cardinality_label}
                      </text>
                    </g>
                  )}
                </g>
              );
            })}

            {/* Entity Nodes */}
            {positioned.map(({ meta, x, y }) => {
              const isSelected = meta.name === selectedType;
              const isHovered = meta.name === hoveredType;
              const matchesSearch =
                !searchNeedle ||
                meta.name.toLowerCase().includes(searchNeedle) ||
                meta.display_label.toLowerCase().includes(searchNeedle);
              const isNeighbor = neighborSet.has(meta.name);
              const hasFsm = Boolean(meta.status_field || Object.keys(meta.transitions).length > 0);
              const hasDevices = meta.attached_devices.length > 0;

              let opacity = 1;
              if (searchNeedle && !matchesSearch) opacity = 0.24;
              else if (focusType && !isNeighbor) opacity = 0.42;

              const strokeColor = isSelected
                ? '#0F172A'
                : isHovered
                ? '#2563EB'
                : matchesSearch && searchNeedle
                ? '#10B981'
                : '#CBD5E1';

              return (
                <g
                  key={meta.name}
                  data-testid={`blueprint-node-${meta.name}`}
                  transform={`translate(${x}, ${y})`}
                  opacity={opacity}
                  className="cursor-pointer"
                  onMouseEnter={() => setHoveredType(meta.name)}
                  onMouseLeave={() => setHoveredType((prev) => (prev === meta.name ? null : prev))}
                  onClick={() => onSelectType(meta.name)}
                >
                  <rect
                    x={0}
                    y={0}
                    width={nodeW}
                    height={nodeH}
                    rx={12}
                    fill={isSelected ? '#EFF6FF' : '#FFFFFF'}
                    stroke={strokeColor}
                    strokeWidth={isSelected ? 2.2 : isHovered ? 1.8 : 1.2}
                  />
                  {/* Title */}
                  <text
                    x={12}
                    y={22}
                    fontSize="12"
                    fontWeight="700"
                    fill="#0F172A"
                  >
                    {meta.display_label.length > 13
                      ? `${meta.display_label.slice(0, 12)}…`
                      : meta.display_label}
                  </text>
                  {/* Mono ID */}
                  <text
                    x={12}
                    y={38}
                    fontSize="10"
                    fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
                    fill="#64748B"
                  >
                    {meta.name.length > 22 ? `${meta.name.slice(0, 21)}…` : meta.name}
                  </text>

                  {/* Footer Pills */}
                  <text
                    x={12}
                    y={60}
                    fontSize="10"
                    fill="#475569"
                  >
                    {meta.fields.length} 字段 · {meta.visible_count} 条
                  </text>

                  {hasFsm && (
                    <g transform={`translate(${nodeW - 50}, 48)`}>
                      <rect width={40} height={17} rx={5} fill="#ECFDF5" stroke="#A7F3D0" />
                      <text
                        x={20}
                        y={11.5}
                        textAnchor="middle"
                        fontSize="9.5"
                        fontWeight="600"
                        fill="#047857"
                      >
                        状态机
                      </text>
                    </g>
                  )}

                  {hasDevices && (
                    <g transform={`translate(${nodeW - 50}, 10)`}>
                      <rect width={40} height={16} rx={5} fill="#E0F2FE" stroke="#BAE6FD" />
                      <text
                        x={20}
                        y={11}
                        textAnchor="middle"
                        fontSize="9"
                        fontWeight="600"
                        fill="#0369A1"
                      >
                        外设
                      </text>
                    </g>
                  )}
                </g>
              );
            })}
          </svg>
        </div>
      </div>
    </div>
  );
};
