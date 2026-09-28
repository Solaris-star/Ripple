import type { Task, VariantContent } from '../../lib/ripple';
import { countXReply } from '../../lib/xText.ts';

export const XHS_TITLE_LIMIT = 20;
export const XHS_BODY_LIMIT = 1000;
export const XHS_IMAGE_LIMIT = 9;

export type MotherSyncField = 'title' | 'body' | 'tags' | 'media';
export const MOTHER_SYNC_FIELDS: { key: MotherSyncField; label: string }[] = [
  { key: 'title', label: '标题' }, { key: 'body', label: '正文' }, { key: 'tags', label: '话题' }, { key: 'media', label: '素材' },
];

export function syncMotherFields(current: VariantContent, mother: Pick<VariantContent, MotherSyncField>, fields: MotherSyncField[]): VariantContent {
  const next = { ...current, media: [...current.media] };
  for (const field of fields) {
    if (field === 'media') next.media = [...mother.media];
    else next[field] = mother[field];
  }
  return next;
}

function stableValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableValue).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${stableValue(item)}`).join(',')}}`;
  return JSON.stringify(value) ?? 'null';
}

export function variantDraftChanged(draft: VariantContent, saved: VariantContent, sourceVersion: string, savedSourceVersion: string): boolean {
  return sourceVersion !== savedSourceVersion || stableValue(draft) !== stableValue(saved);
}

export function publishResultText(task: Pick<Task, 'status' | 'receipt' | 'preflight_problems'>): string {
  if (['draft', 'review_ready'].includes(task.status) && task.preflight_problems?.length) return '预检未通过，请按提示修改稿件后重新检查。';
  if (task.receipt?.not_submitted) return '已确认尚未提交，可以检查内容后重试。';
  if (task.receipt?.draft_only) return '已写入公众号草稿箱，尚未公开发布。';
  if (task.receipt?.remote_status === 'reviewing') return '平台已收到作品，仍在审核中；公开范围尚未确认。';
  if (task.receipt?.remote_status === 'published' && task.receipt?.visibility === 'private') return '作品仅自己可见，未公开发布。';
  if (task.receipt?.remote_status === 'published' && task.receipt?.visibility === 'unknown') return '已读取本人作品，公开范围尚未确认。';
  const labels: Record<string, string> = {
    accepted: '平台已接收，尚未确认发布成功。', dispatching: '正在提交到平台，请稍候。',
    unknown_result: '暂时无法确定发布结果，请检查结果或填写已找到的作品链接。',
    verification_required: '发布结果需要核对，请检查结果或填写已找到的作品链接。',
    published: '已确认发布成功。', succeeded: '已确认发布成功。', simulated: '本地模拟已完成。', exported: '导出已完成。',
    scheduled: '已确认定时发布。', approved: '已审核，可以继续发布。',
    failed_retryable: '本次提交失败，可以检查原因后重试。', failed_terminal: '发布失败，请查看任务详情。',
    cancelled: '任务已取消。', draft: '草稿待检查。', review_ready: '内容已检查，等待确认发布。',
  };
  return labels[task.status] || '任务状态已更新，请查看详情。';
}

export function xhsTitleLength(value: string): number {
  // 与后端一致：按 UTF-16 单元加权，两个 ASCII 单元折算一个字。
  const text = value.trim();
  let units = 0;
  for (let index = 0; index < text.length; index += 1) units += text.charCodeAt(index) > 127 ? 2 : 1;
  return Math.ceil(units / 2);
}

export const bodyLength = (value: string): number => Array.from(value.trim()).length;
export const isVideoMedia = (path: string): boolean => /\.(mp4|mov|webm)$/i.test(path);

export function platformContentProblems(platform: string, content: VariantContent): string[] {
  if (content.delivery === 'export') return [];
  const problems: string[] = [];
  if (platform === 'x') {
    const count = countXReply(content.body);
    if (!count.valid) problems.push(`X 正文须为有效文字，最多 280 加权字符；当前为 ${count.weightedLength}。`);
    if (content.tags.trim()) problems.push('X 话题请移入正文，清空独立话题字段。');
  }
  if (platform === 'wechat' && bodyLength(content.title) > 32) problems.push('微信公众号标题最多 32 字，请缩短标题。');
  if (platform === 'bilibili') {
    const tid = content.options.bilibili_tid;
    if (typeof tid !== 'number' || !Number.isInteger(tid) || tid < 1 || tid > 99999) problems.push('请选择 B 站投稿分区，或填写有效的分区 ID。');
    if (![1, 2].includes(Number(content.options.bilibili_copyright))) problems.push('请选择 B 站版权类型。');
    if (content.options.bilibili_copyright === 2 && !String(content.options.bilibili_source || '').trim()) problems.push('转载作品必须填写来源。');
  }
  return problems;
}

export function taskNeedsAttention(task: Pick<Task, 'status' | 'preflight_problems'>): boolean {
  return ['accepted', 'unknown_result', 'verification_required', 'failed_retryable', 'failed_terminal'].includes(task.status)
    || (['draft', 'review_ready'].includes(task.status) && !!task.preflight_problems?.length);
}

export function taskStatusText(task: Pick<Task, 'status' | 'receipt' | 'preflight_problems'>): string | undefined {
  if (task.receipt?.remote_status === 'reviewing') return '审核中';
  if (task.receipt?.visibility === 'private') return '仅自己可见';
  if (task.status === 'published') return task.receipt?.verification === 'manual_user_confirmation' ? '已发布 · 人工已确认'
    : task.receipt?.evidence === 'remote_api' || task.receipt?.verification === 'remote_api' ? '已发布 · 接口已确认' : '已发布';
  if (['draft', 'review_ready'].includes(task.status) && task.preflight_problems?.length) return '预检未通过';
  return undefined;
}

export function xhsContentProblems(content: Pick<VariantContent, 'title' | 'body' | 'media'>): string[] {
  const problems: string[] = [];
  const titleCount = xhsTitleLength(content.title);
  const bodyCount = bodyLength(content.body);
  if (titleCount > XHS_TITLE_LIMIT) problems.push(`小红书标题超出 ${titleCount - XHS_TITLE_LIMIT} 字，请缩短至 ${XHS_TITLE_LIMIT} 字以内。`);
  if (bodyCount > XHS_BODY_LIMIT) problems.push(`小红书正文超出 ${bodyCount - XHS_BODY_LIMIT} 字，请缩短至 ${XHS_BODY_LIMIT} 字以内。`);
  const videos = content.media.filter(isVideoMedia);
  if (!content.media.length) problems.push('小红书需要图片或视频，请先添加素材。');
  if (videos.length && (videos.length > 1 || videos.length !== content.media.length)) problems.push('每条内容只允许一个视频，不能混用图片与视频。');
  if (content.media.length - videos.length > XHS_IMAGE_LIMIT) problems.push(`小红书最多支持 ${XHS_IMAGE_LIMIT} 张图片，请移除多余素材。`);
  return problems;
}

export function needsResultCheck(task: Pick<Task, 'status' | 'content' | 'receipt'>): boolean {
  if (task.content.mode !== 'real') return false;
  if (task.status === 'dispatching') return true;
  return ['accepted', 'unknown_result', 'verification_required'].includes(task.status) && !task.receipt?.not_submitted;
}

export function selectVariantTask(tasks: Task[], variantVersionId: string): Task | null {
  // 旧版本的活动任务也必须显示，不能被新草稿遮住。
  return tasks.find(needsResultCheck)
    || tasks.find(task => task.status === 'scheduled')
    || tasks.find(task => task.variant_version_id === variantVersionId && !['cancelled', 'failed_terminal'].includes(task.status))
    || null;
}

export function canDispatchVariantTask(task: Task | null, variantVersionId: string, dirty: boolean, uploading: boolean): boolean {
  return !!task && !dirty && !uploading && task.variant_version_id === variantVersionId
    && ['approved', 'failed_retryable'].includes(task.status) && !needsResultCheck(task);
}

export function reorderMedia(media: string[], index: number, direction: -1 | 1): string[] {
  const next = [...media];
  const target = index + direction;
  if (index < 0 || index >= media.length || target < 0 || target >= media.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}
