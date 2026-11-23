import { registerDomainPack } from '../../shell/packRegistry';
import { EnterpriseKpiBar } from './EnterpriseKpiBar';
import { EnterpriseLineageBar } from './EnterpriseLineageBar';
import { EnterpriseSandboxPanel } from './EnterpriseSandboxPanel';

const ENTERPRISE_MARKERS = new Set([
  'O2cOrder',
  'P2pPurchaseOrder',
  'WorkOrder',
  'GlAccount',
  'JournalEntry',
  'CrmDeal',
]);

export function registerEnterprisePack(): void {
  registerDomainPack({
    id: 'enterprise',
    label: '企业标准六闭环数字孪生视图包',
    matches: (meta, schema) => {
      if (meta.pack_hint === 'enterprise') return true;
      const present = new Set(schema.object_types.map((t) => t.name));
      let hits = 0;
      for (const marker of ENTERPRISE_MARKERS) {
        if (present.has(marker)) hits += 1;
      }
      return hits >= 3;
    },
    terminology: {
      cockpitNavLabel: '经营驾驶舱',
      catalogTitle: '实时业务台账与关系穿透',
      catalogCategoryLabel: '业务台账分类',
      catalogCountPrefix: '可见台账记录',
      recordNoun: '单据',
      drawerBackLabel: '返回上一单据',
      drawerOwnedBadge: '扩展台账',
      drawerAttrsTitle: '单据详细属性与客户自有扩展字段',
      lineageTitle: '单据前后关联图谱（受权可见范围）',
      sandboxDiffTitle: '现状 vs 沙盘 · 逐单据变更明细对比',
      sandboxManualSubtitle: '任选业务操作在平行空间验证合规性与关键指标变化',
      auditSearchPlaceholder: '搜索流水号、动作或单据...',
    },
    preferredTypes: [
      'O2cOrder',
      'P2pPurchaseOrder',
      'WorkOrder',
      'O2cInvoice',
      'JournalEntry',
      'CrmDeal',
      'CrmTicket',
      'GlAccount',
      'WorkCard',
      'Employee',
    ],
    workQueueFallback: {
      object_type: 'WorkCard',
      id_field: 'card_id',
      kind_field: 'card_kind',
      status_field: 'status',
      active_statuses: ['OPEN', 'IN_PROGRESS', 'SLA_BREACHED'],
      target_type_field: 'target_object_type',
      target_id_field: 'target_object_id',
      intent_action_field: 'requested_action',
      intent_params_field: 'requested_params_json',
      requester_field: 'requester_actor_id',
      assignee_role_field: 'assignee_role',
      accept_op: 'complete_work_card',
      decline_op: 'reject_work_card',
      claim_op: 'claim_work_card',
      id_param: 'card_id',
      note_param: 'resolution_note',
      settled_run_param: 'settled_run_id',
    },
    KpiBar: EnterpriseKpiBar,
    LineageBar: EnterpriseLineageBar,
    SandboxPanel: EnterpriseSandboxPanel,
  });
}
