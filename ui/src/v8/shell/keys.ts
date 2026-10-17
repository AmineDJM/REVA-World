/**
 * Global keyboard shortcuts of the console (pure handler, testable):
 *   Ctrl/⌘-K  palette (also inside inputs)      /  palette        ?  shortcuts sheet
 *   g <key>   go to the feature with that goKey  [  collapse/expand the rail
 * Single-key shortcuts are ignored while typing (input, textarea, select, contenteditable).
 */
export interface KeyActions {
  openPalette: () => void;
  openHelp: () => void;
  /** Returns true when a feature owns the key. */
  go: (key: string) => boolean;
  toggleRail?: () => void;
}

export const GO_TIMEOUT_MS = 1500;

export function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || typeof el !== 'object' || !('tagName' in el)) return false;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (el as HTMLInputElement).type;
    return !['button', 'checkbox', 'radio', 'range', 'submit', 'reset', 'color', 'file'].includes(type);
  }
  return el.isContentEditable === true || el.getAttribute?.('contenteditable') === 'true';
}

export function createKeyHandler(actions: KeyActions, now: () => number = () => Date.now()) {
  let gAt = 0;
  return (e: KeyboardEvent): void => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      gAt = 0;
      actions.openPalette();
      return;
    }
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTypingTarget(e.target)) return;
    if (e.key === 'Shift') return;
    if (gAt) {
      const fresh = now() - gAt < GO_TIMEOUT_MS;
      gAt = 0;
      if (fresh) {
        if (actions.go(e.key.toLowerCase())) e.preventDefault();
        return;
      }
    }
    switch (e.key) {
      case '/':
        e.preventDefault();
        actions.openPalette();
        break;
      case '?':
        e.preventDefault();
        actions.openHelp();
        break;
      case 'g':
        gAt = now();
        break;
      case '[':
        if (actions.toggleRail) {
          e.preventDefault();
          actions.toggleRail();
        }
        break;
      default:
        break;
    }
  };
}
