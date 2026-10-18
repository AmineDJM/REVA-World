import { act, fireEvent, render, screen } from '@testing-library/react';
import { linkNote, PinButton } from './PinButton';
import { validatePin } from './pin';

const pinItem = vi.fn(async () => ({ boardId: 'b1', boardTitle: 'Mes épingles', added: true, origin: 'local' as const, href: '/v8/tableaux/b1' }));
vi.mock('./pin', async (orig) => ({ ...(await orig<typeof import('./pin')>()), pinItem: (...a: unknown[]) => pinItem(...(a as [])) }));

describe('PinButton / linkNote', () => {
  it('linkNote is a valid note pin pointing at the V8 URL (brackets and parentheses neutralised)', () => {
    const n = linkNote('Chemin [A] → (B)', '/v8/liens?from=a&to=b (x)', ['ligne']);
    expect(validatePin(n)).toEqual(n);
    expect(n.kind).toBe('note');
    expect(n.kind === 'note' && n.markdown).toBe('[Chemin A → (B)](/v8/liens?from=a&to=b%20%28x%29)\n\nligne');
  });

  it('pins every item returned at click time and says where', async () => {
    render(<PinButton item={() => [linkNote('Carte', '/v8/monde?table=t'), { kind: 'series', concept: 'c', geo: 'FRA', label: 'x' }]} />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Épingler' }));
    });
    expect(pinItem).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('status')).toHaveTextContent('Épinglé sur « Mes épingles » (dans ce navigateur).');
  });

  it('nothing to pin → says so, pins nothing', async () => {
    pinItem.mockClear();
    render(<PinButton item={() => null} />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Épingler' }));
    });
    expect(pinItem).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('Rien à épingler');
  });
});
