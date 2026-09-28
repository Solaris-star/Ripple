import { useEffect, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import type { Mother, PlatformVariant } from '../../lib/ripple';
import { Feedback, Modal } from './Common';
import VariantMediaPreview from './VariantMediaPreview';

export default function SharedMediaPicker({ source, current, onApply, onClose }: {
  source: Mother; current: string[]; onApply: (paths: string[]) => void; onClose: () => void;
}) {
  const [paths, setPaths] = useState<string[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    let stopped = false;
    void api<{ items: PlatformVariant[] }>(`/api/ripple/contents/${source.id}/variants`).then(rows => {
      if (!stopped) setPaths([...new Set([...source.content.media, ...rows.items.flatMap(row => row.content.media)])].filter(path => !current.includes(path)));
    }).catch(cause => { if (!stopped) setError(errorText(cause)); }).finally(() => { if (!stopped) setLoading(false); });
    return () => { stopped = true; };
  }, [source.id, source.content.media, current]);
  return <Modal title="使用已有素材" onClose={onClose}>
    <p>选择这份主稿及其平台稿已经保存的素材，无需再次上传。</p><Feedback error={error} />
    {loading ? <p>正在读取素材…</p> : !paths.length ? <p>暂无可添加的素材。先在主稿或任一平台稿上传并保存。</p> : <div className="variant-media-grid">{paths.map((path, index) => <label className="variant-media-card" key={path}>
      <VariantMediaPreview path={path} index={index} />
      <span><input type="checkbox" checked={selected.includes(path)} disabled={!selected.includes(path) && current.length + selected.length >= 12} onChange={event => setSelected(values => event.target.checked ? [...values, path] : values.filter(value => value !== path))} />{path.split('/').at(-1)}</span>
    </label>)}</div>}
    <footer><button className="r2-button" onClick={onClose}>取消</button><button className="r2-button primary" disabled={!selected.length || loading} onClick={() => onApply([...current, ...selected])}>添加 {selected.length} 个素材</button></footer>
  </Modal>;
}
