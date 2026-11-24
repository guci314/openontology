import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';
import type { ConsoleMeta, RecordInstance, SchemaEntityMeta } from '../../api/client';
import {
  displayEntityName,
  displayFieldName,
  formatSmartFieldValue,
  getStatusBadgeMeta,
} from '../i18nErrorMap';

interface ObjectRefComboboxProps {
  paramName: string;
  testId?: string;
  value: string;
  onChange: (nextValue: string) => void;
  targetEntity: SchemaEntityMeta;
  options: RecordInstance[];
  loading?: boolean;
  meta: ConsoleMeta;
  placeholder?: string;
  hasError?: boolean;
}

const TONE_BADGE: Record<string, string> = {
  emerald: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  amber: 'bg-amber-50 text-amber-800 ring-1 ring-amber-200',
  rose: 'bg-rose-50 text-rose-700 ring-1 ring-rose-200',
  sky: 'bg-sky-50 text-sky-700 ring-1 ring-sky-200',
  slate: 'bg-slate-100 text-slate-700 ring-1 ring-slate-200',
};

function summarizeInstanceFields(
  obj: RecordInstance,
  entityMeta: SchemaEntityMeta,
  meta: ConsoleMeta
): Array<{ key: string; label: string; text: string }> {
  const excluded = new Set<string>([
    entityMeta.primary_key,
    entityMeta.title || '',
    entityMeta.status_field || '',
    'status',
    'stage',
  ]);
  const props = obj.properties || {};
  const entries: Array<{ key: string; label: string; text: string }> = [];

  for (const [k, v] of Object.entries(props)) {
    if (excluded.has(k)) continue;
    if (v === null || v === undefined || v === '' || v === '***') continue;
    if (typeof v === 'object') continue;
    const text = formatSmartFieldValue(k, v, meta);
    if (!text || text === '—' || text === '***') continue;
    const label = displayFieldName(obj.object_type, k, meta.display);
    entries.push({ key: k, label, text });
    if (entries.length >= 2) break;
  }
  return entries;
}

export const ObjectRefCombobox: React.FC<ObjectRefComboboxProps> = ({
  paramName,
  testId,
  value,
  onChange,
  targetEntity,
  options,
  loading = false,
  meta,
  placeholder,
  hasError = false,
}) => {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onDocMouseDown = (ev: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(ev.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, []);

  const trimmed = value.trim();
  const matchedOption = useMemo(
    () => options.find((o) => o.object_id === trimmed) || null,
    [options, trimmed]
  );

  const filteredOptions = useMemo(() => {
    // When value already matches a selected ID exactly, keep full list accessible on reopen
    if (!trimmed || matchedOption !== null) return options;
    const needle = trimmed.toLowerCase();
    return options.filter(
      (o) =>
        o.object_id.toLowerCase().includes(needle) ||
        String(o.title || '').toLowerCase().includes(needle)
    );
  }, [options, trimmed, matchedOption]);

  // Never keep a floating menu obstructing buttons below when user typed an unmatched custom/new ID
  const showMenu = open && !(trimmed.length > 0 && filteredOptions.length === 0);
  const showUnmatchedHint = Boolean(trimmed.length > 0 && filteredOptions.length === 0);

  const entityLabel = displayEntityName(targetEntity.name, meta.display);
  const resolvedDetails = useMemo(() => {
    if (!matchedOption) return null;
    const statusKey = targetEntity.status_field || 'status';
    const rawStatus =
      matchedOption.properties?.[statusKey] ?? matchedOption.properties?.['stage'];
    const parts: string[] = [matchedOption.object_id];
    if (rawStatus !== undefined && rawStatus !== null && rawStatus !== '') {
      parts.push(getStatusBadgeMeta(rawStatus, meta.display).label);
    } else {
      const firstExtra = summarizeInstanceFields(matchedOption, targetEntity, meta)[0];
      if (firstExtra) parts.push(`${firstExtra.label}: ${firstExtra.text}`);
    }
    const displayName = matchedOption.title || matchedOption.object_id;
    return `已选${entityLabel}：${displayName}（${parts.join(' · ')}）`;
  }, [matchedOption, targetEntity, entityLabel, meta]);

  return (
    <div ref={rootRef} className="relative">
      <div className="relative flex items-center">
        <input
          type="text"
          autoComplete="off"
          data-testid={testId}
          value={value}
          onFocus={() => setOpen(true)}
          onClick={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onChange={(e) => {
            const nextVal = e.target.value;
            onChange(nextVal);
            const nextTrim = nextVal.trim();
            const isExactMatch =
              Boolean(nextTrim) && options.some((o) => o.object_id === nextTrim);
            setOpen(!isExactMatch);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape' || e.key === 'Enter') {
              setOpen(false);
            }
          }}
          placeholder={placeholder || `搜索名称或选择${entityLabel}...`}
          className={`w-full rounded-lg border py-1.5 pl-3 pr-8 text-xs text-slate-900 focus:bg-white focus:outline-none ${
            hasError
              ? 'border-rose-400 bg-rose-50/40 focus:border-rose-500'
              : 'border-slate-200 bg-slate-50 focus:border-blue-500'
          }`}
        />
        <button
          type="button"
          tabIndex={-1}
          aria-label={`展开${entityLabel}选项`}
          data-testid={`combobox-toggle-${paramName}`}
          onMouseDown={(e) => {
            e.preventDefault();
            setOpen((prev) => !prev);
          }}
          className="absolute right-2 rounded p-0.5 text-slate-400 hover:text-slate-700"
        >
          <ChevronDown className={`h-3.5 w-3.5 transition ${showMenu ? 'rotate-180' : ''}`} />
        </button>
      </div>

      {showMenu && (
        <div
          data-testid={`combobox-menu-${paramName}`}
          className="absolute left-0 right-0 z-40 mt-1 max-h-60 overflow-y-auto rounded-xl border border-slate-200 bg-white p-1 shadow-lg"
        >
          <div className="flex items-center justify-between px-2.5 py-1 text-[10px] font-medium text-slate-400">
            <span>{entityLabel}（共 {options.length} 项）</span>
            {loading && <span>加载中...</span>}
          </div>

          {filteredOptions.length === 0 ? (
            <div className="px-3 py-3 text-center text-[11px] text-slate-400">
              当前权限视角下暂无可选{entityLabel}
            </div>
          ) : (
            filteredOptions.slice(0, 30).map((obj) => {
              const isSelected = obj.object_id === trimmed;
              const statusKey = targetEntity.status_field || 'status';
              const rawStatus = obj.properties?.[statusKey] ?? obj.properties?.['stage'];
              const badge =
                rawStatus !== undefined && rawStatus !== null && rawStatus !== ''
                  ? getStatusBadgeMeta(rawStatus, meta.display)
                  : null;
              const subFields = summarizeInstanceFields(obj, targetEntity, meta);

              return (
                <button
                  key={`${obj.object_type}:${obj.object_id}`}
                  type="button"
                  data-testid={`combobox-option-${obj.object_id}`}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    onChange(obj.object_id);
                    setOpen(false);
                  }}
                  className={`flex w-full flex-col rounded-lg px-2.5 py-2 text-left transition ${
                    isSelected ? 'bg-blue-50/80' : 'hover:bg-slate-50'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span className="truncate text-xs font-semibold text-slate-900">
                        {obj.title || obj.object_id}
                      </span>
                      <code className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] font-medium text-slate-600">
                        {obj.object_id}
                      </code>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      {badge && (
                        <span
                          className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                            TONE_BADGE[badge.tone] || TONE_BADGE.slate
                          }`}
                        >
                          {badge.label}
                        </span>
                      )}
                      {isSelected && <Check className="h-3.5 w-3.5 text-blue-700" />}
                    </div>
                  </div>
                  {subFields.length > 0 && (
                    <div className="mt-0.5 truncate text-[11px] text-slate-500">
                      {subFields.map((sf) => `${sf.label}: ${sf.text}`).join(' · ')}
                    </div>
                  )}
                </button>
              );
            })
          )}
        </div>
      )}

      {resolvedDetails ? (
        <div
          data-testid={`combobox-resolved-${paramName}`}
          className="mt-1 inline-flex max-w-full items-center truncate rounded-md bg-emerald-50/80 px-2 py-0.5 text-[11px] font-medium text-emerald-800 ring-1 ring-emerald-200/80"
        >
          {resolvedDetails}
        </div>
      ) : (
        showUnmatchedHint && (
          <div className="mt-1 text-[11px] text-slate-400">
            未匹配到现有{entityLabel}，将按自定义编号提交校验
          </div>
        )
      )}
    </div>
  );
};
