"""V3 atlas operations (agent ATLASOPS): atlas job types in the API submission allow-list.

``ops.submit_job`` (0006) refuses job types outside a fixed allow-list. The atlas platform adds two
worker job types (``reva.operations.jobs.HANDLERS``):

* ``atlas_ingest`` -> ``reva.atlas.ingest:handle_atlas_ingest`` (payload ``{"source", "dataset"?, "mode",
  "options"?}``; budget-metered: reserve before the first request, metered client, settle after);
* ``atlas_snapshot`` -> ``reva.atlas.snapshot:handle_atlas_snapshot`` (payload ``{"origin", "warm",
  "skip_unchanged"}``).

The function is replaced with the same body and the extended allow-list (``CREATE OR REPLACE`` keeps
its owner, ``SECURITY DEFINER``, fixed ``search_path`` and the grants of 0007). No table changes:
atlas tables, snapshots and their provenance live in ``evidence.dataset`` / ``meta.artifact`` /
``meta.activity`` (``docs/dev/v3_atlascore.md``).

Revision ID: 0010_atlas_ops
Revises: 0009_kg
"""

from alembic import op

revision = "0010_atlas_ops"
down_revision = "0009_kg"
branch_labels = None
depends_on = None

SUBMIT_JOB_ATLAS = """
CREATE OR REPLACE FUNCTION ops.submit_job(p_type text, p_payload jsonb, p_idempotency_key text,
                               p_priority integer, p_created_by text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE jid uuid; k text;
BEGIN
  IF meta.session_principal() NOT IN ('reva_api','owner') THEN
    RAISE EXCEPTION 'ops.submit_job is the API submission path; % may not submit jobs as an API principal',
      session_user USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_created_by IS NULL OR length(p_created_by) < 1 OR length(p_created_by) > 200 THEN
    RAISE EXCEPTION 'invalid job creator';
  END IF;
  IF p_type NOT IN ('simulation','replay','discovery','ingest','evaluation','population','health','backtest',
                    'atlas_ingest','atlas_snapshot') THEN
    RAISE EXCEPTION 'job type % cannot be submitted through the API', p_type;
  END IF;
  IF p_idempotency_key IS NULL OR length(p_idempotency_key) < 8 OR length(p_idempotency_key) > 200 THEN
    RAISE EXCEPTION 'invalid idempotency key';
  END IF;
  k := 'api:' || p_created_by || ':' || p_type || ':'
       || replace(replace(p_idempotency_key, '%', '%25'), ':', '%3A');
  IF p_priority < -10 OR p_priority > 10 THEN
    RAISE EXCEPTION 'priority out of API range';
  END IF;
  -- (R-26) bounded payloads: a job payload is a small parameter document.
  IF p_payload IS NULL OR octet_length(p_payload::text) > 65536 THEN
    RAISE EXCEPTION 'job payload missing or larger than 64 KiB';
  END IF;
  INSERT INTO ops.job(job_type, payload, idempotency_key, priority, created_by)
  VALUES (p_type, p_payload, k, p_priority, p_created_by)
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id INTO jid;
  IF jid IS NULL THEN
    SELECT id INTO jid FROM ops.job WHERE idempotency_key = k
      AND job_type = p_type AND payload = p_payload;
    IF jid IS NULL THEN
      RAISE EXCEPTION 'idempotency key reused with a different request';
    END IF;
  END IF;
  RETURN jid;
END $$;
"""

#: 0006's definition (restored on downgrade).
SUBMIT_JOB_0006 = """
CREATE OR REPLACE FUNCTION ops.submit_job(p_type text, p_payload jsonb, p_idempotency_key text,
                               p_priority integer, p_created_by text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE jid uuid; k text;
BEGIN
  IF meta.session_principal() NOT IN ('reva_api','owner') THEN
    RAISE EXCEPTION 'ops.submit_job is the API submission path; % may not submit jobs as an API principal',
      session_user USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_created_by IS NULL OR length(p_created_by) < 1 OR length(p_created_by) > 200 THEN
    RAISE EXCEPTION 'invalid job creator';
  END IF;
  IF p_type NOT IN ('simulation','replay','discovery','ingest','evaluation','population','health','backtest') THEN
    RAISE EXCEPTION 'job type % cannot be submitted through the API', p_type;
  END IF;
  IF p_idempotency_key IS NULL OR length(p_idempotency_key) < 8 OR length(p_idempotency_key) > 200 THEN
    RAISE EXCEPTION 'invalid idempotency key';
  END IF;
  k := 'api:' || p_created_by || ':' || p_type || ':'
       || replace(replace(p_idempotency_key, '%', '%25'), ':', '%3A');
  IF p_priority < -10 OR p_priority > 10 THEN
    RAISE EXCEPTION 'priority out of API range';
  END IF;
  -- (R-26) bounded payloads: a job payload is a small parameter document.
  IF p_payload IS NULL OR octet_length(p_payload::text) > 65536 THEN
    RAISE EXCEPTION 'job payload missing or larger than 64 KiB';
  END IF;
  INSERT INTO ops.job(job_type, payload, idempotency_key, priority, created_by)
  VALUES (p_type, p_payload, k, p_priority, p_created_by)
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id INTO jid;
  IF jid IS NULL THEN
    SELECT id INTO jid FROM ops.job WHERE idempotency_key = k
      AND job_type = p_type AND payload = p_payload;
    IF jid IS NULL THEN
      RAISE EXCEPTION 'idempotency key reused with a different request';
    END IF;
  END IF;
  RETURN jid;
END $$;
"""


def upgrade() -> None:
    op.execute(SUBMIT_JOB_ATLAS)


def downgrade() -> None:
    op.execute(SUBMIT_JOB_0006)
