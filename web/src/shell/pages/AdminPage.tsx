import React, { useCallback, useEffect, useState } from 'react';
import {
  CheckCircle2,
  ChevronDown,
  Cpu,
  Network,
  Shield,
  Sliders,
} from 'lucide-react';
import type {
  AdminAutonomyResponse,
  AdminDevicesResponse,
  AdminOrgResponse,
  ConsoleMeta,
  SchemaCatalog,
} from '../../api/client';
import { consoleApi } from '../../api/client';

interface AdminPageProps {
  meta: ConsoleMeta;
  schema: SchemaCatalog;
  onPolicyUpdated: () => void;
}

const AUTONOMY_MODES: Array<{
  mode: 'SHADOW' | 'COPILOT_ESCALATE' | 'AUTO_UNDER_CEILING';
  title: string;
  desc: string;
  badge: string;
}> = [
  {
    mode: 'SHADOW',
    title: '影子观望 (SHADOW)',
    desc: '只预演不落库、零外部写入，将智能决策与真实结果比对写入影子成绩单。',
    badge: '绝对零风险试运行',
  },
  {
    mode: 'COPILOT_ESCALATE',
    title: '人机协同签批 (COPILOT)',
    desc: '先过合规预检，通过后一律自动生成待办审批卡片交人工负责人核准签批。',
    badge: '人工逐笔兜底',
  },
  {
    mode: 'AUTO_UNDER_CEILING',
    title: '限额自动放行 (AUTO)',
    desc: '风险额度以内受规则校验后自动落库执行，超出上限时自动降级为人工审批。',
    badge: '受控半自主',
  },
];

export const AdminPage: React.FC<AdminPageProps> = ({
  meta,
  schema,
  onPolicyUpdated,
}) => {
  const [devices, setDevices] = useState<AdminDevicesResponse | null>(null);
  const [autonomy, setAutonomy] = useState<AdminAutonomyResponse | null>(null);
  const [org, setOrg] = useState<AdminOrgResponse | null>(null);

  const [selectedMode, setSelectedMode] = useState<
    'SHADOW' | 'COPILOT_ESCALATE' | 'AUTO_UNDER_CEILING'
  >('AUTO_UNDER_CEILING');
  const [ceilingValue, setCeilingValue] = useState<string>('300000');
  const [expandDomains, setExpandDomains] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveFeedback, setSaveFeedback] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    if (!meta.capabilities.is_admin_ops) return;
    const [devRes, autoRes, orgRes] = await Promise.all([
      consoleApi.getAdminDevices(),
      consoleApi.getAdminAutonomy(),
      consoleApi.getAdminOrg(),
    ]);
    setDevices(devRes);
    setAutonomy(autoRes);
    setOrg(orgRes);
    setSelectedMode(autoRes.policy.default_mode);
    const wildcard = autoRes.policy.rules.find((r) => r.action === '*');
    if (wildcard?.max_value !== undefined && wildcard.max_value !== null) {
      setCeilingValue(String(wildcard.max_value));
    }
  }, [meta.actor.actor_id, meta.capabilities.is_admin_ops]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const handleSaveAutonomy = async (
    overrideMode?: 'SHADOW' | 'COPILOT_ESCALATE' | 'AUTO_UNDER_CEILING'
  ) => {
    const modeToUse = overrideMode || selectedMode;
    setSaving(true);
    setSaveFeedback(null);
    try {
      const updated = await consoleApi.updateAdminAutonomy({
        default_mode: modeToUse,
        max_value: parseFloat(ceilingValue) || 300000,
      });
      setAutonomy(updated);
      setSelectedMode(updated.policy.default_mode);
      const modeTitle =
        AUTONOMY_MODES.find((m) => m.mode === updated.policy.default_mode)?.title ||
        updated.policy.default_mode;
      setSaveFeedback(`已切换为「${modeTitle}」并即时生效。`);
      onPolicyUpdated();
    } finally {
      setSaving(false);
    }
  };

  if (!meta.capabilities.is_admin_ops) {
    return (
      <div
        data-testid="admin-access-denied"
        className="rounded-xl border border-amber-200 bg-amber-50 p-6 text-sm text-amber-900"
      >
        当前登录角色「{meta.actor.title}」不具备实施运维管理权限。请切换为拥有运维权限的管理员账号查看。
      </div>
    );
  }

  return (
    <div className="space-y-6" data-testid="admin-page">
      {/* Section 1: Autonomy Ramp Slider & Shadow Telemetry */}
      <div className="rounded-xl border border-slate-200/80 bg-white p-6 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-slate-100 pb-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-blue-50 text-blue-700 ring-1 ring-blue-200">
              <Sliders className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-bold text-slate-900">
                自主执行爬坡控制器与影子成绩单
              </h2>
              <p className="text-xs text-slate-500">
                支持热切换三档治理策略，所有未满足条件或超限操作严格降级且保持 0 脏写
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">
              自动放行阈值{meta.value_unit_label ? ` (${meta.value_unit_label})` : ''}：
            </span>
            <input
              type="number"
              value={ceilingValue}
              onChange={(e) => setCeilingValue(e.target.value)}
              className="w-28 rounded-lg border border-slate-200 px-2.5 py-1.5 font-mono text-xs tabular-nums text-slate-900 focus:border-blue-500 focus:outline-none"
            />
            <button
              type="button"
              data-testid="save-autonomy-btn"
              disabled={saving}
              onClick={() => handleSaveAutonomy()}
              className="rounded-lg bg-[#0F172A] px-3.5 py-1.5 text-xs font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50"
            >
              应用当前策略
            </button>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
          {AUTONOMY_MODES.map((m) => {
            const active = selectedMode === m.mode;
            return (
              <button
                key={m.mode}
                type="button"
                data-testid={`autonomy-mode-${m.mode}`}
                disabled={saving}
                onClick={() => {
                  setSelectedMode(m.mode);
                  handleSaveAutonomy(m.mode);
                }}
                className={`flex flex-col justify-between rounded-xl border p-4 text-left transition ${
                  active
                    ? 'border-blue-600 bg-blue-50/60 ring-2 ring-blue-600/20'
                    : 'border-slate-200 bg-white hover:border-slate-300'
                }`}
              >
                <div>
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-bold text-slate-900">{m.title}</span>
                    {active && <CheckCircle2 className="h-4 w-4 text-blue-600" />}
                  </div>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-slate-600">{m.desc}</p>
                </div>
                <span className="mt-3 inline-block w-fit rounded bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600 ring-1 ring-slate-200">
                  {m.badge}
                </span>
              </button>
            );
          })}
        </div>

        {saveFeedback && (
          <div
            data-testid="autonomy-save-feedback"
            className="mt-3 rounded-lg bg-emerald-50 px-3.5 py-2 text-xs font-medium text-emerald-800"
          >
            {saveFeedback}
          </div>
        )}

        {/* Shadow scorecard */}
        {autonomy && (
          <div className="mt-5 grid grid-cols-2 gap-3 border-t border-slate-100 pt-4 sm:grid-cols-5">
            <div className="rounded-lg bg-slate-50 px-3 py-2.5">
              <div className="text-[11px] text-slate-500">累计托管决策</div>
              <div className="mt-1 font-mono text-lg font-bold tabular-nums text-slate-900">
                {autonomy.shadow_report.total} 次
              </div>
            </div>
            <div className="rounded-lg bg-slate-50 px-3 py-2.5">
              <div className="text-[11px] text-slate-500">影子演练试算</div>
              <div className="mt-1 font-mono text-lg font-bold tabular-nums text-slate-900">
                {autonomy.shadow_report.shadow_total} 次
              </div>
            </div>
            <div className="rounded-lg bg-slate-50 px-3 py-2.5">
              <div className="text-[11px] text-slate-500">影子拦截异常</div>
              <div className="mt-1 font-mono text-lg font-bold tabular-nums text-rose-700">
                {autonomy.shadow_report.shadow_would_reject} 次
              </div>
            </div>
            <div className="rounded-lg bg-slate-50 px-3 py-2.5">
              <div className="text-[11px] text-slate-500">升级转人工批</div>
              <div className="mt-1 font-mono text-lg font-bold tabular-nums text-amber-700">
                {autonomy.shadow_report.escalated} 次
              </div>
            </div>
            <div className="rounded-lg bg-emerald-50 px-3 py-2.5 ring-1 ring-emerald-200/70">
              <div className="text-[11px] text-emerald-700">外部系统脏写</div>
              <div className="mt-1 font-mono text-lg font-bold tabular-nums text-emerald-700">
                {autonomy.external_dirty_writes} 笔
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Section 2: Devices & Coupled vs Standalone Domains + Org Tree */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-3 flex items-center gap-2">
            <Cpu className="h-4 w-4 text-slate-700" />
            <h2 className="text-sm font-semibold text-slate-900">
              外设系统挂载与领域自治分布
            </h2>
          </div>

          {devices && (
            <div className="space-y-3 text-xs">
              {devices.devices.length === 0 ? (
                <div className="rounded-lg border border-emerald-200 bg-emerald-50/50 p-3.5 text-emerald-900">
                  当前工作模式为 <strong>独立数字孪生（standalone）</strong>，所有核心对象数据均由本引擎本地安全托管。
                </div>
              ) : (
                devices.devices.map((d) => (
                  <div
                    key={d.device_id}
                    className="rounded-lg border border-sky-200 bg-sky-50/40 p-3.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-slate-900">{d.name}</span>
                      <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-800">
                        coupled · {d.status}
                      </span>
                    </div>
                    <div className="mt-1 font-mono text-[11px] text-slate-500">
                      端点：{d.endpoint}
                    </div>
                  </div>
                ))
              )}

              <div>
                <div className="mb-1.5 flex items-center justify-between text-[11px] font-medium text-slate-500">
                  <span>本引擎直接托管域 ({devices.standalone_object_types.length} 类)</span>
                  {devices.standalone_object_types.length > 14 && (
                    <button
                      type="button"
                      data-testid="admin-toggle-domains-btn"
                      onClick={() => setExpandDomains((v) => !v)}
                      className="font-semibold text-blue-700 hover:underline"
                    >
                      {expandDomains
                        ? '收起列表'
                        : `+${devices.standalone_object_types.length - 14} 展开全部`}
                    </button>
                  )}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {(expandDomains
                    ? devices.standalone_object_types
                    : devices.standalone_object_types.slice(0, 14)
                  ).map((st) => (
                    <span
                      key={st.object_type}
                      className="rounded-md bg-slate-100 px-2 py-0.5 text-[11px] text-slate-700"
                    >
                      {st.display_label}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Org Authority Tree */}
        <div className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-3 flex items-center gap-2">
            <Network className="h-4 w-4 text-slate-700" />
            <h2 className="text-sm font-semibold text-slate-900">组织权限与隔离范围</h2>
          </div>
          {org && org.members.length > 0 ? (
            <div className="max-h-64 space-y-2 overflow-y-auto text-xs">
              {org.members.map((m) => (
                <div
                  key={m.user_id}
                  className="flex items-center justify-between rounded-lg border border-slate-100 bg-slate-50/70 px-3 py-2"
                >
                  <div>
                    <span className="font-semibold text-slate-900">{m.display_name}</span>
                    <span className="ml-2 text-slate-500">{m.title}</span>
                  </div>
                  <span className="rounded bg-white px-2 py-0.5 font-mono text-[10px] text-slate-600 ring-1 ring-slate-200">
                    {m.clearance_tags.join(', ') || m.role}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="space-y-2 text-xs text-slate-500">
              <p>已配置静态岗位角色权限体系：</p>
              <div className="flex flex-wrap gap-1.5">
                {schema.roles.map((r) => (
                  <span key={r.name} className="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-700">
                    {r.name}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Section 3: Invariant Redline Registry */}
      <div className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="mb-3 flex items-center gap-2">
          <Shield className="h-4 w-4 text-emerald-700" />
          <h2 className="text-sm font-semibold text-slate-900">
            同步硬不变量与红线守护规则 ({schema.invariants.length})
          </h2>
        </div>
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          {schema.invariants.map((inv) => (
            <div
              key={`${inv.kind}-${inv.name}`}
              className="rounded-lg border border-slate-200/80 bg-slate-50/60 px-3.5 py-2.5 text-xs"
            >
              <div className="flex items-center justify-between">
                <span className="font-mono font-semibold text-slate-800">{inv.code}</span>
                <span className="rounded bg-white px-1.5 py-0.5 text-[10px] font-medium text-slate-500 ring-1 ring-slate-200">
                  {inv.kind === 'transition' ? '状态跃迁锁' : '全局不变量'}
                </span>
              </div>
              <p className="mt-1 text-[11px] text-slate-600">
                {inv.description || inv.name}
              </p>
            </div>
          ))}
        </div>
      </div>

      {/* Section 4: Default-Collapsed Low-Level Schema Inspector (ONLY allowed here in /admin) */}
      <details
        data-testid="admin-collapsed-schema-inspector"
        className="rounded-xl border border-slate-200 bg-white p-4 text-xs text-slate-600 shadow-sm"
      >
        <summary className="flex cursor-pointer list-none items-center justify-between font-semibold text-slate-700">
          <span>高级底层模型结构检查器（TBox / ABox 元数据与状态摘要，默认折叠）</span>
          <ChevronDown className="h-4 w-4 text-slate-400" />
        </summary>
        <div className="mt-4 space-y-3 border-t border-slate-100 pt-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="rounded bg-slate-50 p-2 font-mono text-[11px]">
              ObjectTypes: {schema.object_types.length}
            </div>
            <div className="rounded bg-slate-50 p-2 font-mono text-[11px]">
              Actions: {schema.actions.length}
            </div>
            <div className="rounded bg-slate-50 p-2 font-mono text-[11px]">
              Functions: {schema.functions.length}
            </div>
            <div className="rounded bg-slate-50 p-2 font-mono text-[11px]">
              Invariants: {schema.invariants.length}
            </div>
          </div>
          <pre className="max-h-64 overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] text-slate-100">
            {JSON.stringify(
              {
                world_mode: meta.world_mode,
                state_fingerprint: meta.state_fingerprint,
                autonomy_policy: autonomy?.policy,
                object_types: schema.object_types.map((t) => ({
                  name: t.name,
                  primary_key: t.primary_key,
                  owned: t.owned,
                  visible_count: t.visible_count,
                })),
              },
              null,
              2
            )}
          </pre>
        </div>
      </details>
    </div>
  );
};
