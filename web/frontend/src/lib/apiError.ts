export function apiErrorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== 'object') return fallback;
  const value = payload as { detail?: unknown; message?: unknown };
  const detail = value.detail ?? value.message;
  if (typeof detail === 'string') return detail || fallback;
  if (Array.isArray(detail)) {
    const errors = detail.filter((item): item is { msg?: string; loc?: unknown[]; type?: string } => !!item && typeof item === 'object');
    if (errors.some(item => item.type === 'extra_forbidden')) {
      return '页面与当前运行服务的接口版本不一致。请更新并重启 Ripple 服务后重试，已保存的内容会保留。';
    }
    return errors.map(item => `${item.loc?.filter(part => part !== 'body').join('.') || '请求参数'}：${item.msg || '格式无效'}`).join('；') || fallback;
  }
  if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') return detail.message;
  return fallback;
}
