import { useEffect, useState } from 'react';
import { humanize, isRecord } from '../lib/format';
import { LOCKED_SPEC_KEYS } from '../lib/draft';

type Path = (string | number)[];

export interface SpecEditorProps {
  spec: Record<string, unknown>;
  onChange: (path: Path, value: unknown) => void;
  /** Keys shown read-only (identity / release pinning). */
  locked?: Set<string>;
  disabled?: boolean;
}

function pathId(path: Path): string {
  return `spec-${path.map(String).join('-').replace(/[^\w-]/g, '_')}`;
}

function labelOf(key: string | number): string {
  return typeof key === 'number' ? `Item ${key + 1}` : humanize(key);
}

/** Number input that keeps the typed text while it is not yet a valid number. */
function NumberField({ id, value, onCommit, disabled }: { id: string; value: number; onCommit: (n: number) => void; disabled?: boolean }) {
  const [text, setText] = useState(String(value));
  useEffect(() => {
    if (Number(text) !== value) setText(String(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  const valid = text.trim() !== '' && Number.isFinite(Number(text));
  return (
    <>
      <input
        id={id}
        type="text"
        inputMode="decimal"
        value={text}
        disabled={disabled}
        aria-invalid={!valid || undefined}
        onChange={(e) => {
          setText(e.target.value);
          const n = Number(e.target.value);
          if (e.target.value.trim() !== '' && Number.isFinite(n)) onCommit(n);
        }}
      />
      {!valid && <span className="field-error">Not a number; the last valid value ({value}) is kept.</span>}
    </>
  );
}

function ListField({ id, value, onCommit, disabled }: { id: string; value: (string | number)[]; onCommit: (v: (string | number)[]) => void; disabled?: boolean }) {
  const numeric = value.length > 0 && value.every((x) => typeof x === 'number');
  const [text, setText] = useState(value.join(', '));
  useEffect(() => {
    if (text.split(',').map((s) => s.trim()).filter(Boolean).join(',') !== value.join(',')) setText(value.join(', '));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.join(',')]);
  return (
    <>
      <input
        id={id}
        type="text"
        value={text}
        disabled={disabled}
        onChange={(e) => {
          setText(e.target.value);
          const parts = e.target.value.split(',').map((s) => s.trim()).filter(Boolean);
          if (numeric) {
            const nums = parts.map(Number);
            if (nums.every(Number.isFinite)) onCommit(nums);
          } else onCommit(parts);
        }}
      />
      <span className="field-hint">Comma-separated{numeric ? ' numbers' : ''}.</span>
    </>
  );
}

function Field({ k, path, value, onChange, locked, disabled }: { k: string | number; path: Path; value: unknown; onChange: SpecEditorProps['onChange']; locked: Set<string>; disabled?: boolean }) {
  const id = pathId(path);
  const isLocked = path.length === 1 && typeof k === 'string' && locked.has(k);
  if (isRecord(value)) {
    return (
      <fieldset className="spec-group">
        <legend>{labelOf(k)}</legend>
        <div className="spec-grid">
          {Object.entries(value).map(([ck, cv]) => (
            <Field key={ck} k={ck} path={[...path, ck]} value={cv} onChange={onChange} locked={locked} disabled={disabled} />
          ))}
        </div>
      </fieldset>
    );
  }
  if (Array.isArray(value) && value.some((x) => isRecord(x) || Array.isArray(x))) {
    return (
      <fieldset className="spec-group">
        <legend>
          {labelOf(k)} ({value.length})
        </legend>
        <div className="spec-grid">
          {value.map((item, i) => (
            <Field key={i} k={i} path={[...path, i]} value={item} onChange={onChange} locked={locked} disabled={disabled} />
          ))}
        </div>
      </fieldset>
    );
  }
  if (isLocked || value === null || value === undefined) {
    return (
      <div className="spec-field spec-locked">
        <span className="spec-label">{labelOf(k)}</span>
        <span className="mono break" data-spec-path={path.join('.')}>
          {value === null || value === undefined ? 'null (not set; the server applies the model default or refuses)' : String(value)}
        </span>
        {isLocked && <span className="field-hint">Set by the server / pinned release; not editable here.</span>}
      </div>
    );
  }
  return (
    <div className="spec-field">
      <label htmlFor={id}>{labelOf(k)}</label>
      {typeof value === 'boolean' ? (
        <input id={id} type="checkbox" checked={value} disabled={disabled} onChange={(e) => onChange(path, e.target.checked)} />
      ) : typeof value === 'number' ? (
        <NumberField id={id} value={value} disabled={disabled} onCommit={(n) => onChange(path, n)} />
      ) : Array.isArray(value) ? (
        <ListField id={id} value={value as (string | number)[]} disabled={disabled} onCommit={(v) => onChange(path, v)} />
      ) : (
        <input id={id} type="text" value={String(value)} disabled={disabled} onChange={(e) => onChange(path, e.target.value)} />
      )}
    </div>
  );
}

/**
 * The draft ScenarioSpec as a readable, editable form. Types are kept (numbers stay numbers,
 * lists stay lists); identity and release-pinning keys are read-only. The server re-validates
 * whatever is confirmed with the same schema and compiler as hand-written specs.
 */
export function SpecEditor({ spec, onChange, locked = LOCKED_SPEC_KEYS, disabled }: SpecEditorProps) {
  return (
    <div className="spec-editor" data-testid="spec-editor">
      {Object.entries(spec).map(([k, v]) => (
        <Field key={k} k={k} path={[k]} value={v} onChange={onChange} locked={locked} disabled={disabled} />
      ))}
    </div>
  );
}
