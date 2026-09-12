import { useMemo, useState } from 'react';
import type { ChatMessage } from '../lib/store';
import { renderMarkdown } from '../lib/sanitize';
import { IconCopy, IconCheck, IconRetry } from './icons';

export interface BubbleActions {
  onCopy: () => void;
  onRetry?: () => void;    // 仅最后一轮可用（append-only：不改写历史）
  canModify: boolean;      // 流式中禁用 retry
}

interface MessageBubbleProps {
  message: ChatMessage;
  isStreaming?: boolean;
  thinking?: string;
  activity?: string;
  actions?: BubbleActions;
  onOpenContent?: (contentId: string) => void;
}

function ActionBar({ actions }: { actions: BubbleActions }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    actions.onCopy();
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  return (
    <div className="msg-actions">
      <button className="msg-action" onClick={copy} title="复制">
        {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}<span>{copied ? '已复制' : '复制'}</span>
      </button>
      {actions.onRetry && actions.canModify && (
        <button className="msg-action" onClick={actions.onRetry} title="重新生成"><IconRetry size={14} /><span>重试</span></button>
      )}
    </div>
  );
}

export default function MessageBubble({ message, isStreaming, thinking, activity, actions, onOpenContent }: MessageBubbleProps) {
  const html = useMemo(() => {
    if (message.role === 'user') return '';
    return renderMarkdown(message.content);
  }, [message.content, message.role]);

  // ---- 用户消息 ----
  if (message.role === 'user') {
    // Attachment-only turns are intentionally invisible; the structured refs
    // remain in session state for retry but never leak paths into the chat UI.
    if (!message.content.trim()) return null;
    return (
      <div className="message-row user">
        <div className="msg-col user">
          <div className="message-bubble user">{message.content}</div>
          {actions && <ActionBar actions={actions} />}
        </div>
      </div>
    );
  }

  // ---- 助手消息 ----
  // 思考 / 活动：流式时用实时值；结束后用消息里持久化的值 —— 一直保留，不隐藏
  const effThinking = isStreaming ? (thinking || '') : (message.thinking || '');
  const liveActivity = isStreaming ? (activity || '') : '';
  const doneSteps = !isStreaming ? (message.activity || '') : '';
  const artifacts = message.artifacts || [];

  const livePanel = (effThinking || liveActivity || doneSteps) ? (
    <div className="live-panel">
      {liveActivity && (
        <div className="live-activity"><span className="live-pulse" />{liveActivity}</div>
      )}
      {doneSteps && (
        <details className="thinking-block">
          <summary>🧠 执行过程（{doneSteps.split('\n').length} 步）</summary>
          <div className="thinking-text">{doneSteps}</div>
        </details>
      )}
      {effThinking && (
        <details className="thinking-block" open={isStreaming && !message.content}>
          <summary>💭 思考过程</summary>
          <div className="thinking-text">{effThinking}</div>
        </details>
      )}
    </div>
  ) : null;

  // 等待回复中（还没有正文、思考、活动）
  if (isStreaming && !message.content && !effThinking && !liveActivity && artifacts.length === 0) {
    return (
      <div className="message-row assistant">
        <div className="message-bubble assistant">
          <div className="typing-indicator">
            <span className="typing-dot" />
            <span className="typing-dot" />
            <span className="typing-dot" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="message-row assistant">
      <div className="msg-col assistant">
        <div className="message-bubble assistant">
          {livePanel}
          {message.content && <div dangerouslySetInnerHTML={{ __html: html }} />}
          {artifacts.length > 0 && <div className="chat-artifact-list">{artifacts.map((artifact) => <div className="chat-artifact-card" key={`${artifact.kind}-${artifact.id}-${artifact.version_id}`}><div className="chat-artifact-icon">文</div><div className="chat-artifact-main"><span>内容工作台草稿</span><strong>{artifact.title || '未命名内容'}</strong><small>草稿 · 已保存到 Ripple 内容库</small></div>{onOpenContent && <button onClick={() => onOpenContent(artifact.id)}>打开内容工作台</button>}</div>)}</div>}
          {isStreaming && <span className="streaming-cursor" />}
        </div>
        {actions && !isStreaming && <ActionBar actions={actions} />}
      </div>
    </div>
  );
}
