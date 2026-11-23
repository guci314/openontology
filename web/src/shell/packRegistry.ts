import type { ComponentType } from 'react';
import type {
  ConsoleMeta,
  SandboxDiffResponse,
  SchemaCatalog,
  WorkQueueDescriptor,
} from '../api/client';

export interface DomainKpiSlotProps {
  meta: ConsoleMeta;
  schema: SchemaCatalog;
  refreshTick: number;
  onInspectObject: (entityType: string, recordId: string) => void;
}

export interface DomainLineageSlotProps {
  meta: ConsoleMeta;
  schema: SchemaCatalog;
  refreshTick: number;
  onSelectEntityType: (entityType: string) => void;
}

export interface DomainSandboxSlotProps {
  meta: ConsoleMeta;
  schema: SchemaCatalog;
  sandboxId: string | null;
  sandboxDiff: SandboxDiffResponse | null;
  busy: boolean;
  onRunPreset: (presetId: string) => Promise<void>;
}

export interface ShellTerminology {
  cockpitNavLabel: string;
  catalogTitle: string;
  catalogCategoryLabel: string;
  catalogCountPrefix: string;
  recordNoun: string;
  drawerBackLabel: string;
  drawerOwnedBadge: string;
  drawerAttrsTitle: string;
  lineageTitle: string;
  sandboxDiffTitle: string;
  sandboxManualSubtitle: string;
  auditSearchPlaceholder: string;
}

export const DEFAULT_SHELL_TERMINOLOGY: ShellTerminology = {
  cockpitNavLabel: '总览工作台',
  catalogTitle: '实时对象目录与关系穿透',
  catalogCategoryLabel: '对象分类',
  catalogCountPrefix: '可见对象记录',
  recordNoun: '对象',
  drawerBackLabel: '返回上一对象',
  drawerOwnedBadge: '自有扩展',
  drawerAttrsTitle: '详细属性与自有扩展字段',
  lineageTitle: '对象前后关联图谱（受权可见范围）',
  sandboxDiffTitle: '现状 vs 沙盘 · 逐对象变更明细对比',
  sandboxManualSubtitle: '任选已声明动作在平行空间验证合规性与关键指标变化',
  auditSearchPlaceholder: '搜索流水号、动作或对象...',
};

export interface DomainViewPack {
  id: string;
  label: string;
  matches: (meta: ConsoleMeta, schema: SchemaCatalog) => boolean;
  terminology?: Partial<ShellTerminology>;
  workQueueFallback?: WorkQueueDescriptor;
  preferredTypes?: string[];
  KpiBar?: ComponentType<DomainKpiSlotProps>;
  LineageBar?: ComponentType<DomainLineageSlotProps>;
  SandboxPanel?: ComponentType<DomainSandboxSlotProps>;
}

export function resolveShellTerminology(
  pack?: DomainViewPack | null,
  overrides?: ShellTerminology
): ShellTerminology {
  if (overrides) return overrides;
  return {
    ...DEFAULT_SHELL_TERMINOLOGY,
    ...(pack?.terminology || {}),
  };
}

const registeredPacks: DomainViewPack[] = [];

export function registerDomainPack(pack: DomainViewPack): void {
  if (!registeredPacks.some((p) => p.id === pack.id)) {
    registeredPacks.push(pack);
  }
}

export function resolveActivePack(meta: ConsoleMeta, schema: SchemaCatalog): DomainViewPack | null {
  for (const pack of registeredPacks) {
    if (pack.matches(meta, schema)) {
      return pack;
    }
  }
  return null;
}
