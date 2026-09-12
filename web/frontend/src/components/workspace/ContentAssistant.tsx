import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import MessageBubble from '../MessageBubble';
import AgentSessionSelectors from '../AgentSessionSelectors';
import { uploadFiles } from '../../lib/api';
import type { AgentTurnInjection, UploadedFile } from '../../lib/api';
import type { ChatSession, StreamState } from '../../lib/store';
import {
  errorText, fetchAgentCapabilities, fetchAgentProfiles, fetchAgentRuntimes, fetchAgentSessionConfig, saveAgentSessionConfig,
} from '../../lib/ripple';
import type { AgentCapabilityCatalog, AgentProfileState, AgentRuntimeCatalog, AgentSessionConfig } from '../../lib/ripple';
import { IconArrowUp, IconFile, IconNewChat, IconPlus, IconStop } from '../icons';

interface Props {
  contentId?: string;
  contentTitle?: string;
  sessions: ChatSession[];
  session: ChatSession | null;
  stream?: StreamState;
  onSend: (text: string, attachments?: UploadedFile[], injection?: AgentTurnInjection) => void;
  onStop: () => void;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onResend: (userIndex: number, text: string, attachments?: UploadedFile[], agentText?: string, injection?: AgentTurnInjection) => void;
}

type MenuTab = 'skills' | 'mcp' | 'plugins' | 'assets';
const QUICK = [
  '把这篇内容写得更自然、更像真人表达',
  '帮我检查标题、正文和话题，并直接完善当前主稿',
  '根据当前内容生成合适的配图，并加入素材',
];

export default function ContentAssistant({
  contentId, contentTitle, sessions, session, stream, onSend, onStop, onSelectSession, onNewSession, onResend,
}: Props) {
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuTab, setMenuTab] = useState<MenuTab>('skills');
  const [dragOver, setDragOver] = useState(false);
  const [catalog, setCatalog] = useState<AgentCapabilityCatalog | null>(null);
  const [config, setConfig] = useState<AgentSessionConfig | null>(null);
  const [runtimes, setRuntimes] = useState<AgentRuntimeCatalog | null>(null);
  const [profiles, setProfiles] = useState<AgentProfileState | null>(null);
  const [configError, setConfigError] = useState('');
  const [turnSkills, setTurnSkills] = useState<string[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const isStreaming = !!stream;

  useEffect(() => {
    if (!session?.id) { setConfig(null); return; }
    let ignore = false;
    Promise.allSettled([fetchAgentCapabilities(), fetchAgentSessionConfig(session.id), fetchAgentRuntimes(), fetchAgentProfiles()]).then(([cap, cfg, runtimeState, profileState]) => {
      if (ignore) return;
      if (cap.status === 'fulfilled') setCatalog(cap.value);
      if (cfg.status === 'fulfilled') setConfig(cfg.value);
      if (runtimeState.status === 'fulfilled') setRuntimes(runtimeState.value);
      if (profileState.status === 'fulfilled') setProfiles(profileState.value);
      const failed = [cap, cfg, runtimeState, profileState].find((row) => row.status === 'rejected');
      setConfigError(failed?.status === 'rejected' ? errorText(failed.reason) : '');
      setTurnSkills([]);
    });
    return () => { ignore = true; };
  }, [session?.id]);

  const updateConfig = useCallback(async (patch: Partial<AgentSessionConfig>) => {
    if (!session) return;
    try { setConfig(await saveAgentSessionConfig(session.id, patch)); setConfigError(''); }
    catch (e) { setConfigError(errorText(e)); }
  }, [session]);
  useEffect(() => {
    if (!session || !config || !runtimes || isStreaming || session.messages.length > 0 || config.runtime_locked) return;
    const target = runtimes.items.find((row) => row.runtime === runtimes.default_runtime);
    if (!target || !(target.selectable ?? target.healthy) || config.runtime_id === target.runtime) return;
    void updateConfig({ runtime_id: target.runtime, effort: 'auto' });
  }, [session, config, runtimes, isStreaming, updateConfig]);
  const availableSessions = useMemo(() => {
    const active = sessions.filter((item) => item.messages.length > 0 || item.id === session?.id);
    return [...active].sort((a, b) => {
      const aMatch = a.contentContext?.id === contentId ? 1 : 0;
      const bMatch = b.contentContext?.id === contentId ? 1 : 0;
      return bMatch - aMatch || b.created - a.created;
    });
  }, [contentId, session?.id, sessions]);
  const messages = useMemo(() => {
    if (!session) return [];
    const list = [...session.messages];
    if (stream) list.push({ role: 'assistant' as const, content: stream.content || '', artifacts: stream.artifacts });
    return list;
  }, [session, stream]);
  const slashQuery = input.startsWith('/') ? input.slice(1).trim().toLowerCase() : '';
  const slashOpen = input.startsWith('/') && !input.includes('\n');
  const skillMatches = (catalog?.skills || []).filter((skill) => skill.ready && (!slashQuery || skill.name.toLowerCase().includes(slashQuery) || skill.description.toLowerCase().includes(slashQuery))).slice(0, 12);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages.length, stream?.content, stream?.activity]);
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 150) + 'px';
  }, [input]);

  const doUpload = async (files: FileList | File[]) => {
    if (!session) return;
    const values = Array.from(files); if (!values.length) return;
    setUploading(true); setUploadError('');
    try { const uploaded = await uploadFiles(values, session.id); setAttachments((current) => [...current, ...uploaded]); }
    catch (error) { setUploadError(errorText(error)); }
    finally { setUploading(false); }
  };
  const send = () => {
    const text = input.trim();
    if ((!text && !attachments.length) || isStreaming || uploading || !session) return;
    onSend(text, attachments, { skills: turnSkills });
    setInput(''); setAttachments([]); setUploadError(''); setTurnSkills([]); setMenuOpen(false);
  };
  const selectSkill = (id: string) => {
    if (!turnSkills.includes(id) && !config?.pinned_skills.includes(id)) setTurnSkills((rows) => [...rows, id]);
    setInput(''); textareaRef.current?.focus();
  };
  const pinSkill = (id: string, pin: boolean) => {
    if (!config) return;
    const next = pin ? [...new Set([...config.pinned_skills, id])] : config.pinned_skills.filter((x) => x !== id);
    void updateConfig({ pinned_skills: next });
  };

  return <aside className="r2-content-ai" aria-label="AI 协作">
    <header className="r2-content-ai-head r2-agent-console-head">
      <div className="r2-ai-title"><strong>AI 协作</strong><span>{contentId ? `当前主稿 · ${contentTitle || '未命名内容'}` : '从一句话开始，AI 会创建内容并写入工作台'}</span></div>
      <div className="r2-agent-console-actions">
        <AgentSessionSelectors config={config} catalog={catalog} runtimes={runtimes} profiles={profiles} isStreaming={isStreaming} messageCount={session?.messages.length || 0} onChange={updateConfig} agentMode="label" />
        <div className="r2-content-ai-head-actions"><button className="r2-icon-button" title="对话历史" aria-label="对话历史" onClick={() => setHistoryOpen((value) => !value)}>⋯</button><button className="r2-icon-button" title="新建协作会话" aria-label="新建协作会话" onClick={onNewSession}><IconNewChat size={15} /></button></div>
      </div>
    </header>
    {configError && <div className="r2-ai-upload-error">{configError}</div>}

    {historyOpen && <div className="r2-ai-history"><div className="r2-ai-history-title"><strong>对话历史</strong><span>{availableSessions.length}</span></div>{availableSessions.length ? availableSessions.slice(0, 40).map((item) => <button key={item.id} className={item.id === session?.id ? 'active' : ''} onClick={() => { onSelectSession(item.id); setHistoryOpen(false); }}><strong>{item.title}</strong><span>{item.archived ? '已归档' : item.contentContext?.id === contentId && contentId ? '当前内容' : item.contentContext ? '其他内容' : '历史对话'}</span></button>) : <p>还没有历史对话。</p>}</div>}

    <div className="r2-content-ai-thread">
      {!messages.length && <div className="r2-ai-empty"><img src="./static/ripple-mark.svg" alt="" /><h3>{contentId ? '直接告诉 AI 你想怎么改' : '想创作什么？'}</h3><p>{contentId ? 'AI 会读取当前主稿；输入 / 或点击＋都可以选择业务 Skill。底层工具由 Skill 与 Ripple Broker 自动决定。' : '描述主题和要求；输入 / 或点击＋选择技能，也可以注入 MCP、插件或素材。'}</p><div>{QUICK.map((text) => <button key={text} disabled={!session || isStreaming} onClick={() => onSend(text)}>{text}</button>)}</div></div>}
      {messages.map((message, index) => {
        const live = !!stream && index === messages.length - 1 && message.role === 'assistant'; const final = !live && !!session && index < session.messages.length; let actions;
        if (final) { const copy = () => navigator.clipboard?.writeText(message.content); const last = index === session.messages.length - 1;
          if (message.role === 'user') actions = { onCopy: copy, onRetry: last ? () => onResend(index, message.content, message.attachments, message.agentContent, { skills: message.turnSkills }) : undefined, canModify: !isStreaming };
          else { const previous = index > 0 && session.messages[index - 1]?.role === 'user' ? session.messages[index - 1] : null; actions = { onCopy: copy, onRetry: last && previous ? () => onResend(index - 1, previous.content, previous.attachments, previous.agentContent, { skills: previous.turnSkills }) : undefined, canModify: !isStreaming }; }
        }
        return <MessageBubble key={`${index}-${message.role}`} message={message} isStreaming={live} thinking={live ? stream?.thinking : ''} activity={live ? stream?.activity : ''} actions={actions} />;
      })}
      <div ref={endRef} />
    </div>

    <div className="r2-agent-chips">
      {config?.pinned_skills.map((id) => <span key={`ps-${id}`}>技能 · {id}<button title="取消固定" onClick={() => pinSkill(id, false)}>×</button></span>)}
      {turnSkills.map((id) => <span key={`ts-${id}`}>仅本次 · {id}<button title="固定到会话" onClick={() => pinSkill(id, true)}>📌</button><button onClick={() => setTurnSkills((rows) => rows.filter((x) => x !== id))}>×</button></span>)}
      {config?.enabled_mcp_tools.map((id) => <span key={`mcp-${id}`}>MCP · {catalog?.mcp_tools.find((x) => x.id === id)?.label || id}<button onClick={() => void updateConfig({ enabled_mcp_tools: config.enabled_mcp_tools.filter((x) => x !== id) })}>×</button></span>)}
      {config?.enabled_plugins.map((id) => <span key={`pl-${id}`}>插件 · {catalog?.plugins.find((x) => x.id === id)?.name || id}<button onClick={() => void updateConfig({ enabled_plugins: config.enabled_plugins.filter((x) => x !== id) })}>×</button></span>)}
    </div>

    <div className={`r2-content-ai-composer ${dragOver ? 'drag' : ''}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={() => setDragOver(false)} onDrop={(e) => { e.preventDefault(); setDragOver(false); if (e.dataTransfer.files?.length) void doUpload(e.dataTransfer.files); }}>
      {slashOpen && <div className="r2-slash-palette"><div className="r2-capability-menu-title">技能 · 业务能力由 Ripple 自动绑定所需底层工具</div>{skillMatches.map((skill) => <button key={skill.id} onClick={() => selectSkill(skill.id)}><strong>/{skill.name}</strong><span>{skill.description}</span></button>)}{!skillMatches.length && <p>没有匹配的技能</p>}</div>}
      {menuOpen && <div className="r2-capability-menu"><div className="r2-capability-tabs"><button className={menuTab === 'skills' ? 'active' : ''} onClick={() => setMenuTab('skills')}>技能</button><button className={menuTab === 'mcp' ? 'active' : ''} onClick={() => setMenuTab('mcp')}>MCP</button><button className={menuTab === 'plugins' ? 'active' : ''} onClick={() => setMenuTab('plugins')}>插件</button><button className={menuTab === 'assets' ? 'active' : ''} onClick={() => setMenuTab('assets')}>素材</button></div>
        {menuTab === 'skills' && <div className="r2-capability-list">{(catalog?.skills || []).filter((skill) => skill.ready).map((skill) => <div key={skill.id}><div><strong>{skill.name}</strong><span>{skill.description}</span></div><button disabled={turnSkills.includes(skill.id) || config?.pinned_skills.includes(skill.id)} onClick={() => selectSkill(skill.id)}>仅本次</button><button disabled={!config} onClick={() => pinSkill(skill.id, !config?.pinned_skills.includes(skill.id))}>{config?.pinned_skills.includes(skill.id) ? '已启用 ✓' : '固定'}</button></div>)}</div>}
        {menuTab === 'mcp' && <div className="r2-capability-list">{(catalog?.mcp_tools || []).length ? catalog!.mcp_tools.map((tool) => <div key={tool.id} className={!tool.ready ? 'disabled' : ''}><div><strong>{tool.label}</strong><span>{tool.ready ? tool.description : 'MCP 已注册，但当前未连接；不能注入 Agent。'}</span></div><button disabled={!tool.ready || !config} onClick={() => config && void updateConfig({ enabled_mcp_tools: [...new Set([...config.enabled_mcp_tools, tool.id])] })}>{config?.enabled_mcp_tools.includes(tool.id) ? '已固定' : '固定'}</button></div>) : <p>还没有已注册的 MCP 能力。可在设置 → Agent 扩展配置。</p>}</div>}
        {menuTab === 'plugins' && <div className="r2-capability-list">{(catalog?.plugins || []).map((plugin) => <div key={plugin.id}><div><strong>{plugin.name}</strong><span>{plugin.description}</span></div><button disabled={!config} onClick={() => config && void updateConfig({ enabled_plugins: config.enabled_plugins.includes(plugin.id) ? config.enabled_plugins.filter((x) => x !== plugin.id) : [...config.enabled_plugins, plugin.id] })}>{config?.enabled_plugins.includes(plugin.id) ? '已启用 ✓' : '启用'}</button></div>)}</div>}
        {menuTab === 'assets' && <div className="r2-capability-assets"><p>素材只绑定本轮消息，不会让 Agent 扫描其他文件。</p><button onClick={() => fileInputRef.current?.click()}>选择图片 / 文档 / 视频</button></div>}
      </div>}
      {attachments.length > 0 && <div className="composer-attachments">{attachments.map((item) => <span className="attach-chip" key={item.path}><IconFile size={12} /><span className="attach-name">{item.name}</span><button className="attach-x" onClick={() => setAttachments((current) => current.filter((row) => row.path !== item.path))}>×</button></span>)}</div>}
      {uploadError && <div className="r2-ai-upload-error">{uploadError}</div>}
      <textarea ref={textareaRef} value={input} disabled={!session || isStreaming} placeholder={contentId ? '告诉 AI 怎么修改当前内容… 输入 / 唤出技能' : '描述你想创作的内容… 输入 / 唤出技能'} rows={1} onPaste={(e) => { if (e.clipboardData.files?.length) { e.preventDefault(); void doUpload(e.clipboardData.files); } }} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !slashOpen) { e.preventDefault(); send(); } if (e.key === 'Escape') { setMenuOpen(false); if (slashOpen) setInput(''); } }} />
      <div className="r2-content-ai-composer-bar"><input ref={fileInputRef} type="file" multiple hidden onChange={(e) => { if (e.target.files) void doUpload(e.target.files); e.target.value = ''; }} /><button className="r2-ai-attach" disabled={!session || isStreaming || uploading} onClick={() => setMenuOpen((value) => !value)}><IconPlus size={14} />能力</button>{isStreaming ? <button className="r2-ai-send" onClick={onStop} title="停止"><IconStop size={14} /></button> : <button className="r2-ai-send" disabled={!session || uploading || slashOpen || (!input.trim() && !attachments.length)} onClick={send} title="发送"><IconArrowUp size={16} /></button>}</div>
    </div>
  </aside>;
}
