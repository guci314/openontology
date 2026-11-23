import type { ConsoleMeta, DisplayDictionary } from '../api/client';

const GENERIC_ERROR_HINTS: Record<string, string> = {
  PERMISSION_DENIED: '当前角色暂无执行权限，已被系统安全拦截。',
  SOD_SELF_APPROVAL_FORBIDDEN: '触发职责分离合规红线：申请人不可审批自己提交的工单。',
  UNBOUND_SOURCE_BRIDGE: '外部系统连接处于断开状态，已阻止不安全的写入操作。',
  DEVICE_STATE_MISMATCH: '外部系统回读状态校验不一致，本次事务已自动中止且保持零脏写。',
  ILLEGAL_DOCUMENT_TRANSITION: '当前单据状态尚不满足流转条件，禁止跳步执行。',
  WORK_CARD_ASSIGNEE_MISMATCH: '该事项已指派给其他审批岗位，请切换到对应负责人账号处理。',
  WORK_CARD_NOT_ACTIVE: '该待办事项已审批结束，无需重复处理。',
  INVALID_PARAMETER: '提交参数不完整或数值格式有误，请核对后重试。',
};

const GENERIC_STATUS_LABELS: Record<string, { label: string; tone: 'emerald' | 'amber' | 'rose' | 'sky' | 'slate' }> = {
  COMMITTED: { label: '已执行落库', tone: 'emerald' },
  REJECTED: { label: '已驳回', tone: 'rose' },
  REVERTED: { label: '已撤回冲正', tone: 'amber' },
  AUTO_COMMITTED: { label: '限额自动放行', tone: 'emerald' },
  ESCALATED_COMMITTED: { label: '已转人工签批', tone: 'amber' },
  ESCALATION_REQUIRED: { label: '需人工审批', tone: 'amber' },
  SHADOW_RECORDED: { label: '影子记录（零写入）', tone: 'sky' },
  SHADOW_WOULD_REJECT: { label: '影子预拦截', tone: 'rose' },
  OPEN: { label: '待审批处理', tone: 'amber' },
  IN_PROGRESS: { label: '处理中', tone: 'sky' },
  COMPLETED: { label: '已完成', tone: 'emerald' },
  SLA_BREACHED: { label: '超时预警', tone: 'rose' },
  CANCELLED: { label: '已撤销', tone: 'slate' },
  MOUNTED: { label: '实时连接', tone: 'emerald' },
  CONNECTED: { label: '实时连接', tone: 'emerald' },
  DEGRADED: { label: '降级待查', tone: 'amber' },
  UNPLUGGED: { label: '已断开安全态', tone: 'slate' },
  HIGH: { label: '高优', tone: 'rose' },
  MEDIUM: { label: '中等', tone: 'amber' },
  LOW: { label: '常规', tone: 'slate' },
  DEBIT: { label: '借方', tone: 'sky' },
  CREDIT: { label: '贷方', tone: 'emerald' },
  active: { label: '启用中', tone: 'emerald' },
  draft: { label: '草稿待办', tone: 'slate' },
  confirmed: { label: '已确认', tone: 'sky' },
  released: { label: '已下达', tone: 'sky' },
  in_progress: { label: '执行中', tone: 'sky' },
  disputed: { label: '争议挂起', tone: 'rose' },
  received: { label: '已收货待账', tone: 'sky' },
  delivered: { label: '已发货', tone: 'sky' },
  invoiced: { label: '已开票', tone: 'emerald' },
  billed: { label: '已对账开票', tone: 'emerald' },
  posted: { label: '已过账', tone: 'emerald' },
  paid: { label: '已结清', tone: 'emerald' },
  partial: { label: '部分结清', tone: 'amber' },
  not_paid: { label: '待核销', tone: 'amber' },
  pending: { label: '待处理', tone: 'slate' },
  recorded: { label: '已记录', tone: 'emerald' },
  agreed: { label: '已生效', tone: 'emerald' },
  passed: { label: '合格', tone: 'emerald' },
  completed: { label: '已完成', tone: 'emerald' },
  closed: { label: '已关账锁定', tone: 'slate' },
  open: { label: '开放记账中', tone: 'emerald' },
  resolved: { label: '已闭环', tone: 'emerald' },
  RESOLVED: { label: '已闭环', tone: 'emerald' },
  CLOSED_WON: { label: '已赢单', tone: 'emerald' },
  PROPOSAL: { label: '方案报价', tone: 'sky' },
  NEGOTIATION: { label: '商务谈判', tone: 'amber' },
};

export function translateError(code?: string | null, fallback?: string | null, backendHint?: string): string {
  const rawFallback = String(fallback || '');
  if (rawFallback.includes('errors.pydantic.dev') || rawFallback.toLowerCase().includes('validation error')) {
    return backendHint && !backendHint.includes('errors.pydantic.dev')
      ? backendHint
      : GENERIC_ERROR_HINTS.INVALID_PARAMETER;
  }
  if (backendHint) return backendHint;
  const c = String(code || '').trim();
  if (c && GENERIC_ERROR_HINTS[c]) return GENERIC_ERROR_HINTS[c];
  if (c.startsWith('APPROVAL_REQUIRED_')) return '金额或风险指标超出自动放行阈值，须转人工复核签批。';
  if (fallback) return `校验拦截：${fallback}`;
  return '规则校验未通过，真实业务数据保持零改动。';
}

export function getStatusBadgeMeta(
  rawStatus: unknown,
  display?: DisplayDictionary
): { label: string; tone: 'emerald' | 'amber' | 'rose' | 'sky' | 'slate' } {
  const key = String(rawStatus ?? '');
  if (GENERIC_STATUS_LABELS[key]) return GENERIC_STATUS_LABELS[key];
  const fromDict = display?.statuses?.[key];
  if (fromDict) {
    return { label: fromDict, tone: 'slate' };
  }
  return { label: key || '-', tone: 'slate' };
}

export function displayEntityName(entityName: string, display?: DisplayDictionary): string {
  return display?.objects?.[entityName] || entityName;
}

export function displayFieldName(entityName: string, fieldName: string, display?: DisplayDictionary): string {
  return (
    display?.field_overrides?.[`${entityName}.${fieldName}`] ||
    display?.fields?.[fieldName] ||
    fieldName
  );
}

export function displayActionName(actionName: string, display?: DisplayDictionary): string {
  return display?.actions?.[actionName] || actionName;
}

export function displayRoleName(roleName: string, display?: DisplayDictionary): string {
  return display?.roles?.[roleName] || roleName;
}

export function displayYuanFieldLabel(rawLabel: string, fieldName: string): string {
  if (fieldName.endsWith('_cents')) {
    return rawLabel.replace(/[（(]分[)）]$/, '');
  }
  return rawLabel;
}

export function displayEnumValue(rawVal: string, display?: DisplayDictionary): string {
  return display?.statuses?.[rawVal] || GENERIC_STATUS_LABELS[rawVal]?.label || rawVal;
}

export function formatSignedAmount(
  val: number,
  unit = '',
  opts?: { withPlus?: boolean; decimals?: number }
): string {
  const absVal = Math.abs(val);
  const decimals = opts?.decimals ?? 0;
  const numStr = absVal.toLocaleString('zh-CN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  const sign = val < 0 ? '-' : val > 0 && opts?.withPlus ? '+' : '';
  return `${sign}${unit}${numStr}`;
}

export function isCurrencyLikeField(fieldName: string): boolean {
  return (
    fieldName.endsWith('_cents') ||
    fieldName.includes('amount') ||
    fieldName.includes('limit') ||
    fieldName.includes('price') ||
    fieldName === 'settled_amount' ||
    fieldName === 'payment_amount'
  );
}

export function formatSmartFieldValue(
  fieldName: string,
  val: unknown,
  meta?: ConsoleMeta,
  opts?: { decimals?: number }
): string {
  if (val === null || val === undefined || val === '') return '—';
  if (typeof val === 'boolean') return val ? '是' : '否';
  const unit = meta?.value_unit_label ?? '';
  if (typeof val === 'number') {
    if (fieldName.endsWith('_cents')) {
      return formatSignedAmount(val / 100, unit, { decimals: opts?.decimals ?? 0 });
    }
    if (isCurrencyLikeField(fieldName)) {
      return formatSignedAmount(val, unit, { decimals: opts?.decimals ?? 0 });
    }
    return val.toLocaleString('zh-CN', { maximumFractionDigits: 4 });
  }
  if (typeof val === 'string') {
    return displayEnumValue(val, meta?.display);
  }
  if (Array.isArray(val)) {
    return val.map((item) => displayEnumValue(String(item), meta?.display)).join(', ');
  }
  return String(val);
}

export function formatLocalTime(iso?: string | null): string {
  const raw = String(iso || '').trim();
  if (!raw) return '—';
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
