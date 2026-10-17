/**
 * Scores — the honest scoreboard of REVA's numeric forecasts against every official forecaster (owner V8SCORES).
 * Route /v8/scores (deep link v8Links.scoreboard(track) → ?track=…), `g p`, palette « scores FMI », « scores énergie ».
 */
import { lazy } from 'react';
import type { V8Feature } from '../../contract';
import { buildCommands } from './model';

export const feature: V8Feature = {
  id: 'scores',
  title: 'Scores',
  path: 'scores',
  order: 60,
  icon: '◎',
  goKey: 'p',
  element: lazy(() => import('./ScoresPage')),
  commands: buildCommands(),
};
