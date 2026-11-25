import React, { useEffect, useRef, useState } from 'react';
import { Bot, Send, Sparkles, X, ArrowUpRight, CheckCircle2, AlertCircle } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import type { AssistantCard, AssistantToolTraceItem, ConsoleMeta } from '../../api/client';
import { consoleApi } from '../../api/client';

interface AssistantMessage {
  id: string;
  sender: 'user' | 'assistant';
  text: string;
  highlights?: Array<{ label: string; value: string }>;
  cards?: AssistantCard[];
  toolTrace?: AssistantToolTraceItem[];
  sandboxId?: string | null;
}

interface AssistantDrawerProps {
  open: boolean;
  meta: ConsoleMeta;
  activeSandboxId?: string | null;
  onClose: () => void;
  onInspectObject: (entityType: string, recordId: string) => void;
  onSelectSandboxPreset?: (presetId: string) => void;
  onOpenSandboxSession?: (sandboxId: string) => void;
  onSandboxUpdated?: (sandboxId: string) => void;
}

const FALLBACK_QUICK_PROMPTS = [
  '当前有哪些待处理的协同事项？',
  '当前可见记录与状态分布如何？',
  '最近有哪些规则拦截或审计记录？',
  '我当前的权限可见范围是什么？',
];

export const AssistantDrawer: React.FC<AssistantDrawerProps> = ({
  open,
  meta,
  activeSandboxId = null,
  onClose,
  onInspectObject,
  onSelectSandboxPreset,
  onOpenSandboxSession,
  onSandboxUpdated,
}) => {
  const navigate = useNavigate();
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const welcomeText =
    meta.assistant_welcome ||
    `您好 ${meta.actor.display_name}！我是运行在「${meta.actor.title}」权限视角内的智能助手，可为您检索可见记录、待办审批与平行沙盘推演。`;
  const [messages, setMessages] = useState<AssistantMessage[]>([
    {
      id: 'welcome',
      sender: 'assistant',
      text: welcomeText,
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

  const quickPrompts =
    meta.quick_prompts && meta.quick_prompts.length > 0
      ? meta.quick_prompts
      : FALLBACK_QUICK_PROMPTS;

  const isLlmMode = meta.assistant_mode === 'llm_sandbox';
  const modeBadgeLabel = isLlmMode
    ? `DeepSeek 沙盘代理 · ${meta.assistant_model || 'deepseek-flash'}`
    : '离线检索模式';

  const sendQuestion = async (textToSend: string) => {
    const trimmed = textToSend.trim();
    if (!trimmed || busy) return;
    const historyTurns = messages
      .filter((m) => m.id !== 'welcome' && m.text.trim().length > 0)
      .slice(-8)
      .map((m) => ({ role: m.sender, content: m.text }));
    const userMsg: AssistantMessage = {
      id: `u-${Date.now()}`,
      sender: 'user',
      text: trimmed,
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setBusy(true);
    try {
      const res = await consoleApi.assistantChat(trimmed, {
        sandbox_id: activeSandboxId,
        history: historyTurns,
      });
      if (res.sandbox_id) {
        onSandboxUpdated?.(res.sandbox_id);
      }
      setMessages((prev) => [
        ...prev,
        {
          id: `a-${Date.now()}`,
          sender: 'assistant',
          text: res.reply,
          highlights: res.highlights,
          cards: res.cards,
          toolTrace: res.tool_trace,
          sandboxId: res.sandbox_id ?? null,
        },
      ]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          id: `err-${Date.now()}`,
          sender: 'assistant',
          text: err instanceof Error ? err.message : '问答服务暂时不可用',
        },
      ]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-900/30 backdrop-blur-[1px]">
      <div
        className="flex h-full w-full max-w-md flex-col bg-white shadow-2xl"
        data-testid="assistant-drawer"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-blue-50 text-blue-700 ring-1 ring-blue-200">
              <Bot className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold text-slate-900">AI 数字智能管家</h3>
                <span
                  data-testid="assistant-mode-badge"
                  className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                    isLlmMode
                      ? 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200'
                      : 'bg-slate-100 text-slate-600 ring-1 ring-slate-200'
                  }`}
                >
                  {modeBadgeLabel}
                </span>
              </div>
              <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-slate-500">
                <span>
                  当前视角：{meta.actor.display_name}（{meta.actor.unit_name || meta.actor.title}）
                </span>
                {activeSandboxId && (
                  <span
                    data-testid="assistant-active-sandbox-badge"
                    className="inline-flex items-center rounded-md bg-blue-50 px-1.5 py-0.5 font-mono text-[10px] font-medium text-blue-700 ring-1 ring-blue-200/80"
                  >
                    已同步沙盘 {activeSandboxId}
                  </span>
                )}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            data-testid="close-assistant-drawer"
            className="rounded-lg border border-slate-200 p-1.5 text-slate-500 hover:bg-slate-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Quick prompts */}
        <div className="border-b border-slate-100 bg-slate-50/70 px-5 py-3">
          <div className="mb-1.5 flex items-center gap-1 text-[11px] font-medium text-slate-500">
            <Sparkles className="h-3 w-3 text-blue-600" />
            <span>一键提问</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {quickPrompts.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => sendQuestion(q)}
                className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-left text-[11px] text-slate-700 transition hover:border-blue-300 hover:text-blue-700"
              >
                {q}
              </button>
            ))}
          </div>
        </div>

        {/* Message stream */}
        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {messages.map((m) => (
            <div
              key={m.id}
              className={`flex flex-col ${m.sender === 'user' ? 'items-end' : 'items-start'}`}
            >
              <div
                className={`max-w-[92%] rounded-2xl px-4 py-2.5 text-xs leading-relaxed ${
                  m.sender === 'user'
                    ? 'bg-[#0F172A] text-white'
                    : 'border border-slate-200 bg-slate-50 text-slate-800'
                }`}
              >
                {m.toolTrace && m.toolTrace.length > 0 && (
                  <div
                    data-testid="assistant-tool-trace"
                    className="mb-2 flex flex-wrap gap-1 border-b border-slate-200/80 pb-2"
                  >
                    {m.toolTrace.map((trace, idx) => (
                      <span
                        key={`${m.id}-tr-${idx}`}
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
                {m.highlights && m.highlights.length > 0 && (
                  <div className="mt-2.5 grid grid-cols-2 gap-1.5 border-t border-slate-200/80 pt-2">
                    {m.highlights.map((h) => (
                      <div key={h.label} className="rounded-lg bg-white px-2.5 py-1.5 ring-1 ring-slate-200/60">
                        <div className="text-[10px] text-slate-500">{h.label}</div>
                        <div className="font-mono text-xs font-semibold tabular-nums text-slate-900">
                          {h.value}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {m.cards && m.cards.length > 0 && (
                <div className="mt-2 w-full space-y-1.5">
                  {m.cards.map((card, i) => (
                    <button
                      key={`${m.id}-c-${i}`}
                      type="button"
                      data-testid={`assistant-card-${card.kind}`}
                      onClick={() => {
                        if (card.kind === 'object' && card.object_type && card.object_id) {
                          onInspectObject(card.object_type, card.object_id);
                          onClose();
                        } else if (card.kind === 'sandbox_session' && card.sandbox_id) {
                          if (onOpenSandboxSession) {
                            onOpenSandboxSession(card.sandbox_id);
                          }
                          onClose();
                          navigate(card.route || '/sandbox');
                        } else if (card.kind === 'sandbox_preset' || card.kind === 'sandbox') {
                          if (card.preset_id && onSelectSandboxPreset) {
                            onSelectSandboxPreset(card.preset_id);
                          }
                          onClose();
                          navigate(card.route || '/sandbox');
                        } else if (card.kind === 'audit') {
                          onClose();
                          navigate(card.route || '/audit');
                        }
                      }}
                      className={`flex w-full items-center justify-between rounded-xl border px-3 py-2 text-left transition ${
                        card.kind === 'sandbox_session'
                          ? 'border-emerald-300 bg-emerald-50/70 hover:border-emerald-400 hover:bg-emerald-50'
                          : 'border-slate-200 bg-white hover:border-blue-300 hover:bg-blue-50/40'
                      }`}
                    >
                      <div className="min-w-0 pr-2">
                        <div className="truncate text-xs font-semibold text-slate-900">
                          {card.title}
                        </div>
                        <div className="truncate text-[11px] text-slate-500">{card.subtitle}</div>
                      </div>
                      <ArrowUpRight
                        className={`h-4 w-4 shrink-0 ${
                          card.kind === 'sandbox_session' ? 'text-emerald-700' : 'text-blue-600'
                        }`}
                      />
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
          <div ref={streamEndRef} />
        </div>

        {/* Input box */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            sendQuestion(input);
          }}
          className="border-t border-slate-200 bg-white p-4"
        >
          <div className="flex items-center gap-2">
            <input
              type="text"
              data-testid="assistant-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                isLlmMode
                  ? '输入自然语言指令，例如：在沙盘中试演一次状态调整...'
                  : '输入问题，例如：当前有哪些待办或规则拦截...'
              }
              className="flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-2 text-xs text-slate-900 placeholder:text-slate-400 focus:border-blue-500 focus:bg-white focus:outline-none"
            />
            <button
              type="submit"
              data-testid="assistant-send-btn"
              disabled={busy || !input.trim()}
              className="inline-flex h-9 w-9 items-center justify-center rounded-xl bg-[#0F172A] text-white transition hover:bg-slate-800 disabled:opacity-40"
            >
              <Send className="h-4 w-4" />
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
