import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertCircle, CheckCircle2, FileText, Search, Shield } from 'lucide-react';
import type { ConsoleMeta, RecordInstance, SchemaCatalog } from '../../api/client';
import { consoleApi } from '../../api/client';
import {
  displayEntityName,
  displayEnumValue,
  displayFieldName,
  displayYuanFieldLabel,
  formatSmartFieldValue,
  getStatusBadgeMeta,
} from '../i18nErrorMap';
import type { DomainViewPack } from '../packRegistry';
import { WorkCardInbox } from '../components/WorkCardInbox';

interface CockpitPageProps {
  meta: ConsoleMeta;
  schema: SchemaCatalog;
  pack: DomainViewPack | null;
  refreshTick: number;
  onRefresh: () => void;
  onInspectObject: (entityType: string, recordId: string) => void;
}

const BADGE_CLS: Record<string, string> = {
  emerald: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  amber: 'bg-amber-50 text-amber-800 ring-1 ring-amber-200',
  rose: 'bg-rose-50 text-rose-700 ring-1 ring-rose-200',
  sky: 'bg-sky-50 text-sky-700 ring-1 ring-sky-200',
  slate: 'bg-slate-100 text-slate-700 ring-1 ring-slate-200',
};

export const CockpitPage: React.FC<CockpitPageProps> = ({
  meta,
  schema,
  pack,
  refreshTick,
  onRefresh,
  onInspectObject,
}) => {
  const orderedEntityTypes = useMemo(() => {
    const preferred = pack?.preferredTypes || [];
    const prefRank = new Map(preferred.map((name, i) => [name, i]));
    return [...schema.object_types]
      .filter((t) => t.visible_count > 0 || t.in_role_scope)
      .sort((a, b) => {
        const ra = prefRank.has(a.name) ? prefRank.get(a.name)! : 999;
        const rb = prefRank.has(b.name) ? prefRank.get(b.name)! : 999;
        if (ra !== rb) return ra - rb;
        if (b.visible_count !== a.visible_count) return b.visible_count - a.visible_count;
        return a.name.localeCompare(b.name);
      });
  }, [schema, pack]);

  const [selectedType, setSelectedType] = useState<string>('');
  const [records, setRecords] = useState<RecordInstance[]>([]);
  const [allRecordsForStatus, setAllRecordsForStatus] = useState<RecordInstance[]>([]);
  const [loadingList, setLoadingList] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [banner, setBanner] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!orderedEntityTypes.length) return;
    const stillValid = orderedEntityTypes.some((t) => t.name === selectedType);
    if (!stillValid) {
      const firstPopulated =
        orderedEntityTypes.find((t) => t.visible_count > 0) || orderedEntityTypes[0];
      setSelectedType(firstPopulated.name);
      setStatusFilter('');
    }
  }, [orderedEntityTypes, selectedType]);

  const loadRecords = useCallback(async () => {
    if (!selectedType) return;
    setLoadingList(true);
    try {
      const res = await consoleApi.listObjects(selectedType, {
        status: statusFilter || undefined,
        q: searchTerm.trim() || undefined,
      });
      setRecords(res.items || []);
      if (!statusFilter && !searchTerm.trim()) {
        setAllRecordsForStatus(res.items || []);
      }
    } catch {
      setRecords([]);
    } finally {
      setLoadingList(false);
    }
  }, [selectedType, statusFilter, searchTerm, meta.actor.actor_id, refreshTick]);

  useEffect(() => {
    loadRecords();
  }, [loadRecords]);

  const selectedEntityMeta = useMemo(
    () => schema.object_types.find((t) => t.name === selectedType),
    [schema, selectedType]
  );

  const previewFields = useMemo(() => {
    if (!selectedEntityMeta) return [];
    const pk = selectedEntityMeta.primary_key;
    const titleField = selectedEntityMeta.title || '';
    const ownedSet = new Set(selectedEntityMeta.owned_fields || []);
    const candidates = selectedEntityMeta.fields.filter(
      (f) => f.name !== pk && f.name !== titleField && f.name !== 'status' && f.name !== 'stage'
    );
    const prioritized = [
      ...candidates.filter((f) =>
        f.name.includes('amount') || f.name.includes('qty') || f.name.includes('total') || ownedSet.has(f.name)
      ),
      ...candidates.filter(
        (f) =>
          !f.name.includes('amount') &&
          !f.name.includes('qty') &&
          !f.name.includes('total') &&
          !ownedSet.has(f.name)
      ),
    ];
    return prioritized.slice(0, 3);
  }, [selectedEntityMeta]);

  const availableStatuses = useMemo(() => {
    const vals = new Set<string>();
    const sf = selectedEntityMeta?.status_field || 'status';
    for (const s of selectedEntityMeta?.initial_statuses || []) vals.add(s);
    for (const k of Object.keys(selectedEntityMeta?.transitions || {})) vals.add(k);
    for (const r of allRecordsForStatus) {
      const sv = r.properties[sf] ?? r.properties['stage'];
      if (typeof sv === 'string' && sv) vals.add(sv);
    }
    return Array.from(vals);
  }, [selectedEntityMeta, allRecordsForStatus]);

  const hasActiveFilter = Boolean(searchTerm.trim() || statusFilter);

  const totalVisibleRecords = useMemo(
    () => schema.object_types.reduce((acc, t) => acc + (t.visible_count || 0), 0),
    [schema]
  );

  const KpiBar = pack?.KpiBar;
  const LineageBar = pack?.LineageBar;

  return (
    <div className="space-y-5" data-testid="cockpit-page">
      {/* Scope Context Strip */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200/80 bg-white px-4 py-2.5 text-xs shadow-[0_1px_2px_rgba(15,23,42,0.03)]">
        <div className="flex items-center gap-2 text-slate-600">
          <Shield className="h-4 w-4 text-emerald-600" />
          <span>
            当前登录视角：<strong className="text-slate-900">{meta.actor.display_name}</strong>（
            {meta.actor.title}
            {meta.actor.unit_name ? ` · ${meta.actor.unit_name}` : ''}）
          </span>
          {meta.actor.badge && (
            <span className="hidden rounded-full bg-slate-100 px-2.5 py-0.5 text-[11px] font-medium text-slate-600 sm:inline">
              {meta.actor.badge}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 font-mono text-[11px] text-slate-500">
          <span>
            可见台账记录：<strong className="tabular-nums text-slate-900">{totalVisibleRecords}</strong> 条
          </span>
          <span className="rounded bg-emerald-50 px-2 py-0.5 font-sans font-medium text-emerald-700">
            行级数据隔离生效中
          </span>
        </div>
      </div>

      {/* Action Feedback Banner */}
      {banner && (
        <div
          data-testid="cockpit-action-banner"
          className={`flex items-center justify-between rounded-xl border px-4 py-3 text-xs font-medium ${
            banner.ok
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
              : 'border-rose-200 bg-rose-50 text-rose-800'
          }`}
        >
          <div className="flex items-center gap-2">
            {banner.ok ? <CheckCircle2 className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
            <span>{banner.text}</span>
          </div>
          <button
            type="button"
            onClick={() => setBanner(null)}
            className="text-[11px] underline opacity-75 hover:opacity-100"
          >
            关闭提示
          </button>
        </div>
      )}

      {/* L2 Domain KPI Bar or L1 Domain-Agnostic Summary Bar */}
      {KpiBar ? (
        <KpiBar
          meta={meta}
          schema={schema}
          refreshTick={refreshTick}
          onInspectObject={onInspectObject}
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
            <div className="text-xs font-medium text-slate-500">业务台账分类</div>
            <div className="mt-2 font-mono text-2xl font-bold tabular-nums text-slate-900">
              {schema.object_types.length} 类
            </div>
            <div className="mt-1 text-[11px] text-slate-400">基于声明模型自动构建工作区</div>
          </div>
          <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
            <div className="text-xs font-medium text-slate-500">当前可见明细总数</div>
            <div className="mt-2 font-mono text-2xl font-bold tabular-nums text-slate-900">
              {totalVisibleRecords} 条
            </div>
            <div className="mt-1 text-[11px] text-emerald-600">已按当前登录人权限实时过滤</div>
          </div>
          <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
            <div className="text-xs font-medium text-slate-500">可执行治理动作</div>
            <div className="mt-2 font-mono text-2xl font-bold tabular-nums text-slate-900">
              {schema.actions.length} 项
            </div>
            <div className="mt-1 text-[11px] text-slate-400">每次执行均受前置红线护栏校验</div>
          </div>
          <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
            <div className="text-xs font-medium text-slate-500">已部署守护规则</div>
            <div className="mt-2 font-mono text-2xl font-bold tabular-nums text-slate-900">
              {schema.invariants.length} 条
            </div>
            <div className="mt-1 text-[11px] text-emerald-600">全程零违规脏写</div>
          </div>
        </div>
      )}

      {/* L2 Enterprise 5-Loop Lineage Bar */}
      {LineageBar && (
        <LineageBar
          meta={meta}
          schema={schema}
          refreshTick={refreshTick}
          onSelectEntityType={(et) => {
            setSelectedType(et);
            setStatusFilter('');
          }}
        />
      )}

      {/* Main Split Workspace: Left Inbox + Right Entity Browser */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <div className="lg:col-span-5">
          <WorkCardInbox
            meta={meta}
            schema={schema}
            workQueue={meta.work_queue || pack?.workQueueFallback || null}
            refreshTick={refreshTick}
            onActionCompleted={(b) => {
              setBanner(b);
              onRefresh();
            }}
            onInspectObject={onInspectObject}
          />
        </div>

        <div className="lg:col-span-7">
          <div className="flex flex-col rounded-xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
              <div className="flex items-center gap-2.5">
                <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
                  <FileText className="h-4 w-4" />
                </div>
                <div>
                  <h2 className="text-sm font-semibold text-slate-900">实时业务台账与关系穿透</h2>
                  <p className="text-xs text-slate-500">
                    点击任意记录行查看完整扩展字段与前后关联图谱
                  </p>
                </div>
              </div>

              <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
                {availableStatuses.length > 0 && (
                  <select
                    aria-label="按状态筛选"
                    data-testid="cockpit-status-filter"
                    value={statusFilter}
                    onChange={(e) => setStatusFilter(e.target.value)}
                    className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-xs text-slate-700 focus:border-blue-500 focus:bg-white focus:outline-none"
                  >
                    <option value="">全部状态</option>
                    {availableStatuses.map((sv) => (
                      <option key={sv} value={sv}>
                        {getStatusBadgeMeta(sv, meta.display).label}
                      </option>
                    ))}
                  </select>
                )}
                <div className="relative flex-1 sm:w-52 sm:flex-initial">
                  <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-slate-400" />
                  <input
                    type="text"
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    placeholder="搜索编号、名称或关键词..."
                    className="w-full rounded-lg border border-slate-200 bg-slate-50 py-1.5 pl-8 pr-3 text-xs text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none"
                  />
                </div>
              </div>
            </div>

            {/* Entity category selector chips */}
            <div className="flex flex-wrap gap-1.5 border-b border-slate-100 bg-slate-50/50 px-5 py-3">
              {orderedEntityTypes.slice(0, 12).map((t) => {
                const active = t.name === selectedType;
                return (
                  <button
                    key={t.name}
                    type="button"
                    data-testid={`entity-tab-${t.name}`}
                    onClick={() => {
                      setSelectedType(t.name);
                      setStatusFilter('');
                    }}
                    className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium transition ${
                      active
                        ? 'bg-[#0F172A] text-white shadow-sm'
                        : 'border border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-slate-900'
                    }`}
                  >
                    <span>{t.display_label || displayEntityName(t.name, meta.display)}</span>
                    <span
                      className={`rounded px-1 font-mono text-[10px] tabular-nums ${
                        active ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-500'
                      }`}
                    >
                      {t.visible_count}
                    </span>
                  </button>
                );
              })}
            </div>

            {/* Records table */}
            <div className="relative">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-slate-100 bg-slate-50/70 text-[11px] font-medium text-slate-500">
                    <tr>
                      <th className="whitespace-nowrap px-4 py-2.5">编号 / 名称</th>
                      {previewFields.map((fld) => (
                        <th key={fld.name} className="whitespace-nowrap px-3 py-2.5">
                          {displayYuanFieldLabel(
                            fld.display_label || displayFieldName(selectedType, fld.name, meta.display),
                            fld.name
                          )}
                        </th>
                      ))}
                      <th className="whitespace-nowrap px-4 py-2.5 text-right">当前状态</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {loadingList ? (
                      <tr>
                        <td colSpan={previewFields.length + 2} className="px-4 py-8 text-center text-slate-400">
                          正在加载业务单据...
                        </td>
                      </tr>
                    ) : records.length === 0 ? (
                      <tr>
                        <td colSpan={previewFields.length + 2} className="px-4 py-10 text-center text-slate-400">
                          {hasActiveFilter ? (
                            <div className="flex flex-col items-center gap-2">
                              <span>未找到匹配当前筛选条件的记录</span>
                              <button
                                type="button"
                                onClick={() => {
                                  setSearchTerm('');
                                  setStatusFilter('');
                                }}
                                className="rounded-lg border border-slate-200 bg-white px-3 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-50"
                              >
                                清空筛选条件
                              </button>
                            </div>
                          ) : (
                            '当前权限视角下该分类暂无可见单据记录'
                          )}
                        </td>
                      </tr>
                    ) : (
                      records.map((rec) => {
                        const stVal = rec.properties['status'] ?? rec.properties['stage'];
                        const badge = getStatusBadgeMeta(stVal, meta.display);
                        const orgVal = rec.properties['org'];
                        return (
                          <tr
                            key={rec.object_id}
                            data-testid={`record-row-${rec.object_id}`}
                            onClick={() => onInspectObject(rec.object_type, rec.object_id)}
                            className="cursor-pointer transition hover:bg-blue-50/40"
                          >
                            <td className="whitespace-nowrap px-4 py-3">
                              <div className="flex items-center gap-2">
                                <span className="font-mono font-semibold text-slate-900">
                                  {rec.object_id}
                                </span>
                                {Boolean(orgVal) && (
                                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
                                    {displayEnumValue(String(orgVal), meta.display)}
                                  </span>
                                )}
                              </div>
                              {rec.title && rec.title !== rec.object_id && (
                                <div className="mt-0.5 max-w-[230px] truncate text-[11px] text-slate-500">
                                  {rec.title}
                                </div>
                              )}
                            </td>
                            {previewFields.map((fld) => {
                              const v = rec.properties[fld.name];
                              const formatted = formatSmartFieldValue(fld.name, v, meta, {
                                decimals: 0,
                              });
                              return (
                                <td key={fld.name} className="whitespace-nowrap px-3 py-3 font-mono tabular-nums text-slate-700">
                                  <span className="inline-block max-w-[140px] truncate">{formatted}</span>
                                </td>
                              );
                            })}
                            <td className="whitespace-nowrap px-4 py-3 text-right">
                              <span
                                className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${
                                  BADGE_CLS[badge.tone] || BADGE_CLS.slate
                                }`}
                              >
                                {badge.label}
                              </span>
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
              <div className="pointer-events-none absolute inset-y-0 right-0 w-4 bg-gradient-to-l from-white/80 to-transparent lg:hidden" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
