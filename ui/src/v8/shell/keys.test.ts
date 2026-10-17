import { createKeyHandler, isTypingTarget } from './keys';

function key(k: string, init: KeyboardEventInit & { target?: EventTarget } = {}) {
  const e = new KeyboardEvent('keydown', { key: k, cancelable: true, ...init });
  if (init.target) Object.defineProperty(e, 'target', { value: init.target });
  return e;
}

describe('global shortcuts', () => {
  const setup = () => {
    let t = 1000;
    const calls: string[] = [];
    const h = createKeyHandler(
      {
        openPalette: () => calls.push('palette'),
        openHelp: () => calls.push('help'),
        go: (k) => (calls.push(`go:${k}`), k === 'm'),
        toggleRail: () => calls.push('rail'),
      },
      () => t,
    );
    return { h, calls, tick: (ms: number) => (t += ms) };
  };

  it('Ctrl/Cmd-K opens the palette, even from an input', () => {
    const { h, calls } = setup();
    const input = document.createElement('input');
    const e = key('k', { ctrlKey: true, target: input });
    h(e);
    h(key('k', { metaKey: true }));
    expect(calls).toEqual(['palette', 'palette']);
    expect(e.defaultPrevented).toBe(true);
  });

  it('"/" and "?" open palette and help, but not while typing', () => {
    const { h, calls } = setup();
    h(key('/'));
    h(key('?'));
    h(key('/', { target: document.createElement('textarea') }));
    expect(calls).toEqual(['palette', 'help']);
  });

  it('g <key> jumps when the key belongs to a feature, within the timeout', () => {
    const { h, calls, tick } = setup();
    h(key('g'));
    const e = key('m');
    h(e);
    expect(calls).toEqual(['go:m']);
    expect(e.defaultPrevented).toBe(true);
    h(key('g'));
    tick(2000);
    h(key('m'));
    expect(calls).toEqual(['go:m']); // too late: plain "m" does nothing
    h(key('g'));
    h(key('Shift'));
    h(key('M', { shiftKey: true }));
    expect(calls).toEqual(['go:m', 'go:m']);
  });

  it('"[" toggles the rail; modifiers are ignored', () => {
    const { h, calls } = setup();
    h(key('['));
    h(key('/', { altKey: true }));
    expect(calls).toEqual(['rail']);
  });

  it('isTypingTarget', () => {
    expect(isTypingTarget(document.createElement('input'))).toBe(true);
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    expect(isTypingTarget(cb)).toBe(false);
    const div = document.createElement('div');
    div.setAttribute('contenteditable', 'true');
    expect(isTypingTarget(div)).toBe(true);
    expect(isTypingTarget(document.body)).toBe(false);
  });
});
