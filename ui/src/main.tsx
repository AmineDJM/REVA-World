import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { App } from './App';
import { installLocaleGuard } from './lib/locale';
import './styles.css';

// Sanitise the browser locale once, before any library builds an Intl formatter from it.
installLocaleGuard();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
