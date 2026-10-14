import { useEffect, useState } from 'react';
import { cancelJob, getJob } from '../api/client';
import { ApiError } from '../api/problem';
import { TERMINAL_JOB_STATUSES, type Job } from '../api/types';
import { formatDateTime, humanize, shortId } from '../lib/format';
import { JsonView } from './JsonView';
import { ErrorState } from './StateViews';

/** Polls /v1/jobs/{id} until terminal; offers cooperative cancellation. */
export function JobTracker({ jobId, label, onDone }: { jobId: string; label: string; onDone?: (job: Job) => void }) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [cancelMsg, setCancelMsg] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    let delay = 800;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const j = await getJob(jobId);
        if (stopped) return;
        setJob(j);
        setError(null);
        if ((TERMINAL_JOB_STATUSES as readonly string[]).includes(j.status)) {
          onDone?.(j);
          return;
        }
      } catch (e) {
        if (stopped) return;
        setError(e);
        if (e instanceof ApiError && (e.kind === 'not_found' || e.kind === 'forbidden')) return;
      }
      delay = Math.min(delay * 1.5, 5000);
      timer = setTimeout(tick, delay);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  const cancel = async () => {
    try {
      const r = await cancelJob(jobId);
      setCancelMsg(`Cancellation requested (${humanize(r.status)}).`);
    } catch (e) {
      setCancelMsg(e instanceof ApiError ? (e.problem.detail ?? e.message) : String(e));
    }
  };

  return (
    <div className="job-tracker" aria-label={`${label} job`}>
      <p role="status" aria-live="polite">
        {label} job <span className="mono">{shortId(jobId)}</span>: {job ? humanize(job.status) : 'checking…'}
        {job?.finished_at && <span className="muted"> · finished {formatDateTime(job.finished_at)}</span>}
      </p>
      {job && Object.keys(job.progress ?? {}).length > 0 && <JsonView value={job.progress} />}
      {job?.cancellable && (
        <button type="button" onClick={cancel}>
          Cancel
        </button>
      )}
      {cancelMsg && <p role="status">{cancelMsg}</p>}
      {job?.last_error && (
        <div className="problem" role="alert">
          <JsonView value={job.last_error} />
        </div>
      )}
      {job?.result_ref && (
        <details>
          <summary>Result reference</summary>
          <JsonView value={job.result_ref} />
        </details>
      )}
      {!!error && <ErrorState error={error} what="Job status" />}
    </div>
  );
}
