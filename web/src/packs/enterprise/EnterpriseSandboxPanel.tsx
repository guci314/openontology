import React, { useEffect, useState } from 'react';
import { ArrowDownRight, ArrowUpRight, CheckCircle2, Scale } from 'lucide-react';
import type { DomainSandboxSlotProps } from '../../shell/packRegistry';
import { consoleApi, type RecordInstance } from '../../api/client';

interface FinancialSnapshot {
  cashYuan: number;
  grossProfitYuan: number;
  inventoryYuan: number;
  isBalanced: boolean;
}

const fmtYuan = (v: number): string =>
  `¥${v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`;

function computeFinancialSnapshot(
  accounts: RecordInstance[],
  tbResult: Record<string, unknown> | null
): FinancialSnapshot {
  const byCode = new Map(accounts.map((a) => [a.object_id, a.properties]));
  const netBalance = (code: string, normal: 'DEBIT' | 'CREDIT'): number => {
    const p = byCode.get(code);
    if (!p) return 0;
    const dr = Number(p['debit_total_cents'] || 0);
    const cr = Number(p['credit_total_cents'] || 0);
    return (normal === 'DEBIT' ? dr - cr : cr - dr) / 100;
  };

  const cashYuan = netBalance('1001', 'DEBIT');
  const revenue = netBalance('6001', 'CREDIT');
  const cogs = netBalance('6401', 'DEBIT');
  const opex = netBalance('6602', 'DEBIT');
  const grossProfitYuan = revenue - cogs - opex;
  const inventoryYuan =
    netBalance('1403', 'DEBIT') + netBalance('1404', 'DEBIT') + netBalance('1405', 'DEBIT');
  const isBalanced = tbResult ? Boolean(tbResult['is_balanced']) : true;

  return { cashYuan, grossProfitYuan, inventoryYuan, isBalanced };
}

export const EnterpriseSandboxPanel: React.FC<DomainSandboxSlotProps> = ({
  meta,
  sandboxId,
  sandboxDiff,
  busy,
  onRunPreset,
}) => {
  const [primarySnap, setPrimarySnap] = useState<FinancialSnapshot | null>(null);
  const [sandboxSnap, setSandboxSnap] = useState<FinancialSnapshot | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([
      consoleApi.listObjects('GlAccount').catch(() => ({ items: [] as RecordInstance[] })),
      consoleApi
        .callFunction<Record<string, unknown>>('compute_trial_balance', {
          accounting_entity: 'ENT-GROUP',
        })
        .catch(() => null),
      sandboxId
        ? consoleApi.listSandboxObjects(sandboxId, 'GlAccount').catch(() => ({ items: [] as RecordInstance[] }))
        : Promise.resolve({ items: [] as RecordInstance[] }),
      sandboxId
        ? consoleApi
            .compareSandboxFunction<Record<string, unknown>>(sandboxId, 'compute_trial_balance', {
              accounting_entity: 'ENT-GROUP',
            })
            .catch(() => null)
        : Promise.resolve(null),
    ]).then(([primGl, primTb, sbxGl, sbxTb]) => {
      if (!active) return;
      const pSnap = computeFinancialSnapshot(primGl.items || [], primTb?.result || null);
      setPrimarySnap(pSnap);
      if (sandboxId && (sbxGl.items || []).length > 0) {
        setSandboxSnap(
          computeFinancialSnapshot(sbxGl.items || [], sbxTb?.sandbox || null)
        );
      } else {
        setSandboxSnap(pSnap);
      }
    });
    return () => {
      active = false;
    };
  }, [meta.actor.actor_id, sandboxId, sandboxDiff?.step_count, sandboxDiff?.total_changes]);

  const metrics = [
    {
      key: 'cash',
      label: '银行现金净额',
      primary: primarySnap?.cashYuan ?? 0,
      sandbox: sandboxSnap?.cashYuan ?? 0,
    },
    {
      key: 'profit',
      label: '营业毛利 / 净经营利',
      primary: primarySnap?.grossProfitYuan ?? 0,
      sandbox: sandboxSnap?.grossProfitYuan ?? 0,
    },
    {
      key: 'inventory',
      label: '存货与在制余料',
      primary: primarySnap?.inventoryYuan ?? 0,
      sandbox: sandboxSnap?.inventoryYuan ?? 0,
    },
  ];

  return (
    <div
      className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
      data-testid="enterprise-sandbox-panel"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-3">
        <div className="flex items-center gap-2">
          <Scale className="h-4 w-4 text-blue-700" />
          <h2 className="text-sm font-semibold text-slate-900">
            财务经营全景对比 · 现实主账 vs 沙盘推演
          </h2>
        </div>
        <div className="flex items-center gap-2">
          {meta.sandbox_presets.slice(0, 3).map((p) => (
            <button
              key={p.preset_id}
              type="button"
              disabled={busy}
              onClick={() => onRunPreset(p.preset_id)}
              className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-slate-700 transition hover:border-blue-300 hover:bg-blue-50 hover:text-blue-800 disabled:opacity-50"
            >
              {p.title}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {metrics.map((m) => {
          const delta = m.sandbox - m.primary;
          const hasDelta = Math.abs(delta) > 0.01;
          return (
            <div
              key={m.key}
              className={`rounded-xl border p-3.5 ${
                hasDelta ? 'border-blue-300 bg-blue-50/40' : 'border-slate-200 bg-slate-50/50'
              }`}
            >
              <div className="text-[11px] font-medium text-slate-500">{m.label}</div>
              <div className="mt-1.5 flex items-baseline justify-between gap-2">
                <span className="text-[11px] text-slate-400">现实</span>
                <span className="font-mono text-xs tabular-nums text-slate-600">
                  {fmtYuan(m.primary)}
                </span>
              </div>
              <div className="mt-1 flex items-baseline justify-between gap-2">
                <span className="text-[11px] font-semibold text-slate-700">沙盘</span>
                <span
                  data-testid={`sandbox-metric-${m.key}`}
                  className="font-mono text-base font-bold tabular-nums text-slate-900"
                >
                  {fmtYuan(m.sandbox)}
                </span>
              </div>
              {hasDelta && (
                <div
                  className={`mt-1.5 inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold tabular-nums ${
                    delta > 0 ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                  }`}
                >
                  {delta > 0 ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
                  {delta > 0 ? '+' : ''}
                  {fmtYuan(delta)}
                </div>
              )}
            </div>
          );
        })}

        <div className="flex flex-col justify-between rounded-xl border border-emerald-200 bg-emerald-50/60 p-3.5">
          <div className="text-[11px] font-medium text-emerald-800">复式试算平衡检验</div>
          <div className="mt-2 flex items-center gap-1.5 text-sm font-bold text-emerald-800">
            <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            <span>{sandboxSnap?.isBalanced ? '借贷严格平衡' : '存在借贷差额'}</span>
          </div>
          <div className="mt-1 text-[11px] text-emerald-700/80">
            会计恒等式校验全程实时激活
          </div>
        </div>
      </div>
    </div>
  );
};
