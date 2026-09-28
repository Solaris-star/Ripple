import type { VariantContent } from '../../lib/ripple';
import VariantMediaPreview from './VariantMediaPreview';
import './ContentVariantPublisher.css';

export default function PublishPreview({ content, heading = '内容预览' }: {
  content: Pick<VariantContent, 'title' | 'body' | 'tags' | 'media'> & Partial<Pick<VariantContent, 'options'>>;
  heading?: string;
}) {
  return <section className="variant-publish-preview" aria-label={heading}>
    <div className="r2-section-heading"><h3>{heading}</h3><span className="r2-muted">素材 {content.media.length}</span></div>
    <h3>{content.title || '未命名内容'}</h3>
    <p className="variant-review-body">{content.body || '未填写正文。'}</p>
    <p className="variant-review-tags"><strong>话题标签：</strong>{content.tags || '未添加'}</p>
    {content.options?.bilibili_tid != null && <dl><dt>B 站分区 ID</dt><dd>{String(content.options.bilibili_tid)}</dd><dt>版权</dt><dd>{content.options.bilibili_copyright === 2 ? '转载' : '原创'}</dd>{content.options.bilibili_copyright === 2 && <><dt>转载来源</dt><dd>{String(content.options.bilibili_source || '')}</dd></>}</dl>}
    {content.options?.wechat_cover && <p>公众号封面：{String(content.options.wechat_cover)}</p>}
    {content.options && <dl>{[
      ['content_type', 'Blog 内容类型'], ['slug', '文章路径'], ['excerpt', 'Blog 摘要'], ['date', '发布日期'], ['thought_tag', '想法标签'],
      ['wechat_author', '公众号作者'], ['wechat_digest', '公众号摘要'], ['wechat_source_url', '阅读原文链接'],
    ].filter(([key]) => content.options?.[key]).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{String(content.options?.[key])}</dd></div>)}</dl>}
    {!!content.media.length && <div className="variant-media-grid">{content.media.map((path, index) => <figure className="variant-media-card" key={`${path}-${index}`}>
      <VariantMediaPreview path={path} index={index} />
      <figcaption>素材 {index + 1}</figcaption>
    </figure>)}</div>}
  </section>;
}
