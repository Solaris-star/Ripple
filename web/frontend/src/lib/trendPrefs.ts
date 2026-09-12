export const TREND_PLATFORMS = [
  { key: 'weibo', label: '微博' },
  { key: 'douyin', label: '抖音' },
  { key: 'xiaohongshu', label: '小红书' },
  { key: 'zhihu', label: '知乎' },
  { key: 'bilibili', label: 'B站' },
  { key: 'baidu', label: '百度' },
  { key: 'toutiao', label: '头条' },
] as const;

export const TREND_SELECTION_KEY = 'ripple_trends_selected_v1';
export const ALL_TREND_KEYS = TREND_PLATFORMS.map((p) => p.key);

export function loadTrendSelection(): string[] {
  try {
    const raw = sessionStorage.getItem(TREND_SELECTION_KEY);
    if (raw === null) return [...ALL_TREND_KEYS];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [...ALL_TREND_KEYS];
    return parsed.filter((x): x is string => typeof x === 'string' && ALL_TREND_KEYS.includes(x as never));
  } catch {
    return [...ALL_TREND_KEYS];
  }
}

export function saveTrendSelection(keys: string[]) {
  try {
    sessionStorage.setItem(TREND_SELECTION_KEY, JSON.stringify(keys.filter((x) => ALL_TREND_KEYS.includes(x as never))));
  } catch { /* storage may be unavailable */ }
}
