import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { UncertaintyPanel, describeUncertainty } from './UncertaintyPanel';

describe('UncertaintyPanel', () => {
  it('labels a sensitivity range as not a confidence interval and attaches no probability', () => {
    const u = {
      status: 'quantified' as const,
      kind: 'sensitivity_range',
      lower: '-10',
      upper: '25',
      method_version: 'grid@1',
    };
    const { container } = render(<UncertaintyPanel uncertainty={u} />);
    expect(screen.getByText('Sensitivity range (not a confidence interval)')).toBeInTheDocument();
    expect(container.querySelector('.ubar-sensitivity')).not.toBeNull();
    expect(container.querySelector('.ubar-prediction')).toBeNull();
    expect(container.textContent).not.toMatch(/\d+% /);
    expect(describeUncertainty(u).probabilistic).toBe(false);
  });

  it('labels a prediction interval with its nominal level and a distinct treatment', () => {
    const u = { status: 'quantified' as const, kind: 'prediction_interval', level: 0.8, lower: '1', upper: '2', method_version: 'x@1' };
    const { container } = render(<UncertaintyPanel uncertainty={u} />);
    expect(screen.getByText('Prediction interval (80% nominal)')).toBeInTheDocument();
    expect(container.querySelector('.ubar-prediction')).not.toBeNull();
    expect(container.querySelector('.ubar-sensitivity')).toBeNull();
    expect(describeUncertainty(u).probabilistic).toBe(true);
  });

  it('labels bounds without a probability', () => {
    render(<UncertaintyPanel uncertainty={{ status: 'quantified', kind: 'bounds', lower: '0', upper: '5', method_version: 'b@1' }} />);
    expect(screen.getByText('Bounds (no probability attached)')).toBeInTheDocument();
  });

  it('shows unquantified uncertainty as not quantified and never as a zero-width interval', () => {
    const { container } = render(
      <UncertaintyPanel uncertainty={{ status: 'unquantified', kind: 'unknown', lower: '5', upper: '5' }} />,
    );
    expect(screen.getByText('Uncertainty not quantified')).toBeInTheDocument();
    expect(container.querySelector('.ubar')).toBeNull();
    expect(container.textContent).not.toMatch(/±\s*0|5 – 5/);
  });

  it('compact badges keep the distinct labels', () => {
    render(
      <>
        <UncertaintyPanel compact uncertainty={{ status: 'quantified', kind: 'sensitivity_range', lower: '1', upper: '2' }} />
        <UncertaintyPanel compact uncertainty={{ status: 'quantified', kind: 'prediction_interval', level: 0.95, lower: '1', upper: '2' }} />
      </>,
    );
    expect(screen.getByText('Sensitivity range (not a confidence interval)')).toBeInTheDocument();
    expect(screen.getByText('Prediction interval (95% nominal)')).toBeInTheDocument();
  });
});
