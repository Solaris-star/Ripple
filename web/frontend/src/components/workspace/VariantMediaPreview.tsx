import { useState } from 'react';
import { mediaUrl } from '../../lib/api';
import { isVideoMedia } from './variantPublishing';

export default function VariantMediaPreview({ path, index }: { path: string; index: number }) {
  const [failed, setFailed] = useState(false);
  const label = `${isVideoMedia(path) ? '视频' : '图片'} ${index + 1}`;
  return <div className="variant-media-preview">
    {failed ? <p role="status">此素材暂时无法预览，请检查原文件。</p> : isVideoMedia(path)
      ? <video src={mediaUrl(path)} controls preload="metadata" aria-label={label} onError={() => setFailed(true)} />
      : <a href={mediaUrl(path)} target="_blank" rel="noreferrer" aria-label={`查看原图，${label}`}><img src={mediaUrl(path)} alt={label} loading="lazy" onError={() => setFailed(true)} /></a>}
  </div>;
}
