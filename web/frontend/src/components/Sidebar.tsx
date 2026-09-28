import { useEffect, useRef, useState } from 'react';
import type { ContentProfileContext, PersonaItem } from '../lib/api';
import type { ComponentType } from 'react';
import type { ChatSession } from '../lib/store';
import { platformDisplayName } from '../lib/platforms';
import { fetchInteractionSources, fetchInteractions } from '../lib/ripple';
import { interactionStats } from '../lib/replyEditing';
import SessionActions from './SessionActions';
import type { SessionAction } from './SessionActions';
import { getTheme, setTheme, type ThemeMode } from '../lib/theme';
import '../styles/account-profile.css';
import {
  IconSkills, IconOutputs, IconChevron, IconLayout,
  IconDashboard, IconPublish, IconCompass, IconFile, IconSun, IconMoon, IconChat,
} from './icons';

export type Page = 'dashboard' | 'chat' | 'history' | 'trends' | 'campaigns' | 'ideas' | 'calendar' | 'publish' | 'interactions' | 'breakdown' | 'skills' | 'outputs' | 'accounts' | 'profile' | 'channels' | 'analytics' | 'contents' | 'integrations' | 'planning';

interface SidebarProps {
  currentPage: Page;
  onPageChange: (page: Page) => void;
  personas: PersonaItem[];
  selectedPersona: string;
  profileContext: ContentProfileContext | null;
  selectedTarget: string;
  onScopeChange: (scope: string) => void;
  onEditProfile: (name: string) => void;
  onManageAccounts: () => void;
  agentStatus: string;
  recommendationAiReady: boolean;
  sessions: ChatSession[];
  activeSessionId: string | null;
  runningIds: Set<string>;
  onNewConversation: () => void;
  onOpenConversation: (id: string) => void;
  onOpenHistory: () => void;
  onSessionAction: (id: string, action: SessionAction, title?: string) => Promise<string | null>;
}

const TOPIC_PAGES: Page[] = ['trends', 'campaigns', 'ideas', 'planning', 'breakdown'];
const PUBLISH_PAGES: Page[] = ['publish', 'calendar', 'analytics'];
const RESOURCE_NAV: { page: Page; Icon: ComponentType<{ size?: number }>; label: string }[] = [
  { page: 'outputs', Icon: IconOutputs, label: '素材' },
];
const TOOL_NAV: { page: Page; Icon: ComponentType<{ size?: number }>; label: string }[] = [
  { page: 'skills', Icon: IconSkills, label: '技能' },
  { page: 'integrations', Icon: IconLayout, label: '设置' },
];

const WIDTH_KEY = 'ripple_sidebar_width_v1';
const COLLAPSED_KEY = 'ripple_sidebar_collapsed_v1';
const DEFAULT_WIDTH = 272;
const MIN_WIDTH = 165;
const MAX_WIDTH = 350;
const COLLAPSED_WIDTH = 58;
const MOBILE_BREAKPOINT = 768;
const TOPIC_TAB_KEY = 'ripple_last_topic_tab_v1';
const PUBLISH_TAB_KEY = 'ripple_last_publish_tab_v1';
function rememberedPage(key: string, allowed: Page[], fallback: Page): Page {
  try { const value = localStorage.getItem(key) as Page | null; return value && allowed.includes(value) ? value : fallback; }
  catch { return fallback; }
}

function storedWidth(): number {
  try {
    const raw = localStorage.getItem(WIDTH_KEY);
    if (raw === null || raw.trim() === '') return DEFAULT_WIDTH;
    const value = Number(raw);
    // 旧版默认宽度为 212 px；其他手动调整过的宽度保持原值。
    return Number.isFinite(value) ? Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, value === 212 ? DEFAULT_WIDTH : value)) : DEFAULT_WIDTH;
  } catch { return DEFAULT_WIDTH; }
}
function storedCollapsed(): boolean {
  try { return localStorage.getItem(COLLAPSED_KEY) === '1'; } catch { return false; }
}
function widthCap(viewport: number): number {
  return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.floor(viewport * .4)));
}

export default function Sidebar({
  currentPage, onPageChange, personas, selectedPersona,
  profileContext, selectedTarget, onScopeChange, onEditProfile, onManageAccounts,
  agentStatus, recommendationAiReady, sessions, activeSessionId, runningIds,
  onNewConversation, onOpenConversation, onOpenHistory, onSessionAction,
}: SidebarProps) {
  const [theme, setThemeState] = useState<ThemeMode>(() => getTheme());
  const [width, setWidth] = useState(storedWidth);
  const [collapsed, setCollapsed] = useState(storedCollapsed);
  const [viewport, setViewport] = useState(() => window.innerWidth);
  const [resizing, setResizing] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [pendingComments, setPendingComments] = useState<number | null>(null);
  const pointerId = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const [sources, tasks] = await Promise.all([fetchInteractionSources('', 24), fetchInteractions({ limit: 200 })]);
        if (!cancelled) setPendingComments(interactionStats(sources.items.filter(row => row.kind === 'remote'), tasks.items.filter(row => row.delivery === 'remote' && row.source_kind !== 'import')).pending);
      } catch { if (!cancelled) setPendingComments(null); }
    };
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 60000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [currentPage]);

  useEffect(() => {
    try {
      if (TOPIC_PAGES.includes(currentPage)) localStorage.setItem(TOPIC_TAB_KEY, currentPage);
      if (PUBLISH_PAGES.includes(currentPage)) localStorage.setItem(PUBLISH_TAB_KEY, currentPage);
    } catch { /* ignore */ }
  }, [currentPage]);
  useEffect(() => {
    const onResize = () => setViewport(window.innerWidth);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const mobileRail = viewport <= MOBILE_BREAKPOINT;
  const effectiveWidth = mobileRail ? (mobileOpen ? Math.min(290, Math.floor(viewport * .86)) : 0) : collapsed ? COLLAPSED_WIDTH : Math.min(width, widthCap(viewport));
  useEffect(() => {
    document.documentElement.style.setProperty('--sidebar-width', `${effectiveWidth}px`);
    return () => { document.documentElement.style.removeProperty('--sidebar-width'); };
  }, [effectiveWidth]);
  useEffect(() => { try { localStorage.setItem(WIDTH_KEY, String(width)); } catch { /* ignore */ } }, [width]);
  useEffect(() => { try { localStorage.setItem(COLLAPSED_KEY, collapsed ? '1' : '0'); } catch { /* ignore */ } }, [collapsed]);

  useEffect(() => {
    if (!resizing) return;
    const previous = document.body.style.userSelect;
    document.body.style.userSelect = 'none';
    document.body.classList.add('sidebar-resizing');
    const move = (event: PointerEvent) => {
      if (pointerId.current !== null && event.pointerId !== pointerId.current) return;
      setWidth(Math.max(MIN_WIDTH, Math.min(widthCap(window.innerWidth), event.clientX)));
    };
    const finish = (event: PointerEvent) => {
      if (pointerId.current !== null && event.pointerId !== pointerId.current) return;
      pointerId.current = null; setResizing(false);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
    return () => {
      document.body.style.userSelect = previous;
      document.body.classList.remove('sidebar-resizing');
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
    };
  }, [resizing]);

  const toggleTheme = () => {
    const next: ThemeMode = theme === 'light' ? 'dark' : 'light';
    setTheme(next); setThemeState(next);
  };
  const resetWidth = () => { setWidth(DEFAULT_WIDTH); setCollapsed(false); };
  const adjustWidth = (delta: number) => { setCollapsed(false); setWidth((current) => Math.max(MIN_WIDTH, Math.min(widthCap(window.innerWidth), current + delta))); };
  const topicActive = TOPIC_PAGES.includes(currentPage);
  const publishActive = PUBLISH_PAGES.includes(currentPage);
  const compact = collapsed || mobileRail;
  const visibleSessions = [...sessions].filter((item) => !item.archived).sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)) || (b.updatedAt || b.created) - (a.updatedAt || a.created));
  const targetRows = [...(profileContext?.accounts || []), ...(profileContext?.blogs || [])];
  const bindingByTarget = new Map((profileContext?.bindings || []).map((item) => [`${item.target_kind}:${item.account_id}`, item]));
  const currentProfile = (profileContext?.profiles || []).find((item) => item.legacy_name === selectedPersona);
  const scopeValue = selectedTarget ? `target:${selectedTarget}` : currentProfile ? `profile:${currentProfile.id}` : selectedPersona ? `legacy:${selectedPersona}` : 'generic';

  return (
    <>
    {mobileRail && <button className="focus-mobile-menu-trigger" aria-label="打开侧边栏" onClick={() => setMobileOpen(true)}>☰</button>}
    {mobileRail && mobileOpen && <button className="focus-mobile-shade" aria-label="关闭侧边栏" onClick={() => setMobileOpen(false)} />}
    <div className={`sidebar ${compact && !mobileOpen ? 'collapsed' : ''} ${mobileOpen ? 'focus-mobile-open' : ''} ${resizing ? 'resizing' : ''}`} style={{ width: effectiveWidth, minWidth: effectiveWidth }}>
      <div className="sidebar-header">
        <div className="sidebar-logo-row">
          <div className="sidebar-logo">
            <img className="sidebar-logo-icon" src="./static/ripple-mark.svg" alt="" />
            <h1>Ripple</h1>
          </div>
          {!mobileRail && <button className="sidebar-collapse-btn" type="button" aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'} title={collapsed ? '展开侧边栏' : '折叠侧边栏'} onClick={() => setCollapsed((value) => !value)}><IconChevron size={15} /></button>}
          {mobileRail && <button className="sidebar-collapse-btn" aria-label="关闭侧边栏" onClick={() => setMobileOpen(false)}>×</button>}
        </div>
        <select className="persona-select" aria-label="当前工作范围" value={scopeValue} onChange={(e) => onScopeChange(e.target.value)} title="当前工作范围：账号画像或具体平台账号">
          <option value="generic">通用 / 未绑定规划</option>
          {!profileContext && personas.map((profile) => <option key={profile.name} value={`legacy:${profile.name}`}>{profile.name}</option>)}
          {(profileContext?.profiles || []).map((profile) => {
            const bound = (profileContext?.bindings || []).filter((item) => item.profile_id === profile.id);
            return <optgroup key={profile.id} label={profile.display_name}>
              <option value={`profile:${profile.id}`}>{profile.display_name} · 全部关联账号</option>
              {bound.map((binding) => {
                const target = targetRows.find((item) => item.target_kind === binding.target_kind && item.id === binding.account_id);
                return target ? <option key={`${binding.target_kind}:${binding.account_id}`} value={`target:${binding.target_kind}:${binding.account_id}`}>{platformDisplayName(target.platform)} · {target.identity?.name || target.label}</option> : null;
              })}
            </optgroup>;
          })}
          {selectedPersona && !currentProfile && <option value={`legacy:${selectedPersona}`}>{selectedPersona}</option>}
          {targetRows.filter((target) => !bindingByTarget.has(`${target.target_kind}:${target.id}`)).length > 0 && <optgroup label="待关联画像">
            {targetRows.filter((target) => !bindingByTarget.has(`${target.target_kind}:${target.id}`)).map((target) => <option key={`${target.target_kind}:${target.id}`} value={`target:${target.target_kind}:${target.id}`}>{platformDisplayName(target.platform)} · {target.identity?.name || target.label} · 待关联</option>)}
          </optgroup>}
          <option value="__new__">+ 新建画像…</option>
        </select>
        {!compact && <div className="sidebar-scope-actions">
          <button type="button" disabled={!selectedPersona} onClick={() => selectedPersona && onEditProfile(selectedPersona)}>编辑画像</button>
          <button type="button" onClick={onManageAccounts}>管理账号</button>
        </div>}
      </div>

      <nav className="sidebar-nav" aria-label="主导航">
        <div className="nav-section-label">工作流</div>
        <button aria-label="首页" title="首页" className={`nav-item ${currentPage === 'dashboard' ? 'active' : ''}`} onClick={() => onPageChange('dashboard')}><span className="nav-icon"><IconDashboard size={18} /></span><span className="nav-item-label">首页</span></button>

        <button aria-label="选题" title="选题" className={`nav-item ${topicActive ? 'active' : ''}`} onClick={() => onPageChange(rememberedPage(TOPIC_TAB_KEY, TOPIC_PAGES, 'trends'))}><span className="nav-icon"><IconCompass size={18} /></span><span className="nav-item-label">选题</span></button>

        <button aria-label="内容" title="内容" className={`nav-item ${currentPage === 'contents' ? 'active' : ''}`} onClick={() => onPageChange('contents')}><span className="nav-icon"><IconFile size={18} /></span><span className="nav-item-label">内容</span></button>

        <button aria-label="发布" title="发布" className={`nav-item ${publishActive ? 'active' : ''}`} onClick={() => onPageChange(rememberedPage(PUBLISH_TAB_KEY, PUBLISH_PAGES, 'publish'))}><span className="nav-icon"><IconPublish size={18} /></span><span className="nav-item-label">发布</span></button>
        <button aria-label="互动" title={pendingComments === null ? '互动' : `互动 · 全部账号已同步评论中 ${pendingComments} 条待回复`} className={`nav-item ${currentPage === 'interactions' ? 'active' : ''}`} onClick={() => onPageChange('interactions')}><span className="nav-icon"><IconChat size={18} /></span><span className="nav-item-label">互动{pendingComments !== null && pendingComments > 0 && <small style={{ marginLeft: 8 }}>{pendingComments}</small>}</span></button>

        <div className="nav-section-label">资源</div>
        {RESOURCE_NAV.map(({ page, Icon, label }) => <button key={page} aria-label={label} title={label} className={`nav-item ${currentPage === page || (page === 'channels' && currentPage === 'accounts') ? 'active' : ''}`} onClick={() => onPageChange(page)}><span className="nav-icon"><Icon size={18} /></span><span className="nav-item-label">{label}</span></button>)}
        <div className="nav-section-label">工具</div>
        {TOOL_NAV.map(({ page, Icon, label }) => <button key={page} aria-label={label} title={label} className={`nav-item ${currentPage === page ? 'active' : ''}`} onClick={() => onPageChange(page)}><span className="nav-icon"><Icon size={18} /></span><span className="nav-item-label">{label}</span></button>)}
      </nav>

      <section className="focus-sidebar-sessions" aria-label="会话列表">
        <button className="focus-new-conversation" onClick={() => { onNewConversation(); setMobileOpen(false); }}>＋ 新会话</button>
        <div className="focus-session-scroll">{visibleSessions.map((item) => <div className="focus-session-entry" key={item.id}>
          <button className={`focus-session-select ${item.id === activeSessionId && (currentPage === 'chat' || currentPage === 'contents') ? 'active' : ''}`} onClick={() => { onOpenConversation(item.id); setMobileOpen(false); }} title={item.title}>
            <strong>{item.pinned && <span aria-label="已置顶">⌃ </span>}{item.title}</strong>
            <span>{item.contentContext ? `主稿 · ${item.contentContext.title}` : item.workScope ? `${platformDisplayName(item.workScope.platform)} · ${item.workScope.accountLabel}` : item.persona ? `画像 · ${item.persona}` : '通用会话'} · {new Date(item.updatedAt || item.created).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>
          </button>
          <SessionActions session={item} running={runningIds.has(item.id)} onAction={onSessionAction} />
        </div>)}</div>
        <button className="focus-all-sessions" onClick={() => { onOpenHistory(); setMobileOpen(false); }}>查看历史会话 <span>{sessions.length} ↗</span></button>
      </section>

      <div className="sidebar-status">
        <span className={`status-dot ${agentStatus === 'connected' || recommendationAiReady ? '' : 'offline'}`} />
        <span className="sidebar-status-label">{agentStatus === 'connected' ? 'Ripple Agent 已连接' : recommendationAiReady ? 'AI 推荐已就绪 / Agent 离线' : agentStatus === 'disconnected' ? 'Agent 未配置 / 离线' : 'Agent 连接中…'}</span>
        <button className="theme-toggle" type="button" onClick={toggleTheme} aria-label={theme === 'light' ? '切换到深色模式' : '切换到浅色模式'} title={theme === 'light' ? '切换到深色模式' : '切换到浅色模式'}>{theme === 'light' ? <IconMoon size={15} /> : <IconSun size={15} />}</button>
        <span className="sidebar-version">0.2.7</span>
      </div>

      {!mobileRail && <div className="sidebar-resize-handle" role="separator" aria-label="调整侧边栏宽度" aria-orientation="vertical" aria-valuemin={MIN_WIDTH} aria-valuemax={widthCap(viewport)} aria-valuenow={collapsed ? COLLAPSED_WIDTH : effectiveWidth} tabIndex={0}
        onPointerDown={(event) => { if (collapsed) return; pointerId.current = event.pointerId; event.currentTarget.setPointerCapture?.(event.pointerId); setResizing(true); }}
        onDoubleClick={resetWidth}
        onKeyDown={(event) => { if (event.key === 'ArrowLeft') { event.preventDefault(); adjustWidth(-10); } else if (event.key === 'ArrowRight') { event.preventDefault(); adjustWidth(10); } else if (event.key === 'Home') { event.preventDefault(); resetWidth(); } }} />}
    </div>
    </>
  );
}
