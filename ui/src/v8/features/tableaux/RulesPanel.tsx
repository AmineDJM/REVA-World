/**
 * Watch rules of a board: list + form. Only rules that make sense for the chosen item are offered
 * (new data / revision / threshold on series, resolution on forecasts, new data on entities); a threshold
 * compares the latest OBSERVED value (never a projection) with a decimal typed in French or English notation.
 */
import { useEffect, useState } from 'react';
import {
  describeRule,
  itemTitle,
  normaliseDecimalInput,
  RULE_LABEL_FR,
  ruleProblem,
  rulesFor,
  THRESHOLD_OPS,
  type Board,
  type RuleKind,
  type ThresholdOp,
  type WatchRule,
} from './model';

const RULE_HELP: Record<RuleKind, string> = {
  new_data: 'Une nouvelle table ou un nouveau chargement touche cet élément.',
  revision: 'Une nouvelle version d’une table déjà chargée (valeurs possiblement révisées).',
  resolution: 'La question est résolue (résultat officiel publié) : prévisions jugées.',
  threshold: 'La dernière valeur OBSERVÉE franchit le seuil (les projections ne comptent pas).',
};

export function RulesPanel({
  board,
  focusUid,
  onAdd,
  onRemove,
  onClose,
}: {
  board: Board;
  focusUid: string | null;
  onAdd: (r: Omit<WatchRule, 'id' | 'created_at'>) => void;
  onRemove: (id: string) => void;
  onClose: () => void;
}) {
  const watchable = board.items.filter((i) => rulesFor(i.kind).length > 0);
  const [uid, setUid] = useState(focusUid ?? watchable[0]?.uid ?? '');
  const item = board.items.find((i) => i.uid === uid);
  const kinds = item ? rulesFor(item.kind) : [];
  const [kind, setKind] = useState<RuleKind>(kinds[0] ?? 'new_data');
  const [op, setOp] = useState<ThresholdOp>('>');
  const [value, setValue] = useState('');
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (focusUid) setUid(focusUid);
  }, [focusUid]);
  useEffect(() => {
    if (!kinds.includes(kind) && kinds[0]) setKind(kinds[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uid]);

  const submit = () => {
    const v = normaliseDecimalInput(value);
    const r = { kind, item_uid: uid, ...(kind === 'threshold' ? { op, value: v } : {}) };
    const p = ruleProblem(r, item);
    if (p) {
      setErr(p);
      return;
    }
    if (board.rules.some((x) => x.item_uid === uid && x.kind === kind && (kind !== 'threshold' || (x.op === op && x.value === v)))) {
      setErr('Cette règle existe déjà.');
      return;
    }
    setErr(null);
    setValue('');
    onAdd(r);
  };

  return (
    <div className="v8b-panel v8b-rules" aria-labelledby="v8b-rules-h">
      <div className="v8b-row v8b-between">
        <h2 id="v8b-rules-h" className="v8b-h2">
          Veille
        </h2>
        <button type="button" className="v8b-btn" onClick={onClose}>
          Fermer
        </button>
      </div>
      {board.rules.length === 0 ? (
        <p className="v8b-soft">Aucune règle. Une règle surveille un élément et produit une alerte reliée au changement qui l’a déclenchée.</p>
      ) : (
        <ul className="v8b-rule-list">
          {board.rules.map((r) => (
            <li key={r.id}>
              <span className={`v8b-rule-kind v8b-rule-${r.kind}`}>{RULE_LABEL_FR[r.kind]}</span>
              <span className="v8b-grow">{describeRule(r, board.items.find((i) => i.uid === r.item_uid))}</span>
              <button type="button" className="v8b-icon" aria-label={`Supprimer la règle : ${describeRule(r, board.items.find((i) => i.uid === r.item_uid))}`} onClick={() => onRemove(r.id)}>
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {watchable.length === 0 ? (
        <p className="v8b-soft">Rien à surveiller ici : seules les séries, prévisions et entités peuvent l’être (pas les notes ni les décisions).</p>
      ) : (
        <form
          className="v8b-rule-form"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <label>
            <span className="v8b-label">Élément</span>
            <select className="v8b-input" value={uid} onChange={(e) => setUid(e.target.value)}>
              {watchable.map((i) => (
                <option key={i.uid} value={i.uid}>
                  {itemTitle(i)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="v8b-label">Quand</span>
            <select className="v8b-input" value={kind} onChange={(e) => setKind(e.target.value as RuleKind)}>
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {RULE_LABEL_FR[k]}
                </option>
              ))}
            </select>
          </label>
          {kind === 'threshold' && (
            <>
              <label>
                <span className="v8b-label">Opérateur</span>
                <select className="v8b-input" value={op} onChange={(e) => setOp(e.target.value as ThresholdOp)}>
                  {THRESHOLD_OPS.map((o) => (
                    <option key={o} value={o}>
                      {o.replace('>=', '≥').replace('<=', '≤')}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span className="v8b-label">Seuil{item?.kind === 'series' && item.unit ? ` (${item.unit})` : ''}</span>
                <input className="v8b-input v8b-num-in" inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} placeholder="ex. 2,5" />
              </label>
            </>
          )}
          <button type="submit" className="v8b-btn v8b-btn-primary">
            Ajouter la règle
          </button>
          <p className="v8b-meta muted v8b-full">{RULE_HELP[kind]}</p>
          {err && (
            <p className="v8b-err v8b-full" role="alert">
              {err}
            </p>
          )}
        </form>
      )}
    </div>
  );
}
