import { useState } from 'react';
import { api, errorText, executeStructuredOperation } from '../../lib/ripple';
import type { PlatformVariant, TextPolishOutput } from '../../lib/ripple';
import { platformDisplayName } from '../../lib/platforms';
import { Feedback } from './Common';

export default function VariantAssistant({ variantId, platform, canApply, onUpdated }: {
  variantId: string; platform: string; canApply: boolean; onUpdated: () => void;
}) {
  const [goal, setGoal] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [preview, setPreview] = useState<{ variant: PlatformVariant; result: TextPolishOutput } | null>(null);
  const generate = async () => {
    if (busy || !canApply) return;
    setBusy(true); setError(''); setNotice(''); setPreview(null);
    try {
      const variant = await api<PlatformVariant>(`/api/ripple/variants/${variantId}`);
      const result = await executeStructuredOperation<TextPolishOutput>('text_polish', {
        title: variant.content.title, body: variant.content.body, mode: 'full',
        goal: `改写为${platformDisplayName(platform)}正文。${platform === 'x' ? '正文最多 280 加权字符，中文计 2，链接计 23。' : ''}${goal || '使表达自然，适合本平台读者。'}`,
      }, { kind: 'platform_variant', ref: variant.id, version: variant.version_id });
      setPreview({ variant, result: result.output });
    } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  };
  const apply = async () => {
    if (!preview || busy || !canApply) return;
    setBusy(true); setError('');
    try {
      await api<PlatformVariant>(`/api/ripple/variants/${preview.variant.id}`, 'PUT', {
        ...preview.variant.content, body: preview.result.revised_text,
        expected_version: preview.variant.version_id, source_version_id: preview.variant.source_version_id,
      });
      setPreview(null); setNotice('已保存到当前平台稿，通用草稿保持原样。'); onUpdated();
    } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  };
  return <div className="r2-variant-ai">
    <p>直接改写 {platformDisplayName(platform)} 正文，核对差异后保存到当前平台稿。</p>
    {!canApply && <p role="status">请先保存编辑器中的修改，再生成或应用建议。</p>}
    <label className="r2-field"><span>改写要求</span><textarea rows={4} maxLength={350} value={goal} disabled={busy} onChange={e => setGoal(e.target.value)} placeholder={platform === 'x' ? '例如：缩短成一条 X 帖子，保留结论' : '例如：改成适合这个平台的教程，保留具体步骤'} /></label>
    <button className="r2-button primary" disabled={busy || !canApply} onClick={() => void generate()}>{busy ? '正在处理…' : '生成平台改写预览'}</button>
    <Feedback error={error} notice={notice} />
    {preview && <section className="variant-ai-preview"><h3>改写差异</h3><details><summary>当前正文</summary><pre>{preview.variant.content.body}</pre></details><h4>建议正文</h4><pre>{preview.result.revised_text}</pre>
      {preview.result.changes.map(item => <p key={item}>{item}</p>)}{preview.result.warnings.map(item => <p className="r2-warning" key={item}>{item}</p>)}
      <button className="r2-button" disabled={busy} onClick={() => setPreview(null)}>放弃建议</button><button className="r2-button primary" disabled={busy || !canApply} onClick={() => void apply()}>应用到当前平台稿</button>
    </section>}
  </div>;
}
