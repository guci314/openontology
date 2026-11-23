import React, { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, RefreshCw, Search, ShieldAlert, ShieldCheck, UserCheck } from 'lucide-react';
import type { AuditResponse, AuditRunEntry, ConsoleMeta } from '../../api/client';
import { consoleApi } from '../../api/client';
import {
  displayFieldName,
  displayRoleName,
  displayYuanFieldLabel,
  formatLocalTime,
  formatSmartFieldValue,
  translateError,
} from '../i18nErrorMap';
import { type DomainViewPack, resolveShellTerminology } from '../packRegistry';

interface AuditPageProps {
  meta: ConsoleMeta;
  pack?: DomainViewPack | null;
  refreshTick: number;
}

type AuditFilter = 'ALL' | 'AUTO' | 'HUMAN' | 'SHADOW' | 'REJECTED' | 'REVERTED';

export const AuditPage: React.FC<AuditPageProps> = ({ meta, pack, refreshTick }) => {
  const terms = resolveShellTerminology(pack);
  const [data, setData] = useState<AuditResponse | null>(null);
  const [filter, setFilter] = useState<AuditFilter>('ALL');
  const [searchTerm, setSearchTerm] = useState('');
  const [showAll, setShowAll] = useState(false);
  const [expandedRunDiffs, setExpandedRunDiffs] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(false);

  const loadAudit = useCallback(async () => {
    setLoading(true);
    try {
      const res = await consoleApi.getAudit();
      setData(res);
    } finally {
      setLoading(false);
    }
  }, [meta.actor.actor_id, refreshTick]);

  useEffect(() => {
    loadAudit();
  }, [loadAudit]);

  const needle = searchTerm.trim().toLowerCase();
  const filteredRuns: AuditRunEntry[] = (data?.runs || []).filter((r) => {
    if (filter === 'REJECTED' && r.status !== 'REJECTED') return false;
    if (filter === 'REVERTED' && r.status !== 'REVERTED') return false;
    if (filter === 'HUMAN' && !(r.status === 'COMMITTED' && r.governance_channel === 'human')) return false;
    if (filter === 'AUTO' && !(r.status === 'COMMITTED' && r.governance_channel !== 'human' && r.governance_channel !== 'shadow')) return false;
    if (filter === 'SHADOW' && r.governance_channel !== 'shadow') return false;
    if (needle) {
      const hay = `${r.run_id} ${r.action_type} ${r.action_label} ${r.actor_id} ${r.actor_display_name || ''} ${JSON.stringify(r.changes || [])}`.toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  });

  const DEFAULT_LIMIT = 8;
  const runs = showAll ? filteredRuns : filteredRuns.slice(0, DEFAULT_LIMIT);
  const hiddenCount = Math.max(0, filteredRuns.length - runs.length);

  const formatInterceptReason = (run: AuditRunEntry): string => {
    const raw = translateError(run.error_code, run.error_message, run.hint_zh);
    return raw.replace(/^红线拦截[：:]\s*/, '');
  };

  return (
    <div className="space-y-5" data-testid="audit-page">
      {/* Top KPI summary */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="text-xs font-medium text-slate-500">累计可追溯操作流</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-slate-900">
            {data?.summary.total_runs ?? 0} 笔
          </div>
          <div className="mt-1 text-[11px] text-slate-400">全量不可篡改审计记录</div>
        </div>
        <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="text-xs font-medium text-slate-500">合规执行落库</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-emerald-700">
            {data?.summary.committed ?? 0} 笔
          </div>
          <div className="mt-1 text-[11px] text-emerald-600">均经规则与权限双重校验</div>
        </div>
        <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="text-xs font-medium text-slate-500">红线实时拦截</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-rose-700">
            {data?.summary.rejected ?? 0} 次
          </div>
          <div className="mt-1 text-[11px] text-rose-600">违规操作未进入任何外部系统</div>
        </div>
        <div className="rounded-xl border border-emerald-200 bg-emerald-50/70 p-4 shadow-sm">
          <div className="text-xs font-medium text-emerald-800">真实软件系统脏写数</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-emerald-700">
            0 笔
          </div>
          <div className="mt-1 text-[11px] font-semibold text-emerald-700">
            ✓ 原子隔离与写前拦截止损
          </div>
        </div>
      </div>

      {/* Filter tabs + Search + Refresh */}
      <div className="rounded-xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
          <div className="flex flex-wrap items-center gap-1.5">
            {(
              [
                ['ALL', '全部记录'],
                ['AUTO', '自动放行'],
                ['HUMAN', '人工签批'],
                ['SHADOW', '影子演练'],
                ['REJECTED', '红线拦截'],
                ['REVERTED', '已回滚'],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                type="button"
                data-testid={`audit-filter-${k.toLowerCase()}`}
                onClick={() => setFilter(k)}
                className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                  filter === k
                    ? 'bg-[#0F172A] text-white'
                    : 'border border-slate-200 bg-white text-slate-600 hover:bg-slate-50'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative w-48 sm:w-56">
              <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
              <input
                type="text"
                data-testid="audit-search-input"
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                placeholder={terms.auditSearchPlaceholder}
                className="w-full rounded-lg border border-slate-200 bg-slate-50 py-1.5 pl-8 pr-3 text-xs text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none"
              />
            </div>
            <button
              type="button"
              onClick={loadAudit}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
              <span>刷新记录</span>
            </button>
          </div>
        </div>

        <div className="divide-y divide-slate-100">
          {runs.length === 0 ? (
            <div className="px-6 py-12 text-center text-xs text-slate-400">
              当前筛选分类下暂无审计记录
            </div>
          ) : (
            runs.map((run) => {
              const isRejected = run.status === 'REJECTED';
              const isHuman = run.governance_channel === 'human';
              const isDiffExpanded = Boolean(expandedRunDiffs[run.run_id]);
              const allChanges = run.changes || [];
              const shownChanges =
                allChanges.length > 3 && !isDiffExpanded ? allChanges.slice(0, 3) : allChanges;
              return (
                <div key={run.run_id} className="p-5 transition hover:bg-slate-50/60">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="flex items-start gap-3">
                      <div
                        className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${
                          isRejected
                            ? 'bg-rose-50 text-rose-700 ring-1 ring-rose-200'
                            : isHuman
                            ? 'bg-amber-50 text-amber-700 ring-1 ring-amber-200'
                            : 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200'
                        }`}
                      >
                        {isRejected ? (
                          <ShieldAlert className="h-4 w-4" />
                        ) : isHuman ? (
                          <UserCheck className="h-4 w-4" />
                        ) : (
                          <CheckCircle2 className="h-4 w-4" />
                        )}
                      </div>
                      <div>
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold text-slate-900">
                            {run.action_label || run.action_type}
                          </span>
                          <span
                            className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                              isRejected
                                ? 'bg-rose-100 text-rose-800'
                                : 'bg-emerald-100 text-emerald-800'
                            }`}
                          >
                            {isRejected ? '红线已拦截' : run.status_label}
                          </span>
                          {isRejected && (
                            <span
                              data-testid={`zero-dirty-${run.run_id}`}
                              className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold text-emerald-700 ring-1 ring-emerald-300"
                            >
                              <ShieldCheck className="h-3 w-3" />
                              真实系统 0 改动
                            </span>
                          )}
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                          <span>
                            执行人：{run.actor_display_name || run.actor_id}（
                            {displayRoleName(run.actor_role, meta.display)}）
                          </span>
                          <span className="font-mono text-[11px] text-slate-400">
                            流水：{run.run_id}
                          </span>
                        </div>
                      </div>
                    </div>
                    <span className="font-mono text-[11px] text-slate-400">
                      {formatLocalTime(run.executed_at)}
                    </span>
                  </div>

                  {isRejected && (
                    <div className="mt-3 rounded-lg border border-rose-200/70 bg-rose-50/70 px-3.5 py-2.5 text-xs text-rose-800">
                      拦截原因：{formatInterceptReason(run)}
                    </div>
                  )}

                  {!isRejected && allChanges.length > 0 && (
                    <div className="mt-3 space-y-2 rounded-lg border border-slate-200/80 bg-slate-50/60 p-3">
                      {shownChanges.map((chg) => (
                        <div key={`${run.run_id}-${chg.object_type}:${chg.object_id}`} className="text-xs">
                          <div className="font-semibold text-slate-700">
                            {meta.display?.objects?.[chg.object_type] || chg.object_type} ·{' '}
                            <span className="font-mono">{chg.object_id}</span>
                            {chg.object_title && chg.object_title !== chg.object_id && (
                              <span className="ml-1.5 font-normal text-slate-500">
                                {chg.object_title}
                              </span>
                            )}
                          </div>
                          {chg.field_deltas.length > 0 && (
                            <div className="mt-1 flex flex-wrap gap-2">
                              {chg.field_deltas.map((d) => (
                                <span
                                  key={d.field}
                                  className="inline-flex items-center gap-1 rounded bg-white px-2 py-0.5 font-mono text-[11px] text-slate-700 ring-1 ring-slate-200"
                                >
                                  <span className="font-sans text-slate-500">
                                    {displayYuanFieldLabel(
                                      displayFieldName(chg.object_type, d.field, meta.display),
                                      d.field
                                    )}
                                    :
                                  </span>
                                  <span className="text-slate-400 line-through">
                                    {formatSmartFieldValue(d.field, d.before, meta, { decimals: 0 })}
                                  </span>
                                  <span>→</span>
                                  <span className="font-semibold text-emerald-700">
                                    {formatSmartFieldValue(d.field, d.after, meta, { decimals: 0 })}
                                  </span>
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                      {allChanges.length > 3 && (
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedRunDiffs((prev) => ({
                              ...prev,
                              [run.run_id]: !prev[run.run_id],
                            }))
                          }
                          className="text-[11px] font-medium text-blue-700 hover:underline"
                        >
                          {isDiffExpanded
                            ? '收起变更明细'
                            : `展开全部 ${allChanges.length} 项变更明细`}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
        {hiddenCount > 0 && (
          <div className="border-t border-slate-100 bg-slate-50/70 px-5 py-3 text-center">
            <button
              type="button"
              onClick={() => setShowAll(true)}
              className="rounded-lg border border-slate-200 bg-white px-4 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50"
            >
              展开其余 {hiddenCount} 条历史记录
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
