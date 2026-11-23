import React, { useEffect, useState } from 'react';
import { Banknote, CheckCircle2, Factory, Receipt, ShoppingBag } from 'lucide-react';
import type { DomainKpiSlotProps } from '../../shell/packRegistry';
import { consoleApi, type RecordInstance } from '../../api/client';

const fmtYuan = (v: number): string =>
  `¥${v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`;

export const EnterpriseKpiBar: React.FC<DomainKpiSlotProps> = ({
  meta,
  refreshTick,
  onInspectObject,
}) => {
  const [orders, setOrders] = useState<RecordInstance[]>([]);
  const [invoices, setInvoices] = useState<RecordInstance[]>([]);
  const [workOrders, setWorkOrders] = useState<RecordInstance[]>([]);
  const [glAccounts, setGlAccounts] = useState<RecordInstance[]>([]);
  const [tb, setTb] = useState<Record<string, unknown> | null>(null);
  const [p2mDiag, setP2mDiag] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([
      consoleApi.listObjects('O2cOrder').catch(() => ({ items: [] as RecordInstance[] })),
      consoleApi.listObjects('O2cInvoice').catch(() => ({ items: [] as RecordInstance[] })),
      consoleApi.listObjects('WorkOrder').catch(() => ({ items: [] as RecordInstance[] })),
      consoleApi.listObjects('GlAccount').catch(() => ({ items: [] as RecordInstance[] })),
      consoleApi
        .callFunction<Record<string, unknown>>('compute_trial_balance', {
          accounting_entity: 'ENT-GROUP',
        })
        .catch(() => null),
      consoleApi
        .callFunction<Record<string, unknown>>('diagnose_work_order_conservation', {})
        .catch(() => null),
    ]).then(([soRes, invRes, woRes, glRes, tbRes, diagRes]) => {
      if (!active) return;
      setOrders(soRes.items || []);
      setInvoices(invRes.items || []);
      setWorkOrders(woRes.items || []);
      setGlAccounts(glRes.items || []);
      setTb(tbRes?.result || null);
      setP2mDiag(diagRes?.result || null);
    });
    return () => {
      active = false;
    };
  }, [meta.actor.actor_id, refreshTick]);

  const orderTotal = orders
    .filter((o) => o.properties['status'] !== 'cancelled')
    .reduce((acc, o) => acc + Number(o.properties['amount_total'] || 0), 0);

  const visibleOrderIds = new Set(orders.map((o) => o.object_id));
  const scopedInvoices =
    orders.length > 0
      ? invoices.filter((inv) => visibleOrderIds.has(String(inv.properties['order'] || '')))
      : invoices;

  const arResidual = scopedInvoices
    .filter((inv) => inv.properties['status'] === 'posted')
    .reduce((acc, inv) => acc + Number(inv.properties['amount_residual'] || 0), 0);
  const collectedAmount = scopedInvoices
    .filter((inv) => inv.properties['status'] === 'posted')
    .reduce(
      (acc, inv) =>
        acc + (Number(inv.properties['amount_total'] || 0) - Number(inv.properties['amount_residual'] || 0)),
      0
    );

  const inProgressWos = workOrders.filter((w) => w.properties['status'] === 'in_progress').length;
  const allConserved = p2mDiag ? Boolean(p2mDiag['all_cost_conserved']) : true;

  const bankAcc = glAccounts.find((a) => a.object_id === '1001');
  const bankCash = bankAcc
    ? (Number(bankAcc.properties['debit_total_cents'] || 0) -
        Number(bankAcc.properties['credit_total_cents'] || 0)) /
      100
    : 0;
  const isBalanced = tb ? Boolean(tb['is_balanced']) && Boolean(tb['accounting_equation_holds']) : true;

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4" data-testid="enterprise-kpi-bar">
      {/* Card 1: Orders */}
      <button
        type="button"
        onClick={() => orders[0] && onInspectObject('O2cOrder', orders[0].object_id)}
        className="rounded-xl border border-slate-200/80 bg-white p-4 text-left shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition hover:border-blue-300"
      >
        <div className="flex items-center justify-between text-xs font-medium text-slate-500">
          <span>在手销售订单总额</span>
          <ShoppingBag className="h-4 w-4 text-blue-700" />
        </div>
        <div
          data-testid="kpi-orders-total"
          className="mt-2 font-mono text-2xl font-bold tabular-nums text-slate-900"
        >
          {fmtYuan(orderTotal)}
        </div>
        <div className="mt-1 flex items-center justify-between text-[11px] text-slate-500">
          <span>当前权限可见 {orders.length} 笔订单</span>
          <span className="font-medium text-blue-700">穿透详情 →</span>
        </div>
      </button>

      {/* Card 2: AR & Collection */}
      <button
        type="button"
        onClick={() => scopedInvoices[0] && onInspectObject('O2cInvoice', scopedInvoices[0].object_id)}
        className="rounded-xl border border-slate-200/80 bg-white p-4 text-left shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition hover:border-blue-300"
      >
        <div className="flex items-center justify-between text-xs font-medium text-slate-500">
          <span>应收未清与累计回款</span>
          <Receipt className="h-4 w-4 text-amber-600" />
        </div>
        <div
          data-testid="kpi-ar-residual"
          className="mt-2 font-mono text-2xl font-bold tabular-nums text-slate-900"
        >
          {fmtYuan(arResidual)}
        </div>
        <div className="mt-1 flex items-center justify-between text-[11px] text-slate-500">
          <span>已核销回款 {fmtYuan(collectedAmount)}</span>
          <span className="font-medium text-amber-700">待收监控</span>
        </div>
      </button>

      {/* Card 3: P2M & Cost Conservation */}
      <button
        type="button"
        onClick={() => workOrders[0] && onInspectObject('WorkOrder', workOrders[0].object_id)}
        className="rounded-xl border border-slate-200/80 bg-white p-4 text-left shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition hover:border-blue-300"
      >
        <div className="flex items-center justify-between text-xs font-medium text-slate-500">
          <span>在产工单与成本守恒</span>
          <Factory className="h-4 w-4 text-indigo-700" />
        </div>
        <div className="mt-2 flex items-baseline gap-2">
          <span
            data-testid="kpi-wo-count"
            className="font-mono text-2xl font-bold tabular-nums text-slate-900"
          >
            {inProgressWos} 在产 / {workOrders.length} 总
          </span>
        </div>
        <div className="mt-1 flex items-center justify-between text-[11px]">
          <span className={allConserved ? 'font-semibold text-emerald-700' : 'font-semibold text-rose-700'}>
            {allConserved ? '● 投入产出成本守恒' : '▲ 成本守恒待核查'}
          </span>
          <span className="text-slate-400">厂区隔离视图</span>
        </div>
      </button>

      {/* Card 4: Bank Cash & Trial Balance */}
      <button
        type="button"
        onClick={() => bankAcc && onInspectObject('GlAccount', bankAcc.object_id)}
        className="rounded-xl border border-slate-200/80 bg-white p-4 text-left shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition hover:border-blue-300"
      >
        <div className="flex items-center justify-between text-xs font-medium text-slate-500">
          <span>银行现金与试算平衡</span>
          <Banknote className="h-4 w-4 text-emerald-700" />
        </div>
        <div
          data-testid="kpi-bank-cash"
          className="mt-2 font-mono text-2xl font-bold tabular-nums text-slate-900"
        >
          {fmtYuan(bankCash)}
        </div>
        <div className="mt-1 flex items-center justify-between text-[11px]">
          <span
            data-testid="kpi-trial-balance-badge"
            className={`inline-flex items-center gap-1 font-semibold ${
              isBalanced ? 'text-emerald-700' : 'text-rose-700'
            }`}
          >
            <CheckCircle2 className="h-3.5 w-3.5" />
            {isBalanced ? '复式总账试算平衡绿灯' : '试算存在不平差额'}
          </span>
          <span className="font-mono text-slate-400">2026-11</span>
        </div>
      </button>
    </div>
  );
};
