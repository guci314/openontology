import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  FlaskConical,
  Play,
  RotateCcw,
  Rocket,
  ShieldCheck,
} from 'lucide-react';
import type {
  ConsoleMeta,
  RecordInstance,
  SandboxDiffResponse,
  SandboxPreset,
  SchemaCatalog,
} from '../../api/client';
import { consoleApi } from '../../api/client';
import {
  displayActionName,
  displayEntityName,
  displayFieldName,
  displayYuanFieldLabel,
  formatSmartFieldValue,
  translateError,
} from '../i18nErrorMap';
import { type DomainViewPack, resolveShellTerminology } from '../packRegistry';

interface SandboxPageProps {
  meta: ConsoleMeta;
  schema: SchemaCatalog;
  pack: DomainViewPack | null;
  pendingPresetId: string | null;
  onConsumePendingPreset: () => void;
  onPromotedToPrimary: () => void;
}

export const SandboxPage: React.FC<SandboxPageProps> = ({
  meta,
  schema,
  pack,
  pendingPresetId,
  onConsumePendingPreset,
  onPromotedToPrimary,
}) => {
  const terms = resolveShellTerminology(pack);
  const [sandboxId, setSandboxId] = useState<string | null>(null);
  const sandboxIdRef = useRef<string | null>(null);
  const [diff, setDiff] = useState<SandboxDiffResponse | null>(null);
  const [activePresetId, setActivePresetId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusBanner, setStatusBanner] = useState<{ ok: boolean; text: string } | null>(null);

  const [selectedAction, setSelectedAction] = useState<string>('');
  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [candidateObjects, setCandidateObjects] = useState<RecordInstance[]>([]);

  const ensureSandbox = useCallback(async (): Promise<string> => {
    if (sandboxIdRef.current) return sandboxIdRef.current;
    const created = await consoleApi.createSandbox();
    sandboxIdRef.current = created.sandbox_id;
    setSandboxId(created.sandbox_id);
    return created.sandbox_id;
  }, []);

  const refreshDiff = useCallback(async (sid: string) => {
    const d = await consoleApi.diffSandbox(sid);
    setDiff(d);
    return d;
  }, []);

  useEffect(() => {
    if (pendingPresetId) return;
    let active = true;
    consoleApi
      .createSandbox()
      .then((res) => {
        if (!active) return;
        sandboxIdRef.current = res.sandbox_id;
        setSandboxId(res.sandbox_id);
        return consoleApi.diffSandbox(res.sandbox_id).then((d) => {
          if (active) setDiff(d);
        });
      })
      .catch(() => {});
    return () => {
      active = false;
    };
    // Initialize a clean fork per actor mount when not jumping straight into a preset
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta.actor.actor_id]);

  useEffect(() => {
    if (schema.actions.length && !schema.actions.some((a) => a.name === selectedAction)) {
      setSelectedAction(schema.actions[0].name);
      setFormValues({});
      setFieldErrors({});
    }
  }, [schema.actions, selectedAction]);

  const selectedActDef = useMemo(
    () => schema.actions.find((a) => a.name === selectedAction),
    [schema.actions, selectedAction]
  );

  // Preload visible candidate objects of touched entity types for <datalist> ID hints
  useEffect(() => {
    const touched = selectedActDef?.touched_object_types || [];
    if (!touched.length) {
      setCandidateObjects([]);
      return;
    }
    let active = true;
    Promise.all(
      touched.map((ot) =>
        consoleApi.listObjects(ot).then((r) => r.items || []).catch(() => [] as RecordInstance[])
      )
    ).then((lists) => {
      if (active) setCandidateObjects(lists.flat());
    });
    return () => {
      active = false;
    };
  }, [selectedActDef, meta.actor.actor_id]);

  const runPreset = useCallback(
    async (preset: SandboxPreset) => {
      setBusy(true);
      setStatusBanner(null);
      try {
        const oldSid = sandboxIdRef.current;
        if (oldSid) {
          await consoleApi.destroySandbox(oldSid).catch(() => {});
          sandboxIdRef.current = null;
        }
        const created = await consoleApi.createSandbox();
        const sid = created.sandbox_id;
        sandboxIdRef.current = sid;
        setSandboxId(sid);

        const res = await consoleApi.submitSandbox(sid, { steps: preset.steps });
        await refreshDiff(sid);
        const stepResults =
          (res.step_results as Array<{
            ok?: boolean;
            error_code?: string;
            error?: string;
            hint_zh?: string;
          }>) || [];
        const allOk = Boolean(res.ok) && stepResults.every((s) => Boolean(s.ok));
        if (allOk) {
          setActivePresetId(preset.preset_id);
          setStatusBanner({
            ok: true,
            text: `沙盘推演已完成「${preset.title}」(${preset.steps.length} 步)，真实系统保持零影响。`,
          });
        } else {
          const failedStep = stepResults.find((s) => !s.ok);
          setStatusBanner({
            ok: false,
            text: translateError(
              (failedStep?.error_code || res.error_code) as string,
              (failedStep?.error || res.error) as string,
              (failedStep?.hint_zh || res.hint_zh) as string
            ),
          });
        }
      } catch (err) {
        setStatusBanner({
          ok: false,
          text: err instanceof Error ? err.message : '沙盘推演执行失败',
        });
      } finally {
        setBusy(false);
      }
    },
    [refreshDiff]
  );

  useEffect(() => {
    if (!pendingPresetId) return;
    const target = meta.sandbox_presets.find((p) => p.preset_id === pendingPresetId);
    onConsumePendingPreset();
    if (target) {
      runPreset(target);
    }
  }, [pendingPresetId, meta.sandbox_presets, onConsumePendingPreset, runPreset]);

  const handleRunCustomStep = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedAction || busy) return;
    const props = selectedActDef?.params?.properties || {};
    const requiredSet = new Set(selectedActDef?.params?.required || []);
    const nextErrs: Record<string, string> = {};
    const parsedParams: Record<string, unknown> = {};

    for (const [key, propMeta] of Object.entries(props)) {
      const raw = (formValues[key] ?? '').trim();
      const label = displayFieldName('', key, meta.display);
      if (!raw) {
        if (propMeta.default !== undefined && propMeta.default !== '') {
          parsedParams[key] = propMeta.default;
        } else if (requiredSet.has(key)) {
          nextErrs[key] = `请输入${label}`;
        }
        continue;
      }
      if (propMeta.type === 'integer') {
        const num = parseInt(raw, 10);
        if (Number.isNaN(num)) nextErrs[key] = `「${label}」须为整数`;
        else parsedParams[key] = num;
      } else if (propMeta.type === 'number') {
        const num = parseFloat(raw);
        if (Number.isNaN(num)) nextErrs[key] = `「${label}」须为有效数值`;
        else parsedParams[key] = num;
      } else {
        parsedParams[key] = raw;
      }
    }

    if (Object.keys(nextErrs).length > 0) {
      setFieldErrors(nextErrs);
      setStatusBanner({
        ok: false,
        text: `请补充必填参数后再提交推演：${Object.values(nextErrs).join('；')}`,
      });
      return;
    }

    setFieldErrors({});
    setBusy(true);
    setStatusBanner(null);
    try {
      const sid = await ensureSandbox();
      const res = await consoleApi.submitSandbox(sid, {
        action: selectedAction,
        params: parsedParams,
      });
      await refreshDiff(sid);
      const stepResults =
        (res.step_results as Array<{
          ok?: boolean;
          error_code?: string;
          error?: string;
          hint_zh?: string;
        }>) || [];
      const allOk = Boolean(res.ok) && stepResults.every((s) => Boolean(s.ok));
      if (allOk) {
        setStatusBanner({
          ok: true,
          text: `已在沙盘内推演动作「${displayActionName(selectedAction, meta.display)}」，真实系统零写入。`,
        });
      } else {
        const failedStep = stepResults.find((s) => !s.ok);
        setStatusBanner({
          ok: false,
          text: translateError(
            (failedStep?.error_code || res.error_code) as string,
            (failedStep?.error || res.error) as string,
            (failedStep?.hint_zh || res.hint_zh) as string
          ),
        });
      }
    } catch (err) {
      setStatusBanner({
        ok: false,
        text: err instanceof Error ? err.message : '动作试演未通过校验',
      });
    } finally {
      setBusy(false);
    }
  };

  const handleResetSandbox = async () => {
    if (busy) return;
    setBusy(true);
    setStatusBanner(null);
    setActivePresetId(null);
    try {
      const oldSid = sandboxIdRef.current;
      if (oldSid) {
        await consoleApi.destroySandbox(oldSid).catch(() => {});
        sandboxIdRef.current = null;
      }
      const created = await consoleApi.createSandbox();
      sandboxIdRef.current = created.sandbox_id;
      setSandboxId(created.sandbox_id);
      await refreshDiff(created.sandbox_id);
      setStatusBanner({
        ok: true,
        text: '已清空并销毁旧沙盘分支，主世界状态零变化。',
      });
    } finally {
      setBusy(false);
    }
  };

  const handlePromoteSandbox = async () => {
    const sid = sandboxIdRef.current;
    if (!sid || !diff?.step_count || busy) return;
    setBusy(true);
    setStatusBanner(null);
    try {
      const res = await consoleApi.promoteSandbox(sid);
      if (res.ok) {
        setActivePresetId(null);
        setStatusBanner({
          ok: true,
          text: `已将 ${Number(res.promoted_count || 0)} 步推演正式提交至主世界执行并计入审计。`,
        });
        const oldSid = sandboxIdRef.current;
        if (oldSid) {
          await consoleApi.destroySandbox(oldSid).catch(() => {});
          sandboxIdRef.current = null;
        }
        const created = await consoleApi.createSandbox();
        sandboxIdRef.current = created.sandbox_id;
        setSandboxId(created.sandbox_id);
        await refreshDiff(created.sandbox_id);
        onPromotedToPrimary();
      } else {
        const results = (res.results as Array<Record<string, unknown>>) || [];
        const failed = results.find((r) => !r.ok || !r.committed);
        setStatusBanner({
          ok: false,
          text: translateError(
            failed?.error_code as string,
            failed?.error as string,
            failed?.hint_zh as string
          ),
        });
      }
    } catch (err) {
      setStatusBanner({
        ok: false,
        text: err instanceof Error ? err.message : '转正式执行未获通过',
      });
    } finally {
      setBusy(false);
    }
  };

  const SandboxPanel = pack?.SandboxPanel;

  return (
    <div className="space-y-5" data-testid="sandbox-page">
      {/* Top Safe Isolation Green Strip */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50/90 px-5 py-3.5 text-emerald-900 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-600 text-white">
            <ShieldCheck className="h-4 w-4" />
          </div>
          <div>
            <div className="text-sm font-bold">
              沙盘模式：对真实软件零影响
            </div>
            <div className="text-xs text-emerald-800/80">
              当前所有假设演练均在物理隔离的内存分支运行，绝不写入真实运行系统或外部软件。
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            data-testid="sandbox-reset-btn"
            disabled={busy}
            onClick={handleResetSandbox}
            className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg border border-emerald-300 bg-white px-3 py-1.5 text-xs font-semibold text-emerald-900 transition hover:bg-emerald-100/60 disabled:opacity-50"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            丢弃并重置沙盘
          </button>
          <button
            type="button"
            data-testid="sandbox-promote-btn"
            disabled={busy || !diff?.step_count}
            onClick={handlePromoteSandbox}
            className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg bg-[#0F172A] px-3.5 py-1.5 text-xs font-semibold text-white shadow-sm transition hover:bg-slate-800 disabled:opacity-40"
          >
            <Rocket className="h-3.5 w-3.5" />
            转正式执行 ({diff?.step_count || 0})
          </button>
        </div>
      </div>

      {statusBanner && (
        <div
          data-testid="sandbox-status-banner"
          className={`flex items-center gap-2 rounded-xl border px-4 py-3 text-xs font-medium ${
            statusBanner.ok
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
              : 'border-rose-200 bg-rose-50 text-rose-800'
          }`}
        >
          {statusBanner.ok ? <CheckCircle2 className="h-4 w-4 shrink-0" /> : <AlertCircle className="h-4 w-4 shrink-0" />}
          <span>{statusBanner.text}</span>
        </div>
      )}

      {/* Split Workspace */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* Left: Scenarios + Custom Simulation Form */}
        <div className="space-y-5 lg:col-span-5">
          {meta.sandbox_presets.length > 0 && (
            <div className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
              <div className="mb-3 flex items-center gap-2">
                <FlaskConical className="h-4 w-4 text-blue-700" />
                <h2 className="text-sm font-semibold text-slate-900">一键决策推演场景</h2>
              </div>
              <div className="space-y-2.5">
                {meta.sandbox_presets.map((p) => {
                  const isActive = activePresetId === p.preset_id;
                  return (
                    <button
                      key={p.preset_id}
                      type="button"
                      data-testid={`sandbox-preset-${p.preset_id}`}
                      disabled={busy}
                      onClick={() => runPreset(p)}
                      className={`group flex w-full items-start justify-between gap-3 rounded-xl border p-3.5 text-left transition disabled:opacity-50 ${
                        isActive
                          ? 'border-blue-500 bg-blue-50/60 ring-1 ring-blue-500/20'
                          : 'border-slate-200 hover:border-blue-400 hover:bg-blue-50/40'
                      }`}
                    >
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-bold text-slate-900 group-hover:text-blue-800">
                            {p.title}
                          </span>
                          {p.badge && (
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
                              {p.badge}
                            </span>
                          )}
                        </div>
                        <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
                          {p.subtitle}
                        </p>
                      </div>
                      <span className="mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-lg bg-[#0F172A] px-2.5 py-1 text-[11px] font-semibold text-white group-hover:bg-blue-700">
                        <Play className="h-3 w-3" /> {isActive ? '重跑' : '推演'}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Custom manual action trial */}
          <div className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
            <h2 className="text-sm font-semibold text-slate-900">手动单步假设试演</h2>
            <p className="mt-0.5 text-xs text-slate-500">
              {terms.sandboxManualSubtitle}
            </p>
            <form onSubmit={handleRunCustomStep} noValidate className="mt-4 space-y-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">选择推演操作</label>
                <select
                  value={selectedAction}
                  onChange={(e) => {
                    setSelectedAction(e.target.value);
                    setFormValues({});
                    setFieldErrors({});
                  }}
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-900 focus:border-blue-500 focus:outline-none"
                >
                  {schema.actions.map((act) => (
                    <option key={act.name} value={act.name}>
                      {act.display_label || displayActionName(act.name, meta.display)}
                    </option>
                  ))}
                </select>
                {selectedActDef?.description && (
                  <p className="mt-1 text-[11px] text-slate-400">{selectedActDef.description}</p>
                )}
              </div>

              {Object.entries(selectedActDef?.params?.properties || {}).map(([pname, pschema]) => {
                const label = displayFieldName('', pname, meta.display);
                const hasDefault = pschema.default !== undefined && pschema.default !== '';
                const isReq = Boolean(selectedActDef?.params?.required?.includes(pname));
                const listId =
                  pschema.type !== 'integer' && pschema.type !== 'number' && candidateObjects.length > 0
                    ? `sbx-dl-${selectedAction}-${pname}`
                    : undefined;
                const matchingCandidates = listId
                  ? candidateObjects.filter((obj) => {
                      if (candidateObjects.length <= 8) return true;
                      const pnameBase = pname.replace(/_id$/, '').toLowerCase();
                      return (
                        obj.object_type.toLowerCase().includes(pnameBase) ||
                        obj.primary_key.toLowerCase() === pname.toLowerCase()
                      );
                    })
                  : [];
                const suggestions =
                  matchingCandidates.length > 0 ? matchingCandidates : candidateObjects.slice(0, 12);

                return (
                  <div key={pname}>
                    <label className="mb-1 block text-xs font-medium text-slate-600">
                      {label}
                      {isReq && <span className="ml-1 text-rose-500">*</span>}
                    </label>
                    <input
                      type={pschema.type === 'integer' || pschema.type === 'number' ? 'number' : 'text'}
                      step="any"
                      list={listId}
                      data-testid={`sandbox-param-${pname}`}
                      value={formValues[pname] ?? ''}
                      onChange={(e) => {
                        setFormValues((prev) => ({ ...prev, [pname]: e.target.value }));
                        if (fieldErrors[pname]) {
                          setFieldErrors((prev) => {
                            const next = { ...prev };
                            delete next[pname];
                            return next;
                          });
                        }
                      }}
                      placeholder={hasDefault ? `默认：${String(pschema.default)}` : `请输入${label}`}
                      className={`w-full rounded-lg border px-3 py-1.5 font-mono text-xs text-slate-900 focus:bg-white focus:outline-none ${
                        fieldErrors[pname]
                          ? 'border-rose-400 bg-rose-50/40 focus:border-rose-500'
                          : 'border-slate-200 bg-slate-50 focus:border-blue-500'
                      }`}
                    />
                    {listId && suggestions.length > 0 && (
                      <datalist id={listId}>
                        {suggestions.slice(0, 15).map((obj) => (
                          <option key={`${obj.object_type}:${obj.object_id}`} value={obj.object_id}>
                            {displayEntityName(obj.object_type, meta.display)} · {obj.title}
                          </option>
                        ))}
                      </datalist>
                    )}
                    {fieldErrors[pname] && (
                      <p className="mt-1 text-[11px] text-rose-600">{fieldErrors[pname]}</p>
                    )}
                  </div>
                );
              })}

              <button
                type="submit"
                data-testid="sandbox-custom-submit"
                disabled={busy || !selectedAction}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-[#0F172A] px-4 py-2 text-xs font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" />
                加入沙盘演练
              </button>
            </form>
          </div>
        </div>

        {/* Right: Primary vs Sandbox Side-by-Side Comparison */}
        <div className="space-y-5 lg:col-span-7">
          {SandboxPanel && (
            <SandboxPanel
              meta={meta}
              schema={schema}
              sandboxId={sandboxId}
              sandboxDiff={diff}
              busy={busy}
              onRunPreset={async (pid) => {
                const target = meta.sandbox_presets.find((p) => p.preset_id === pid);
                if (target) await runPreset(target);
              }}
            />
          )}

          {/* Generic L1 Object-by-Object Diff Table */}
          <div className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div>
                <h2 className="text-sm font-semibold text-slate-900">
                  {terms.sandboxDiffTitle}
                </h2>
                <p className="text-xs text-slate-500">
                  共 {diff?.step_count || 0} 步推演，引发 {diff?.total_changes || 0} 项{terms.recordNoun}变动
                </p>
              </div>
              <span className="whitespace-nowrap rounded-full bg-emerald-50 px-2.5 py-0.5 text-[11px] font-semibold text-emerald-700 ring-1 ring-emerald-200">
                主库零写入
              </span>
            </div>

            {!diff || diff.total_changes === 0 ? (
              <div className="py-10 text-center text-xs text-slate-400">
                请从左侧选择一键场景或试演一个动作，系统将即时展示「当前现状」与「推演之后」的精准对比。
              </div>
            ) : (
              <div className="mt-4 space-y-3" data-testid="sandbox-diff-list">
                {diff.updated.map((u) => (
                  <div
                    key={`${u.object_type}:${u.object_id}`}
                    className="rounded-xl border border-slate-200 bg-slate-50/50 p-3.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-slate-900">
                        {displayEntityName(u.object_type, meta.display)} ·{' '}
                        <span className="font-mono">{u.object_id}</span>
                        {u.object_title && u.object_title !== u.object_id && (
                          <span className="ml-1.5 font-normal text-slate-500">
                            {u.object_title}
                          </span>
                        )}
                      </span>
                      <span className="shrink-0 whitespace-nowrap rounded bg-blue-50 px-2 py-0.5 text-[10px] font-semibold text-blue-700">
                        属性变更 ({u.field_deltas.length})
                      </span>
                    </div>
                    <div className="mt-2.5 divide-y divide-slate-200/60 rounded-lg border border-slate-200 bg-white">
                      {u.field_deltas.map((fd) => (
                        <div
                          key={fd.field}
                          className="grid grid-cols-12 items-center gap-2 px-3 py-2 text-xs"
                        >
                          <span className="col-span-4 font-medium text-slate-600">
                            {displayYuanFieldLabel(
                              displayFieldName(u.object_type, fd.field, meta.display),
                              fd.field
                            )}
                          </span>
                          <span className="col-span-4 font-mono tabular-nums text-slate-400 line-through">
                            {formatSmartFieldValue(fd.field, fd.primary, meta, { decimals: 0 })}
                          </span>
                          <span className="col-span-4 text-right font-mono font-semibold tabular-nums text-blue-700">
                            → {formatSmartFieldValue(fd.field, fd.sandbox, meta, { decimals: 0 })}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}

                {diff.created.map((c) => (
                  <div
                    key={`new-${c.object_type}:${c.object_id}`}
                    className="flex items-center justify-between rounded-xl border border-emerald-200 bg-emerald-50/40 px-4 py-3 text-xs"
                  >
                    <span className="font-semibold text-emerald-900">
                      新增 {displayEntityName(c.object_type, meta.display)} ·{' '}
                      <span className="font-mono">{c.object_id}</span>
                      {c.object_title && c.object_title !== c.object_id && (
                        <span className="ml-1.5 font-normal text-emerald-700">
                          {c.object_title}
                        </span>
                      )}
                    </span>
                    <span className="rounded bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-800">
                      沙盘模拟新建
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
