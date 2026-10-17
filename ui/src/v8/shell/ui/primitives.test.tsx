import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { Badge, MissingValue, SourceChip, Stat, Tabs, VirtualTable, MiniLine, frCompact, type ColumnDef } from '.';

describe('V8 primitives', () => {
  it('Stat renders an exact decimal string in French, and a missing value hatched with its reason (never 0)', () => {
    const { rerender } = render(<Stat label="PIB" value="57082396439.6" unit="US$" period="2024" maxFractionDigits={1} />);
    expect(screen.getByText('57 082 396 439,6')).toBeInTheDocument();
    rerender(<Stat label="PIB" value={null} missing={{ reason: 'not_reported' }} />);
    expect(screen.getByTestId('v8-missing')).toHaveTextContent('manquant : non rapporté par la source');
    expect(screen.queryByText('0')).toBeNull();
  });

  it('Stat badges projections, not observations', () => {
    const { rerender } = render(<Stat label="x" value={2.1} />);
    expect(screen.queryByText('observé')).toBeNull();
    rerender(<Stat label="x" value={2.1} kind="projection" />);
    expect(screen.getByText('projection')).toBeInTheDocument();
  });

  it('Badge default labels', () => {
    render(
      <>
        <Badge kind="scenario" />
        <Badge kind="judged" />
      </>,
    );
    expect(screen.getByText('scénario non validé')).toBeInTheDocument();
    expect(screen.getByText('prévision jugée')).toBeInTheDocument();
  });

  it('MissingValue translates codes', () => {
    render(<MissingValue reason="confidential" />);
    expect(screen.getByText('manquant : confidentiel')).toBeInTheDocument();
  });

  it('SourceChip flags non-commercial licences and shows terms in its popover', () => {
    render(<SourceChip source="ilostat" licence={{ licence: 'CC BY-NC 4.0', non_commercial: true }} period="2023" />);
    expect(screen.getByText('NC')).toBeInTheDocument();
    expect(screen.getByRole('tooltip', { hidden: true })).toHaveTextContent('licence non commerciale');
  });

  it('Tabs move with arrow keys', async () => {
    const seen: string[] = [];
    const tabs = [
      { id: 'a', label: 'A' },
      { id: 'b', label: 'B' },
    ];
    render(<Tabs tabs={tabs} value="a" onChange={(v) => seen.push(v)} label="t" />);
    screen.getByRole('tab', { name: 'A' }).focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(seen).toEqual(['b']);
  });

  it('VirtualTable renders rows and sorts', async () => {
    type R = { name: string; v: number };
    const cols: ColumnDef<R>[] = [
      { header: 'Nom', accessorKey: 'name' },
      { header: 'Valeur', accessorKey: 'v', meta: { numeric: true } },
    ];
    const data = Array.from({ length: 500 }, (_, i) => ({ name: `n${i}`, v: i }));
    render(
      <MemoryRouter>
        <VirtualTable data={data} columns={cols} caption="test" height={300} />
      </MemoryRouter>,
    );
    const rows = screen.getAllByRole('row');
    expect(rows.length).toBeGreaterThan(5);
    expect(rows.length).toBeLessThan(80); // virtualised
    await userEvent.click(screen.getByRole('button', { name: /Valeur/ })); // numbers sort descending first
    expect(screen.getAllByRole('row')[1]).toHaveTextContent('n499');
  }, 20_000);

  it('MiniLine breaks at gaps and dashes projections', () => {
    const { container } = render(<MiniLine points={[{ x: 1, y: 1 }, { x: 2, y: 2 }, { x: 3, y: null }, { x: 4, y: 3 }, { x: 5, y: 4 }]} split={5} />);
    const paths = container.querySelectorAll('path');
    expect(paths.length).toBe(3);
    expect(container.querySelectorAll('path[stroke-dasharray]').length).toBe(1);
  });

  it('frCompact', () => {
    expect(frCompact(2.9e12)).toBe('2,9 T');
  });
});
