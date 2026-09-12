export type ThemeMode = 'light' | 'dark';

const THEME_KEY = 'ripple_theme_v1';

export function getTheme(): ThemeMode {
  try {
    return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

export function applyTheme(theme: ThemeMode): void {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

export function setTheme(theme: ThemeMode): void {
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* storage unavailable */ }
  applyTheme(theme);
}

export function initializeTheme(): ThemeMode {
  const theme = getTheme();
  applyTheme(theme);
  return theme;
}
