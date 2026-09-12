import { useState, useEffect, useCallback, useMemo } from 'react';
import { fetchIdeas, createIdea, updateIdea, deleteIdea, createSchedule, recommendIdeas } from '../lib/api';
import type { Idea, IdeaInput, IdeaRecommendation, IdeaRecommendResponse, PersonaItem } from '../lib/api';
import { IconIdea, IconEdit, IconTrash, IconChat, IconCalendar, IconChevron, IconSkills } from './icons';
import { loadTrendSelection, TREND_PLATFORMS } from '../lib/trendPrefs';
import { executeStructuredOperation, fetchStructuredOperations } from '../lib/ripple';
import type { OperationResult, TopicEvaluationOutput } from '../lib/ripple';

interface IdeasPageProps {
  onUseTopic: (title: string) => void;
  persona: string;
  aiReady: boolean;
  personas: PersonaItem[];
  onPersonaChange: (name: string) => void;
  onNewPersona: () => void;
}

const COLUMNS: { key: string; label: string; color: string }[] = [
  { key: 'pending', label: '待做', color: 'var(--text-tertiary)' },
  { key: 'doing', label: '进行中', color: 'var(--layer-attribute)' },
  { key: 'done', label: '已完成', color: 'var(--layer-publish)' },
];
const NEXT: Record<string, string> = { pending: 'doing', doing: 'done', done: 'pending' };
const EMPTY: IdeaInput = { title: '', note: '', source: '', status: 'pending' };

export default function IdeasPage({ onUseTopic, persona, aiReady, personas, onPersonaChange, onNewPersona }: IdeasPageProps) {
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [form, setForm] = useState<IdeaInput | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [toast, setToast] = useState('');
  const [recommendOpen, setRecommendOpen] = useState(false);
  const [recommending, setRecommending] = useState(false);
  const [recommendError, setRecommendError] = useState('');
  const [recommendResult, setRecommendResult] = useState<IdeaRecommendResponse | null>(null);
  const [addedRecommendations, setAddedRecommendations] = useState<Set<string>>(new Set());
  const [evaluation, setEvaluation] = useState<OperationResult<TopicEvaluationOutput> | null>(null);
  const [evaluationIdea, setEvaluationIdea] = useState<Idea | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  const [evaluationError, setEvaluationError] = useState('');
  const [evaluationReady, setEvaluationReady] = useState(false);

  const load = useCallback(() => { fetchIdeas().then(setIdeas).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { void fetchStructuredOperations().then(({ items }) => setEvaluationReady(!!items.find((x) => x.id === 'topic_evaluate')?.ready)).catch(() => setEvaluationReady(false)); }, []);

  const showToast = (m: string) => { setToast(m); setTimeout(() => setToast(''), 2200); };
  const byStatus = useMemo(() => {
    const g: Record<string, Idea[]> = { pending: [], doing: [], done: [] };
    for (const it of ideas) (g[it.status] || g.pending).push(it);
    return g;
  }, [ideas]);

  const openNew = () => { setEditId(null); setForm({ ...EMPTY }); };
  const openEdit = (it: Idea) => { setEditId(it.id); setForm({ title: it.title, note: it.note, source: it.source, status: it.status }); };
  const save = async () => {
    if (!form || !form.title.trim()) return;
    if (editId) await updateIdea(editId, form); else await createIdea(form);
    setForm(null); setEditId(null); load();
  };
  const advance = async (it: Idea) => { await updateIdea(it.id, { ...it, status: NEXT[it.status] }); load(); };
  const remove = async (it: Idea) => { await deleteIdea(it.id); load(); };
  const schedule = async (it: Idea) => {
    const d = new Date();
    await createSchedule({ title: it.title, date: d.toISOString().slice(0, 10), platform: '', time: '', status: 'idea', note: it.note });
    showToast('已加入日历（今天）');
  };

  const evaluateIdea = async (it: Idea) => {
    setEvaluationIdea(it); setEvaluation(null); setEvaluationError(''); setEvaluating(true);
    try {
      setEvaluation(await executeStructuredOperation<TopicEvaluationOutput>('topic_evaluate', {
        title: it.title, note: it.note || '', platform: '', persona: persona || '',
      }, { kind: 'idea', ref: it.id, version: String(it.created || ''), snapshot: { title: it.title, note: it.note, source: it.source, status: it.status } }));
    } catch (e) { setEvaluationError(e instanceof Error ? e.message : '选题评估失败'); }
    finally { setEvaluating(false); }
  };

  const trendSelection = loadTrendSelection();
  const trendLabels = TREND_PLATFORMS.filter((p) => trendSelection.includes(p.key)).map((p) => p.label);
  const recommendDisabledReason = !persona
    ? '先选择一个账号画像'
    : !aiReady
      ? 'AI 推荐服务未配置或不可用'
      : trendSelection.length === 0
        ? '至少选择一个热点来源'
        : '';

  const runRecommend = async () => {
    if (recommendDisabledReason) return;
    setRecommendOpen(true);
    setRecommending(true);
    setRecommendError('');
    setRecommendResult(null);
    setAddedRecommendations(new Set());
    try {
      setRecommendResult(await recommendIdeas({ persona, platforms: trendSelection, limit: 6 }));
    } catch (e) {
      setRecommendError(e instanceof Error ? e.message : 'AI 推荐失败');
    } finally {
      setRecommending(false);
    }
  };

  const addRecommendation = async (rec: IdeaRecommendation) => {
    if (addedRecommendations.has(rec.title) || ideas.some((it) => it.title.trim() === rec.title.trim())) return;
    const refs = rec.trend_refs.length ? `\n\n关联热点：${rec.trend_refs.join('、')}` : '';
    await createIdea({
      title: rec.title,
      note: `${rec.angle}\n\n推荐理由：${rec.reason}${refs}`,
      source: `AI推荐 · ${persona}`,
      status: 'pending',
    });
    setAddedRecommendations((prev) => new Set(prev).add(rec.title));
    load();
  };

  const addAllRecommendations = async () => {
    if (!recommendResult) return;
    let count = 0;
    for (const rec of recommendResult.recommendations) {
      if (addedRecommendations.has(rec.title) || ideas.some((it) => it.title.trim() === rec.title.trim())) continue;
      try { await addRecommendation(rec); count += 1; } catch { /* keep the remaining recommendations usable */ }
    }
    showToast(count ? `已加入 ${count} 个 AI 推荐选题` : '没有新的推荐需要加入');
  };

  return (
    <div className="page-scroll ideas-page">
      <div className="page-head">
        <div>
          <h1 className="page-title"><IconIdea size={21} /> 选题库</h1>
          <p className="page-subtitle">从实时热点、账号画像和手动灵感中筛选选题，推进到「做内容」再进日历。</p>
        </div>
        <button className="btn btn-sm btn-primary" onClick={openNew}>+ 新建选题</button>
      </div>

      <div className="card idea-ai-panel">
        <div className="idea-ai-copy">
          <span className="idea-ai-icon"><IconSkills size={18} /></span>
          <div>
            <strong>AI 推荐选题</strong>
            <div className="idea-ai-persona">
              <label>推荐画像
                <select aria-label="AI 推荐账号画像" value={persona} onChange={(e) => onPersonaChange(e.target.value)}>
                  <option value="">选择账号画像</option>
                  {personas.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
                </select>
              </label>
              <button className="btn btn-sm" onClick={onNewPersona}>+ 创建画像</button>
            </div>
            <p>{recommendDisabledReason || `结合画像「${persona}」、${trendSelection.length} 个热点来源和 ${ideas.length} 个已有选题，推荐更匹配账号的内容角度。`}</p>
            {trendSelection.length > 0 && <small>热点范围：{trendLabels.join('、')}</small>}
          </div>
        </div>
        <button className="btn btn-sm btn-primary" disabled={!!recommendDisabledReason || recommending} title={recommendDisabledReason || '生成推荐'} onClick={() => void runRecommend()}>
          <IconSkills size={14} /> {recommending ? '分析中…' : 'AI 推荐选题'}
        </button>
      </div>

      <div className="kanban">
        {COLUMNS.map((col) => (
          <div key={col.key} className="kanban-col">
            <div className="kanban-col-head">
              <span className="kanban-dot" style={{ background: col.color }} />
              {col.label}<span className="kanban-count">{byStatus[col.key].length}</span>
            </div>
            <div className="kanban-list">
              {byStatus[col.key].length === 0 && <div className="kanban-empty">拖点选题进来吧</div>}
              {byStatus[col.key].map((it) => (
                <div key={it.id} className="card idea-card">
                  <div className="idea-card-actions">
                    <button className="session-act" title="编辑" onClick={() => openEdit(it)}><IconEdit size={13} /></button>
                    <button className="session-act" title={evaluationReady ? '七维选题评估' : '需要先配置 Agent 模型'} disabled={!evaluationReady} onClick={() => void evaluateIdea(it)}><IconSkills size={13} /></button>
                    <button className="session-act danger" title="删除" onClick={() => remove(it)}><IconTrash size={13} /></button>
                  </div>
                  <div className="idea-title">{it.title}</div>
                  {it.source && <span className="badge" style={{ marginTop: 6 }}>{it.source}</span>}
                  {it.note && <div className="idea-note">{it.note}</div>}
                  <div className="idea-foot">
                    <button className="idea-act" onClick={() => onUseTopic(it.title)}><IconChat size={13} /> 做内容</button>
                    <button className="idea-act" onClick={() => schedule(it)}><IconCalendar size={13} /> 排期</button>
                    <button className="idea-act next" onClick={() => advance(it)} title="推进状态">
                      {COLUMNS.find((c) => c.key === NEXT[it.status])?.label} <IconChevron size={12} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      {evaluationIdea && (
        <div className="overlay" onClick={() => !evaluating && setEvaluationIdea(null)}>
          <div className="modal r2-topic-eval-modal" onClick={(e) => e.stopPropagation()}>
            <div className="idea-rec-head"><div><h3>选题评估</h3><p>{evaluationIdea.title}</p></div><button className="icon-btn" disabled={evaluating} onClick={() => setEvaluationIdea(null)}>×</button></div>
            {evaluating && <div className="idea-rec-loading"><div className="spinner" />正在按统一七维口径评估…</div>}
            {evaluationError && <div className="notice-error" style={{ marginTop: 12 }}>{evaluationError}</div>}
            {evaluation && <div className="r2-topic-eval-body">
              <div className="r2-topic-eval-summary"><div><strong>{evaluation.output.score}</strong><span>/ 100</span></div><div><b>{evaluation.output.decision}</b><p>{evaluation.output.summary}</p></div></div>
              <div className="r2-topic-dimensions">{evaluation.output.dimensions.map((d) => <div key={d.name}><header><strong>{d.name}</strong><span>{d.score}/10</span></header><div className="r2-topic-score-track"><i style={{ width: `${d.score * 10}%` }} /></div><p>{d.reason}</p></div>)}</div>
              {evaluation.output.assumptions.length > 0 && <section><h4>信息边界</h4><ul>{evaluation.output.assumptions.map((x) => <li key={x}>{x}</li>)}</ul></section>}
              {evaluation.output.optimizations.length > 0 && <section><h4>优化建议</h4><ul>{evaluation.output.optimizations.map((x) => <li key={x}>{x}</li>)}</ul></section>}
              {evaluation.output.alternatives.length > 0 && <section><h4>替代选题</h4><ul>{evaluation.output.alternatives.map((x) => <li key={x}>{x}</li>)}</ul></section>}
              <footer><span>评估结果不会自动改变选题状态。</span><button className="btn btn-primary btn-sm" onClick={() => onUseTopic(evaluationIdea.title)}>做内容</button></footer>
            </div>}
          </div>
        </div>
      )}

      {recommendOpen && (
        <div className="overlay" onClick={() => !recommending && setRecommendOpen(false)}>
          <div className="modal idea-rec-modal" onClick={(e) => e.stopPropagation()}>
            <div className="idea-rec-head">
              <div>
                <h3>AI 推荐选题</h3>
                <p>实时热点 × 账号画像 × 已有选题去重</p>
              </div>
              <button className="icon-btn" disabled={recommending} onClick={() => setRecommendOpen(false)}>×</button>
            </div>
            {recommending && <div className="idea-rec-loading"><div className="spinner" />正在读取热点并结合画像分析…</div>}
            {recommendError && <div className="notice-error" style={{ margin: '12px 0 0' }}>{recommendError}</div>}
            {recommendResult && <>
              <div className="idea-rec-context">
                画像：{recommendResult.persona} · 热点来源 {recommendResult.trend_summary.filter((x) => x.count > 0).length}/{recommendResult.trend_summary.length} · 已避开 {recommendResult.existing_count} 个已有选题
              </div>
              <div className="idea-rec-list">
                {recommendResult.recommendations.map((rec) => {
                  const added = addedRecommendations.has(rec.title) || ideas.some((it) => it.title.trim() === rec.title.trim());
                  return <div className="idea-rec-card" key={rec.title}>
                    <div className="idea-rec-score">{rec.score}</div>
                    <div className="idea-rec-main">
                      <h4>{rec.title}</h4>
                      <p className="idea-rec-angle">{rec.angle}</p>
                      <p className="idea-rec-reason">{rec.reason}</p>
                      <div className="idea-rec-tags">
                        {rec.platforms.map((p) => <span key={p}>{p}</span>)}
                        {rec.trend_refs.map((p) => <span className="trend-ref" key={p}>热点 · {p}</span>)}
                      </div>
                    </div>
                    <div className="idea-rec-actions">
                      <button className="btn btn-sm" disabled={added} onClick={() => void addRecommendation(rec)}>{added ? '已加入' : '加入选题库'}</button>
                      <button className="btn btn-sm" onClick={() => onUseTopic(rec.title)}>做内容</button>
                    </div>
                  </div>;
                })}
              </div>
              <div className="idea-rec-foot">
                <span>AI 推荐用于选题判断；热点事实在创作前仍需核验。</span>
                <button className="btn btn-sm btn-primary" onClick={() => void addAllRecommendations()}>全部加入选题库</button>
              </div>
            </>}
          </div>
        </div>
      )}

      {form && (
        <div className="overlay" onClick={() => setForm(null)}>
          <div className="modal" style={{ width: 440, maxWidth: '100%' }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
              <h3 style={{ margin: 0 }}>{editId ? '编辑选题' : '新建选题'}</h3>
              <button className="icon-btn" onClick={() => setForm(null)}>×</button>
            </div>
            <label className="field-label">选题 *</label>
            <input className="field" value={form.title} autoFocus placeholder="想做的内容 / 角度"
              onChange={(e) => setForm({ ...form, title: e.target.value })} />
            <label className="field-label">备注 / 角度</label>
            <textarea className="field" style={{ minHeight: 70 }} value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })} />
            <label className="field-label">来源</label>
            <input className="field" value={form.source} placeholder="如：微博热搜 / 灵感"
              onChange={(e) => setForm({ ...form, source: e.target.value })} />
            <label className="field-label">状态</label>
            <div style={{ display: 'flex', gap: 7 }}>
              {COLUMNS.map((c) => (
                <button key={c.key} className={`chip ${form.status === c.key ? 'active' : ''}`}
                  onClick={() => setForm({ ...form, status: c.key })}>{c.label}</button>
              ))}
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
              <button className="btn btn-sm" onClick={() => setForm(null)}>取消</button>
              <button className="btn btn-sm btn-primary" onClick={save} disabled={!form.title.trim()}>保存</button>
            </div>
          </div>
        </div>
      )}

      {toast && <div className="toast ok"><span className="toast-icon">✓</span>{toast}</div>}
    </div>
  );
}
