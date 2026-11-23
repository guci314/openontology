import React from 'react';
import { ChevronRight } from 'lucide-react';
import type { DomainLineageSlotProps } from '../../shell/packRegistry';

interface LoopStage {
  key: string;
  label: string;
  sub: string;
  entityType: string;
}

const FIVE_LOOPS: LoopStage[] = [
  { key: 'crm', label: '① 获客签约', sub: '商机与客诉把关', entityType: 'CrmDeal' },
  { key: 'p2p', label: '② 采购入料', sub: '订货验收与磅差裁决', entityType: 'P2pPurchaseOrder' },
  { key: 'p2m', label: '③ 投料生产', sub: '工单排产与成本守恒', entityType: 'WorkOrder' },
  { key: 'o2c', label: '④ 发货回款', sub: '先出库后开票核销', entityType: 'O2cOrder' },
  { key: 'r2r', label: '⑤ 总账平衡', sub: '复式过账与试算快照', entityType: 'JournalEntry' },
];

export const EnterpriseLineageBar: React.FC<DomainLineageSlotProps> = ({
  schema,
  onSelectEntityType,
}) => {
  const countMap = new Map(schema.object_types.map((t) => [t.name, t.visible_count]));

  return (
    <div
      className="rounded-xl border border-slate-200/80 bg-white px-4 py-3 shadow-[0_1px_2px_rgba(15,23,42,0.03)]"
      data-testid="enterprise-lineage-bar"
    >
      <div className="mb-2 flex items-center justify-between text-[11px] font-medium text-slate-500">
        <span>企业端到端五闭环运行脉搏</span>
        <span>点击任意闭环即刻切换下方面板业务台账</span>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-5">
        {FIVE_LOOPS.map((stage, idx) => {
          const count = countMap.get(stage.entityType) ?? 0;
          return (
            <div key={stage.key} className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => onSelectEntityType(stage.entityType)}
                className="flex flex-1 items-center justify-between rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2 text-left transition hover:border-blue-400 hover:bg-blue-50/40"
              >
                <div className="min-w-0">
                  <div className="truncate text-xs font-semibold text-slate-900">
                    {stage.label}
                  </div>
                  <div className="truncate text-[10px] text-slate-500">{stage.sub}</div>
                </div>
                <span className="ml-2 shrink-0 rounded-md bg-white px-1.5 py-0.5 font-mono text-xs font-bold tabular-nums text-blue-700 ring-1 ring-slate-200">
                  {count}
                </span>
              </button>
              {idx < FIVE_LOOPS.length - 1 && (
                <ChevronRight className="hidden h-4 w-4 shrink-0 text-slate-300 sm:block" />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
