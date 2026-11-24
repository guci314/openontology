import React from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Cpu,
  RefreshCw,
  ShieldAlert,
  Sparkles,
} from 'lucide-react';
import type { BlueprintProveResponse } from '../../../api/client';

interface Z3ProvePanelProps {
  result: BlueprintProveResponse | null;
  loading: boolean;
  error: string | null;
  collapsed: boolean;
  onToggleCollapse: () => void;
  onReRun: () => void;
}

export const Z3ProvePanel: React.FC<Z3ProvePanelProps> = ({
  result,
  loading,
  error,
  collapsed,
  onToggleCollapse,
  onReRun,
}) => {
  if (!loading && !error && !result) {
    return null;
  }

  if (loading) {
    return (
      <div
        data-testid="z3-prove-loading"
        className="flex items-center justify-between rounded-xl border border-blue-200 bg-blue-50/70 px-5 py-4 text-xs font-medium text-blue-900 shadow-sm"
      >
        <div className="flex items-center gap-3">
          <RefreshCw className="h-4 w-4 animate-spin text-blue-700" />
          <span>正在调用 Z3 形式化定理证明器，执行全模型动作可达性与不变量保真核验...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div
        data-testid="z3-prove-error"
        className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-5 py-4 text-xs text-rose-800 shadow-sm"
      >
        <div className="flex items-center gap-2.5">
          <ShieldAlert className="h-4 w-4 shrink-0 text-rose-700" />
          <span>形式化体检执行失败：{error}</span>
        </div>
        <button
          type="button"
          onClick={onReRun}
          className="inline-flex items-center gap-1 rounded-lg border border-rose-200 bg-white px-3 py-1 font-semibold text-rose-800 hover:bg-rose-100"
        >
          <RefreshCw className="h-3.5 w-3.5" /> 重试
        </button>
      </div>
    );
  }

  if (!result) return null;

  if (!result.prover_available) {
    return (
      <div
        data-testid="z3-prover-unavailable"
        className="rounded-xl border border-amber-200 bg-amber-50/80 px-5 py-4 text-xs text-amber-950 shadow-sm"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-start gap-2.5">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" />
            <div>
              <div className="font-semibold text-amber-900">
                {result.message || '当前运行环境未安装可选 Z3 形式化证明器扩展'}
              </div>
              <p className="mt-1 text-amber-800">
                系统零可选依赖运行态不受影响；如需开启 SMT 可达性与红线冲突证明，可执行：
              </p>
            </div>
          </div>
          {result.install_hint && (
            <code className="rounded-lg border border-amber-300 bg-white px-3 py-1.5 font-mono text-[11px] font-semibold text-amber-900">
              {result.install_hint}
            </code>
          )}
        </div>
      </div>
    );
  }

  const isClean = result.verdict === 'PROVEN_CLEAN';
  const summary = result.summary;
  const fragmentEntries = Object.entries(summary?.fragment_counts || {});
  const deadlocked = (result.action_proofs || []).filter((p) => p.status !== 'REACHABLE');
  const violatedInvs = (result.invariant_proofs || []).filter((p) => p.status === 'VIOLATED');
  const redundantRules = result.redundant_rules || [];

  return (
    <div
      data-testid={isClean ? 'z3-verdict-clean' : 'z3-verdict-issues'}
      className={`rounded-xl border p-5 shadow-sm ${
        isClean
          ? 'border-emerald-200 bg-emerald-50/50'
          : 'border-rose-200 bg-rose-50/60'
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <div
            className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${
              isClean
                ? 'bg-emerald-100 text-emerald-700'
                : 'bg-rose-100 text-rose-700'
            }`}
          >
            {isClean ? (
              <CheckCircle2 className="h-5 w-5" />
            ) : (
              <ShieldAlert className="h-5 w-5" />
            )}
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3
                className={`text-sm font-bold ${
                  isClean ? 'text-emerald-950' : 'text-rose-950'
                }`}
              >
                {isClean
                  ? 'Z3 形式化体检通过 · 全模型 0 死锁 / 0 不变量违例'
                  : 'Z3 形式化体检发现潜在契约冲突或不可达动作'}
              </h3>
              <span className="rounded-full bg-white px-2 py-0.5 font-mono text-[11px] font-semibold text-slate-700 ring-1 ring-slate-200">
                {result.elapsed_ms?.toFixed(1) ?? '0.0'} ms
              </span>
              {result.cached && (
                <span className="rounded-full bg-slate-900 px-2 py-0.5 text-[10px] font-semibold text-white">
                  缓存命中
                </span>
              )}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-3 text-xs text-slate-700">
              <span>
                动作可达：<strong>{summary?.reachable_actions ?? 0}</strong> /{' '}
                {summary?.total_actions ?? 0}
              </span>
              <span>
                Hoare 保真校验：<strong>{summary?.preserved_invariants ?? 0}</strong> /{' '}
                {summary?.total_invariants ?? 0}
                {summary?.declared_invariant_count !== undefined
                  ? `（总声明红线 ${summary.declared_invariant_count} 项）`
                  : ''}
              </span>
              <span>
                冗余前置约束：<strong>{summary?.redundant_rule_count ?? 0}</strong>
              </span>
              <span>
                精确可判定段占比：
                <strong>{Math.round((summary?.exact_linear_ratio ?? 1) * 100)}%</strong>
              </span>
              {result.blueprint_fingerprint && (
                <span className="font-mono text-[11px] text-slate-500">
                  #{result.blueprint_fingerprint.slice(0, 12)}
                </span>
              )}
            </div>

            {fragmentEntries.length > 0 && (
              <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                <span className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500">
                  <Cpu className="h-3.5 w-3.5" /> 逻辑判定段：
                </span>
                {fragmentEntries.map(([frag, cnt]) => (
                  <span
                    key={frag}
                    className="rounded-md border border-slate-200 bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-700"
                  >
                    {frag} × {cnt}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            data-testid="z3-reverify-btn"
            onClick={onReRun}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:bg-slate-50"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            <span>重新验证</span>
          </button>
          <button
            type="button"
            data-testid="z3-toggle-collapse-btn"
            onClick={onToggleCollapse}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-50"
          >
            {collapsed ? (
              <>
                <ChevronDown className="h-3.5 w-3.5" />
                <span>
                  {result.action_proofs?.length
                    ? `展开 ${result.action_proofs.length} 条证明明细`
                    : '展开明细'}
                </span>
              </>
            ) : (
              <>
                <ChevronUp className="h-3.5 w-3.5" /> <span>收起明细</span>
              </>
            )}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="mt-4 space-y-3 border-t border-slate-200/70 pt-3 text-xs">
          {deadlocked.length > 0 && (
            <div className="rounded-lg border border-rose-200 bg-white p-3">
              <div className="font-semibold text-rose-800">不可达 / 死锁动作诊断</div>
              <ul className="mt-2 space-y-1.5">
                {deadlocked.map((item) => (
                  <li key={item.action_name} className="text-slate-700">
                    <span className="font-semibold text-slate-900">
                      {item.display_label || item.action_name}
                    </span>{' '}
                    <code className="font-mono text-[11px] text-slate-500">
                      ({item.action_name})
                    </code>{' '}
                    · <span className="font-semibold text-rose-700">{item.status}</span>
                    {item.unsat_core.length > 0 && (
                      <div className="mt-1 font-mono text-[11px] text-rose-700">
                        UNSAT 冲突核：{item.unsat_core.join(' , ')}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {violatedInvs.length > 0 && (
            <div className="rounded-lg border border-rose-200 bg-white p-3">
              <div className="font-semibold text-rose-800">不变量违例反例轨迹</div>
              <ul className="mt-2 space-y-2">
                {violatedInvs.map((invp) => (
                  <li key={invp.action_name} className="text-slate-700">
                    <span className="font-semibold text-slate-900">
                      {invp.display_label || invp.action_name}
                    </span>{' '}
                    违例：
                    <span className="font-semibold text-rose-700">
                      {(invp.violated_invariant_labels || invp.violated_invariants).join('、')}
                    </span>
                    {invp.counterexamples.slice(0, 1).map((cx, i) => (
                      <pre
                        key={i}
                        className="mt-1 overflow-x-auto rounded bg-slate-50 p-2 font-mono text-[11px] text-slate-700"
                      >
                        {JSON.stringify({ params: cx.params, before: cx.before, after: cx.after }, null, 2)}
                      </pre>
                    ))}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {redundantRules.length > 0 && (
            <div className="rounded-lg border border-amber-200 bg-white p-3">
              <div className="font-semibold text-amber-900">
                冗余前置规则提示（不影响运行正确性）
              </div>
              <ul className="mt-1.5 space-y-1 text-slate-700">
                {redundantRules.map((rr) => (
                  <li key={`${rr.action_name}:${rr.rule_name}`}>
                    <span className="font-semibold">{rr.action_display_label || rr.action_name}</span>{' '}
                    · <code className="font-mono text-[11px]">{rr.rule_name}</code> ({rr.reason})
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.action_proofs && result.action_proofs.length > 0 && (
            <div
              data-testid="z3-action-proof-list"
              className="rounded-lg border border-slate-200/80 bg-white p-3"
            >
              <div className="mb-2 flex items-center justify-between">
                <span className="inline-flex items-center gap-1.5 font-semibold text-slate-800">
                  <Sparkles className="h-3.5 w-3.5 text-emerald-600" />
                  动作 SMT 可达性证据清单 ({result.action_proofs.length})
                </span>
                <span className="text-[11px] text-slate-400">
                  全部符号验证均为零副作用只读推演
                </span>
              </div>
              <div className="grid max-h-52 grid-cols-1 gap-1.5 overflow-y-auto pr-1 md:grid-cols-2 lg:grid-cols-3">
                {result.action_proofs.map((ap) => (
                  <div
                    key={ap.action_name}
                    className="flex items-center justify-between gap-2 rounded-lg border border-slate-100 bg-slate-50/70 px-2.5 py-1.5"
                  >
                    <div className="min-w-0">
                      <div className="truncate font-medium text-slate-800">
                        {ap.display_label || ap.action_name}
                      </div>
                      <div className="truncate font-mono text-[10px] text-slate-400">
                        {ap.action_name}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      <span className="rounded bg-white px-1.5 py-0.5 font-mono text-[10px] text-slate-600 ring-1 ring-slate-200">
                        {ap.logic_fragment}
                      </span>
                      <span
                        className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ${
                          ap.status === 'REACHABLE'
                            ? 'bg-emerald-100 text-emerald-800'
                            : 'bg-rose-100 text-rose-800'
                        }`}
                      >
                        {ap.status === 'REACHABLE' ? '可达' : ap.status}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
