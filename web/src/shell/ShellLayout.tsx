import React from 'react';
import { NavLink } from 'react-router-dom';
import {
  Activity,
  Bot,
  ChevronDown,
  FlaskConical,
  LayoutDashboard,
  Settings,
  ShieldCheck,
  UserCheck,
} from 'lucide-react';
import type { ConsoleMeta } from '../api/client';

interface ShellLayoutProps {
  meta: ConsoleMeta;
  onSwitchActor: (actorId: string) => Promise<void>;
  onOpenAssistant: () => void;
  children: React.ReactNode;
}

export const ShellLayout: React.FC<ShellLayoutProps> = ({
  meta,
  onSwitchActor,
  onOpenAssistant,
  children,
}) => {
  const modeLabel =
    meta.world_mode === 'coupled' ? '实时外设耦合模式' : '独立自治沙盘内核';
  const modeTone =
    meta.world_mode === 'coupled'
      ? 'bg-sky-50 text-sky-700 ring-1 ring-sky-200'
      : 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200';

  const navCls = ({ isActive }: { isActive: boolean }) =>
    `inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition ${
      isActive
        ? 'bg-[#0F172A] text-white shadow-sm'
        : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
    }`;

  return (
    <div className="min-h-screen bg-[#F6F8FB] text-slate-900">
      <header className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/95 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center justify-between gap-4 px-6 py-3">
          {/* Left: Brand & world badge */}
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[#0F172A] text-white shadow-sm">
              <Activity className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-sm font-bold tracking-tight text-slate-900">
                  {meta.app_title}
                </h1>
                <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${modeTone}`}>
                  {modeLabel}
                </span>
              </div>
              <p className="text-xs text-slate-500">{meta.app_subtitle}</p>
            </div>
          </div>

          {/* Center: Strictly 3 business tabs + role-gated Admin entry */}
          <nav className="flex items-center gap-1.5 rounded-xl bg-slate-100/80 p-1">
            <NavLink to="/" end className={navCls} data-testid="nav-cockpit">
              <LayoutDashboard className="h-4 w-4" />
              <span>经营驾驶舱</span>
            </NavLink>
            <NavLink to="/sandbox" className={navCls} data-testid="nav-sandbox">
              <FlaskConical className="h-4 w-4" />
              <span>平行沙盘推演</span>
            </NavLink>
            <NavLink to="/audit" className={navCls} data-testid="nav-audit">
              <ShieldCheck className="h-4 w-4" />
              <span>合规审计台</span>
            </NavLink>
            {meta.capabilities.is_admin_ops && (
              <NavLink to="/admin" className={navCls} data-testid="nav-admin">
                <Settings className="h-4 w-4" />
                <span>实施运维台</span>
              </NavLink>
            )}
          </nav>

          {/* Right: AI Assistant & Persona / Plant RLS Switcher */}
          <div className="flex items-center gap-2.5">
            <button
              type="button"
              data-testid="open-assistant-btn"
              onClick={onOpenAssistant}
              className="inline-flex items-center gap-1.5 rounded-xl border border-blue-200 bg-blue-50/70 px-3 py-1.5 text-xs font-semibold text-blue-800 transition hover:bg-blue-100"
            >
              <Bot className="h-4 w-4" />
              <span>AI 数字管家</span>
            </button>

            {meta.demo_actor_switch && meta.demo_actors.length > 0 ? (
              <div className="relative flex items-center rounded-xl border border-slate-200 bg-white px-2.5 py-1 shadow-sm">
                <UserCheck className="mr-1.5 h-4 w-4 text-slate-500" />
                <select
                  aria-label="切换登录角色与厂区权限"
                  data-testid="actor-switcher"
                  value={meta.actor.actor_id}
                  onChange={(e) => onSwitchActor(e.target.value)}
                  className="appearance-none bg-transparent pr-6 text-xs font-semibold text-slate-900 focus:outline-none"
                >
                  {meta.demo_actors.map((p) => (
                    <option key={p.actor_id} value={p.actor_id}>
                      {p.display_name} · {p.title}
                    </option>
                  ))}
                </select>
                <ChevronDown className="pointer-events-none absolute right-2 h-3.5 w-3.5 text-slate-400" />
              </div>
            ) : (
              <div className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700">
                <UserCheck className="h-3.5 w-3.5 text-emerald-600" />
                <span>{meta.actor.display_name}</span>
              </div>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1440px] px-6 py-6">{children}</main>
    </div>
  );
};
