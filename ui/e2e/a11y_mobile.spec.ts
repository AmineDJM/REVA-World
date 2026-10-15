/**
 * Accessibility basics and narrow-viewport layout on every screen:
 * - every form control has an accessible name; buttons and links have names;
 * - tables have a caption or accessible name;
 * - a visible focus indicator on the main controls;
 * - no horizontal page scroll at 375 px (iPhone SE/mini width).
 * Full-page screenshots (desktop + 375 px) are written to test-results/ for review.
 */
import { expect, test, type Page } from '@playwright/test';
import { BASE_URL, HAVE_SESSION, SCREENS, SKIP_REASON, STORAGE_STATE, settle } from './helpers';

test.skip(!HAVE_SESSION, SKIP_REASON);
test.use({ baseURL: BASE_URL, storageState: STORAGE_STATE });

function slug(path: string) {
  return path.replace(/^\//, '').replace(/[^a-z0-9]+/gi, '_') || 'root';
}

/** Elements without an accessible name (heuristic, no axe dependency). */
async function unnamedControls(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    const visible = (el: Element) => {
      const r = (el as HTMLElement).getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
    };
    const name = (el: HTMLElement): string => {
      const aria = el.getAttribute('aria-label');
      if (aria?.trim()) return aria.trim();
      const by = el.getAttribute('aria-labelledby');
      if (by) {
        const t = by
          .split(/\s+/)
          .map((id) => document.getElementById(id)?.textContent ?? '')
          .join(' ')
          .trim();
        if (t) return t;
      }
      const labels = (el as HTMLInputElement).labels;
      if (labels && labels.length) {
        const t = Array.from(labels)
          .map((l) => l.textContent ?? '')
          .join(' ')
          .trim();
        if (t) return t;
      }
      if (el.title?.trim()) return el.title.trim();
      if (el instanceof HTMLInputElement && ['button', 'submit', 'reset'].includes(el.type)) return el.value;
      if (['BUTTON', 'A', 'SUMMARY'].includes(el.tagName)) {
        const t = (el.innerText || el.textContent || '').trim();
        if (t) return t;
        const img = el.querySelector('img[alt], svg[aria-label]');
        if (img) return img.getAttribute('alt') ?? img.getAttribute('aria-label') ?? '';
      }
      return '';
    };
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('input:not([type=hidden]), select, textarea, button, a[href], summary'))) {
      if (!visible(el)) continue;
      if (!name(el)) out.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}.${el.className}: ${el.outerHTML.slice(0, 120)}`);
    }
    for (const t of Array.from(document.querySelectorAll<HTMLTableElement>('table'))) {
      if (!visible(t)) continue;
      const cap = t.caption?.textContent?.trim() || t.getAttribute('aria-label') || t.getAttribute('aria-labelledby');
      if (!cap) out.push(`table without caption: ${t.outerHTML.slice(0, 120)}`);
    }
    return out;
  });
}

/** Elements that stick out past the right edge of the viewport (cause horizontal page scroll). */
async function overflow(page: Page): Promise<{ scrollWidth: number; clientWidth: number; offenders: string[] }> {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const vw = doc.clientWidth;
    const offenders: string[] = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.right > vw + 1) {
        // Ignore descendants of a horizontally scrollable container (e.g. a table wrapper).
        let p: HTMLElement | null = el.parentElement;
        let clipped = false;
        while (p && p !== document.body) {
          const ox = getComputedStyle(p).overflowX;
          if (ox === 'auto' || ox === 'scroll' || ox === 'hidden' || ox === 'clip') {
            clipped = true;
            break;
          }
          p = p.parentElement;
        }
        if (!clipped) offenders.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 40)} right=${Math.round(r.right)}`);
      }
    }
    return { scrollWidth: doc.scrollWidth, clientWidth: vw, offenders: offenders.slice(0, 15) };
  });
}

for (const path of SCREENS) {
  test(`${path}: controls are named and the page fits 375 px without horizontal scroll`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(path);
    await settle(page);
    await page.screenshot({ path: info.outputPath(`desktop-${slug(path)}.png`), fullPage: true });
    expect.soft(await unnamedControls(page), 'controls without an accessible name').toEqual([]);

    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(path);
    await settle(page);
    await page.waitForTimeout(300);
    await page.screenshot({ path: info.outputPath(`mobile375-${slug(path)}.png`), fullPage: true });
    const o = await overflow(page);
    expect.soft(o.offenders, `elements overflowing the 375 px viewport on ${path}`).toEqual([]);
    expect(o.scrollWidth, `no horizontal page scroll on ${path} (offenders: ${o.offenders.join(', ')})`).toBeLessThanOrEqual(o.clientWidth);
  });
}

test('main controls show a visible keyboard focus indicator', async ({ page }) => {
  await page.goto('/simulation');
  await settle(page);
  const targets = [
    page.getByRole('link', { name: 'Skip to content' }),
    page.getByRole('navigation', { name: 'Screens' }).getByRole('link', { name: 'World' }),
    page.getByLabel('Pinned belief release'),
    page.getByLabel('Title'),
    page.getByRole('group', { name: 'Countries' }).getByRole('checkbox', { name: 'FRA', exact: true }),
    page.getByRole('radio', { name: /L2/ }),
    page.getByRole('button', { name: 'Create scenario and run' }),
  ];
  // Reach the first target by keyboard so :focus-visible applies, then move focus programmatically.
  await page.keyboard.press('Tab');
  for (const t of targets) {
    await t.focus();
    const ring = await t.evaluate((el) => {
      const cs = getComputedStyle(el);
      const outline = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0;
      const shadow = cs.boxShadow !== 'none' && cs.boxShadow !== '';
      return { outline, shadow, active: document.activeElement === el, desc: `${el.tagName} ${cs.outlineStyle} ${cs.outlineWidth} ${cs.boxShadow}` };
    });
    expect(ring.active).toBe(true);
    expect(ring.outline || ring.shadow, `focus indicator on ${ring.desc}`).toBe(true);
  }
});
