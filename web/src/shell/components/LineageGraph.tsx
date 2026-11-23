import React from 'react';
import { ArrowRight, GitBranch } from 'lucide-react';
import type { ConsoleMeta, LineageNode, LineageResponse } from '../../api/client';
import { displayEntityName, getStatusBadgeMeta } from '../i18nErrorMap';

interface LineageGraphProps {
  meta: ConsoleMeta;
  lineage: LineageResponse | null;
  loading: boolean;
  onSelectNode: (entityType: string, recordId: string) => void;
}

const NodeCard: React.FC<{
  node: LineageNode;
  meta: ConsoleMeta;
  highlight?: boolean;
  onSelect: () => void;
}> = ({ node, meta, highlight, onSelect }) => {
  const statusVal = node.properties['status'] ?? node.properties['stage'];
  const badge = statusVal ? getStatusBadgeMeta(statusVal, meta.display) : null;

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`group flex w-full flex-col rounded-lg border p-2.5 text-left transition ${
        highlight
          ? 'border-blue-500 bg-blue-50/70 ring-1 ring-blue-500/20'
          : 'border-slate-200 bg-white hover:border-blue-300 hover:bg-slate-50'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-slate-500">
          {displayEntityName(node.object_type, meta.display)}
        </span>
        {badge && (
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-700">
            {badge.label}
          </span>
        )}
      </div>
      <span className="mt-1 truncate font-mono text-xs font-semibold text-slate-900 group-hover:text-blue-700">
        {node.object_id}
      </span>
      {node.title && node.title !== node.object_id && (
        <span className="mt-0.5 truncate text-[11px] text-slate-500">{node.title}</span>
      )}
    </button>
  );
};

export const LineageGraph: React.FC<LineageGraphProps> = ({
  meta,
  lineage,
  loading,
  onSelectNode,
}) => {
  if (loading) {
    return (
      <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-4 text-xs text-slate-500">
        正在检索当前单据的 1~2 跳上下游关联图谱...
      </div>
    );
  }
  if (!lineage) return null;

  const upstream = lineage.hops.upstream;
  const downstream = lineage.hops.downstream;

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/60 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-semibold text-slate-800">
          <GitBranch className="h-3.5 w-3.5 text-blue-700" />
          <span>单据上下游关联图谱（受权可见范围）</span>
        </div>
        <span className="text-[11px] text-slate-400">点击卡片可穿透跳转</span>
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3 md:items-start">
        <div className="space-y-2">
          <div className="text-[11px] font-medium text-slate-500">
            上游关联引用 ({upstream.length})
          </div>
          {upstream.length === 0 ? (
            <div className="rounded-lg border border-dashed border-slate-200 px-3 py-3 text-center text-[11px] text-slate-400">
              无上游引用的单据
            </div>
          ) : (
            upstream.map((n) => (
              <NodeCard
                key={`${n.object_type}:${n.object_id}`}
                node={n}
                meta={meta}
                onSelect={() => onSelectNode(n.object_type, n.object_id)}
              />
            ))
          )}
        </div>

        <div className="flex flex-col justify-center space-y-2">
          <div className="flex items-center justify-center gap-1 text-[11px] font-semibold text-blue-700">
            <ArrowRight className="hidden h-3 w-3 text-slate-400 md:inline" />
            <span>当前聚焦单据</span>
            <ArrowRight className="hidden h-3 w-3 text-slate-400 md:inline" />
          </div>
          <NodeCard
            node={lineage.root}
            meta={meta}
            highlight
            onSelect={() => onSelectNode(lineage.root.object_type, lineage.root.object_id)}
          />
        </div>

        <div className="space-y-2">
          <div className="text-[11px] font-medium text-slate-500">
            下游依赖档案 ({downstream.length})
          </div>
          {downstream.length === 0 ? (
            <div className="rounded-lg border border-dashed border-slate-200 px-3 py-3 text-center text-[11px] text-slate-400">
              无下游指向对象
            </div>
          ) : (
            downstream.map((n) => (
              <NodeCard
                key={`${n.object_type}:${n.object_id}`}
                node={n}
                meta={meta}
                onSelect={() => onSelectNode(n.object_type, n.object_id)}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
};
