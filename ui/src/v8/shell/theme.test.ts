import { act, renderHook } from '@testing-library/react';
import { applyThemePref, effectiveTheme, readThemePref, THEME_EVENT, THEME_KEY, toggledPref, useTheme } from './theme';

function mockMedia(dark: boolean) {
  window.matchMedia = ((q: string) => ({ matches: dark && q.includes('dark'), media: q, addEventListener: () => undefined, removeEventListener: () => undefined })) as unknown as typeof window.matchMedia;
}

describe('theme', () => {
  afterEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.theme;
  });

  it('system preference follows prefers-color-scheme; data-theme overrides and persists', () => {
    mockMedia(true);
    expect(readThemePref()).toBe('system');
    expect(effectiveTheme('system')).toBe('dark');
    const seen: string[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail.theme);
    window.addEventListener(THEME_EVENT, on);
    applyThemePref('light');
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(localStorage.getItem(THEME_KEY)).toBe('light');
    applyThemePref('system');
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(localStorage.getItem(THEME_KEY)).toBeNull();
    window.removeEventListener(THEME_EVENT, on);
    expect(seen).toEqual(['light', 'dark']);
  });

  it('the toggle flips the effective theme', () => {
    mockMedia(false);
    expect(toggledPref('system')).toBe('dark');
    expect(toggledPref('dark')).toBe('light');
  });

  it('survives a storage that throws', () => {
    mockMedia(false);
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(readThemePref()).toBe('system');
    spy.mockRestore();
  });

  it('useTheme applies the preference to <html>', () => {
    mockMedia(false);
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe('light');
    act(() => result.current.setPref('dark'));
    expect(result.current.theme).toBe('dark');
    expect(document.documentElement.dataset.theme).toBe('dark');
  });
});
