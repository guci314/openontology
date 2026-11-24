import React, { useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import {
  ConsoleMeta,
  M1DraftResponse,
  SchemaCatalog,
  consoleApi,
  getActiveActorId,
  setActiveActorId,
} from './api/client';
import { ShellLayout } from './shell/ShellLayout';
import { CockpitPage } from './shell/pages/CockpitPage';
import { BlueprintPage } from './shell/pages/BlueprintPage';
import { SandboxPage } from './shell/pages/SandboxPage';
import { AuditPage } from './shell/pages/AuditPage';
import { AdminPage } from './shell/pages/AdminPage';
import { DynamicObjectDrawer } from './shell/components/DynamicObjectDrawer';
import { AssistantDrawer } from './shell/components/AssistantDrawer';
import { OntologyExpertDrawer } from './shell/components/blueprint/OntologyExpertDrawer';
import { DomainViewPack, resolveActivePack } from './shell/packRegistry';
import { registerEnterprisePack } from './packs/enterprise';

registerEnterprisePack();

const AppShell: React.FC = () => {
  const location = useLocation();
  const [meta, setMeta] = useState<ConsoleMeta | null>(null);
  const [schema, setSchema] = useState<SchemaCatalog | null>(null);
  const [activePack, setActivePack] = useState<DomainViewPack | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [drawerTarget, setDrawerTarget] = useState<{
    entityType: string;
    recordId: string;
  } | null>(null);
  const [drawerHistory, setDrawerHistory] = useState<
    Array<{ entityType: string; recordId: string }>
  >([]);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [expertOpen, setExpertOpen] = useState(false);
  const [m1Draft, setM1Draft] = useState<M1DraftResponse | null>(null);
  const [pendingPresetId, setPendingPresetId] = useState<string | null>(null);
  const [pendingSandboxId, setPendingSandboxId] = useState<string | null>(null);
  const consumePendingPreset = useCallback(() => setPendingPresetId(null), []);
  const consumePendingSandbox = useCallback(() => setPendingSandboxId(null), []);

  useEffect(() => {
    if (location.pathname.startsWith('/blueprint')) {
      setAssistantOpen(false);
    } else {
      setExpertOpen(false);
    }
  }, [location.pathname]);

  const loadBootstrap = useCallback(async () => {
    try {
      setLoadError(null);
      let m: ConsoleMeta;
      try {
        m = await consoleApi.getMeta();
      } catch (err) {
        if (getActiveActorId()) {
          setActiveActorId(null);
          m = await consoleApi.getMeta();
        } else {
          throw err;
        }
      }
      if (m.demo_actor_switch && m.actor?.actor_id) {
        setActiveActorId(m.actor.actor_id);
      }
      document.title = m.app_title || 'OpenOntology Digital Twin Console';
      const s = await consoleApi.getSchema();
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
      setDrawerHistory([]);
      setAssistantOpen(false);
      setExpertOpen(false);
      setM1Draft(null);
      setPendingPresetId(null);
      setPendingSandboxId(null);
      await loadBootstrap();
      setRefreshTick((t) => t + 1);
    },
    [loadBootstrap]
  );

  const handleExportM1Draft = useCallback(() => {
    if (!m1Draft?.draft_has_changes) return;
    const blob = new Blob([JSON.stringify(m1Draft.draft_overlay, null, 2)], {
      type: 'application/json;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `doal_overlay_${m1Draft.draft_id}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }, [m1Draft]);

  const handleDiscardM1Draft = useCallback(async () => {
    if (m1Draft?.draft_id) {
      await consoleApi.deleteBlueprintDraft(m1Draft.draft_id).catch(() => {});
    }
    setM1Draft(null);
  }, [m1Draft]);

  const handleApplyM1Draft = useCallback(async () => {
    if (!m1Draft?.draft_id || !m1Draft.draft_has_changes) return;
    await consoleApi.applyBlueprintDraft(m1Draft.draft_id);
    setM1Draft(null);
    setExpertOpen(false);
    handleRefresh();
  }, [m1Draft, handleRefresh]);

  const openRootObject = useCallback((entityType: string, recordId: string) => {
    setDrawerHistory([]);
    setDrawerTarget({ entityType, recordId });
  }, []);

  const pushObjectDrilldown = useCallback(
    (entityType: string, recordId: string) => {
      if (drawerTarget) {
        if (drawerTarget.entityType === entityType && drawerTarget.recordId === recordId) return;
        setDrawerHistory((prev) => [...prev.slice(-9), drawerTarget]);
      }
      setDrawerTarget({ entityType, recordId });
    },
    [drawerTarget]
  );

  const popDrawerHistory = useCallback(() => {
    setDrawerHistory((prev) => {
      if (prev.length === 0) return prev;
      const last = prev[prev.length - 1];
      setDrawerTarget(last);
      return prev.slice(0, -1);
    });
  }, []);

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
      pack={activePack}
      onSwitchActor={handleSwitchActor}
      onOpenAssistant={() => setAssistantOpen(true)}
      onOpenOntologyExpert={() => setExpertOpen(true)}
    >
      <Routes>
        <Route
          path="/"
          element={
            <CockpitPage
              key={meta.actor.actor_id}
              meta={meta}
              schema={schema}
              pack={activePack}
              refreshTick={refreshTick}
              onRefresh={handleRefresh}
              onInspectObject={openRootObject}
            />
          }
        />
        <Route
          path="/blueprint"
          element={
            <BlueprintPage
              key={meta.actor.actor_id}
              meta={meta}
              pack={activePack}
              refreshTick={refreshTick}
              draftState={m1Draft}
              onOpenExpert={() => setExpertOpen(true)}
              onExportDraft={handleExportM1Draft}
              onApplyDraft={handleApplyM1Draft}
              onDiscardDraft={handleDiscardM1Draft}
            />
          }
        />
        <Route
          path="/sandbox"
          element={
            <SandboxPage
              key={meta.actor.actor_id}
              meta={meta}
              schema={schema}
              pack={activePack}
              pendingPresetId={pendingPresetId}
              onConsumePendingPreset={consumePendingPreset}
              pendingSandboxId={pendingSandboxId}
              onConsumePendingSandbox={consumePendingSandbox}
              onPromotedToPrimary={handleRefresh}
            />
          }
        />
        <Route
          path="/audit"
          element={
            <AuditPage
              key={meta.actor.actor_id}
              meta={meta}
              pack={activePack}
              refreshTick={refreshTick}
            />
          }
        />
        <Route
          path="/admin"
          element={
            <AdminPage
              key={meta.actor.actor_id}
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
        pack={activePack}
        target={drawerTarget}
        canGoBack={drawerHistory.length > 0}
        onBack={popDrawerHistory}
        onClose={() => {
          setDrawerTarget(null);
          setDrawerHistory([]);
        }}
        onSelectObject={pushObjectDrilldown}
      />

      <AssistantDrawer
        key={`m0-${meta.actor.actor_id}`}
        open={assistantOpen}
        meta={meta}
        onClose={() => setAssistantOpen(false)}
        onInspectObject={openRootObject}
        onSelectSandboxPreset={(pid) => setPendingPresetId(pid)}
        onOpenSandboxSession={(sid) => {
          setPendingPresetId(null);
          setPendingSandboxId(sid);
        }}
      />

      <OntologyExpertDrawer
        key={`m1-${meta.actor.actor_id}`}
        open={expertOpen}
        meta={meta}
        draftState={m1Draft}
        onDraftUpdated={setM1Draft}
        onExportDraft={handleExportM1Draft}
        onApplyDraft={handleApplyM1Draft}
        onDiscardDraft={handleDiscardM1Draft}
        onClose={() => setExpertOpen(false)}
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
