import React, { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, ClipboardCheck, Clock, RefreshCw, XCircle } from 'lucide-react';
import {
  ConsoleMeta,
  RecordInstance,
  SchemaCatalog,
  WorkQueueDescriptor,
  consoleApi,
} from '../../api/client';
import {
  displayActionName,
  displayEntityName,
  displayEnumValue,
  displayRoleName,
  getStatusBadgeMeta,
  translateError,
} from '../i18nErrorMap';

interface WorkCardInboxProps {
  meta: ConsoleMeta;
  schema: SchemaCatalog;
  workQueue: WorkQueueDescriptor | null;
  refreshTick: number;
  onActionCompleted: (banner: { ok: boolean; text: string }) => void;
  onInspectObject: (entityType: string, recordId: string) => void;
}

const TONE_CLASSES: Record<string, string> = {
  emerald: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  amber: 'bg-amber-50 text-amber-800 ring-1 ring-amber-200',
  rose: 'bg-rose-50 text-rose-700 ring-1 ring-rose-200',
  sky: 'bg-sky-50 text-sky-700 ring-1 ring-sky-200',
  slate: 'bg-slate-100 text-slate-700 ring-1 ring-slate-200',
};

export const WorkCardInbox: React.FC<WorkCardInboxProps> = ({
  meta,
  schema,
  workQueue,
  refreshTick,
  onActionCompleted,
  onInspectObject,
}) => {
  const [items, setItems] = useState<RecordInstance[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [cardErrors, setCardErrors] = useState<Record<string, string>>({});
  const [showCompleted, setShowCompleted] = useState(false);

  const effectiveQueue: WorkQueueDescriptor | null = React.useMemo(() => {
    if (workQueue) return workQueue;
    const match = schema.object_types.find((t) => {
      const names = new Set(t.fields.map((f) => f.name));
      return (
        names.has('target_object_type') &&
        names.has('target_object_id') &&
        names.has('requested_action') &&
        names.has('assignee_role')
      );
    });
    if (!match) return null;
    return {
      object_type: match.name,
      id_field: match.primary_key,
      kind_field: 'card_kind',
      status_field: match.status_field || 'status',
      active_statuses: ['OPEN', 'IN_PROGRESS', 'SLA_BREACHED'],
      target_type_field: 'target_object_type',
      target_id_field: 'target_object_id',
      intent_action_field: 'requested_action',
      intent_params_field: 'requested_params_json',
      requester_field: 'requester_actor_id',
      assignee_role_field: 'assignee_role',
      accept_op: '',
      decline_op: '',
      id_param: match.primary_key,
      note_param: 'resolution_note',
      settled_run_param: 'settled_run_id',
    };
  }, [workQueue, schema]);

  const loadItems = useCallback(async () => {
    if (!effectiveQueue) {
      setItems([]);
      return;
    }
    setLoading(true);
    try {
      const res = await consoleApi.listObjects(effectiveQueue.object_type);
      setItems(res.items || []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [effectiveQueue, meta.actor.actor_id, refreshTick]);

  useEffect(() => {
    loadItems();
  }, [loadItems]);

  const handleResolve = async (item: RecordInstance, decision: 'approve' | 'reject') => {
    if (!effectiveQueue) return;
    setBusyId(item.object_id);
    setCardErrors((prev) => {
      if (!prev[item.object_id]) return prev;
      const next = { ...prev };
      delete next[item.object_id];
      return next;
    });
    try {
      const note = notes[item.object_id] || (decision === 'approve' ? '同意批准执行' : '审核驳回');
      const assigneeRole = String(item.properties[effectiveQueue.assignee_role_field] || '');
      const res = await consoleApi.resolveWorkItem(item.object_id, decision, note, effectiveQueue.object_type);
      if (res.ok && res.committed) {
        onActionCompleted({
          ok: true,
          text:
            decision === 'approve'
              ? `已批准并完成结算「${item.object_id}」，业务台账与审计流已同步更新。`
              : `已驳回工单「${item.object_id}」，目标单据保持原状。`,
        });
        await loadItems();
      } else {
        let friendly = translateError(res.error_code, res.error, res.hint_zh);
        if (res.error_code === 'WORK_CARD_ASSIGNEE_MISMATCH') {
          friendly = `权限不足：该事项须由「${displayRoleName(assigneeRole, meta.display)}」签批。`;
        } else if (res.error_code === 'SOD_SELF_APPROVAL_FORBIDDEN') {
          friendly = '职责分离拦截：发起人不可审批自己提交的事项。';
        }
        setCardErrors((prev) => ({ ...prev, [item.object_id]: friendly }));
        onActionCompleted({ ok: false, text: friendly });
      }
    } catch (err) {
      const friendly = err instanceof Error ? err.message : '操作执行未通过校验';
      setCardErrors((prev) => ({ ...prev, [item.object_id]: friendly }));
      onActionCompleted({ ok: false, text: friendly });
    } finally {
      setBusyId(null);
    }
  };

  if (!effectiveQueue) {
    return (
      <div className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
          <ClipboardCheck className="h-4 w-4 text-blue-700" />
          <span>协同审批收件箱</span>
        </div>
        <p className="mt-3 text-xs leading-relaxed text-slate-500">
          当前业务模型未配置待办协同队列。您可以在右侧单据列表中浏览并查看各实体明细与关联关系。
        </p>
      </div>
    );
  }

  const activeSet = new Set(effectiveQueue.active_statuses);
  const visibleItems = items.filter((it) => {
    const st = String(it.properties[effectiveQueue.status_field] || '');
    return showCompleted ? true : activeSet.has(st);
  });
  const activeCount = items.filter((it) =>
    activeSet.has(String(it.properties[effectiveQueue.status_field] || ''))
  ).length;

  return (
    <div className="flex flex-col rounded-xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
      <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-amber-50 text-amber-700 ring-1 ring-amber-200/70">
            <ClipboardCheck className="h-4 w-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-slate-900">异常与审批收件箱</h2>
              <span
                data-testid="inbox-active-count"
                className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 font-mono text-xs font-semibold tabular-nums text-amber-800"
              >
                {activeCount} 待办
              </span>
            </div>
            <p className="text-xs text-slate-500">只展示当前账号可见范围内的高优异常与签批事项</p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => setShowCompleted((v) => !v)}
            className="shrink-0 whitespace-nowrap rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-50"
          >
            {showCompleted ? '只看待办' : `全部 (${items.length})`}
          </button>
          <button
            type="button"
            onClick={loadItems}
            title="刷新待办"
            className="rounded-lg border border-slate-200 p-1.5 text-slate-500 transition hover:bg-slate-50 hover:text-slate-700"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      <div className="divide-y divide-slate-100">
        {visibleItems.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <CheckCircle2 className="mx-auto h-8 w-8 text-emerald-500" />
            <p className="mt-2 text-sm font-medium text-slate-700">当前视角下待办异常已清零</p>
            <p className="mt-1 text-xs text-slate-400">可切换右上角演示账号查看其他部门待审批事项</p>
          </div>
        ) : (
          visibleItems.map((it) => {
            const p = it.properties;
            const stVal = String(p[effectiveQueue.status_field] || 'OPEN');
            const badge = getStatusBadgeMeta(stVal, meta.display);
            const targetType = String(p[effectiveQueue.target_type_field] || '');
            const targetId = String(p[effectiveQueue.target_id_field] || '');
            const actionName = String(p[effectiveQueue.intent_action_field] || '');
            const assigneeRole = String(p[effectiveQueue.assignee_role_field] || '');
            const requesterId = String(p[effectiveQueue.requester_field] || '');
            const externalRef = String(p['external_ref'] || '');
            const slaDue = String(p['sla_due_at'] || '');
            const resNote = String(p['resolution_note'] || '');
            const isActive = activeSet.has(stVal);
            const isRoleAuthorized =
              !assigneeRole || meta.actor.role === assigneeRole || meta.actor.role === 'admin';
            const isSelf = requesterId && requesterId === meta.actor.actor_id;

            return (
              <div
                key={it.object_id}
                data-testid={`workcard-${it.object_id}`}
                className="p-4 transition hover:bg-slate-50/70"
              >
                {/* Row 1: Meta chips & status */}
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs font-semibold text-slate-700">
                      {it.object_id}
                    </span>
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-600">
                      {displayEnumValue(String(p[effectiveQueue.kind_field] || ''), meta.display)}
                    </span>
                  </div>
                  <span
                    className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      TONE_CLASSES[badge.tone] || TONE_CLASSES.slate
                    }`}
                  >
                    {badge.label}
                  </span>
                </div>

                {/* Row 2: Plain Chinese summary */}
                <p className="mt-2 text-sm font-semibold leading-snug text-slate-900">
                  {externalRef || `${displayActionName(actionName, meta.display)} · ${targetId}`}
                </p>

                {/* Row 3: Target link + roles */}
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                  <button
                    type="button"
                    onClick={() => targetType && targetId && onInspectObject(targetType, targetId)}
                    className="font-medium text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-900"
                  >
                    关联：{displayEntityName(targetType, meta.display)} {targetId}
                  </button>
                  <span>指派：{displayRoleName(assigneeRole, meta.display)}</span>
                  {slaDue && (
                    <span className="inline-flex items-center gap-1 font-mono text-[11px] text-slate-400">
                      <Clock className="h-3 w-3" /> {slaDue}
                    </span>
                  )}
                </div>

                {/* Row 4: Action bar or resolution record */}
                {isActive ? (
                  <div className="mt-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <input
                        type="text"
                        value={notes[it.object_id] || ''}
                        onChange={(e) =>
                          setNotes((prev) => ({ ...prev, [it.object_id]: e.target.value }))
                        }
                        placeholder="批注意见（可选，默认记录签批通过）"
                        className="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none"
                      />
                    </div>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-[11px] text-slate-500">
                        {isSelf
                          ? '⚠ 您是发起人（受职责分离红线约束不可自批）'
                          : !isRoleAuthorized
                          ? `提示：该事项需「${displayRoleName(assigneeRole, meta.display)}」或管理员签批`
                          : '✓ 当前账号具备审批签批权限'}
                      </span>
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          data-testid={`reject-${it.object_id}`}
                          disabled={busyId === it.object_id}
                          onClick={() => handleResolve(it, 'reject')}
                          className="inline-flex items-center gap-1 rounded-lg border border-rose-200 bg-rose-50 px-3 py-1.5 text-xs font-medium text-rose-700 transition hover:bg-rose-100 disabled:opacity-50"
                        >
                          <XCircle className="h-3.5 w-3.5" />
                          驳回
                        </button>
                        <button
                          type="button"
                          data-testid={`approve-${it.object_id}`}
                          disabled={busyId === it.object_id}
                          onClick={() => handleResolve(it, 'approve')}
                          className="inline-flex items-center gap-1 rounded-lg bg-[#0F172A] px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-slate-800 disabled:opacity-50"
                        >
                          <CheckCircle2 className="h-3.5 w-3.5" />
                          {busyId === it.object_id ? '提交核准中...' : '批准并落库'}
                        </button>
                      </div>
                    </div>
                    {cardErrors[it.object_id] && (
                      <div
                        data-testid={`workcard-error-${it.object_id}`}
                        className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-medium text-rose-800"
                      >
                        {cardErrors[it.object_id]}
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="mt-2 rounded-lg bg-slate-50 px-2.5 py-1.5 text-xs text-slate-600">
                    处理回执：{resNote || '已办结归档'}
                  </p>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};
