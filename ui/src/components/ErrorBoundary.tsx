import { Component, type ErrorInfo, type ReactNode } from 'react';

interface State {
  error: Error | null;
}

/** Keeps a screen failure from blanking the app; shows what failed and offers a reload. */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('screen failed', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="state state-error" role="alert">
          <p>
            <strong>This screen failed to render.</strong> No data is shown rather than partial or misleading values.
          </p>
          <p className="mono">{this.state.error.message}</p>
          <button type="button" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
