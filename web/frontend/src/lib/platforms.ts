export const PLATFORM_LABELS: Record<string, string> = {
  x: 'X',
  xiaohongshu: '小红书',
  douyin: '抖音',
  tiktok: 'TikTok',
  bilibili: 'B站',
  wechat: '微信公众号',
  'weixin-channels': '微信视频号',
  zhihu: '知乎',
  kuaishou: '快手',
  weibo: '微博',
  blog: 'Blog',
};

const PLATFORM_ALIASES: Record<string, string> = {
  x: 'x', twitter: 'x',
  xiaohongshu: 'xiaohongshu', xhs: 'xiaohongshu', '小红书': 'xiaohongshu',
  douyin: 'douyin', '抖音': 'douyin',
  tiktok: 'tiktok',
  bilibili: 'bilibili', bili: 'bilibili', 'b站': 'bilibili', 'B站': 'bilibili',
  wechat: 'wechat', '微信公众号': 'wechat', '公众号': 'wechat', 'weixin-mp': 'wechat',
  'weixin-channels': 'weixin-channels', channels: 'weixin-channels', '微信视频号': 'weixin-channels', '视频号': 'weixin-channels',
  zhihu: 'zhihu', '知乎': 'zhihu',
  kuaishou: 'kuaishou', '快手': 'kuaishou',
  weibo: 'weibo', '微博': 'weibo',
  blog: 'blog',
};

export function normalizePlatformKey(platform: string): string {
  const raw = String(platform || '').trim();
  return PLATFORM_ALIASES[raw] || PLATFORM_ALIASES[raw.toLowerCase()] || raw.toLowerCase();
}

export function platformDisplayName(platform: string): string {
  const key = normalizePlatformKey(platform);
  return PLATFORM_LABELS[key] || String(platform || '').trim() || '平台';
}
