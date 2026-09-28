import { useEffect, useRef, useState } from 'react';
import type { Idea, IdeaBrief, IdeaBriefData } from '../lib/api';
import { PlatformIcon } from './PlatformBrand';
import { platformDisplayName } from '../lib/platforms';

interface Props {
  idea: Idea;
  brief: IdeaBrief;
  busy?: boolean;
  onSave: (data: IdeaBriefData, locked: string[]) => Promise<void>;
  onConfirm: (data: IdeaBriefData, locked: string[], startAfterConfirm?: boolean) => Promise<void>;
  onDevelop: (instruction: string) => Promise<void>;
  onStart: () => Promise<void>;
  onClose: () => void;
}

const clone = (value: IdeaBriefData): IdeaBriefData => JSON.parse(JSON.stringify(value)) as IdeaBriefData;

const TEXT_FIELDS: { key: keyof IdeaBriefData; label: string; placeholder: string }[] = [
  { key: 'audience', label: '目标受众', placeholder: '这条内容主要给谁看？' },
  { key: 'objective', label: '内容目的', placeholder: '希望用户看完获得什么？' },
  { key: 'core_thesis', label: '核心结论 / 待验证问题', placeholder: '这条内容最核心的观点或验证问题' },
  { key: 'differentiation', label: '差异化', placeholder: '为什么这条内容值得由当前账号来做？' },
  { key: 'hook', label: '开头 / 首屏表达', placeholder: '第一句话、第一屏或视频开场' },
];

function LinesEditor({ label, value, onChange, placeholder }: { label: string; value: string[]; onChange: (value: string[]) => void; placeholder?: string }) {
  return <label className="idea-brief-field"><span>{label}</span><textarea value={value.join('\n')} placeholder={placeholder}
    onChange={(e) => onChange(e.target.value.split('\n').map((x) => x.trim()).filter(Boolean))} /></label>;
}

export default function IdeaBriefEditor({ idea, brief, busy, onSave, onConfirm, onDevelop, onStart, onClose }: Props) {
  const [draft, setDraft] = useState<IdeaBriefData>(() => clone(brief.data));
  const [locked, setLocked] = useState<string[]>(brief.locked_fields || []);
  const [instruction, setInstruction] = useState('');
  const [error, setError] = useState('');
  const [detailed, setDetailed] = useState(false);
  const actionPending = useRef(false);
  const dirty = JSON.stringify(draft) !== JSON.stringify(brief.data)
    || JSON.stringify(locked) !== JSON.stringify(brief.locked_fields || []);
  const confirmed = brief.status === 'confirmed' && !dirty;

  useEffect(() => { setDraft(clone(brief.data)); setLocked(brief.locked_fields || []); setError(''); }, [brief]);

  const patch = <K extends keyof IdeaBriefData>(key: K, value: IdeaBriefData[K]) => setDraft((current) => ({ ...current, [key]: value }));
  const toggleLock = (key: string) => setLocked((current) => current.includes(key) ? current.filter((x) => x !== key) : [...current, key]);
  const run = async (fn: () => Promise<void>) => {
    if (actionPending.current) return;
    actionPending.current = true;
    setError('');
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : '操作失败'); }
    finally { actionPending.current = false; }
  };

  return <div className="overlay idea-brief-overlay" onClick={() => !busy && onClose()}>
    <div className={`modal idea-brief-modal ${detailed ? 'details-open' : 'brief-simple'}`} onClick={(e) => e.stopPropagation()}>
      <header className="idea-brief-head">
        <div><small>内容策划单 · V{brief.revision}</small><h2>{idea.title}</h2><p>确认后可直接生成正文与封面，也可以只保存策划，稍后再制作。</p></div>
        <button className="icon-btn" disabled={busy} onClick={onClose}>×</button>
      </header>
      {error && <div className="notice-error">{error}</div>}
      {(draft.evidence_checks || []).filter(value => value.startsWith('经历与效果待核实：')).map(value => <p className="campaign-snapshot-notice stale" role="status" key={value}>{value}</p>)}
      <button className="btn btn-sm" aria-pressed={detailed} onClick={() => setDetailed(value => !value)}>{detailed ? '收起详细策划' : '详细策划'}</button>
      <div className="idea-brief-grid" inert={busy}>
        <section className="idea-brief-main">
          {TEXT_FIELDS.filter(({ key }) => detailed || key === 'core_thesis').map(({ key, label, placeholder }) => <div className="idea-brief-lock-row" key={key}>
            <label className="idea-brief-field"><span>{label}</span><textarea value={String(draft[key] || '')} placeholder={placeholder}
              onChange={(e) => patch(key, e.target.value as never)} /></label>
            <button className={locked.includes(key) ? 'idea-lock active' : 'idea-lock'} title={locked.includes(key) ? '已锁定，Agent 调整时保留' : '锁定此字段'} onClick={() => toggleLock(key)}>锁定</button>
          </div>)}
          <div className="idea-brief-lock-row">
            <LinesEditor label="标题方向" value={draft.title_directions || []} onChange={(value) => patch('title_directions', value)} placeholder="每行一个标题方向" />
            <button className={locked.includes('title_directions') ? 'idea-lock active' : 'idea-lock'} onClick={() => toggleLock('title_directions')}>锁定</button>
          </div>

          <div className="idea-brief-block">
            <div className="idea-brief-block-head"><strong>结构 / 脚本节奏</strong><button className={locked.includes('outline') ? 'idea-lock active' : 'idea-lock'} onClick={() => toggleLock('outline')}>锁定</button></div>
            {(draft.outline || []).map((row, index) => <div className="idea-outline-row" key={index}>
              <input value={row.title} placeholder="段落 / 镜头标题" onChange={(e) => patch('outline', draft.outline.map((x, i) => i === index ? { ...x, title: e.target.value } : x))} />
              <textarea value={row.purpose} placeholder="这一段要完成什么" onChange={(e) => patch('outline', draft.outline.map((x, i) => i === index ? { ...x, purpose: e.target.value } : x))} />
              <input value={(row.evidence_needed || []).join('、')} placeholder="需要的证据/素材，用顿号分隔" onChange={(e) => patch('outline', draft.outline.map((x, i) => i === index ? { ...x, evidence_needed: e.target.value.split('、').map((v) => v.trim()).filter(Boolean) } : x))} />
              <button className="r2-text-button" onClick={() => patch('outline', draft.outline.filter((_, i) => i !== index))}>删除</button>
            </div>)}
            <button className="btn btn-sm" onClick={() => patch('outline', [...(draft.outline || []), { title: '', purpose: '', evidence_needed: [] }])}>+ 添加段落</button>
          </div>

          <div className="idea-brief-block" hidden={!detailed}>
            <div className="idea-brief-block-head"><strong>平台表达</strong><button className={locked.includes('platform_plans') ? 'idea-lock active' : 'idea-lock'} onClick={() => toggleLock('platform_plans')}>锁定</button></div>
            {(draft.platform_plans || []).map((plan, index) => <div className="idea-platform-plan-editor" key={plan.platform + index}>
              <div className="idea-platform-plan-title"><PlatformIcon platform={plan.platform} size={15} /><b>{platformDisplayName(plan.platform)}</b></div>
              <input value={plan.title} placeholder="平台标题方向" onChange={(e) => patch('platform_plans', draft.platform_plans.map((x, i) => i === index ? { ...x, title: e.target.value } : x))} />
              <input value={plan.format} placeholder="形式：图文 / 60s 视频 / 长文…" onChange={(e) => patch('platform_plans', draft.platform_plans.map((x, i) => i === index ? { ...x, format: e.target.value } : x))} />
              <textarea value={plan.hook} placeholder="平台开头 / 首屏" onChange={(e) => patch('platform_plans', draft.platform_plans.map((x, i) => i === index ? { ...x, hook: e.target.value } : x))} />
              <textarea value={plan.adaptation} placeholder="针对这个平台的改动" onChange={(e) => patch('platform_plans', draft.platform_plans.map((x, i) => i === index ? { ...x, adaptation: e.target.value } : x))} />
            </div>)}
          </div>
        </section>

        <aside className="idea-brief-side">
          <div className="idea-brief-block" hidden={!detailed}>
            <div className="idea-brief-block-head"><strong>事实核验</strong><button className={locked.includes('evidence_checks') ? 'idea-lock active' : 'idea-lock'} onClick={() => toggleLock('evidence_checks')}>锁定</button></div>
            <LinesEditor label="" value={draft.evidence_checks || []} onChange={(value) => patch('evidence_checks', value)} />
          </div>
          <div className="idea-brief-block">
            <div className="idea-brief-block-head"><strong>封面与素材要求</strong><button className={locked.includes('production_tasks') ? 'idea-lock active' : 'idea-lock'} onClick={() => toggleLock('production_tasks')}>锁定</button></div>
            <LinesEditor label="" value={draft.production_tasks || []} onChange={(value) => patch('production_tasks', value)} />
          </div>
          <div className="idea-brief-block" hidden={!detailed}>
            <div className="idea-brief-block-head"><strong>待确认问题</strong><button className={locked.includes('open_questions') ? 'idea-lock active' : 'idea-lock'} onClick={() => toggleLock('open_questions')}>锁定</button></div>
            <LinesEditor label="" value={draft.open_questions || []} onChange={(value) => patch('open_questions', value)} />
          </div>
          {draft.source_refs?.length > 0 && <div className="idea-brief-sources"><strong>来源引用</strong>{draft.source_refs.map((ref) => <code key={ref}>{ref}</code>)}</div>}
          <div className="idea-agent-adjust">
            <label>让 Agent 局部调整<textarea value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="例如：保留核心观点，只把抖音开头改成问题式。" /></label>
            <button className="btn btn-sm" disabled={busy || dirty || !instruction.trim()} onClick={() => void run(async () => { await onDevelop(instruction.trim()); setInstruction(''); })}>Agent 调整</button>
            {dirty && <small>先保存当前修改，再让 Agent 调整。</small>}
          </div>
        </aside>
      </div>
      <footer className="idea-brief-actions">
        <span>{dirty ? '有未保存的修改。确认时会一并保存。' : confirmed ? '当前策划已确认，可继续编辑或生成草稿。' : '确认方向后，下一步生成图文草稿。'}</span>
        <button className="btn btn-sm" disabled={busy || !dirty} onClick={() => void run(() => onSave(draft, locked))}>仅保存策划</button>
        {!confirmed && <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => void run(() => onConfirm(draft, locked, true))}>{idea.content_id ? '保存并继续已有稿' : '采用并生成草稿'}</button>}
        {confirmed && <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => void run(onStart)}>{idea.content_id ? '继续已有稿' : '生成图文草稿'}</button>}
      </footer>
    </div>
  </div>;
}
