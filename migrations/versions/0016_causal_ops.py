"""V3 causality operations (agent CAUSOPS): ``causal_refresh`` in the API submission allow-list.

``ops.submit_job`` (0006, extended by 0010_atlas_ops and 0013_links_ops) refuses job types outside a fixed
allow-list. The causal store (0015_causal) adds one worker job kind (``reva.operations.jobs.HANDLERS``):

* ``causal_refresh`` -> ``reva.operations.jobs:handle_causal_refresh`` (payload ``{"origin": "live" |
  "test_fixture" | null}``): ``reva.causal.refresh`` = KG causal statements -> ``kg.causal_claim`` (loader),
  then grades (``kg.causal_grade``), with worker credentials; nothing is written when nothing changed.

The function is replaced with 0013's body and the extended allow-list (``CREATE OR REPLACE`` keeps its
owner, ``SECURITY DEFINER``, fixed ``search_path`` and the grants of 0007). No table changes.

Revision ID: 0016_causal_ops
Revises: 0015_causal
"""

from alembic import op

revision = "0016_causal_ops"
down_revision = "0015_causal"
branch_labels = None
depends_on = None

SUBMIT_JOB_CAUSAL = """
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
                    'atlas_ingest','atlas_snapshot','links_materialise','causal_refresh') THEN
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

#: 0013_links_ops' definition (restored on downgrade).
SUBMIT_JOB_LINKS = """
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
                    'atlas_ingest','atlas_snapshot','links_materialise') THEN
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
    op.execute(SUBMIT_JOB_CAUSAL)


def downgrade() -> None:
    op.execute(SUBMIT_JOB_LINKS)
