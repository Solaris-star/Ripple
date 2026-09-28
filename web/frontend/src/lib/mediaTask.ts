export interface MediaTask {
  id: string;
  tool: 'ripple_generate_image';
  status: 'pending' | 'running' | 'completed' | 'failed' | 'interrupted';
  started_at: number;
  finished_at?: number;
  path?: string;
  error?: string;
}

export function parseMediaTask(data: string): MediaTask | null {
  try {
    let value: unknown = JSON.parse(data);
    if (typeof value === 'string') value = JSON.parse(value);
    if (!value || typeof value !== 'object') return null;
    const row = value as Record<string, unknown>;
    if (row.tool !== 'ripple_generate_image' || typeof row.id !== 'string' || !row.id || row.id.length > 200
        || !['pending', 'running', 'completed', 'failed', 'interrupted'].includes(String(row.status))
        || typeof row.started_at !== 'number' || !Number.isFinite(row.started_at) || row.started_at <= 0) return null;
    const path = typeof row.path === 'string' && row.path.length <= 1000 && !/[\\:]/.test(row.path)
      && row.path.split('/').every((part) => part && !part.startsWith('.') && !part.startsWith('_'))
      && /\.(png|jpe?g|webp|gif)$/i.test(row.path) ? row.path : undefined;
    const completedWithoutImage = row.status === 'completed' && !path;
    return {
      id: row.id, tool: 'ripple_generate_image', status: completedWithoutImage ? 'interrupted' : row.status as MediaTask['status'],
      started_at: row.started_at,
      ...(typeof row.finished_at === 'number' && Number.isFinite(row.finished_at) ? { finished_at: row.finished_at } : {}),
      ...(path ? { path } : {}),
      ...(completedWithoutImage ? { error: '工具已结束，但没有返回可用的图片产物。' }
        : typeof row.error === 'string' ? { error: row.error.slice(0, 500) } : {}),
    };
  } catch { return null; }
}

export function mergeMediaTask(tasks: MediaTask[], task: MediaTask): MediaTask[] {
  return tasks.some((item) => item.id === task.id)
    ? tasks.map((item) => item.id === task.id ? task : item)
    : [...tasks, task];
}

export function finishMediaTasks(tasks: MediaTask[] = [], now = Date.now()): MediaTask[] {
  return tasks.map((task) => task.status === 'running' || task.status === 'pending'
    ? { ...task, status: 'interrupted', finished_at: now, error: '本轮对话已结束，但图片工具没有返回完成结果。' }
    : task);
}

export function mediaElapsed(task: MediaTask, now: number): string {
  const seconds = Math.max(0, Math.floor(((task.finished_at ?? now) - task.started_at) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

export function imageRetryPrompt(original: string): string {
  return `仅重试上一轮未完成的图片生成步骤。沿用原配图要求，保留当前标题、正文和已有素材，不重新创作整篇内容；成功后提供图片产物，添加素材需给出待确认建议。\n\n原始要求：\n${original}`;
}
