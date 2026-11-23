import React, { useMemo } from 'react';
import type { DisplayDictionary } from '../../../api/client';
import { getStatusBadgeMeta } from '../../i18nErrorMap';

interface FsmDiagramProps {
  statusField: string | null;
  initialStatuses: string[];
  transitions: Record<string, string[]>;
  terminalStatuses: string[];
  display?: DisplayDictionary;
}

interface PositionedState {
  id: string;
  label: string;
  x: number;
  y: number;
  isInitial: boolean;
  isTerminal: boolean;
}

export const FsmDiagram: React.FC<FsmDiagramProps> = ({
  statusField,
  initialStatuses,
  transitions,
  terminalStatuses,
  display,
}) => {
  const states = useMemo(() => {
    const ordered: string[] = [];
    const seen = new Set<string>();
    const pushState = (st: string) => {
      if (st && !seen.has(st)) {
        seen.add(st);
        ordered.push(st);
      }
    };

    // BFS from initial states to preserve topological lifecycle ordering
    const queue: string[] = [];
    for (const s of initialStatuses) {
      pushState(s);
      queue.push(s);
    }
    while (queue.length > 0) {
      const cur = queue.shift()!;
      for (const nxt of transitions[cur] || []) {
        if (!seen.has(nxt)) {
          pushState(nxt);
          queue.push(nxt);
        }
      }
    }
    for (const [src, dsts] of Object.entries(transitions)) {
      pushState(src);
      for (const dst of dsts) {
        pushState(dst);
      }
    }
    return ordered;
  }, [initialStatuses, transitions]);

  const edges = useMemo(() => {
    const list: Array<{ src: string; dst: string }> = [];
    for (const [src, dsts] of Object.entries(transitions)) {
      for (const dst of dsts) {
        list.push({ src, dst });
      }
    }
    return list;
  }, [transitions]);

  if (!statusField && states.length === 0) {
    return (
      <div
        data-testid="fsm-empty-state"
        className="rounded-xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-5 text-center text-xs text-slate-400"
      >
        该模型实体未配置状态机（无受控状态跃迁矩阵）
      </div>
    );
  }

  const nodeW = 118;
  const nodeH = 44;
  const maxCols = Math.min(3, Math.max(1, states.length));
  const colGap = 158;
  const rowGap = 86;
  const padX = 28;
  const padY = 34;

  const initialSet = new Set(initialStatuses);
  const terminalSet = new Set(terminalStatuses);

  const positioned: PositionedState[] = states.map((st, idx) => {
    const row = Math.floor(idx / maxCols);
    const colRaw = idx % maxCols;
    const col = row % 2 === 1 ? maxCols - 1 - colRaw : colRaw;
    return {
      id: st,
      label: getStatusBadgeMeta(st, display).label,
      x: padX + col * colGap,
      y: padY + row * rowGap,
      isInitial: initialSet.has(st),
      isTerminal: terminalSet.has(st),
    };
  });

  const posMap = new Map(positioned.map((p) => [p.id, p]));
  const totalRows = Math.max(1, Math.ceil(states.length / maxCols));
  const svgWidth = padX * 2 + (maxCols - 1) * colGap + nodeW;
  const svgHeight = padY * 2 + (totalRows - 1) * rowGap + nodeH;
  const edgeSet = new Set(edges.map((e) => `${e.src}->${e.dst}`));

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/50 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500">
        <span>
          状态字段：<code className="rounded bg-white px-1.5 py-0.5 font-mono text-slate-700 ring-1 ring-slate-200">{statusField || 'status'}</code>
        </span>
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-full bg-emerald-500" /> 起始态
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm border border-slate-600 bg-slate-200" /> 终态
          </span>
        </div>
      </div>

      <div className="overflow-x-auto">
        <svg
          data-testid="fsm-diagram-svg"
          viewBox={`0 0 ${svgWidth} ${svgHeight}`}
          className="mx-auto h-auto w-full max-w-full"
          style={{ minWidth: `${Math.min(svgWidth, 340)}px` }}
        >
          <defs>
            <marker
              id="fsm-arrow"
              viewBox="0 0 10 10"
              refX="8.5"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="#475569" />
            </marker>
          </defs>

          {edges.map(({ src, dst }, i) => {
            const a = posMap.get(src);
            const b = posMap.get(dst);
            if (!a || !b) return null;

            if (src === dst) {
              const cx = a.x + nodeW / 2;
              const topY = a.y;
              return (
                <path
                  key={`${src}->${dst}-${i}`}
                  d={`M ${cx - 16} ${topY} C ${cx - 20} ${topY - 26}, ${cx + 20} ${topY - 26}, ${cx + 16} ${topY - 2}`}
                  fill="none"
                  stroke="#64748B"
                  strokeWidth={1.5}
                  markerEnd="url(#fsm-arrow)"
                />
              );
            }

            const ax = a.x + nodeW / 2;
            const ay = a.y + nodeH / 2;
            const bx = b.x + nodeW / 2;
            const by = b.y + nodeH / 2;
            const dx = bx - ax;
            const dy = by - ay;
            const dist = Math.hypot(dx, dy) || 1;
            const ux = dx / dist;
            const uy = dy / dist;

            // Clip start/end cleanly to node boundaries
            const startX = ax + ux * (Math.abs(ux) > Math.abs(uy) ? nodeW / 2 + 2 : (nodeH / 2 + 2) * Math.abs(ux / (uy || 0.01)));
            const startY = ay + uy * (Math.abs(uy) >= Math.abs(ux) ? nodeH / 2 + 2 : nodeH / 3);
            const endX = bx - ux * (Math.abs(ux) > Math.abs(uy) ? nodeW / 2 + 4 : (nodeH / 2 + 4) * Math.abs(ux / (uy || 0.01)));
            const endY = by - uy * (Math.abs(uy) >= Math.abs(ux) ? nodeH / 2 + 4 : nodeH / 3);

            const hasReverse = edgeSet.has(`${dst}->${src}`);
            const curveSign = hasReverse ? 1 : src < dst ? 0.4 : -0.4;
            const mx = (startX + endX) / 2;
            const my = (startY + endY) / 2;
            const offset = (hasReverse ? 22 : Math.abs(dy) < 4 ? 14 : 18) * curveSign;
            const cx = mx - uy * offset;
            const cy = my + ux * offset;

            return (
              <path
                key={`${src}->${dst}-${i}`}
                d={`M ${startX.toFixed(1)} ${startY.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${endX.toFixed(1)} ${endY.toFixed(1)}`}
                fill="none"
                stroke="#64748B"
                strokeWidth={1.5}
                markerEnd="url(#fsm-arrow)"
              />
            );
          })}

          {positioned.map((st) => {
            const fill = st.isInitial
              ? '#ECFDF5'
              : st.isTerminal
              ? '#F1F5F9'
              : '#FFFFFF';
            const stroke = st.isInitial
              ? '#10B981'
              : st.isTerminal
              ? '#334155'
              : '#CBD5E1';
            return (
              <g key={st.id} data-testid={`fsm-state-${st.id}`}>
                <rect
                  x={st.x}
                  y={st.y}
                  width={nodeW}
                  height={nodeH}
                  rx={10}
                  fill={fill}
                  stroke={stroke}
                  strokeWidth={st.isInitial || st.isTerminal ? 2 : 1.2}
                />
                {st.isInitial && (
                  <circle cx={st.x + 12} cy={st.y + 13} r={3.5} fill="#10B981" />
                )}
                <text
                  x={st.x + nodeW / 2}
                  y={st.y + 18}
                  textAnchor="middle"
                  fontSize="11"
                  fontWeight="600"
                  fill="#0F172A"
                >
                  {st.label}
                </text>
                <text
                  x={st.x + nodeW / 2}
                  y={st.y + 33}
                  textAnchor="middle"
                  fontSize="9.5"
                  fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
                  fill="#64748B"
                >
                  {st.id}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
};
