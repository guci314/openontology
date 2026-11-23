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

export interface DomainViewPack {
  id: string;
  label: string;
  matches: (meta: ConsoleMeta, schema: SchemaCatalog) => boolean;
  workQueueFallback?: WorkQueueDescriptor;
  preferredTypes?: string[];
  KpiBar?: ComponentType<DomainKpiSlotProps>;
  LineageBar?: ComponentType<DomainLineageSlotProps>;
  SandboxPanel?: ComponentType<DomainSandboxSlotProps>;
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
