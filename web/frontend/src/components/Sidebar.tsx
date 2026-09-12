import { useEffect, useRef, useState } from 'react';
import type { PersonaItem } from '../lib/api';
import type { ComponentType } from 'react';
import { getTheme, setTheme, type ThemeMode } from '../lib/theme';
import {
  IconSkills, IconOutputs, IconAccounts, IconChevron, IconLayout,
  IconDashboard, IconPublish, IconCompass, IconFile, IconSun, IconMoon,
} from './icons';

export type Page = 'dashboard' | 'chat' | 'trends' | 'ideas' | 'calendar' | 'publish' | 'interactions' | 'breakdown' | 'skills' | 'outputs' | 'accounts' | 'profile' | 'channels' | 'analytics' | 'contents' | 'integrations' | 'planning';

interface SidebarProps {
  currentPage: Page;
  onPageChange: (page: Page) => void;
  personas: PersonaItem[];
  selectedPersona: string;
  onPersonaChange: (persona: string) => void;
  onNewProfile: () => void;
  agentStatus: string;
  recommendationAiReady: boolean;
}

const TOPIC_PAGES: Page[] = ['trends', 'ideas', 'planning', 'breakdown'];
const PUBLISH_PAGES: Page[] = ['publish', 'interactions', 'calendar', 'analytics'];
const RESOURCE_NAV: { page: Page; Icon: ComponentType<{ size?: number }>; label: string }[] = [
  { page: 'outputs', Icon: IconOutputs, label: '素材与成品' },
  { page: 'channels', Icon: IconAccounts, label: '账号与平台' },
];
const TOOL_NAV: { page: Page; Icon: ComponentType<{ size?: number }>; label: string }[] = [
  { page: 'skills', Icon: IconSkills, label: '技能库' },
  { page: 'integrations', Icon: IconLayout, label: '设置' },
];

const WIDTH_KEY = 'ripple_sidebar_width_v1';
const COLLAPSED_KEY = 'ripple_sidebar_collapsed_v1';
const DEFAULT_WIDTH = 212;
const MIN_WIDTH = 165;
const MAX_WIDTH = 350;
const COLLAPSED_WIDTH = 58;
const MOBILE_BREAKPOINT = 700;
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
    return Number.isFinite(value) ? Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, value)) : DEFAULT_WIDTH;
  } catch { return DEFAULT_WIDTH; }
}
function storedCollapsed(): boolean {
  try { return localStorage.getItem(COLLAPSED_KEY) === '1'; } catch { return false; }
}
function widthCap(viewport: number): number {
  return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.floor(viewport * .4)));
}

export default function Sidebar({
  currentPage, onPageChange, personas, selectedPersona, onPersonaChange, onNewProfile,
  agentStatus, recommendationAiReady,
}: SidebarProps) {
  const [theme, setThemeState] = useState<ThemeMode>(() => getTheme());
  const [width, setWidth] = useState(storedWidth);
  const [collapsed, setCollapsed] = useState(storedCollapsed);
  const [viewport, setViewport] = useState(() => window.innerWidth);
  const [resizing, setResizing] = useState(false);
  const pointerId = useRef<number | null>(null);

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
  const effectiveWidth = mobileRail || collapsed ? COLLAPSED_WIDTH : Math.min(width, widthCap(viewport));
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

  return (
    <div className={`sidebar ${compact ? 'collapsed' : ''} ${resizing ? 'resizing' : ''}`} style={{ width: effectiveWidth, minWidth: effectiveWidth }}>
      <div className="sidebar-header">
        <div className="sidebar-logo-row">
          <div className="sidebar-logo">
            <img className="sidebar-logo-icon" src="./static/ripple-mark.svg" alt="" />
            <h1>Ripple</h1>
          </div>
          {!mobileRail && <button className="sidebar-collapse-btn" type="button" aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'} title={collapsed ? '展开侧边栏' : '折叠侧边栏'} onClick={() => setCollapsed((value) => !value)}><IconChevron size={15} /></button>}
        </div>
        <select className="persona-select" value={selectedPersona} onChange={(e) => {
          if (e.target.value === '__new__') { onNewProfile(); return; }
          onPersonaChange(e.target.value);
        }} title="当前账号画像；也可在首页或选题库切换">
          <option value="">通用模式</option>
          {personas.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          <option value="__new__">+ 新建画像…</option>
        </select>
      </div>

      <nav className="sidebar-nav" aria-label="主导航">
        <div className="nav-section-label">工作流</div>
        <button aria-label="首页" title="首页" className={`nav-item ${currentPage === 'dashboard' ? 'active' : ''}`} onClick={() => onPageChange('dashboard')}><span className="nav-icon"><IconDashboard size={18} /></span><span className="nav-item-label">首页</span></button>

        <button aria-label="选题中心" title="选题中心" className={`nav-item ${topicActive ? 'active' : ''}`} onClick={() => onPageChange(rememberedPage(TOPIC_TAB_KEY, TOPIC_PAGES, 'trends'))}><span className="nav-icon"><IconCompass size={18} /></span><span className="nav-item-label">选题中心</span></button>

        <button aria-label="内容工作台" title="内容工作台" className={`nav-item ${currentPage === 'contents' ? 'active' : ''}`} onClick={() => onPageChange('contents')}><span className="nav-icon"><IconFile size={18} /></span><span className="nav-item-label">内容工作台</span></button>

        <button aria-label="发布管理" title="发布管理" className={`nav-item ${publishActive ? 'active' : ''}`} onClick={() => onPageChange(rememberedPage(PUBLISH_TAB_KEY, PUBLISH_PAGES, 'publish'))}><span className="nav-icon"><IconPublish size={18} /></span><span className="nav-item-label">发布管理</span></button>

        <div className="nav-section-label">资源</div>
        {RESOURCE_NAV.map(({ page, Icon, label }) => <button key={page} aria-label={label} title={label} className={`nav-item ${currentPage === page || (page === 'channels' && currentPage === 'accounts') ? 'active' : ''}`} onClick={() => onPageChange(page)}><span className="nav-icon"><Icon size={18} /></span><span className="nav-item-label">{label}</span></button>)}
        <div className="nav-section-label">工具</div>
        {TOOL_NAV.map(({ page, Icon, label }) => <button key={page} aria-label={label} title={label} className={`nav-item ${currentPage === page ? 'active' : ''}`} onClick={() => onPageChange(page)}><span className="nav-icon"><Icon size={18} /></span><span className="nav-item-label">{label}</span></button>)}
      </nav>

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
  );
}
