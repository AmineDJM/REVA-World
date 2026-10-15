import { useEffect, useState } from 'react';
import { getJob, getSimulation } from '../api/client';
import { ApiError } from '../api/problem';
import { TERMINAL_JOB_STATUSES, type Envelope, type Job, type SimulationStatus } from '../api/types';

export interface SimulationPoll {
  job: Job | null;
  env: Envelope<SimulationStatus> | null;
  error: unknown;
  done: boolean;
}

/**
 * Poll a simulation job until it is terminal, then load the simulation record. An id that is
 * a run id (not a job id) is loaded directly. Back-off 0.8 s -> 4 s.
 */
export function useSimulation(id: string | null | undefined): SimulationPoll {
  const [state, setState] = useState<SimulationPoll>({ job: null, env: null, error: null, done: false });
  useEffect(() => {
    if (!id) return;
    let stopped = false;
    let delay = 800;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setState({ job: null, env: null, error: null, done: false });
    const loadSim = async (job: Job | null) => {
      const env = await getSimulation(id);
      if (!stopped) setState({ job, env, error: null, done: true });
    };
    const tick = async () => {
      try {
        const j = await getJob(id);
        if (stopped) return;
        if ((TERMINAL_JOB_STATUSES as readonly string[]).includes(j.status)) {
          await loadSim(j);
          return;
        }
        setState((s) => ({ ...s, job: j, error: null }));
      } catch (e) {
        if (stopped) return;
        if (e instanceof ApiError && e.kind === 'not_found') {
          // Not a job id: maybe a run id.
          try {
            await loadSim(null);
          } catch (e2) {
            if (!stopped) setState((s) => ({ ...s, error: e2, done: true }));
          }
          return;
        }
        setState((s) => ({ ...s, error: e }));
        if (e instanceof ApiError && e.kind === 'forbidden') return;
      }
      delay = Math.min(delay * 1.5, 4000);
      timer = setTimeout(tick, delay);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [id]);
  return state;
}
