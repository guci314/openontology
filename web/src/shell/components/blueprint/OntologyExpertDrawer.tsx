import React, { useEffect, useRef, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Cpu,
  Download,
  Rocket,
  Send,
  ShieldAlert,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import type {
  AssistantToolTraceItem,
  BlueprintProveResponse,
  ConsoleMeta,
  M1DraftResponse,
  M1EscalationTicket,
} from '../../../api/client';
import { consoleApi } from '../../../api/client';

interface ExpertChatMessage {
  id: string;
  sender: 'user' | 'assistant';
  text: string;
  toolTrace?: AssistantToolTraceItem[];
  proofReport?: BlueprintProveResponse | null;
  clausesZh?: string[];
  escalations?: M1EscalationTicket[];
}

interface OntologyExpertDrawerProps {
  open: boolean;
  meta: ConsoleMeta;
  draftState: M1DraftResponse | null;
  onDraftUpdated: (nextDraft: M1DraftResponse | null) => void;
  onExportDraft: () => void;
  onApplyDraft: () => Promise<void>;
  onDiscardDraft: () => Promise<void>;
  onClose: () => void;
}

const EXPERT_QUICK_PROMPTS = [
  '为现有主实体派生一个受治理子类并增加 x_tier 字段，然后运行 Z3 数学证明',
  '新增一条保护核心字段非负的红线不变量并核验全路径',
  '当前模型是否存在死类、互斥冲突或未保护的动作分支？',
];

export const OntologyExpertDrawer: React.FC<OntologyExpertDrawerProps> = ({
  open,
  meta,
  draftState,
  onDraftUpdated,
  onExportDraft,
  onApplyDraft,
  onDiscardDraft,
  onClose,
}) => {
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [applyBusy, setApplyBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [messages, setMessages] = useState<ExpertChatMessage[]>([
    {
      id: 'welcome-m1',
      sender: 'assistant',
      text: '您好，我是 M1 AI 本体论专家。我会严格保护基座标准契约（只读 + 单调扩展），协助您在隔离的 DOAL 草稿层中编排实体、子类继承、语义连线、声明式 Rule 动作与红线不变量，并实时调用 Z3 执行 PO-1~PO-4 数学证明。',
    },
  ]);
  const streamEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    streamEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [open, messages, busy]);

  if (!open) return null;

  const isLlmMode = meta.assistant_mode === 'llm_sandbox';
  const modeBadgeLabel = isLlmMode
    ? `DeepSeek DOAL 专家 · ${meta.assistant_model || 'deepseek-flash'}`
    : '离线模式';

  const proofReport = draftState?.proof_report || null;
  const proofHasIssues = Boolean(
    proofReport?.prover_available && proofReport?.verdict === 'ISSUES_FOUND'
  );

  const sendPrompt = async (textToSend: string) => {
    const trimmed = textToSend.trim();
    if (!trimmed || busy) return;
    setActionError(null);
    const historyTurns = messages
      .filter((m) => m.id !== 'welcome-m1' && m.text.trim().length > 0)
      .slice(-8)
      .map((m) => ({ role: m.sender, content: m.text }));
    const userMsg: ExpertChatMessage = {
      id: `m1u-${Date.now()}`,
      sender: 'user',
      text: trimmed,
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setBusy(true);
    try {
      const res = await consoleApi.blueprintExpertChat(trimmed, {
        draft_id: draftState?.draft_id || null,
        history: historyTurns,
      });
      onDraftUpdated(res);
      setMessages((prev) => [
        ...prev,
        {
          id: `m1a-${Date.now()}`,
          sender: 'assistant',
          text: res.reply,
          toolTrace: res.tool_trace,
          proofReport: res.proof_report,
          clausesZh: res.draft_clauses_zh,
          escalations: res.escalations,
        },
      ]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          id: `m1err-${Date.now()}`,
          sender: 'assistant',
          text: err instanceof Error ? err.message : 'M1 建模专家服务暂时不可用',
        },
      ]);
    } finally {
      setBusy(false);
    }
  };

  const handleApplyClick = async () => {
    if (!draftState?.draft_has_changes || applyBusy || proofHasIssues) return;
    setActionError(null);
    setApplyBusy(true);
    try {
      await onApplyDraft();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '应用草稿失败');
    } finally {
      setApplyBusy(false);
    }
  };

  const handleDiscardClick = async () => {
    setActionError(null);
    await onDiscardDraft();
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-900/30 backdrop-blur-[1px]">
      <div
        className="flex h-full w-full max-w-lg flex-col bg-white shadow-2xl"
        data-testid="ontology-expert-drawer"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-200 bg-violet-50/40 px-5 py-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-violet-100 text-violet-700 ring-1 ring-violet-200">
              <Sparkles className="h-5 w-5" />
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold text-slate-900">AI 本体论专家</h3>
                <span
                  data-testid="expert-mode-badge"
                  className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                    isLlmMode
                      ? 'bg-violet-100 text-violet-800 ring-1 ring-violet-200'
                      : 'bg-slate-100 text-slate-600 ring-1 ring-slate-200'
                  }`}
                >
                  {modeBadgeLabel}
                </span>
              </div>
              <p className="mt-0.5 text-[11px] text-slate-500">
                DOAL 建模 · 单调 LSP 护栏 · Z3 实时证明
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            data-testid="close-ontology-expert-drawer"
            className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-500 hover:bg-slate-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Quick prompts */}
        <div className="border-b border-slate-100 bg-slate-50/70 px-5 py-3">
          <div className="mb-1.5 flex items-center gap-1 text-[11px] font-medium text-slate-500">
            <Cpu className="h-3 w-3 text-violet-600" />
            <span>常用 M1 建模指令</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {EXPERT_QUICK_PROMPTS.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => sendPrompt(q)}
                className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-left text-[11px] text-slate-700 transition hover:border-violet-300 hover:text-violet-700"
              >
                {q}
              </button>
            ))}
          </div>
        </div>

        {/* Conversation stream */}
        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {messages.map((m) => (
            <div
              key={m.id}
              className={`flex flex-col ${m.sender === 'user' ? 'items-end' : 'items-start'}`}
            >
              <div
                className={`max-w-[94%] rounded-2xl px-4 py-2.5 text-xs leading-relaxed ${
                  m.sender === 'user'
                    ? 'bg-[#0F172A] text-white'
                    : 'border border-slate-200 bg-slate-50 text-slate-800'
                }`}
              >
                {m.toolTrace && m.toolTrace.length > 0 && (
                  <div
                    data-testid="expert-tool-trace"
                    className="mb-2 flex flex-wrap gap-1 border-b border-slate-200/80 pb-2"
                  >
                    {m.toolTrace.map((trace, idx) => (
                      <span
                        key={`${m.id}-mtr-${idx}`}
                        className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                          trace.ok
                            ? 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200'
                            : 'bg-rose-50 text-rose-700 ring-1 ring-rose-200'
                        }`}
                      >
                        {trace.ok ? (
                          <CheckCircle2 className="h-2.5 w-2.5 shrink-0" />
                        ) : (
                          <AlertCircle className="h-2.5 w-2.5 shrink-0" />
                        )}
                        <span>{trace.summary}</span>
                      </span>
                    ))}
                  </div>
                )}

                <p className="whitespace-pre-wrap">{m.text}</p>

                {/* Inline Z3 Proof Badge */}
                {m.proofReport && (
                  <div
                    data-testid="expert-proof-badge"
                    className={`mt-2.5 rounded-xl border p-2.5 text-[11px] ${
                      !m.proofReport.prover_available
                        ? 'border-amber-200 bg-amber-50 text-amber-900'
                        : m.proofReport.verdict === 'PROVEN_CLEAN'
                        ? 'border-emerald-200 bg-emerald-50 text-emerald-900'
                        : 'border-rose-200 bg-rose-50 text-rose-900'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 font-semibold">
                      <span className="inline-flex items-center gap-1">
                        {!m.proofReport.prover_available ? (
                          <AlertCircle className="h-3.5 w-3.5 text-amber-700" />
                        ) : m.proofReport.verdict === 'PROVEN_CLEAN' ? (
                          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-700" />
                        ) : (
                          <ShieldAlert className="h-3.5 w-3.5 text-rose-700" />
                        )}
                        {!m.proofReport.prover_available
                          ? 'Z3 未安装（降级跳过形式化核验）'
                          : m.proofReport.verdict === 'PROVEN_CLEAN'
                          ? 'Z3 证明通过 · PROVEN_CLEAN'
                          : 'Z3 发现冲突 · ISSUES_FOUND'}
                      </span>
                      {m.proofReport.summary && (
                        <span className="font-mono text-[10px]">
                          {m.proofReport.summary.satisfiable_classes ?? 0}/
                          {m.proofReport.summary.total_classes ?? 0} 类 ·{' '}
                          {m.proofReport.summary.reachable_actions ?? 0}/
                          {m.proofReport.summary.total_actions ?? 0} 动作
                        </span>
                      )}
                    </div>
                    {m.proofReport.verdict === 'ISSUES_FOUND' &&
                      (m.proofReport.class_proofs || [])
                        .filter((cp) => cp.status !== 'SATISFIABLE')
                        .map((cp) => (
                          <div key={cp.object_type} className="mt-1.5 text-[10px] text-rose-800">
                            类 `{cp.object_type}` [{cp.status}]
                            {cp.unsat_core.length > 0 && (
                              <span className="ml-1 font-mono">
                                core={cp.unsat_core.join(', ')}
                              </span>
                            )}
                          </div>
                        ))}
                  </div>
                )}

                {/* Deterministic Chinese Clauses */}
                {m.clausesZh && m.clausesZh.length > 0 && (
                  <div
                    data-testid="expert-clauses-zh"
                    className="mt-2.5 rounded-xl border border-violet-200/80 bg-white p-2.5 text-[11px] text-slate-700"
                  >
                    <div className="mb-1 font-semibold text-violet-900">
                      确定性 DOAL 中文回译条款（防语义漂移核对）
                    </div>
                    <ol className="list-decimal space-y-0.5 pl-4">
                      {m.clausesZh.map((c, ci) => (
                        <li key={ci}>{c}</li>
                      ))}
                    </ol>
                  </div>
                )}

                {/* AG-3 Turing-Complete Escalation Cards */}
                {m.escalations && m.escalations.length > 0 && (
                  <div className="mt-2.5 space-y-1.5">
                    {m.escalations.map((esc) => (
                      <div
                        key={esc.ticket_id}
                        data-testid="expert-escalation-card"
                        className="rounded-xl border border-amber-300 bg-amber-50/90 p-2.5 text-[11px] text-amber-950"
                      >
                        <div className="font-semibold">
                          原语工程升级提案 · {esc.ticket_id}：{esc.title}
                        </div>
                        <div className="mt-0.5 text-amber-900">{esc.reason}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
          <div ref={streamEndRef} />
        </div>

        {/* Sticky Draft Action Bar */}
        {draftState && draftState.draft_has_changes && (
          <div className="border-t border-violet-200 bg-violet-50/60 px-4 py-3">
            {actionError && (
              <div className="mb-2 rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[11px] text-rose-800">
                {actionError}
              </div>
            )}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-[11px] font-medium text-violet-900">
                草稿 <code className="font-mono">{draftState.draft_id}</code> ·{' '}
                {draftState.draft_clauses_zh.length} 项增量变更
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  data-testid="discard-doal-draft-btn"
                  onClick={handleDiscardClick}
                  className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-medium text-slate-600 hover:bg-slate-50"
                >
                  <Trash2 className="h-3 w-3" /> 清空草稿
                </button>
                <button
                  type="button"
                  data-testid="export-doal-btn"
                  onClick={onExportDraft}
                  className="inline-flex items-center gap-1 rounded-lg border border-violet-300 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-violet-800 hover:bg-violet-50"
                >
                  <Download className="h-3 w-3" /> 导出 DOAL overlay.json
                </button>
                {meta.demo_actor_switch && (
                  <button
                    type="button"
                    data-testid="apply-doal-draft-btn"
                    disabled={applyBusy || proofHasIssues}
                    onClick={handleApplyClick}
                    className="inline-flex items-center gap-1 rounded-lg bg-violet-700 px-3 py-1.5 text-[11px] font-semibold text-white shadow-sm hover:bg-violet-800 disabled:opacity-45"
                  >
                    <Rocket className="h-3 w-3" />
                    {applyBusy ? '应用中...' : '应用到当前演示世界'}
                  </button>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Prompt Input */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            sendPrompt(input);
          }}
          className="border-t border-slate-200 bg-white p-4"
        >
          <div className="flex items-center gap-2">
            <input
              type="text"
              data-testid="expert-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="描述模型演化需求，例如：为实体派生带状态机的子类并添加红线..."
              className="flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-2 text-xs text-slate-900 placeholder:text-slate-400 focus:border-violet-500 focus:bg-white focus:outline-none"
            />
            <button
              type="submit"
              data-testid="expert-send-btn"
              disabled={busy || !input.trim()}
              className="inline-flex h-9 w-9 items-center justify-center rounded-xl bg-violet-700 text-white transition hover:bg-violet-800 disabled:opacity-40"
            >
              <Send className="h-4 w-4" />
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
