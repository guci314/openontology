import React, { useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import {
  ConsoleMeta,
  SchemaCatalog,
  consoleApi,
  setActiveActorId,
} from './api/client';
import { ShellLayout } from './shell/ShellLayout';
import { CockpitPage } from './shell/pages/CockpitPage';
import { SandboxPage } from './shell/pages/SandboxPage';
import { AuditPage } from './shell/pages/AuditPage';
import { AdminPage } from './shell/pages/AdminPage';
import { DynamicObjectDrawer } from './shell/components/DynamicObjectDrawer';
import { AssistantDrawer } from './shell/components/AssistantDrawer';
import { DomainViewPack, resolveActivePack } from './shell/packRegistry';
import { registerEnterprisePack } from './packs/enterprise';

registerEnterprisePack();

const AppShell: React.FC = () => {
  const [meta, setMeta] = useState<ConsoleMeta | null>(null);
  const [schema, setSchema] = useState<SchemaCatalog | null>(null);
  const [activePack, setActivePack] = useState<DomainViewPack | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [drawerTarget, setDrawerTarget] = useState<{
    entityType: string;
    recordId: string;
  } | null>(null);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [pendingPresetId, setPendingPresetId] = useState<string | null>(null);

  const loadBootstrap = useCallback(async () => {
    try {
      setLoadError(null);
      const [m, s] = await Promise.all([consoleApi.getMeta(), consoleApi.getSchema()]);
      setMeta(m);
      setSchema(s);
      setActivePack(resolveActivePack(m, s));
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '控制台数据初始化失败');
    }
  }, []);

  useEffect(() => {
    loadBootstrap();
  }, [loadBootstrap]);

  const handleRefresh = useCallback(() => {
    setRefreshTick((t) => t + 1);
    loadBootstrap();
  }, [loadBootstrap]);

  const handleSwitchActor = useCallback(
    async (actorId: string) => {
      setActiveActorId(actorId);
      setDrawerTarget(null);
      await loadBootstrap();
      setRefreshTick((t) => t + 1);
    },
    [loadBootstrap]
  );

  if (loadError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#F6F8FB] p-6">
        <div className="max-w-md rounded-2xl border border-rose-200 bg-white p-6 text-center shadow-sm">
          <h2 className="text-base font-bold text-rose-700">无法连接运营台服务</h2>
          <p className="mt-2 text-xs text-slate-600">{loadError}</p>
          <button
            type="button"
            onClick={loadBootstrap}
            className="mt-4 rounded-lg bg-[#0F172A] px-4 py-2 text-xs font-semibold text-white"
          >
            重新连接
          </button>
        </div>
      </div>
    );
  }

  if (!meta || !schema) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#F6F8FB] text-sm text-slate-500">
        正在加载数字孪生运营台...
      </div>
    );
  }

  return (
    <ShellLayout
      meta={meta}
      onSwitchActor={handleSwitchActor}
      onOpenAssistant={() => setAssistantOpen(true)}
    >
      <Routes>
        <Route
          path="/"
          element={
            <CockpitPage
              meta={meta}
              schema={schema}
              pack={activePack}
              refreshTick={refreshTick}
              onRefresh={handleRefresh}
              onInspectObject={(entityType, recordId) =>
                setDrawerTarget({ entityType, recordId })
              }
            />
          }
        />
        <Route
          path="/sandbox"
          element={
            <SandboxPage
              meta={meta}
              schema={schema}
              pack={activePack}
              pendingPresetId={pendingPresetId}
              onConsumePendingPreset={() => setPendingPresetId(null)}
              onPromotedToPrimary={handleRefresh}
            />
          }
        />
        <Route
          path="/audit"
          element={<AuditPage meta={meta} refreshTick={refreshTick} />}
        />
        <Route
          path="/admin"
          element={
            <AdminPage
              meta={meta}
              schema={schema}
              onPolicyUpdated={handleRefresh}
            />
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>

      <DynamicObjectDrawer
        meta={meta}
        schema={schema}
        target={drawerTarget}
        onClose={() => setDrawerTarget(null)}
        onSelectObject={(entityType, recordId) =>
          setDrawerTarget({ entityType, recordId })
        }
      />

      <AssistantDrawer
        open={assistantOpen}
        meta={meta}
        onClose={() => setAssistantOpen(false)}
        onInspectObject={(entityType, recordId) =>
          setDrawerTarget({ entityType, recordId })
        }
        onSelectSandboxPreset={(pid) => setPendingPresetId(pid)}
      />
    </ShellLayout>
  );
};

export const App: React.FC = () => (
  <BrowserRouter>
    <AppShell />
  </BrowserRouter>
);

export default App;
