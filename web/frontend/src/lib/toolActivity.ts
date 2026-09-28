// 旧记录只保存文字事件；结束后移除已被最终状态替代的等待记录。
export function finishToolActivity(activity: string): string {
  const lines = activity.split('\n').filter(Boolean);
  return lines.flatMap((line, index) => {
    const match = /^(调用 .+) · (pending|running|updated)$/.exec(line);
    if (!match) return [line];
    const prefix = `${match[1]} · `;
    if (lines.slice(index + 1).some(next => next.startsWith(prefix))) return [];
    return [`${match[1]} · 已结束，未收到结果`];
  }).join('\n');
}
