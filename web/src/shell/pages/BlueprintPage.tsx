import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CheckCircle2,
  Code2,
  Cpu,
  Eye,
  GitFork,
  Layers,
  Lock,
  ShieldCheck,
  Sparkles,
  Zap,
} from 'lucide-react';
import type {
  BlueprintProveResponse,
  BlueprintResponse,
  ConsoleMeta,
  M1DraftResponse,
} from '../../api/client';
import { consoleApi } from '../../api/client';
import { displayRoleName } from '../i18nErrorMap';
import type { DomainViewPack } from '../packRegistry';
import { M1TopologyGraph } from '../components/blueprint/M1TopologyGraph';
import { ObjectBlueprintInspector } from '../components/blueprint/ObjectBlueprintInspector';
import { Z3ProvePanel } from '../components/blueprint/Z3ProvePanel';

interface BlueprintPageProps {
  meta: ConsoleMeta;
  pack?: DomainViewPack | null;
  refreshTick?: number;
  draftState?: M1DraftResponse | null;
  onOpenExpert?: () => void;
  onExportDraft?: () => void;
  onApplyDraft?: () => Promise<void>;
  onDiscardDraft?: () => Promise<void>;
}

type BottomTab = 'actions' | 'primitives' | 'invariants';
type ActionFilter = 'all' | 'rule' | 'edit_function' | 'selected';

const AUTONOMY_BADGE: Record<string, { label: string; cls: string }> = {
  auto: {
    label: '限额自动放行',
    cls: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  },
  confirm: {
    label: '人工审批协同',
    cls: 'bg-amber-50 text-amber-800 ring-1 ring-amber-200',
  },
  shadow: {
    label: '影子观测 (零写入)',
    cls: 'bg-sky-50 text-sky-700 ring-1 ring-sky-200',
  },
};

export const BlueprintPage: React.FC<BlueprintPageProps> = ({
  meta,
  pack,
  refreshTick = 0,
  draftState,
  onOpenExpert,
  onExportDraft,
  onApplyDraft,
  onDiscardDraft,
}) => {
  const [blueprint, setBlueprint] = useState<BlueprintResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedType, setSelectedType] = useState<string>('');

  const [proveResult, setProveResult] = useState<BlueprintProveResponse | null>(null);
  const [proveLoading, setProveLoading] = useState(false);
  const [proveError, setProveError] = useState<string | null>(null);
  const [proveCollapsed, setProveCollapsed] = useState(false);

  const [bottomTab, setBottomTab] = useState<BottomTab>('actions');
  const [actionFilter, setActionFilter] = useState<ActionFilter>('all');
  const bottomPanelRef = useRef<HTMLDivElement | null>(null);

  const loadBlueprint = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await consoleApi.getBlueprint();
      setBlueprint(res);
      setSelectedType((prev) => {
        if (prev && res.object_types.some((o) => o.name === prev)) return prev;
        const preferred = pack?.preferredTypes || [];
        for (const pName of preferred) {
          const match = res.object_types.find(
            (o) =>
              o.name === pName &&
              (o.status_field || Object.keys(o.transitions).length > 0)
          );
          if (match) return match.name;
        }
        const firstFsm = res.object_types.find(
          (o) => o.status_field || Object.keys(o.transitions).length > 0
        );
        return firstFsm?.name || res.object_types[0]?.name || '';
      });
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '加载模型蓝图失败');
    } finally {
      setLoading(false);
    }
  }, [pack]);

  useEffect(() => {
    loadBlueprint();
  }, [loadBlueprint, meta.actor.actor_id, refreshTick]);

  const activeBlueprint = useMemo(
    () => (draftState?.draft_has_changes ? draftState.preview_blueprint : blueprint),
    [draftState, blueprint]
  );

  const activeProveResult = useMemo(
    () => (draftState?.draft_has_changes && draftState.proof_report ? draftState.proof_report : proveResult),
    [draftState, proveResult]
  );

  const handleRunProve = useCallback(async (forceRefresh = false) => {
    setProveLoading(true);
    setProveError(null);
    setProveCollapsed(false);
    try {
      const res = await consoleApi.proveBlueprint(forceRefresh);
      setProveResult(res);
      setProveCollapsed(Boolean(res.prover_available && res.verdict === 'PROVEN_CLEAN'));
    } catch (err) {
      setProveError(err instanceof Error ? err.message : '执行形式化体检失败');
    } finally {
      setProveLoading(false);
    }
  }, []);

  const selectedEntity = useMemo(
    () => activeBlueprint?.object_types.find((o) => o.name === selectedType) || null,
    [activeBlueprint, selectedType]
  );

  const filteredActions = useMemo(() => {
    if (!activeBlueprint) return [];
    return activeBlueprint.actions.filter((act) => {
      if (actionFilter === 'rule') return act.implementation_kind === 'rule';
      if (actionFilter === 'edit_function') return act.implementation_kind === 'edit_function';
      if (actionFilter === 'selected') {
        return !selectedType || act.touched_object_types.includes(selectedType);
      }
      return true;
    });
  }, [activeBlueprint, actionFilter, selectedType]);

  if (loadError) {
    return (
      <div className="rounded-2xl border border-rose-200 bg-white p-6 text-center text-xs text-rose-700 shadow-sm">
        <p className="font-semibold">模型蓝图加载失败：{loadError}</p>
        <button
          type="button"
          onClick={loadBlueprint}
          className="mt-3 rounded-lg bg-[#0F172A] px-4 py-1.5 font-semibold text-white"
        >
          重新加载
        </button>
      </div>
    );
  }

  if (loading || !activeBlueprint) {
    return (
      <div
        data-testid="blueprint-loading"
        className="flex items-center justify-center rounded-2xl border border-slate-200 bg-white p-12 text-xs text-slate-500 shadow-sm"
      >
        正在解析声明式本体模型契约与形式化蓝图...
      </div>
    );
  }

  const { summary } = activeBlueprint;
  const isDraftPreview = Boolean(draftState?.draft_has_changes);
  const draftProofBlocked = Boolean(
    activeProveResult?.prover_available && activeProveResult?.verdict === 'ISSUES_FOUND'
  );

  return (
    <div className="space-y-5" data-testid="blueprint-page">
      {/* Draft Live Preview Banner */}
      {isDraftPreview && draftState && (
        <div
          data-testid="blueprint-draft-banner"
          className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-violet-300 bg-violet-50/90 px-5 py-3 text-xs text-violet-950 shadow-sm"
        >
          <div className="flex items-center gap-2.5">
            <Sparkles className="h-4 w-4 shrink-0 text-violet-700" />
            <div>
              <span className="font-semibold">
                正在预览 M1 草稿增量（{draftState.draft_id} · {draftState.draft_clauses_zh.length} 项变更，尚未应用到主世界）
              </span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {onOpenExpert && (
              <button
                type="button"
                onClick={onOpenExpert}
                className="rounded-lg border border-violet-300 bg-white px-2.5 py-1 font-medium text-violet-800 hover:bg-violet-100/60"
              >
                继续编辑草稿
              </button>
            )}
            {onExportDraft && (
              <button
                type="button"
                onClick={onExportDraft}
                className="rounded-lg border border-violet-300 bg-white px-2.5 py-1 font-medium text-violet-800 hover:bg-violet-100/60"
              >
                导出 DOAL JSON
              </button>
            )}
            {meta.demo_actor_switch && onApplyDraft && (
              <button
                type="button"
                disabled={draftProofBlocked}
                onClick={() => onApplyDraft()}
                className="rounded-lg bg-violet-700 px-3 py-1 font-semibold text-white hover:bg-violet-800 disabled:opacity-45"
              >
                应用到演示世界
              </button>
            )}
            {onDiscardDraft && (
              <button
                type="button"
                onClick={() => onDiscardDraft()}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-slate-600 hover:bg-slate-50"
              >
                放弃草稿
              </button>
            )}
          </div>
        </div>
      )}

      {/* Header Strip */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200/80 bg-white px-5 py-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div>
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-base font-bold text-slate-900">
              M1 模型蓝图与形式化契约
            </h1>
            <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-0.5 text-[11px] font-medium text-slate-700 ring-1 ring-slate-200">
              <Lock className="h-3 w-3 text-slate-500" />
              {isDraftPreview ? '草稿实时预览中' : '只读蓝图 · 变更经由 Git / Overlay CI'}
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <span className="font-medium text-slate-700">{activeBlueprint.spec_name}</span>
            <span>·</span>
            <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">
              v{activeBlueprint.spec_version}
            </code>
            <span>·</span>
            <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-500">
              #{activeBlueprint.blueprint_fingerprint.slice(0, 12)}
            </code>
          </div>
        </div>

        <button
          type="button"
          data-testid="run-z3-prove-btn"
          disabled={proveLoading}
          onClick={() => handleRunProve(false)}
          className="inline-flex items-center gap-2 rounded-xl bg-[#0F172A] px-4 py-2.5 text-xs font-semibold text-white shadow-sm transition hover:bg-slate-800 disabled:opacity-60"
        >
          <Cpu className="h-4 w-4 text-emerald-400" />
          <span>{proveLoading ? '形式化核验运行中...' : '一键 Z3 形式化体检'}</span>
        </button>
      </div>

      {/* Top 5 M1 KPI Cards */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
        <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="text-xs font-medium text-slate-500">模型实体类型</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-slate-900">
            {summary.object_type_count} 类
          </div>
          <div className="mt-1 text-[11px] text-emerald-700">
            含状态机：{summary.fsm_object_count} 类
          </div>
        </div>

        <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="text-xs font-medium text-slate-500">声明关系连线</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-slate-900">
            {summary.link_count} 条
          </div>
          <div className="mt-1 text-[11px] text-slate-400">结构化语义外键图谱</div>
        </div>

        <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="text-xs font-medium text-slate-500">受控治理动作</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-slate-900">
            {summary.action_count} 项
          </div>
          <div className="mt-1 flex items-center gap-2 text-[11px]">
            <span className="font-medium text-sky-700">Rule {summary.rule_action_count}</span>
            <span className="text-slate-300">|</span>
            <span className="font-medium text-violet-700">
              EditFn {summary.edit_function_action_count}
            </span>
          </div>
        </div>

        <div className="rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="text-xs font-medium text-slate-500">硬红线不变量</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-slate-900">
            {summary.invariant_count} 条
          </div>
          <div className="mt-1 text-[11px] text-slate-400">每次事务提交前强制阻断违例</div>
        </div>

        <div className="col-span-2 rounded-xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)] sm:col-span-1">
          <div className="text-xs font-medium text-slate-500">只读计算函数</div>
          <div className="mt-1.5 font-mono text-2xl font-bold tabular-nums text-slate-900">
            {summary.function_count} 个
          </div>
          <div className="mt-1 text-[11px] text-emerald-700">
            写入原语 {summary.edit_function_count} 个
          </div>
        </div>
      </div>

      {/* Z3 Formal Proof Panel */}
      <Z3ProvePanel
        result={activeProveResult}
        loading={proveLoading}
        error={proveError}
        collapsed={isDraftPreview ? proveCollapsed && !draftProofBlocked : proveCollapsed}
        onToggleCollapse={() => setProveCollapsed((v) => !v)}
        onReRun={() => handleRunProve(true)}
      />

      {/* Center Workspace: 60% Topology + 40% Object & FSM Inspector */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <div className="lg:col-span-7">
          <M1TopologyGraph
            objectTypes={activeBlueprint.object_types}
            links={activeBlueprint.links}
            selectedType={selectedType}
            onSelectType={(name) => setSelectedType(name)}
          />
        </div>
        <div className="lg:col-span-5">
          <ObjectBlueprintInspector
            entity={selectedEntity}
            actions={activeBlueprint.actions}
            display={meta.display}
            onSelectActionFilter={(typeName) => {
              setSelectedType(typeName);
              setBottomTab('actions');
              setActionFilter('selected');
              bottomPanelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }}
          />
        </div>
      </div>

      {/* Bottom Tripartite Kinetic & Governance Panel */}
      <div
        ref={bottomPanelRef}
        className="rounded-xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
      >
        {/* Tab Strip */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-3.5">
          <div className="flex flex-wrap items-center gap-1 rounded-xl bg-slate-100/80 p-1">
            <button
              type="button"
              data-testid="blueprint-tab-actions"
              onClick={() => setBottomTab('actions')}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                bottomTab === 'actions'
                  ? 'bg-[#0F172A] text-white shadow-sm'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <Zap className="h-3.5 w-3.5" />
              <span>治理动作契约 ({activeBlueprint.actions.length})</span>
            </button>

            <button
              type="button"
              data-testid="blueprint-tab-primitives"
              onClick={() => setBottomTab('primitives')}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                bottomTab === 'primitives'
                  ? 'bg-[#0F172A] text-white shadow-sm'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <Code2 className="h-3.5 w-3.5" />
              <span>
                图灵完备原语 & 只读函数 (
                {activeBlueprint.edit_functions.length + activeBlueprint.functions.length})
              </span>
            </button>

            <button
              type="button"
              data-testid="blueprint-tab-invariants"
              onClick={() => setBottomTab('invariants')}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
                bottomTab === 'invariants'
                  ? 'bg-[#0F172A] text-white shadow-sm'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <ShieldCheck className="h-3.5 w-3.5" />
              <span>系统红线不变量 ({activeBlueprint.invariants.length})</span>
            </button>
          </div>

          {bottomTab === 'actions' && (
            <div className="flex flex-wrap items-center gap-1.5 text-xs">
              {(
                [
                  ['all', `全部 (${activeBlueprint.actions.length})`],
                  ['rule', `声明式 Rule (${summary.rule_action_count})`],
                  ['edit_function', `受审 EditFunction (${summary.edit_function_action_count})`],
                  ['selected', selectedEntity ? `仅 ${selectedEntity.display_label}` : '当前选中'],
                ] as const
              ).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  data-testid={`action-filter-${key}`}
                  onClick={() => setActionFilter(key)}
                  className={`rounded-lg px-2.5 py-1 text-[11px] font-medium transition ${
                    actionFilter === key
                      ? 'bg-blue-600 text-white'
                      : 'border border-slate-200 bg-white text-slate-600 hover:bg-slate-50'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Tab Body */}
        <div className="p-5">
          {bottomTab === 'actions' && (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {filteredActions.length === 0 ? (
                <div className="col-span-full rounded-xl border border-dashed border-slate-200 py-8 text-center text-xs text-slate-400">
                  当前筛选条件下暂无匹配的治理动作
                </div>
              ) : (
                filteredActions.map((act) => {
                  const isRule = act.implementation_kind === 'rule';
                  const autoBadge = AUTONOMY_BADGE[act.autonomy_mode] || AUTONOMY_BADGE.auto;
                  const isDraftAct = act.draft_status === 'draft_added';
                  return (
                    <div
                      key={act.name}
                      data-testid={`blueprint-action-${act.name}`}
                      className={`flex flex-col justify-between rounded-xl border p-4 ${
                        isDraftAct
                          ? 'border-violet-300 bg-violet-50/40'
                          : 'border-slate-200 bg-slate-50/40'
                      }`}
                    >
                      <div>
                        <div className="flex flex-wrap items-start justify-between gap-2">
                          <div>
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="text-sm font-semibold text-slate-900">
                                {act.display_label}
                              </span>
                              {isDraftAct && (
                                <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold text-violet-800">
                                  草稿新增
                                </span>
                              )}
                              <span
                                className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                                  isRule
                                    ? 'bg-sky-100 text-sky-800'
                                    : 'bg-violet-100 text-violet-800'
                                }`}
                              >
                                {isRule ? (
                                  <>
                                    <Sparkles className="h-2.5 w-2.5" /> 声明式 Rule
                                  </>
                                ) : (
                                  <>
                                    <Code2 className="h-2.5 w-2.5" /> 受审 EditFunction
                                  </>
                                )}
                              </span>
                            </div>
                            <div className="mt-0.5 font-mono text-[11px] text-slate-500">
                              {act.name}
                              {!isRule && act.edit_function_name && (
                                <span className="ml-2 text-violet-700">
                                  → {act.edit_function_name}()
                                </span>
                              )}
                            </div>
                          </div>

                          <span
                            className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${autoBadge.cls}`}
                          >
                            {autoBadge.label}
                          </span>
                        </div>

                        {act.description && (
                          <p className="mt-2 text-xs text-slate-600">{act.description}</p>
                        )}

                        {isRule && act.rule_summary && (
                          <div className="mt-2.5 space-y-1.5 rounded-lg border border-sky-200/80 bg-white p-2.5 text-[11px]">
                            <div className="font-semibold text-sky-900">
                              声明式规则契约（Z3 原生全路径可证明）
                            </div>
                            {act.rule_summary.preconditions.map((pre) => (
                              <div key={pre.name} className="text-slate-600">
                                <span className="font-medium text-slate-800">前置守卫：</span>
                                {pre.description || pre.name}{' '}
                                <code className="font-mono text-[10px] text-slate-400">
                                  [{pre.code}]
                                </code>
                              </div>
                            ))}
                            {act.rule_summary.mutations.map((m, idx) => (
                              <div key={idx} className="text-slate-600">
                                <span className="font-medium text-slate-800">状态变更：</span>
                                <code className="font-mono text-[10px] text-sky-700">
                                  {m.op} {m.object_type} ({m.set_fields.join(', ')})
                                </code>
                              </div>
                            ))}
                          </div>
                        )}

                        {!isRule && act.contract && (
                          <div
                            data-testid={`ef-contract-${act.name}`}
                            className="mt-2.5 space-y-1.5 rounded-lg border border-violet-200/80 bg-white p-2.5 text-[11px]"
                          >
                            <div className="font-semibold text-violet-900">
                              模块化行为契约（Z3 PO-5 非空真 + PO-4b 保真证明 · 运行期强验）
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                              {act.contract.modifies.map((m) => (
                                <span
                                  key={`mod-${m}`}
                                  className="rounded border border-violet-200 bg-violet-50 px-1.5 py-0.5 font-mono text-[10px] text-violet-800"
                                >
                                  modifies: {m}
                                </span>
                              ))}
                              {act.contract.creates.map((c) => (
                                <span
                                  key={`cre-${c}`}
                                  className="rounded border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] text-emerald-800"
                                >
                                  creates: {c}
                                </span>
                              ))}
                              {act.contract.deletes.map((d) => (
                                <span
                                  key={`del-${d}`}
                                  className="rounded border border-rose-200 bg-rose-50 px-1.5 py-0.5 font-mono text-[10px] text-rose-800"
                                >
                                  deletes: {d}
                                </span>
                              ))}
                              <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[10px] text-slate-700">
                                ensures × {act.contract.ensures.length}
                              </span>
                              {act.contract.planner_function && (
                                <span
                                  data-testid={`m25-planner-${act.name}`}
                                  className="rounded border border-sky-200 bg-sky-50 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-sky-800"
                                >
                                  M2.5 纯算大脑: {act.contract.planner_function}
                                </span>
                              )}
                              {act.contract.planner_function && (
                                <span
                                  data-testid={`m25-trust-${act.name}`}
                                  className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold ${
                                    act.contract.trust_mode === 'attested_provider'
                                      ? 'border-indigo-200 bg-indigo-50 text-indigo-800'
                                      : 'border-emerald-200 bg-emerald-50 text-emerald-800'
                                  }`}
                                >
                                  {act.contract.trust_mode === 'attested_provider'
                                    ? 'Class B · 权威公式签验'
                                    : 'Class A · 免信任可验真'}
                                  {act.contract.require_read_set_cas ? ' · Read-Set CAS' : ''}
                                </span>
                              )}
                            </div>
                          </div>
                        )}
                      </div>

                      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-slate-200/70 pt-2.5 text-[11px]">
                        <div className="flex flex-wrap items-center gap-1">
                          {act.touched_object_types.map((ot) => (
                            <button
                              key={ot}
                              type="button"
                              onClick={() => setSelectedType(ot)}
                              className="rounded bg-white px-1.5 py-0.5 font-mono text-[10px] font-medium text-slate-700 ring-1 ring-slate-200 hover:bg-blue-50 hover:text-blue-700"
                            >
                              {ot}
                            </button>
                          ))}
                        </div>
                        <div className="flex flex-wrap items-center gap-1 text-[10px] text-slate-500">
                          <span>授权：</span>
                          {act.allowed_roles.slice(0, 3).map((r) => (
                            <span key={r} className="rounded bg-slate-100 px-1.5 py-0.5">
                              {displayRoleName(r, meta.display)}
                            </span>
                          ))}
                          {act.allowed_roles.length > 3 && (
                            <span>+{act.allowed_roles.length - 3}</span>
                          )}
                          {act.in_role_scope && (
                            <CheckCircle2
                              className="ml-0.5 h-3 w-3 text-emerald-600"
                              aria-label="当前账号可执行"
                            />
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          )}

          {bottomTab === 'primitives' && (
            <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
              {/* Write Primitives */}
              <div>
                <div className="mb-3 flex items-center gap-2 text-xs font-semibold text-slate-900">
                  <GitFork className="h-4 w-4 text-violet-700" />
                  <span>图灵完备写原语 · EditFunctions ({activeBlueprint.edit_functions.length})</span>
                </div>
                <div className="max-h-96 space-y-2 overflow-y-auto pr-1">
                  {activeBlueprint.edit_functions.map((ef) => (
                    <div
                      key={ef.name}
                      className="rounded-xl border border-slate-200 bg-slate-50/50 p-3 text-xs"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-semibold text-slate-900">{ef.display_label}</span>
                        <code className="rounded bg-violet-50 px-1.5 py-0.5 font-mono text-[10px] text-violet-800 ring-1 ring-violet-200">
                          {ef.name}
                        </code>
                      </div>
                      {ef.description && (
                        <p className="mt-1 text-[11px] text-slate-600">{ef.description}</p>
                      )}
                      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px] text-slate-500">
                        <span>绑定动作：{ef.bound_actions.join(', ') || '—'}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Read-Only Functions */}
              <div>
                <div className="mb-3 flex items-center gap-2 text-xs font-semibold text-slate-900">
                  <Eye className="h-4 w-4 text-emerald-700" />
                  <span>只读计算函数 · Read-Only Functions ({activeBlueprint.functions.length})</span>
                </div>
                <div className="max-h-96 space-y-2 overflow-y-auto pr-1">
                  {activeBlueprint.functions.map((fn) => (
                    <div
                      key={fn.name}
                      className="rounded-xl border border-emerald-200/80 bg-emerald-50/30 p-3 text-xs"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-semibold text-slate-900">{fn.display_label}</span>
                        <span className="rounded-full bg-emerald-100 px-2 py-0.5 font-mono text-[10px] font-semibold text-emerald-800">
                          read_only
                        </span>
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-slate-500">{fn.name}</div>
                      {fn.description && (
                        <p className="mt-1 text-[11px] text-slate-600">{fn.description}</p>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {bottomTab === 'invariants' && (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {activeBlueprint.invariants.map((inv) => (
                <div
                  key={`${inv.kind}:${inv.code || inv.name}`}
                  className={`rounded-xl border p-4 text-xs ${
                    inv.draft_status === 'draft_added'
                      ? 'border-violet-300 bg-violet-50/40'
                      : 'border-slate-200 bg-slate-50/40'
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <Layers className="h-3.5 w-3.5 text-slate-600" />
                      <span className="font-mono font-semibold text-slate-900">{inv.name}</span>
                      {inv.draft_status === 'draft_added' && (
                        <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold text-violet-800">
                          草稿新增
                        </span>
                      )}
                    </div>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                        inv.kind === 'transition'
                          ? 'bg-amber-50 text-amber-800 ring-1 ring-amber-200'
                          : 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200'
                      }`}
                    >
                      {inv.kind === 'transition' ? '跃迁不变量' : '状态不变量'}
                    </span>
                  </div>
                  {inv.code && (
                    <div className="mt-1 font-mono text-[10px] text-slate-500">
                      错误码：{inv.code}
                    </div>
                  )}
                  <p className="mt-2 leading-relaxed text-slate-700">{inv.description}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
