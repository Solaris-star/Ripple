import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import type { CSSProperties } from 'react';
import { fetchOutputs, fetchOutputContent, mediaUrl, deleteOutput } from '../lib/api';
import type { OutputNode, OutputMeta } from '../lib/api';
import { cleanImplicitWatermark, inspectImplicitWatermark } from '../lib/ripple';
import type { WatermarkCleanResult, WatermarkInspection } from '../lib/ripple';
import { renderMarkdown } from '../lib/sanitize';
import { IconOutputs, IconImage, IconVideo, IconMusic, IconFile, IconFolder, IconRefresh, IconChevron, IconTrash } from './icons';
import { Modal } from './workspace/Common';

const FILTERS: { key: string; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'image', label: '图片' },
  { key: 'video', label: '视频' },
  { key: 'audio', label: '音频' },
  { key: 'text', label: '文档' },
];

const KIND_LABEL: Record<string, string> = {
  article: '文章', 'xhs-note': '小红书', video: '视频', cards: '卡片',
  poster: '海报', audio: '音频', other: '其他',
};
const STATUS_LABEL: Record<string, string> = { draft: '草稿', ready: '待发', published: '已发' };
const STATUS_COLOR: Record<string, string> = { draft: '#94a3b8', ready: '#d97706', published: '#16a34a' };

const badge: CSSProperties = {
  fontSize: 11, padding: '1px 7px', borderRadius: 999,
  background: 'rgba(0,0,0,0.05)', color: 'var(--text-secondary)', whiteSpace: 'nowrap',
};
const statusBadge = (s: string): CSSProperties => ({
  ...badge, background: `${STATUS_COLOR[s] || '#94a3b8'}22`, color: STATUS_COLOR[s] || '#64748b',
});

function kindIcon(kind: string | undefined, size = 30) {
  if (kind === 'video') return <IconVideo size={size} />;
  if (kind === 'audio') return <IconMusic size={size} />;
  if (kind === 'image') return <IconImage size={size} />;
  return <IconFile size={size} />;
}
const isHtml = (name: string) => /\.html?$/i.test(name);
const kindLabel = (f: OutputNode) =>
  f.kind === 'text' ? (isHtml(f.name) ? '卡片' : '文档')
    : f.kind === 'image' ? '图片' : f.kind === 'video' ? '视频' : f.kind === 'audio' ? '音频' : '文件';
const WATERMARK_STATUS: Record<string, string> = {
  evidence: '发现可识别线索', unknown: '未知', unsupported: '暂不支持', unavailable: '检查组件未就绪', error: '检查未完成',
};

/** 递归找目录下第一张图/视频作封面缩略图。 */
function firstMedia(node: OutputNode): OutputNode | null {
  if (node.type === 'file') return (node.kind === 'image' || node.kind === 'video') ? node : null;
  for (const c of node.children || []) {
    const m = firstMedia(c);
    if (m) return m;
  }
  return null;
}

/** 展示头声明的封面 → 伪 file 节点（供 Thumb 渲染）。 */
function coverNode(m?: OutputMeta): OutputNode | null {
  if (!m?.cover) return null;
  const kind = /\.(mp4|mov|webm|mkv)$/i.test(m.cover) ? 'video' : 'image';
  return { name: 'cover', type: 'file', path: m.cover, kind } as OutputNode;
}

/** 按名称路径解析到当前目录的 children（stackNames 稳定，刷新后仍有效）。 */
function resolvePath(roots: OutputNode[], names: string[]): OutputNode[] {
  let nodes = roots;
  for (const nm of names) {
    const found = nodes.find((n) => n.type === 'dir' && n.name === nm);
    if (!found) return nodes;   // 路径失效（被删/改）→ 停在能解析到的层
    nodes = found.children || [];
  }
  return nodes;
}

function Thumb({ f, big }: { f: OutputNode | null; big?: boolean }) {
  if (f && f.kind === 'image') return <img src={mediaUrl(f.path)} alt="" loading="lazy" />;
  if (f && f.kind === 'video') return <video src={mediaUrl(f.path)} preload="metadata" muted />;
  return <div className="gcard-ph">{kindIcon(f?.kind, big ? 34 : 30)}</div>;
}

export default function OutputsPage() {
  const [roots, setRoots] = useState<OutputNode[]>([]);
  const [treeError, setTreeError] = useState('');
  const [stack, setStack] = useState<string[]>([]);   // 当前所在的文件夹名称路径
  const [filter, setFilter] = useState('all');
  const [selected, setSelected] = useState<OutputNode | null>(null);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<OutputNode | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const reqSeq = useRef(0);
  const [watermarkReport, setWatermarkReport] = useState<WatermarkInspection | null>(null);
  const [watermarkResult, setWatermarkResult] = useState<WatermarkCleanResult | null>(null);
  const [watermarkBusy, setWatermarkBusy] = useState(false);
  const [watermarkError, setWatermarkError] = useState('');
  const watermarkSeq = useRef(0);

  const load = useCallback(() => {
    setTreeError('');
    fetchOutputs().then(setRoots).catch(() => setTreeError('加载产物列表失败'));
  }, []);
  useEffect(() => { load(); }, [load]);

  const currentNodes = useMemo(() => resolvePath(roots, stack), [roots, stack]);
  const dirs = useMemo(
    () => currentNodes.filter((n) => n.type === 'dir').sort((a, b) => (b.mtime || 0) - (a.mtime || 0)),
    [currentNodes]);
  const files = useMemo(() => {
    const fs = currentNodes.filter((n) => n.type === 'file').sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
    return filter === 'all' ? fs : fs.filter((f) => f.kind === filter);
  }, [currentNodes, filter]);

  const atTop = stack.length === 0;
  const atProjectRoot = stack.length === 1;
  // 当前项目的展示头（进入项目后才有），用于「成品/素材」分区
  const projectMeta = useMemo(
    () => (stack.length >= 1 ? roots.find((r) => r.name === stack[0])?.meta : undefined),
    [roots, stack]);
  const deliverableSet = useMemo(
    () => new Set(atProjectRoot ? (projectMeta?.deliverablePaths || []) : []),
    [projectMeta, atProjectRoot]);
  const hasSplit = atProjectRoot && deliverableSet.size > 0;
  const deliverableFiles = useMemo(
    () => (hasSplit ? files.filter((f) => deliverableSet.has(f.path)) : []),
    [files, deliverableSet, hasSplit]);
  const restFiles = useMemo(
    () => (hasSplit ? files.filter((f) => !deliverableSet.has(f.path)) : files),
    [files, deliverableSet, hasSplit]);

  const enterDir = useCallback((name: string) => { setStack((s) => [...s, name]); setFilter('all'); }, []);
  const goTo = useCallback((depth: number) => { setStack((s) => s.slice(0, depth)); setFilter('all'); }, []);

  const requestDelete = useCallback((node: OutputNode, e?: React.MouseEvent) => {
    e?.stopPropagation();
    if (node.synthetic) return;
    setDeleteError(''); setDeleteTarget(node);
  }, []);
  const confirmDelete = useCallback(async () => {
    if (!deleteTarget || deleteBusy) return;
    setDeleteBusy(true); setDeleteError('');
    try {
      await deleteOutput(deleteTarget.path, true);
      setSelected((cur) => (cur?.path === deleteTarget.path ? null : cur));
      setDeleteTarget(null); load();
    } catch (err) {
      setDeleteError((err as Error).message || '删除失败');
    } finally { setDeleteBusy(false); }
  }, [deleteBusy, deleteTarget, load]);

  const inspectWatermark = useCallback(async (f: OutputNode) => {
    const seq = ++watermarkSeq.current;
    setWatermarkReport(null); setWatermarkResult(null); setWatermarkError(''); setWatermarkBusy(true);
    try {
      const report = await inspectImplicitWatermark(f.path);
      if (seq === watermarkSeq.current) setWatermarkReport(report);
    } catch (err) {
      if (seq === watermarkSeq.current) setWatermarkError(err instanceof Error ? err.message : '隐式水印检查失败');
    } finally {
      if (seq === watermarkSeq.current) setWatermarkBusy(false);
    }
  }, []);

  const open = useCallback(async (f: OutputNode) => {
    const seq = ++reqSeq.current;
    setSelected(f); setContent('');
    if (f.kind === 'image') void inspectWatermark(f);
    else { watermarkSeq.current += 1; setWatermarkReport(null); setWatermarkResult(null); setWatermarkError(''); }
    if (f.kind === 'text' && !isHtml(f.name)) {
      setLoading(true);
      try {
        const res = await fetchOutputContent(f.path);
        if (seq === reqSeq.current) setContent(res.isBinary ? '' : res.content);
      } finally { if (seq === reqSeq.current) setLoading(false); }
    }
  }, [inspectWatermark]);

  const cleanSelectedWatermark = useCallback(async () => {
    if (!selected || selected.kind !== 'image' || watermarkBusy) return;
    if (!window.confirm('隐式水印深度清理会重建整张图片像素，可能影响文字、人脸和细节。原图会保留，并生成新的派生副本。继续？')) return;
    setWatermarkBusy(true); setWatermarkError(''); setWatermarkResult(null);
    try {
      const result = await cleanImplicitWatermark(selected.path);
      setWatermarkResult(result); setWatermarkReport(result.inspection); load();
    } catch (err) {
      setWatermarkError(err instanceof Error ? err.message : '隐式水印清理失败');
    } finally { setWatermarkBusy(false); }
  }, [selected, watermarkBusy, load]);

  const preview = () => {
    if (!selected) return null;
    const url = mediaUrl(selected.path);
    if (selected.kind === 'image') return <img src={url} alt={selected.name} style={{ maxWidth: '100%', borderRadius: 'var(--radius)' }} />;
    if (selected.kind === 'video') return <video src={url} controls style={{ maxWidth: '100%', borderRadius: 'var(--radius)' }} />;
    if (selected.kind === 'audio') return <audio src={url} controls style={{ width: '100%' }} />;
    if (selected.kind === 'text' && isHtml(selected.name)) return (
      <>
        <iframe src={url} title={selected.name} sandbox=""
          style={{ width: '100%', height: '68vh', border: '1px solid var(--border)', borderRadius: 'var(--radius)', background: '#fff' }} />
        <div style={{ marginTop: 8 }}><a href={url} target="_blank" rel="noreferrer" style={{ color: 'var(--accent-start)', fontSize: 13 }}>在新标签打开 ↗</a></div>
      </>
    );
    if (selected.kind === 'text') {
      if (loading) return <div className="loading"><div className="spinner" />加载中…</div>;
      return <div className="outputs-viewer-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }} />;
    }
    return <div style={{ color: 'var(--text-secondary)', fontSize: 14 }}>无法预览。<a href={url} download style={{ color: 'var(--accent-start)' }}>下载 {selected.name}</a></div>;
  };

  /** 项目/文件夹卡片：顶层项目用展示头（标题/平台/状态/封面），嵌套子文件夹回退朴素样式。 */
  const renderDir = (d: OutputNode) => {
    const m = d.meta;
    const cover = coverNode(m) || firstMedia(d);
    return (
      <div key={d.path} className="card card-hover gcard" onClick={() => enterDir(d.name)}>
        <div className="gcard-thumb">
          <span className="gcard-kind">{d.synthetic ? (d.legacy_unlinked ? '历史' : '内容') : m?.kind ? (KIND_LABEL[m.kind] || m.kind) : '文件夹'}</span>
          {!d.synthetic && <button className="gcard-del" title="删除" onClick={(e) => requestDelete(d, e)}><IconTrash size={14} /></button>}
          {cover ? <Thumb f={cover} big /> : <div className="gcard-ph"><IconFolder size={38} /></div>}
        </div>
        <div className="gcard-meta">
          <div className="gcard-name" title={m?.title || d.name}>
            {!m && <IconFolder size={13} />} {m?.title || d.name}
          </div>
          <div className="gcard-sub" style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            {m?.platform && <span style={badge}>{m.platform}</span>}
            {m?.status && <span style={statusBadge(m.status)}>{STATUS_LABEL[m.status] || m.status}</span>}
            <span>{d.fileCount ?? 0} 个文件</span>
          </div>
        </div>
      </div>
    );
  };

  const renderFile = (f: OutputNode) => (
    <div key={f.path} className="card card-hover gcard" onClick={() => open(f)}>
      <div className="gcard-thumb">
        <span className="gcard-kind">{kindLabel(f)}</span>
        <button className="gcard-del" title="删除" onClick={(e) => requestDelete(f, e)}><IconTrash size={14} /></button>
        <Thumb f={f} />
      </div>
      <div className="gcard-meta">
        <div className="gcard-name" title={f.name}>{f.name}</div>
      </div>
    </div>
  );

  const empty = dirs.length === 0 && files.length === 0;

  return (
    <div className="gallery-page">
      <div className="gallery-head">
        <div>
          <h1 className="page-title">
            <IconOutputs size={21} />
            <span className="crumb" onClick={() => goTo(0)}>素材与成品</span>
            {stack.map((name, i) => (
              <span key={i}>
                <span className="crumb-sep">/</span>
                {i === stack.length - 1
                  ? (projectMeta?.title && i === 0 ? projectMeta.title : name)
                  : <span className="crumb" onClick={() => goTo(i + 1)}>{name}</span>}
              </span>
            ))}
          </h1>
          <p className="page-subtitle">
            {atTop
              ? `按内容归档，共 ${roots.filter((item) => !item.legacy_unlinked).length} 份内容${roots.some((item) => item.legacy_unlinked) ? '；另有历史未关联产物' : ''}。`
              : `${dirs.length} 个文件夹 · ${files.length} 个文件（可继续点开子文件夹）`}
          </p>
        </div>
        <button className="btn btn-sm" onClick={load}><IconRefresh size={14} /> 刷新</button>
      </div>

      {treeError && <div className="notice-error">{treeError}</div>}

      {/* 项目主题标签（进入项目根时展示） */}
      {atProjectRoot && projectMeta?.tags && projectMeta.tags.length > 0 && (
        <div className="gallery-filters" style={{ marginBottom: 4 }}>
          {projectMeta.tags.map((t) => <span key={t} style={badge}>#{t}</span>)}
        </div>
      )}

      {/* 面包屑返回 + 文件过滤（进入任意层后显示） */}
      {stack.length > 0 && (
        <div className="gallery-filters">
          <button className="btn btn-sm" onClick={() => goTo(stack.length - 1)}>
            <span style={{ transform: 'rotate(180deg)', display: 'inline-flex' }}><IconChevron size={13} /></span> 返回上级
          </button>
          {files.length > 0 && FILTERS.map((f) => (
            <button key={f.key} className={`chip ${filter === f.key ? 'active' : ''}`} onClick={() => setFilter(f.key)}>{f.label}</button>
          ))}
        </div>
      )}

      {empty && !treeError ? (
        <div className="empty-state" style={{ height: 300 }}>
          <div className="empty-icon"><IconOutputs size={44} /></div>
          <p>{atTop ? '还没有产物——去对话或技能库生成第一条内容吧' : '这个文件夹是空的'}</p>
        </div>
      ) : hasSplit ? (
        <>
          {/* 成品区 */}
          {deliverableFiles.length > 0 && (
            <>
              <div className="section-label" style={{ margin: '6px 0 8px', fontSize: 13, fontWeight: 600, color: 'var(--text-secondary)' }}>
                成品 · {deliverableFiles.length}
              </div>
              <div className="gallery-grid">{deliverableFiles.map(renderFile)}</div>
            </>
          )}
          {/* 素材 / 过程文件区 */}
          {(dirs.length > 0 || restFiles.length > 0) && (
            <>
              <div className="section-label" style={{ margin: '18px 0 8px', fontSize: 13, fontWeight: 600, color: 'var(--text-tertiary)' }}>
                素材 / 过程文件
              </div>
              <div className="gallery-grid">
                {dirs.map(renderDir)}
                {restFiles.map(renderFile)}
              </div>
            </>
          )}
        </>
      ) : (
        <div className="gallery-grid">
          {dirs.map(renderDir)}
          {files.map(renderFile)}
        </div>
      )}

      {selected && (
        <div className="drawer-overlay" onClick={() => setSelected(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ minWidth: 0 }}>
                <div className="skill-detail-title" style={{ fontSize: 16 }}>{selected.name}</div>
                <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginTop: 3, fontFamily: "'SF Mono','Consolas',monospace" }}>{selected.path}</div>
              </div>
              <button className="icon-btn" onClick={() => setSelected(null)}>×</button>
            </div>
            <div className="drawer-body">
              {preview()}
              {selected.kind === 'image' && (
                <div style={{ marginTop: 16, padding: 12, border: '1px solid var(--border)', borderRadius: 'var(--radius)', background: 'var(--surface-2)' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
                    <strong style={{ fontSize: 12 }}>隐式水印检查</strong>
                    <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{watermarkBusy && !watermarkResult ? '检查中…' : watermarkReport ? WATERMARK_STATUS[watermarkReport.status] || watermarkReport.status : '待检查'}</span>
                  </div>
                  {watermarkReport && <p style={{ marginTop: 8, fontSize: 11, lineHeight: 1.7, color: 'var(--text-secondary)' }}>{watermarkReport.note}</p>}
                  {watermarkReport?.provider && <p style={{ marginTop: 5, fontSize: 11, color: 'var(--text-tertiary)' }}>来源线索：{watermarkReport.provider}</p>}
                  {watermarkReport && !watermarkReport.deep_clean_ready && <p style={{ marginTop: 5, fontSize: 11, color: 'var(--text-tertiary)' }}>{watermarkReport.reason}</p>}
                  {watermarkError && <p style={{ marginTop: 8, fontSize: 11, color: 'var(--red)' }}>{watermarkError}</p>}
                  {watermarkResult && <p style={{ marginTop: 8, fontSize: 11, lineHeight: 1.7, color: 'var(--text-secondary)' }}>已生成派生副本：<code>{watermarkResult.output}</code></p>}
                </div>
              )}
            </div>
            <div style={{ padding: '10px 16px', borderTop: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', gap: 10 }}>
              <div>{selected.kind === 'image' && <button className="btn btn-sm" disabled={watermarkBusy || !watermarkReport?.deep_clean_ready} onClick={() => void cleanSelectedWatermark()}>{watermarkBusy ? '处理中…' : '隐式水印清理'}</button>}</div>
              <button className="btn btn-sm btn-danger" onClick={(e) => requestDelete(selected, e)}><IconTrash size={13} /> 删除此文件</button>
            </div>
          </div>
        </div>
      )}
      {deleteTarget && <Modal title={deleteTarget.type === 'dir' ? '删除内容目录' : '删除素材'} busy={deleteBusy} onClose={() => { if (!deleteBusy) { setDeleteTarget(null); setDeleteError(''); } }}>
        <p>{deleteTarget.type === 'dir' ? `将永久删除「${deleteTarget.meta?.title || deleteTarget.name}」目录中的 ${deleteTarget.fileCount || 0} 个文件。` : `将永久删除文件「${deleteTarget.name}」。`}</p>
        <p className="r2-muted">Ripple 会在服务端再次检查母版内容、平台版本和未完成发布任务的素材引用；仍被使用的文件不会删除。</p>
        {deleteError && <div className="notice-error">{deleteError}</div>}
        <footer><button className="r2-button" disabled={deleteBusy} onClick={() => { setDeleteTarget(null); setDeleteError(''); }}>取消</button><button className="r2-button danger" disabled={deleteBusy} onClick={() => void confirmDelete()}>{deleteBusy ? '删除中…' : deleteTarget.type === 'dir' ? '确认删除全部' : '确认删除'}</button></footer>
      </Modal>}
    </div>
  );
}
