export interface ActorProfile {
  actor_id: string;
  role: string;
  display_name: string;
  unit_id: string;
  unit_name: string;
  title: string;
  badge: string;
  clearance_tags: string[];
  is_admin_ops: boolean;
}

export interface DisplayDictionary {
  ontologies?: Record<string, string>;
  categories?: Record<string, string>;
  objects?: Record<string, string>;
  object_properties?: Record<string, string>;
  links?: Record<string, string>;
  fields?: Record<string, string>;
  field_overrides?: Record<string, string>;
  actions?: Record<string, string>;
  edit_functions?: Record<string, string>;
  functions?: Record<string, string>;
  automations?: Record<string, string>;
  interfaces?: Record<string, string>;
  roles?: Record<string, string>;
  statuses?: Record<string, string>;
  values?: Record<string, string>;
  cardinalities?: Record<string, string>;
  kinds?: Record<string, string>;
  invariants?: Record<string, string>;
  errors?: Record<string, string>;
}

export interface WorkQueueDescriptor {
  object_type: string;
  id_field: string;
  kind_field: string;
  status_field: string;
  active_statuses: string[];
  target_type_field: string;
  target_id_field: string;
  intent_action_field: string;
  intent_params_field: string;
  requester_field: string;
  assignee_role_field: string;
  accept_op: string;
  decline_op: string;
  claim_op?: string | null;
  id_param: string;
  note_param: string;
  settled_run_param: string;
}

export interface SandboxPresetStep {
  action: string;
  params: Record<string, unknown>;
  actor_id?: string;
}

export interface SandboxPreset {
  preset_id: string;
  title: string;
  subtitle: string;
  badge?: string;
  steps: SandboxPresetStep[];
}

export interface ConsoleMeta {
  ok: boolean;
  world_mode: string;
  app_title: string;
  app_subtitle: string;
  pack_hint: string;
  actor: ActorProfile;
  demo_actor_switch: boolean;
  demo_actors: ActorProfile[];
  display: DisplayDictionary;
  work_queue: WorkQueueDescriptor | null;
  sandbox_presets: SandboxPreset[];
  quick_prompts?: string[];
  assistant_welcome?: string;
  value_unit_label?: string;
  autonomy_mode: string;
  state_fingerprint: Record<string, unknown>;
  capabilities: {
    is_admin_ops: boolean;
    has_org_directory: boolean;
    device_count: number;
  };
}

export interface SchemaFieldMeta {
  name: string;
  display_label: string;
  type: string;
  property_kind: 'datatype' | 'object';
  range?: string;
  multiple?: boolean;
  masked?: boolean;
  masked_roles?: string[];
  owned: boolean;
  required?: boolean;
  choices?: unknown[];
  default?: unknown;
}

export interface SchemaEntityMeta {
  name: string;
  display_label: string;
  primary_key: string;
  title?: string | null;
  fields: SchemaFieldMeta[];
  interfaces: string[];
  owned: boolean;
  owned_fields: string[];
  security_tag_fields: string[];
  aggregate_root: boolean;
  append_only: boolean;
  superclasses: string[];
  read_only_source: boolean;
  status_field?: string | null;
  initial_statuses?: string[];
  transitions?: Record<string, string[]>;
  in_role_scope: boolean;
  visible_count: number;
}

export interface SchemaActionMeta {
  name: string;
  display_label: string;
  description: string;
  allowed_roles: string[];
  revert_roles: string[];
  touched_object_types?: string[];
  affects_source: boolean;
  visibility: string;
  params: {
    type?: string;
    properties?: Record<
      string,
      {
        type?: string;
        title?: string;
        default?: unknown;
        description?: string;
        enum?: unknown[];
      }
    >;
    required?: string[];
  };
}

export interface SchemaFunctionMeta {
  name: string;
  display_label: string;
  description: string;
  params_schema?: Record<string, unknown>;
}

export interface SchemaInvariantMeta {
  kind: 'state' | 'transition';
  name: string;
  code: string;
  description: string;
}

export interface SchemaCatalog {
  ok: boolean;
  object_types: SchemaEntityMeta[];
  interfaces: Array<{ name: string; required_fields: string[]; description: string; implementations: string[] }>;
  object_properties: Array<{
    name: string;
    domain: string;
    range: string;
    cardinality: string;
    owned: boolean;
    field?: string;
    description?: string;
  }>;
  actions: SchemaActionMeta[];
  functions: SchemaFunctionMeta[];
  automations: Array<{ name: string; watch_object_type: string; description: string }>;
  roles: Array<{
    name: string;
    description: string;
    declared: boolean;
    allowed_actions: string[];
    revert_actions: string[];
    masked_fields: string[];
  }>;
  invariants: SchemaInvariantMeta[];
}

export interface RecordInstance {
  object_type: string;
  object_id: string;
  primary_key: string;
  title: string;
  version: number;
  security_tags: string[];
  owned: boolean;
  read_only_source: boolean;
  properties: Record<string, unknown>;
}

export interface LineageNode extends RecordInstance {
  display_label: string;
  hop: number;
  relation: 'root' | 'upstream' | 'downstream';
}

export interface LineageEdge {
  direction: 'upstream' | 'downstream';
  link_type: string;
  source_type: string;
  source_id: string;
  target_type: string;
  target_id: string;
  peer_type: string;
  peer_id: string;
}

export interface LineageResponse {
  ok: boolean;
  root: LineageNode;
  nodes: LineageNode[];
  edges: LineageEdge[];
  hops: {
    upstream: LineageNode[];
    downstream: LineageNode[];
  };
}

export interface FieldDelta {
  field: string;
  before?: unknown;
  after?: unknown;
  primary?: unknown;
  sandbox?: unknown;
}

export interface ObjectDeltaSummary {
  kind?: string;
  object_type: string;
  object_id: string;
  object_title?: string;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  field_deltas: FieldDelta[];
}

export interface ActionSubmitResponse {
  ok: boolean;
  valid?: boolean;
  committed?: boolean;
  mode?: string;
  effective_mode?: string;
  status: string;
  outcome?: string;
  action: string;
  run_id?: string;
  changes?: ObjectDeltaSummary[];
  error_code?: string | null;
  error?: string | null;
  hint_zh?: string;
  zero_external_dirty_write?: boolean;
}

export interface SandboxCreateResponse {
  ok: boolean;
  sandbox_id: string;
  world_mode: string;
  base_fingerprint: Record<string, unknown>;
  steps: Array<Record<string, unknown>>;
}

export interface SandboxDiffResponse {
  ok: boolean;
  sandbox_id: string;
  created: Array<{ object_type: string; object_id: string; object_title?: string; after: Record<string, unknown> }>;
  updated: Array<{
    object_type: string;
    object_id: string;
    object_title?: string;
    before: Record<string, unknown>;
    after: Record<string, unknown>;
    field_deltas: FieldDelta[];
  }>;
  deleted: Array<{ object_type: string; object_id: string; object_title?: string; before: Record<string, unknown> }>;
  total_changes: number;
  step_count: number;
  steps: Array<Record<string, unknown>>;
  primary_fingerprint_unchanged: boolean;
  zero_external_dirty_write: boolean;
}

export interface AuditRunEntry {
  run_id: string;
  action_type: string;
  action_label: string;
  actor_id: string;
  actor_display_name?: string;
  actor_role: string;
  params: Record<string, unknown>;
  status: 'COMMITTED' | 'REJECTED' | 'REVERTED';
  status_label: string;
  governance_channel: 'auto' | 'human' | 'escalated' | 'shadow';
  executed_at: string;
  approved_by?: string | null;
  reverted_by_run_id?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  hint_zh?: string;
  changes: ObjectDeltaSummary[];
  external_dirty_writes: number;
}

export interface AuditResponse {
  ok: boolean;
  runs: AuditRunEntry[];
  shadow_entries: Array<Record<string, unknown>>;
  shadow_report: {
    total: number;
    shadow_total: number;
    shadow_would_approve: number;
    shadow_would_reject: number;
    shadow_would_approve_rate: number;
    auto_committed: number;
    escalated: number;
    error_code_distribution: Record<string, number>;
    outcome_distribution: Record<string, number>;
  };
  summary: {
    total_runs: number;
    committed: number;
    rejected: number;
    reverted: number;
    external_dirty_writes: number;
  };
}

export interface AssistantCard {
  kind: 'object' | 'sandbox_preset';
  title: string;
  subtitle: string;
  object_type?: string;
  object_id?: string;
  preset_id?: string;
  route?: string;
}

export interface AssistantChatResponse {
  ok: boolean;
  reply: string;
  highlights: Array<{ label: string; value: string }>;
  cards: AssistantCard[];
  rls_scope: string[];
}

export interface AdminDevicesResponse {
  ok: boolean;
  world_mode: string;
  devices: Array<{
    device_id: string;
    name: string;
    kind: string;
    endpoint: string;
    status: string;
    status_label?: string;
    domain_mode: 'coupled' | 'standalone';
    owned_object_types: string[];
    shared_object_types: string[];
    handled_actions: string[];
  }>;
  standalone_object_types: Array<{
    object_type: string;
    display_label: string;
    domain_mode: 'standalone';
    owned: boolean;
  }>;
  coupled_object_types: string[];
}

export interface AdminAutonomyResponse {
  ok: boolean;
  policy: {
    default_mode: 'SHADOW' | 'COPILOT_ESCALATE' | 'AUTO_UNDER_CEILING';
    default_escalation_action?: string | null;
    default_escalation_assignee_role?: string | null;
    rules: Array<{
      action: string;
      roles: string[];
      mode: 'SHADOW' | 'COPILOT_ESCALATE' | 'AUTO_UNDER_CEILING';
      ceiling_field?: string | null;
      max_value?: number | null;
      escalation_action?: string | null;
      escalation_assignee_role?: string | null;
    }>;
  };
  shadow_report: AuditResponse['shadow_report'];
  recent_ledger: Array<Record<string, unknown>>;
  external_dirty_writes: number;
}

export interface AdminOrgResponse {
  ok: boolean;
  units: Array<{
    unit_id: string;
    name: string;
    parent_id: string | null;
    unit_kind: string;
    scope_tag: string;
  }>;
  members: Array<{
    user_id: string;
    display_name: string;
    role: string;
    unit_id: string;
    title: string;
    active: boolean;
    clearance_tags: string[];
  }>;
  roles: SchemaCatalog['roles'];
}

export type BlueprintFieldOwnership = 'external_synced' | 'overlay_extension' | 'ontology_owned';

export interface BlueprintFieldMeta {
  name: string;
  display_label: string;
  type: string;
  required: boolean;
  is_primary_key: boolean;
  is_status_field: boolean;
  ownership: BlueprintFieldOwnership;
}

export interface BlueprintAttachedDevice {
  device_id: string;
  name: string;
  kind: string;
  status: string;
  binding: 'owned' | 'shared';
}

export interface BlueprintEntityMeta {
  name: string;
  display_label: string;
  description: string;
  extends?: string | null;
  primary_key: string;
  status_field: string | null;
  initial_statuses: string[];
  transitions: Record<string, string[]>;
  terminal_statuses: string[];
  fields: BlueprintFieldMeta[];
  attached_devices: BlueprintAttachedDevice[];
  touched_by_actions: string[];
  in_role_scope: boolean;
  visible_count: number;
}

export interface BlueprintLinkMeta {
  name: string;
  field: string;
  display_label: string;
  source: string;
  target: string;
  cardinality: string;
  cardinality_label: string;
  min_cardinality?: number;
  max_cardinality?: number | null;
  inverse_name?: string | null;
  description: string;
}

export interface BlueprintRuleSummary {
  precondition_count: number;
  preconditions: Array<{
    name: string;
    code: string;
    description: string;
  }>;
  mutation_count: number;
  mutations: Array<{
    op: string;
    object_type: string;
    set_fields: string[];
  }>;
}

export interface BlueprintActionMeta {
  name: string;
  display_label: string;
  description: string;
  implementation_kind: 'rule' | 'edit_function';
  edit_function_name: string | null;
  allowed_roles: string[];
  in_role_scope: boolean;
  touched_object_types: string[];
  params_schema: Record<string, unknown>;
  rule_summary: BlueprintRuleSummary | null;
  autonomy_mode: string;
}

export interface BlueprintEditFunctionMeta {
  name: string;
  display_label: string;
  description: string;
  touched_object_types: string[];
  bound_actions: string[];
  params_schema: Record<string, unknown>;
}

export interface BlueprintFunctionMeta {
  name: string;
  display_label: string;
  description: string;
  read_only: boolean;
  params_schema: Record<string, unknown>;
}

export interface BlueprintInvariantMeta {
  kind: string;
  name: string;
  code: string | null;
  description: string;
}

export interface BlueprintSummary {
  object_type_count: number;
  link_count: number;
  action_count: number;
  rule_action_count: number;
  edit_function_action_count: number;
  edit_function_count: number;
  function_count: number;
  invariant_count: number;
  fsm_object_count: number;
}

export interface BlueprintResponse {
  ok: boolean;
  spec_name: string;
  spec_version: string;
  blueprint_fingerprint: string;
  summary: BlueprintSummary;
  object_types: BlueprintEntityMeta[];
  disjoint_types?: Array<[string, string]>;
  links: BlueprintLinkMeta[];
  actions: BlueprintActionMeta[];
  edit_functions: BlueprintEditFunctionMeta[];
  functions: BlueprintFunctionMeta[];
  invariants: BlueprintInvariantMeta[];
}

export interface BlueprintActionProof {
  action_name: string;
  display_label?: string;
  implementation_kind: 'rule' | 'edit_function';
  status: 'REACHABLE' | 'TOTAL_DEADLOCK' | 'CONDITIONAL_DEADLOCK' | 'UNKNOWN';
  rule_count: number;
  unsat_core: string[];
  deadlocked_slices?: Array<{
    slice_name: string;
    slice_kind: string;
    status: string;
    unsat_core: string[];
    logic_fragment: string;
  }>;
  logic_fragment: string;
  elapsed_ms: number;
}

export interface BlueprintInvariantProof {
  action_name: string;
  display_label?: string;
  status: 'PROVED' | 'VIOLATED' | 'VACUOUS_DEADLOCK' | 'UNKNOWN';
  checked_invariants: string[];
  violated_invariants: string[];
  checked_invariant_labels?: string[];
  violated_invariant_labels?: string[];
  counterexamples: Array<{
    violated_invariant: string;
    violated_code: string;
    actor: Record<string, unknown>;
    params: Record<string, unknown>;
    before: Record<string, unknown>;
    after: Record<string, unknown>;
  }>;
  logic_fragment: string;
  elapsed_ms: number;
}

export interface BlueprintRedundantRuleFinding {
  action_name: string;
  action_display_label?: string;
  rule_name: string;
  rule_display_label?: string;
  rule_code: string;
  reason: string;
  entailed_by: string[];
  logic_fragment: string;
  elapsed_ms: number;
}

export interface BlueprintClassProof {
  object_type: string;
  display_label?: string;
  extends?: string | null;
  extends_display_label?: string | null;
  status:
    | 'SATISFIABLE'
    | 'UNSATISFIABLE_CLASS'
    | 'DL_AXIOM_CONFLICT'
    | 'LSP_VIOLATION'
    | 'UNKNOWN';
  logic_fragment: string;
  inherited_invariants: string[];
  own_invariants: string[];
  unsat_core: string[];
  unsat_core_labels?: string[];
  detail: string;
  witness?: Record<string, unknown> | null;
  elapsed_ms: number;
}

export interface BlueprintProveSummary {
  total_classes?: number;
  satisfiable_classes?: number;
  dead_class_count?: number;
  dl_conflict_count?: number;
  lsp_violation_count?: number;
  total_actions: number;
  reachable_actions: number;
  deadlocked_actions: number;
  total_invariants: number;
  preserved_invariants: number;
  violated_invariants: number;
  declared_invariant_count?: number;
  redundant_rule_count: number;
  exact_linear_ratio: number;
  fragment_counts: Record<string, number>;
}

export interface BlueprintProveResponse {
  ok: boolean;
  prover_available: boolean;
  cached?: boolean;
  reason?: string;
  message?: string;
  install_hint?: string;
  verdict?: 'PROVEN_CLEAN' | 'ISSUES_FOUND';
  elapsed_ms?: number;
  blueprint_fingerprint?: string;
  summary?: BlueprintProveSummary;
  class_proofs?: BlueprintClassProof[];
  action_proofs?: BlueprintActionProof[];
  invariant_proofs?: BlueprintInvariantProof[];
  redundant_rules?: BlueprintRedundantRuleFinding[];
}

const ACTOR_STORAGE_KEY = 'oo_console_actor_id';

function readStoredActorId(): string | null {
  try {
    if (typeof window !== 'undefined' && window.sessionStorage) {
      const saved = window.sessionStorage.getItem(ACTOR_STORAGE_KEY);
      return saved && saved.trim() ? saved.trim() : null;
    }
  } catch {
    // ignore storage access failure
  }
  return null;
}

let activeActorHeader: string | null = readStoredActorId();

export function setActiveActorId(actorId: string | null): void {
  activeActorHeader = actorId && actorId.trim() ? actorId.trim() : null;
  try {
    if (typeof window !== 'undefined' && window.sessionStorage) {
      if (activeActorHeader) {
        window.sessionStorage.setItem(ACTOR_STORAGE_KEY, activeActorHeader);
      } else {
        window.sessionStorage.removeItem(ACTOR_STORAGE_KEY);
      }
    }
  } catch {
    // ignore storage write failure
  }
}

export function getActiveActorId(): string | null {
  return activeActorHeader;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers || {});
  if (!headers.has('Content-Type') && init?.body) {
    headers.set('Content-Type', 'application/json');
  }
  if (activeActorHeader) {
    headers.set('X-Actor-Id', activeActorHeader);
  }
  const resp = await fetch(path, { ...init, headers });
  const text = await resp.text();
  let data: unknown = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { ok: false, error: text || `HTTP ${resp.status}` };
  }
  if (!resp.ok) {
    const errObj = data as { error?: string; hint_zh?: string };
    throw new Error(errObj.hint_zh || errObj.error || `请求失败 (${resp.status})`);
  }
  return data as T;
}

export const consoleApi = {
  getMeta: () => apiFetch<ConsoleMeta>('/api/meta'),
  getSchema: () => apiFetch<SchemaCatalog>('/api/tbox'),
  getBlueprint: () => apiFetch<BlueprintResponse>('/api/blueprint'),
  proveBlueprint: (forceRefresh = false) =>
    apiFetch<BlueprintProveResponse>('/api/blueprint/prove', {
      method: 'POST',
      body: JSON.stringify({ force_refresh: forceRefresh }),
    }),
  listObjects: (typeName: string, opts?: { status?: string; q?: string }) => {
    const params = new URLSearchParams();
    if (opts?.status) params.set('status', opts.status);
    if (opts?.q) params.set('q', opts.q);
    const qs = params.toString();
    return apiFetch<{ ok: boolean; object_type: string; display_label: string; count: number; items: RecordInstance[] }>(
      `/api/objects/${encodeURIComponent(typeName)}${qs ? `?${qs}` : ''}`
    );
  },
  getObjectLineage: (typeName: string, objectId: string) =>
    apiFetch<LineageResponse>(
      `/api/objects/${encodeURIComponent(typeName)}/${encodeURIComponent(objectId)}/lineage`
    ),
  simulateAction: (action: string, params: Record<string, unknown>) =>
    apiFetch<ActionSubmitResponse>('/api/actions/simulate', {
      method: 'POST',
      body: JSON.stringify({ action, params }),
    }),
  submitAction: (action: string, params: Record<string, unknown>) =>
    apiFetch<ActionSubmitResponse>('/api/actions/submit', {
      method: 'POST',
      body: JSON.stringify({ action, params }),
    }),
  resolveWorkItem: (itemId: string, decision: 'approve' | 'reject', note: string, itemType?: string) =>
    apiFetch<ActionSubmitResponse>(`/api/work-queue/${encodeURIComponent(itemId)}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ decision, note, item_type: itemType }),
    }),
  callFunction: <R = Record<string, unknown>>(fnName: string, params: Record<string, unknown> = {}) =>
    apiFetch<{ ok: boolean; function: string; result: R }>(
      `/api/functions/${encodeURIComponent(fnName)}`,
      { method: 'POST', body: JSON.stringify(params) }
    ),
  createSandbox: () => apiFetch<SandboxCreateResponse>('/api/sandboxes', { method: 'POST' }),
  submitSandbox: (
    sid: string,
    payload: { action?: string; params?: Record<string, unknown>; actor_id?: string; steps?: SandboxPresetStep[] }
  ) =>
    apiFetch<Record<string, unknown>>(`/api/sandboxes/${encodeURIComponent(sid)}/submit`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  compareSandboxFunction: <R = Record<string, unknown>>(
    sid: string,
    fnName: string,
    params: Record<string, unknown> = {}
  ) =>
    apiFetch<{ ok: boolean; function: string; primary: R; sandbox: R }>(
      `/api/sandboxes/${encodeURIComponent(sid)}/function/${encodeURIComponent(fnName)}`,
      { method: 'POST', body: JSON.stringify(params) }
    ),
  listSandboxObjects: (sid: string, typeName: string) =>
    apiFetch<{ ok: boolean; count: number; items: RecordInstance[] }>(
      `/api/sandboxes/${encodeURIComponent(sid)}/objects/${encodeURIComponent(typeName)}`
    ),
  diffSandbox: (sid: string) =>
    apiFetch<SandboxDiffResponse>(`/api/sandboxes/${encodeURIComponent(sid)}/diff`),
  promoteSandbox: (sid: string) =>
    apiFetch<Record<string, unknown>>(`/api/sandboxes/${encodeURIComponent(sid)}/promote`, {
      method: 'POST',
    }),
  destroySandbox: (sid: string) =>
    apiFetch<Record<string, unknown>>(`/api/sandboxes/${encodeURIComponent(sid)}`, {
      method: 'DELETE',
    }),
  getAudit: (status?: string) => {
    const qs = status ? `?status=${encodeURIComponent(status)}` : '';
    return apiFetch<AuditResponse>(`/api/audit${qs}`);
  },
  assistantChat: (message: string) =>
    apiFetch<AssistantChatResponse>('/api/assistant/chat', {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),
  getAdminDevices: () => apiFetch<AdminDevicesResponse>('/api/admin/devices'),
  getAdminAutonomy: () => apiFetch<AdminAutonomyResponse>('/api/admin/autonomy'),
  updateAdminAutonomy: (payload: {
    default_mode?: 'SHADOW' | 'COPILOT_ESCALATE' | 'AUTO_UNDER_CEILING';
    max_value?: number;
    ceiling_field?: string | null;
  }) =>
    apiFetch<AdminAutonomyResponse>('/api/admin/autonomy', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  getAdminOrg: () => apiFetch<AdminOrgResponse>('/api/admin/org'),
};
