import React from 'react';
import { Cpu, Database, KeyRound, Layers, Lock, ShieldCheck, Sparkles, Zap } from 'lucide-react';
import type {
  BlueprintActionMeta,
  BlueprintEntityMeta,
  BlueprintFieldOwnership,
  DisplayDictionary,
} from '../../../api/client';
import { FsmDiagram } from './FsmDiagram';

interface ObjectBlueprintInspectorProps {
  entity: BlueprintEntityMeta | null;
  actions: BlueprintActionMeta[];
  display?: DisplayDictionary;
  onSelectActionFilter: (entityTypeName: string) => void;
}

const OWNERSHIP_BADGE: Record<BlueprintFieldOwnership, { label: string; cls: string }> = {
  external_synced: {
    label: '外设同步',
    cls: 'bg-sky-50 text-sky-700 ring-1 ring-sky-200',
  },
  overlay_extension: {
    label: '扩展字段',
    cls: 'bg-violet-50 text-violet-700 ring-1 ring-violet-200',
  },
  ontology_owned: {
    label: '本体自有',
    cls: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  },
};

export const ObjectBlueprintInspector: React.FC<ObjectBlueprintInspectorProps> = ({
  entity,
  actions,
  display,
  onSelectActionFilter,
}) => {
  if (!entity) {
    return (
      <div className="flex h-full items-center justify-center rounded-xl border border-slate-200/80 bg-white p-8 text-xs text-slate-400">
        请在左侧模型拓扑图中选择一个节点以查看其契约细节
      </div>
    );
  }

  const actionByName = new Map(actions.map((a) => [a.name, a]));

  return (
    <div
      data-testid="blueprint-object-inspector"
      className="flex flex-col rounded-xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
    >
      {/* Header */}
      <div className="border-b border-slate-100 px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-bold text-slate-900">{entity.display_label}</h3>
              <code className="rounded-md bg-slate-100 px-2 py-0.5 font-mono text-xs font-semibold text-slate-700">
                {entity.name}
              </code>
              {entity.extends && (
                <span
                  data-testid="blueprint-extends-badge"
                  className="inline-flex items-center gap-1 rounded-md border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-800"
                >
                  继承自 {display?.objects?.[entity.extends] || entity.extends}
                </span>
              )}
            </div>
            {entity.description && (
              <p className="mt-1 text-xs text-slate-500">{entity.description}</p>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
            <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-0.5 font-mono font-medium text-slate-700">
              <KeyRound className="h-3 w-3 text-slate-500" />
              {entity.primary_key}
            </span>
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 font-medium ${
                entity.in_role_scope
                  ? 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200'
                  : 'bg-slate-100 text-slate-500'
              }`}
            >
              <Lock className="h-3 w-3" />
              当前视角可见 {entity.visible_count} 条（零实例暴露）
            </span>
          </div>
        </div>

        {entity.attached_devices.length > 0 && (
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            {entity.attached_devices.map((dev) => (
              <span
                key={`${dev.device_id}:${dev.binding}`}
                className="inline-flex items-center gap-1 rounded-lg border border-sky-200 bg-sky-50/70 px-2 py-0.5 text-[11px] font-medium text-sky-800"
              >
                <Cpu className="h-3 w-3" />
                <span>{dev.name}</span>
                <span className="text-[10px] text-sky-600">
                  ({dev.binding === 'owned' ? '主控' : '共享'})
                </span>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Inspector Body */}
      <div className="max-h-[452px] space-y-5 overflow-y-auto px-5 py-4">
        {/* FSM Section */}
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-slate-800">
            <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />
            <span>状态机契约与合法跃迁路径</span>
          </div>
          <FsmDiagram
            statusField={entity.status_field}
            initialStatuses={entity.initial_statuses}
            transitions={entity.transitions}
            terminalStatuses={entity.terminal_statuses}
            display={display}
          />
        </div>

        {/* Field Ownership Contract Table */}
        <div>
          <div className="mb-2 flex items-center justify-between">
            <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-800">
              <Layers className="h-3.5 w-3.5 text-slate-600" />
              <span>字段契约清单 ({entity.fields.length})</span>
            </div>
            <div className="flex items-center gap-1.5 text-[10px] text-slate-500">
              <span className="inline-flex items-center gap-0.5">
                <Database className="h-2.5 w-2.5 text-sky-600" /> 外设同步
              </span>
              <span className="inline-flex items-center gap-0.5">
                <Sparkles className="h-2.5 w-2.5 text-violet-600" /> 扩展字段
              </span>
            </div>
          </div>

          <div className="overflow-hidden rounded-xl border border-slate-200">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-100 bg-slate-50 text-[11px] font-medium text-slate-500">
                <tr>
                  <th className="px-3 py-2">字段标识</th>
                  <th className="px-2.5 py-2">声明类型</th>
                  <th className="px-2.5 py-2 text-right">主权归属</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {entity.fields.map((fld) => {
                  const ownMeta = OWNERSHIP_BADGE[fld.ownership] || OWNERSHIP_BADGE.ontology_owned;
                  return (
                    <tr key={fld.name} className="hover:bg-slate-50/70">
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-medium text-slate-800">{fld.display_label}</span>
                          {fld.is_primary_key && (
                            <span className="rounded bg-slate-800 px-1 py-0.2 font-mono text-[9px] font-semibold text-white">
                              PK
                            </span>
                          )}
                          {fld.is_status_field && (
                            <span className="rounded bg-emerald-100 px-1 py-0.2 text-[9px] font-semibold text-emerald-800">
                              状态
                            </span>
                          )}
                          {fld.required && (
                            <span className="text-[10px] text-rose-500" title="必填">
                              *
                            </span>
                          )}
                        </div>
                        <div className="font-mono text-[10px] text-slate-400">{fld.name}</div>
                      </td>
                      <td className="px-2.5 py-2 font-mono text-[11px] text-slate-600">
                        <span className="block max-w-[145px] truncate" title={fld.type}>
                          {fld.type}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-2.5 py-2 text-right">
                        <span
                          className={`inline-block rounded-full px-2 py-0.5 text-[10px] font-medium ${ownMeta.cls}`}
                        >
                          {ownMeta.label}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* Touched Actions */}
        <div>
          <div className="mb-2 flex items-center justify-between">
            <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-800">
              <Zap className="h-3.5 w-3.5 text-blue-700" />
              <span>触及该实体的治理动作 ({entity.touched_by_actions.length})</span>
            </div>
            {entity.touched_by_actions.length > 0 && (
              <button
                type="button"
                onClick={() => onSelectActionFilter(entity.name)}
                className="text-[11px] font-semibold text-blue-700 hover:underline"
              >
                在下方契约筛选
              </button>
            )}
          </div>
          {entity.touched_by_actions.length === 0 ? (
            <div className="rounded-lg border border-dashed border-slate-200 px-3 py-2.5 text-[11px] text-slate-400">
              暂无直接绑定写动作（主要作为结构关联目标存在）
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {entity.touched_by_actions.map((actName) => {
                const act = actionByName.get(actName);
                const isRule = act?.implementation_kind === 'rule';
                return (
                  <button
                    key={actName}
                    type="button"
                    onClick={() => onSelectActionFilter(entity.name)}
                    className={`inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-[11px] font-medium transition ${
                      isRule
                        ? 'border-sky-200 bg-sky-50 text-sky-800 hover:bg-sky-100'
                        : 'border-violet-200 bg-violet-50 text-violet-800 hover:bg-violet-100'
                    }`}
                  >
                    <span>{act?.display_label || actName}</span>
                    <span className="font-mono text-[9px] opacity-75">
                      {isRule ? 'Rule' : 'EditFn'}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
