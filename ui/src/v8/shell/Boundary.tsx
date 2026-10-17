import { Component, type ErrorInfo, type ReactNode } from 'react';

/** A feature that throws never blanks the console: the frame stays, the failure is named. */
export class FeatureBoundary extends Component<{ children: ReactNode; resetKey: string; title: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`V8 feature "${this.props.title}" failed`, error, info.componentStack);
  }
  render() {
    if (!this.state.error) return this.props.children;
    const chunk = /dynamically imported module|Loading chunk|Failed to fetch/i.test(this.state.error.message);
    return (
      <div className="v8-error" role="alert">
        <p className="v8-error-title">
          <strong>{chunk ? 'Section non chargée' : 'Cette section a échoué'}</strong> — {this.props.title}
        </p>
        <p className="v8-error-text">
          {chunk
            ? 'Le code de la section n’a pas pu être téléchargé (nouvelle version publiée ou réseau coupé).'
            : 'Rien n’est affiché plutôt que des valeurs partielles ou trompeuses.'}
        </p>
        <p className="v8-error-detail v8-mono">{this.state.error.message}</p>
        <button type="button" className="v8-btn v8-btn-sm" onClick={() => window.location.reload()}>
          Recharger
        </button>
      </div>
    );
  }
}
