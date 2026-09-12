const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

export function cookieValue(name: string): string {
  if (typeof document === 'undefined') return '';
  const prefix = `${encodeURIComponent(name)}=`;
  for (const part of document.cookie.split(';')) {
    const value = part.trim();
    if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
  }
  return '';
}

export function securedFetchOptions(options: RequestInit = {}): RequestInit {
  const method = String(options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!SAFE_METHODS.has(method)) {
    const csrf = cookieValue('ripple_csrf');
    if (csrf) headers.set('X-Ripple-CSRF', csrf);
  }
  return { ...options, headers, credentials: 'same-origin' };
}

export function csrfToken(): string {
  return cookieValue('ripple_csrf');
}
