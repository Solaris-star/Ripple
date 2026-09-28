import { useMemo, useState } from 'react';
import type { ChatSession } from '../lib/store';
import SessionActions from './SessionActions';
import type { SessionAction } from './SessionActions';

interface Props {
  sessions: ChatSession[];
  runningIds: Set<string>;
  onOpen: (id: string, index?: number) => void;
  onNew: () => void;
  onAction: (id: string, action: SessionAction, title?: string) => Promise<string | null>;
}

function matchOf(session: ChatSession, query: string) {
  const needle = query.trim().toLocaleLowerCase();
  const messages = session.messages;
  const messageIndex = needle ? messages.findIndex((item) => item.content.toLocaleLowerCase().includes(needle)) : -1;
  const searchable = `${session.title} ${session.contentContext?.title || ''}`.toLocaleLowerCase();
  if (needle && messageIndex < 0 && !searchable.includes(needle)) return null;
  const text = (messageIndex >= 0 ? messages[messageIndex].content : messages.at(-1)?.content) || '还没有消息。';
  const at = needle ? text.toLocaleLowerCase().indexOf(needle) : -1;
  const start = Math.max(0, at - 25);
  const excerpt = text.slice(start, start + 130);
  const highlightedAt = at < 0 ? -1 : at - start;
  return { index: messageIndex, before: highlightedAt < 0 ? excerpt : excerpt.slice(0, highlightedAt), match: highlightedAt < 0 ? '' : excerpt.slice(highlightedAt, highlightedAt + needle.length), after: highlightedAt < 0 ? '' : excerpt.slice(highlightedAt + needle.length), prefix: start > 0 };
}

export default function ConversationHistory({ sessions, runningIds, onOpen, onNew, onAction }: Props) {
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState<'all' | 'recent' | 'archived'>('all');
  const [kind, setKind] = useState<'all' | 'general' | 'linked'>('all');
  const rows = useMemo(() => [...sessions].filter((item) => (scope === 'all' || Boolean(item.archived) === (scope === 'archived')) && (kind === 'all' || Boolean(item.contentContext) === (kind === 'linked')))
    .sort((a, b) => (b.updatedAt || b.created) - (a.updatedAt || a.created))
    .map((item) => ({ item, hit: matchOf(item, query) })).filter((row) => row.hit !== null), [sessions, scope, kind, query]);

  return <div className="focus-history-page page-scroll">
    <header className="focus-history-heading"><div><span className="focus-kicker">YOUR CONVERSATIONS</span><h1>会话历史</h1><p>查找完整记录，或整理不常用的会话。</p></div><button className="focus-primary" onClick={onNew}>＋ 新会话</button></header>
    <div className="focus-history-tabs" role="group" aria-label="会话范围">{([['all', '全部记录'], ['recent', '最近使用'], ['archived', '已归档']] as const).map(([value, label]) => <button key={value} className={scope === value ? 'active' : ''} onClick={() => setScope(value)}>{label}</button>)}</div>
    <div className="focus-history-toolbar"><label className="focus-history-search"><span>⌕</span><input aria-label="搜索会话历史" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索会话标题、主稿或消息内容" /></label><div role="group" aria-label="会话类型">{([['all', '全部类型'], ['general', '通用'], ['linked', '关联主稿']] as const).map(([value, label]) => <button key={value} className={kind === value ? 'active' : ''} onClick={() => setKind(value)}>{label}</button>)}</div></div>
    <div className="focus-history-results"><div className="focus-history-count"><span>按最近消息排序</span><span>{rows.length} 个会话</span></div>{rows.map(({ item, hit }) => <div className="focus-history-item" key={item.id}><button onClick={() => onOpen(item.id, hit!.index >= 0 ? hit!.index : undefined)}><strong>{item.title} {item.archived && <span className="focus-archived-tag">已归档</span>}</strong><span className="focus-history-excerpt">{hit!.prefix ? '…' : ''}{hit!.before}{hit!.match && <mark>{hit!.match}</mark>}{hit!.after}</span></button><span className="focus-history-binding">{item.contentContext ? `主稿 · ${item.contentContext.title}` : '通用会话'}</span><SessionActions session={item} running={runningIds.has(item.id)} onAction={onAction} /></div>)}{!rows.length && <p className="focus-history-empty">没有找到相关会话。</p>}</div>
  </div>;
}
