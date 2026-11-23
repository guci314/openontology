import React, { useEffect, useState } from 'react';
import { ArrowLeft, Layers, Shield, Sparkles, X } from 'lucide-react';
import type {
  ConsoleMeta,
  LineageResponse,
  SchemaCatalog,
  SchemaEntityMeta,
} from '../../api/client';
import { consoleApi } from '../../api/client';
import {
  displayEntityName,
  displayEnumValue,
  displayFieldName,
  displayYuanFieldLabel,
  formatSmartFieldValue,
  getStatusBadgeMeta,
} from '../i18nErrorMap';
import { LineageGraph } from './LineageGraph';

interface DynamicObjectDrawerProps {
  meta: ConsoleMeta;
  schema: SchemaCatalog;
  target: { entityType: string; recordId: string } | null;
  canGoBack?: boolean;
  onBack?: () => void;
  onClose: () => void;
  onSelectObject: (entityType: string, recordId: string) => void;
}

const formatDisplayValue = (
  val: unknown,
  fieldName: string,
  meta: ConsoleMeta
): string => {
  if (val === null || val === undefined || val === '') return '—';
  if (typeof val === 'boolean') return val ? '是' : '否';
  if (fieldName === 'status' || fieldName === 'stage' || fieldName === 'payment_state') {
    return getStatusBadgeMeta(val, meta.display).label;
  }
  if (typeof val === 'number') {
    return formatSmartFieldValue(fieldName, val, meta, {
      decimals: fieldName.endsWith('_cents') ? 2 : 0,
    });
  }
  if (typeof val === 'string' && val.startsWith('{') && val.endsWith('}')) {
    try {
      const parsed = JSON.parse(val) as Record<string, unknown>;
      return Object.entries(parsed)
        .map(([k, v]) => `${displayFieldName('', k, meta.display)}: ${String(v)}`)
        .join('，');
    } catch {
      return val;
    }
  }
  if (Array.isArray(val)) {
    return val.map((x) => displayEnumValue(String(x), meta.display)).join(', ');
  }
  if (typeof val === 'string') return displayEnumValue(val, meta.display);
  return String(val);
};

export const DynamicObjectDrawer: React.FC<DynamicObjectDrawerProps> = ({
  meta,
  schema,
  target,
  canGoBack,
  onBack,
  onClose,
  onSelectObject,
}) => {
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!target) {
      setLineage(null);
      setErrorMsg(null);
      return;
    }
    let cancelled = false;
    setLineage(null);
    setLoading(true);
    setErrorMsg(null);
    consoleApi
      .getObjectLineage(target.entityType, target.recordId)
      .then((res) => {
        if (!cancelled) setLineage(res);
      })
      .catch((err) => {
        if (!cancelled) {
          setLineage(null);
          setErrorMsg(err instanceof Error ? err.message : '无权访问该记录或记录不存在');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [target, meta.actor.actor_id]);

  useEffect(() => {
    if (!target) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [target, onClose]);

  if (!target) return null;

  const entityDef: SchemaEntityMeta | undefined = schema.object_types.find(
    (t) => t.name === target.entityType
  );
  const record = lineage?.root;
  const props = record?.properties || {};
  const ownedSet = new Set(entityDef?.owned_fields || []);
  const statusVal = props['status'] ?? props['stage'];
  const statusBadge = statusVal ? getStatusBadgeMeta(statusVal, meta.display) : null;

  const orderedFields = entityDef
    ? entityDef.fields
    : Object.keys(props).map((k) => ({
        name: k,
        display_label: displayFieldName(target.entityType, k, meta.display),
        property_kind: 'datatype' as const,
        owned: ownedSet.has(k),
        type: 'str',
      }));

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-900/30 backdrop-blur-[1px]">
      <div
        className="flex h-full w-full max-w-2xl flex-col bg-white shadow-2xl"
        data-testid="object-detail-drawer"
      >
        {/* Header */}
        <div className="flex items-start justify-between border-b border-slate-200 px-6 py-5">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              {canGoBack && onBack && (
                <button
                  type="button"
                  onClick={onBack}
                  data-testid="drawer-back-btn"
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                >
                  <ArrowLeft className="h-3 w-3" />
                  <span>返回上一单据</span>
                </button>
              )}
              <span className="rounded-md bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
                {displayEntityName(target.entityType, meta.display)}
              </span>
              {!errorMsg && statusBadge && (
                <span className="rounded-full bg-blue-50 px-2.5 py-0.5 text-xs font-medium text-blue-700 ring-1 ring-blue-200">
                  {statusBadge.label}
                </span>
              )}
              {!errorMsg && record?.owned && (
                <span className="inline-flex items-center gap-1 rounded-full bg-sky-50 px-2 py-0.5 text-[11px] font-medium text-sky-700 ring-1 ring-sky-200">
                  <Sparkles className="h-3 w-3" /> 扩展台账
                </span>
              )}
            </div>
            <h3 className="mt-2 font-mono text-lg font-bold text-slate-900">
              {target.recordId}
            </h3>
            {!errorMsg && record?.title && record.title !== target.recordId && (
              <p className="text-sm text-slate-600">{record.title}</p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            data-testid="close-object-drawer"
            className="rounded-lg border border-slate-200 p-2 text-slate-500 transition hover:bg-slate-50 hover:text-slate-800"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 space-y-5 overflow-y-auto px-6 py-5">
          {errorMsg ? (
            <div className="space-y-3 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
              <p>{errorMsg}</p>
              {canGoBack && onBack && (
                <button
                  type="button"
                  onClick={onBack}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-white px-3 py-1.5 text-xs font-semibold text-rose-800 hover:bg-rose-100"
                >
                  <ArrowLeft className="h-3.5 w-3.5" />
                  返回上一单据
                </button>
              )}
            </div>
          ) : (
            <>
              <LineageGraph
                meta={meta}
                lineage={lineage}
                loading={loading}
                onSelectNode={(et, rid) => onSelectObject(et, rid)}
              />

              <div>
                <div className="mb-3 flex items-center justify-between">
                  <div className="flex items-center gap-2 text-xs font-semibold text-slate-800">
                    <Layers className="h-3.5 w-3.5 text-slate-500" />
                    <span>单据详细属性与客户自有扩展字段</span>
                  </div>
                  {record && record.security_tags.length > 0 && (
                    <span className="inline-flex items-center gap-1 text-[11px] text-slate-500">
                      <Shield className="h-3 w-3 text-emerald-600" />
                      权限标签：{record.security_tags.join(', ')}
                    </span>
                  )}
                </div>

                <div className="overflow-hidden rounded-xl border border-slate-200">
                  <dl className="divide-y divide-slate-100">
                    {orderedFields.map((fld) => {
                      const rawVal = props[fld.name];
                      const rawLabel =
                        'display_label' in fld && fld.display_label
                          ? String(fld.display_label)
                          : displayFieldName(target.entityType, fld.name, meta.display);
                      const label = displayYuanFieldLabel(rawLabel, fld.name);
                      const isRel = fld.property_kind === 'object' && fld.range && rawVal;
                      const isOwned = fld.owned || ownedSet.has(fld.name);

                      return (
                        <div
                          key={fld.name}
                          className={`grid grid-cols-12 items-center gap-2 px-4 py-2.5 text-xs ${
                            isOwned ? 'bg-sky-50/40' : 'bg-white'
                          }`}
                        >
                          <dt className="col-span-5 flex items-center gap-1.5 font-medium text-slate-600">
                            <span>{label}</span>
                            {isOwned && (
                              <span
                                data-testid={`owned-badge-${fld.name}`}
                                className="rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700"
                              >
                                自有扩展
                              </span>
                            )}
                          </dt>
                          <dd className="col-span-7 text-right font-mono tabular-nums text-slate-900">
                            {isRel ? (
                              <button
                                type="button"
                                onClick={() => onSelectObject(fld.range!, String(rawVal))}
                                className="font-semibold text-blue-700 underline decoration-blue-200 underline-offset-2 hover:text-blue-900"
                              >
                                {String(rawVal)}
                              </button>
                            ) : (
                              formatDisplayValue(rawVal, fld.name, meta)
                            )}
                          </dd>
                        </div>
                      );
                    })}
                  </dl>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
