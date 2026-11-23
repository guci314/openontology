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
