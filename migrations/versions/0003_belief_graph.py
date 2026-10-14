"""Beliefs and graph: model versions (method registry), belief releases, estimates,
inputs, publication sequence, n-ary relations and occurrences.

model.version is created here (not 0004) because belief.estimate references the
publishing method version; dependency ordering wins over the reference grouping.

Revision ID: 0003_belief_graph
Revises: 0002_evidence
"""

from alembic import op

from reva.adapters.postgres.ddl import immutable

revision = "0003_belief_graph"
down_revision = "0002_evidence"
branch_labels = None
depends_on = None

SQL = r"""
CREATE TABLE model.version (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_key text NOT NULL,
  version text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('belief_method','forecast','scenario','population','compression','transform')),
  spec_sha256 text NOT NULL REFERENCES meta.artifact,
  code_digest text NOT NULL,
  input_ports jsonb NOT NULL,
  output_ports jsonb NOT NULL,
  applicability jsonb NOT NULL,
  assumptions jsonb NOT NULL,
  training_manifest_sha256 text REFERENCES meta.artifact,
  training_cutoff timestamptz,
  approval_status text NOT NULL CHECK (approval_status IN ('approved','candidate','retired')),
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (model_key, version)
);

CREATE FUNCTION meta.require_context_mode(ctx uuid, expected text) RETURNS void
  LANGUAGE plpgsql STABLE AS $$
DECLARE m text;
BEGIN
  SELECT mode INTO m FROM meta.context WHERE id = ctx;
  IF m IS DISTINCT FROM expected THEN
    RAISE EXCEPTION 'context % has mode %, expected %', ctx, m, expected;
  END IF;
END $$;

CREATE TABLE belief.release (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  context_id uuid NOT NULL UNIQUE REFERENCES meta.context,
  origin text NOT NULL CHECK (origin IN ('live','test_fixture')),
  manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  ontology_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  model_manifest_sha256 text NOT NULL REFERENCES meta.artifact,
  knowledge_cutoff timestamptz NOT NULL,
  method text NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- (7) a belief release lives in a belief-mode context with the same origin/cutoff
CREATE FUNCTION belief.check_release() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c meta.context;
BEGIN
  SELECT * INTO c FROM meta.context WHERE id = NEW.context_id;
  IF c.mode <> 'belief' THEN
    RAISE EXCEPTION 'belief release requires a belief-mode context (got %)', c.mode;
  END IF;
  IF c.origin <> NEW.origin OR c.knowledge_cutoff <> NEW.knowledge_cutoff THEN
    RAISE EXCEPTION 'release origin/cutoff must equal its context';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER release_context BEFORE INSERT ON belief.release
  FOR EACH ROW EXECUTE FUNCTION belief.check_release();

CREATE TABLE belief.release_artifact (
  release_id uuid NOT NULL REFERENCES belief.release,
  artifact_sha256 text NOT NULL REFERENCES meta.artifact,
  role text NOT NULL,
  PRIMARY KEY (release_id, artifact_sha256)
);

CREATE TABLE belief.estimate (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  release_id uuid NOT NULL REFERENCES belief.release,
  subject_id uuid NOT NULL REFERENCES world.entity,
  concept_version_id uuid NOT NULL REFERENCES ontology.concept_version,
  support_id uuid NOT NULL REFERENCES world.support,
  method_version_id uuid NOT NULL REFERENCES model.version,
  modality text NOT NULL CHECK (modality IN ('selected_report','estimated_state','forecast')),
  value jsonb NOT NULL CHECK (jsonb_typeof(value) = 'object' AND value ? 'kind'),
  uncertainty jsonb NOT NULL CHECK (uncertainty ? 'status'),
  alternatives jsonb NOT NULL DEFAULT '[]'::jsonb,
  discrepancy_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  UNIQUE (release_id, subject_id, concept_version_id, support_id, modality)
);
CREATE INDEX estimate_subject ON belief.estimate(subject_id, concept_version_id);

-- (9) belief inputs have real foreign keys to admissible records only:
-- source observations or prior belief estimates (scientific claims are added in
-- 0005). sim.result and evaluation predictions cannot be referenced.
CREATE TABLE belief.input (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  estimate_id uuid NOT NULL REFERENCES belief.estimate,
  observation_id uuid REFERENCES evidence.observation,
  prior_estimate_id uuid REFERENCES belief.estimate,
  CONSTRAINT input_exactly_one CHECK (num_nonnulls(observation_id, prior_estimate_id) = 1),
  CHECK (prior_estimate_id IS NULL OR prior_estimate_id <> estimate_id)
);
CREATE INDEX input_estimate ON belief.input(estimate_id);

CREATE FUNCTION belief.check_input() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r belief.release; o evidence.observation; prior_cutoff timestamptz; ref_year integer;
BEGIN
  SELECT rel.* INTO r FROM belief.estimate e JOIN belief.release rel ON rel.id = e.release_id
   WHERE e.id = NEW.estimate_id;
  IF NEW.observation_id IS NOT NULL THEN
    SELECT * INTO o FROM evidence.observation WHERE id = NEW.observation_id;
    -- temporal: only records committed at or before the release cutoff
    IF o.recorded_at > r.knowledge_cutoff THEN
      RAISE EXCEPTION 'observation % recorded after release cutoff', o.id;
    END IF;
    -- (6) eligible only for concepts/scopes approved for that connector version
    -- Support year (UTC) of the observation; 0 (no such year) when the support has no
    -- bounded time range, so years-scoped decisions fail closed.
    SELECT CASE WHEN sp.valid_range IS NULL OR lower_inf(sp.valid_range) OR NOT isfinite(lower(sp.valid_range))
                THEN 0 ELSE extract(year FROM lower(sp.valid_range) AT TIME ZONE 'UTC')::int END
      INTO ref_year FROM world.support sp WHERE sp.id = o.support_id;
    IF NOT evidence.is_eligible(o.connector_version_id, o.concept_version_id, o.subject_id, r.knowledge_cutoff,
                                COALESCE(ref_year, 0)) THEN
      RAISE EXCEPTION 'observation % is not eligible for concept % under connector %',
        o.id, o.concept_version_id, o.connector_version_id;
    END IF;
    IF EXISTS (SELECT 1 FROM evidence.observation_retraction t
               WHERE t.observation_id = o.id AND t.recorded_at <= r.knowledge_cutoff) THEN
      RAISE EXCEPTION 'observation % was retracted before the cutoff', o.id;
    END IF;
    IF r.origin = 'live' AND o.origin <> 'live' THEN
      RAISE EXCEPTION 'fixture-origin observation cannot enter a live release';
    END IF;
  ELSE
    SELECT rel.knowledge_cutoff INTO prior_cutoff FROM belief.estimate e
      JOIN belief.release rel ON rel.id = e.release_id WHERE e.id = NEW.prior_estimate_id;
    IF prior_cutoff > r.knowledge_cutoff THEN
      RAISE EXCEPTION 'prior estimate comes from a later knowledge cutoff';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER input_admissible BEFORE INSERT ON belief.input
  FOR EACH ROW EXECUTE FUNCTION belief.check_input();

-- Publication order: rows are inserted while holding a transaction-level
-- advisory lock so sequence order equals commit order.
CREATE TABLE ops.publication (
  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  release_id uuid NOT NULL UNIQUE REFERENCES belief.release,
  published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  activity_id uuid NOT NULL REFERENCES meta.activity
);

-- (11) published release manifests reference only committed, checksum-verified artifacts
CREATE FUNCTION ops.check_publication() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r belief.release; bad text; cmm text; session_held boolean;
BEGIN
  -- The identity value is allocated before this trigger runs, so the caller
  -- must take the publication lock before INSERT for sequence = commit order,
  -- and it must stay held until commit: only pg_advisory_xact_lock(7262011)
  -- (single bigint key: classid 0, objid 7262011, objsubid 1) qualifies.
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks WHERE locktype = 'advisory' AND pid = pg_catalog.pg_backend_pid()
                 AND classid = 0 AND objid = 7262011 AND objsubid = 1 AND mode = 'ExclusiveLock' AND granted) THEN
    RAISE EXCEPTION 'publication advisory lock (7262011) not held by this transaction';
  END IF;
  -- (R-19) pg_locks shows session-level and transaction-level advisory holds
  -- identically (verified on PostgreSQL 17: same row, same virtualtransaction).
  -- pg_advisory_unlock releases only a session-level hold and returns true iff
  -- one existed, so it detects a session lock (which could be released before
  -- commit). The probe emits a WARNING when no session hold exists (the normal
  -- case); it is hidden from the client here (the server log may record it).
  cmm := current_setting('client_min_messages');
  PERFORM set_config('client_min_messages', 'error', true);
  session_held := pg_catalog.pg_advisory_unlock(7262011);
  PERFORM set_config('client_min_messages', cmm, true);
  IF session_held THEN
    RAISE EXCEPTION 'publication advisory lock must be transaction-scoped (pg_advisory_xact_lock), not session-level';
  END IF;
  SELECT * INTO r FROM belief.release WHERE id = NEW.release_id;
  FOR bad IN
    SELECT a FROM unnest(ARRAY[r.manifest_sha256, r.ontology_manifest_sha256, r.model_manifest_sha256]) a
    WHERE NOT meta.artifact_is_verified(a)
    UNION
    SELECT artifact_sha256 FROM belief.release_artifact
    WHERE release_id = r.id AND NOT meta.artifact_is_verified(artifact_sha256)
  LOOP
    RAISE EXCEPTION 'release % references unverified artifact %', r.id, bad;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM belief.estimate WHERE release_id = r.id) THEN
    RAISE EXCEPTION 'release % has no estimates', r.id;
  END IF;
  IF EXISTS (SELECT 1 FROM belief.estimate e WHERE e.release_id = r.id
             AND NOT EXISTS (SELECT 1 FROM belief.input i WHERE i.estimate_id = e.id)) THEN
    RAISE EXCEPTION 'release % has an estimate without inputs (missing provenance)', r.id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER publication_verified BEFORE INSERT ON ops.publication
  FOR EACH ROW EXECUTE FUNCTION ops.check_publication();

-- (R-08) published_at is server time; only the table owner may supply it.
CREATE FUNCTION ops.force_published_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c
                 WHERE c.oid = TG_RELID AND c.relowner = (SELECT r.oid FROM pg_catalog.pg_roles r
                                                          WHERE r.rolname = current_user)) THEN
    NEW.published_at := clock_timestamp();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER a00_server_published_at BEFORE INSERT ON ops.publication
  FOR EACH ROW EXECUTE FUNCTION ops.force_published_at();

-- (R-02) A published release is sealed: nothing can be appended to it.
CREATE FUNCTION belief.reject_if_published() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rel uuid;
BEGIN
  IF TG_TABLE_NAME = 'input' THEN
    SELECT e.release_id INTO rel FROM belief.estimate e WHERE e.id = NEW.estimate_id;
  ELSE
    rel := NEW.release_id;
  END IF;
  IF EXISTS (SELECT 1 FROM ops.publication p WHERE p.release_id = rel) THEN
    RAISE EXCEPTION 'release % is published and sealed; publish a new release instead', rel;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER release_sealed BEFORE INSERT ON belief.estimate
  FOR EACH ROW EXECUTE FUNCTION belief.reject_if_published();
CREATE TRIGGER release_sealed BEFORE INSERT ON belief.input
  FOR EACH ROW EXECUTE FUNCTION belief.reject_if_published();
CREATE TRIGGER release_sealed BEFORE INSERT ON belief.release_artifact
  FOR EACH ROW EXECUTE FUNCTION belief.reject_if_published();

CREATE TABLE world.relation (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  predicate_id uuid NOT NULL REFERENCES ontology.concept_version,
  layer text NOT NULL,
  support_id uuid NOT NULL REFERENCES world.support,
  release_id uuid REFERENCES belief.release,
  evidence_observation_id uuid REFERENCES evidence.observation,
  existence_uncertainty jsonb NOT NULL,
  strength jsonb,
  component jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (num_nonnulls(release_id, evidence_observation_id) = 1)
);
CREATE INDEX relation_release ON world.relation(release_id, layer);
CREATE INDEX relation_observation ON world.relation(evidence_observation_id);
CREATE INDEX relation_layer ON world.relation(layer, support_id);
CREATE TABLE world.relation_participant (
  relation_id uuid NOT NULL REFERENCES world.relation,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  role_concept_id uuid NOT NULL REFERENCES ontology.concept_version,
  entity_id uuid NOT NULL REFERENCES world.entity,
  direction text NOT NULL CHECK (direction IN ('in','out','undirected')),
  PRIMARY KEY (relation_id, ordinal)
);
CREATE INDEX participant_lookup ON world.relation_participant(entity_id, role_concept_id, relation_id);

-- (10) participant role/entity type satisfy the predicate signature
CREATE FUNCTION world.check_participant() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE sig jsonb; role_key text; kind_key text; pred_kind text; rel_xmin xid;
BEGIN
  SELECT cv.semantic_profile->'signature', cv.kind, r.xmin INTO sig, pred_kind, rel_xmin
    FROM world.relation r JOIN ontology.concept_version cv ON cv.id = r.predicate_id
   WHERE r.id = NEW.relation_id;
  -- (R-09) A relation is sealed with its creating transaction: participants can
  -- only be inserted together with the relation (cardinality is checked at that
  -- commit), never appended later.
  IF NOT meta.inserted_in_current_xact(rel_xmin) THEN
    RAISE EXCEPTION 'relation % is sealed; participants must be inserted in the transaction that created it',
      NEW.relation_id;
  END IF;
  IF pred_kind <> 'predicate' OR sig IS NULL THEN
    RAISE EXCEPTION 'relation predicate has no registered signature';
  END IF;
  SELECT concept_id INTO role_key FROM ontology.concept_version WHERE id = NEW.role_concept_id AND kind = 'role';
  IF role_key IS NULL OR NOT (sig->'roles') ? role_key THEN
    RAISE EXCEPTION 'role % is not permitted by the predicate signature', COALESCE(role_key, NEW.role_concept_id::text);
  END IF;
  SELECT cv.concept_id INTO kind_key FROM world.entity e JOIN ontology.concept_version cv ON cv.id = e.kind_concept_id
   WHERE e.id = NEW.entity_id;
  IF NOT COALESCE((sig->'roles'->role_key->'entity_kinds') ? kind_key, false) THEN
    RAISE EXCEPTION 'entity kind % cannot fill role %', kind_key, role_key;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER participant_signature BEFORE INSERT ON world.relation_participant
  FOR EACH ROW EXECUTE FUNCTION world.check_participant();

-- Cardinality is checked at commit, after all participants were inserted;
-- both for the relation and (defence in depth) for every participant insert.
CREATE FUNCTION world.assert_relation_cardinality(rel uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE sig jsonb; role_key text; spec jsonb; n integer;
BEGIN
  SELECT cv.semantic_profile->'signature' INTO sig
    FROM world.relation r JOIN ontology.concept_version cv ON cv.id = r.predicate_id WHERE r.id = rel;
  IF sig IS NULL THEN
    RAISE EXCEPTION 'relation % predicate has no registered signature', rel;
  END IF;
  FOR role_key, spec IN SELECT key, value FROM jsonb_each(sig->'roles') LOOP
    SELECT count(*) INTO n FROM world.relation_participant p
      JOIN ontology.concept_version rc ON rc.id = p.role_concept_id
     WHERE p.relation_id = rel AND rc.concept_id = role_key;
    IF n < COALESCE((spec->>'min')::int, 0) OR n > COALESCE((spec->>'max')::int, 2147483647) THEN
      RAISE EXCEPTION 'relation % role % has % participants, outside [%..%]', rel, role_key, n, spec->>'min', spec->>'max';
    END IF;
  END LOOP;
END $$;
CREATE FUNCTION world.check_relation_cardinality() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM world.assert_relation_cardinality(NEW.id);
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER relation_cardinality AFTER INSERT ON world.relation
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION world.check_relation_cardinality();
CREATE FUNCTION world.check_participant_cardinality() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM world.assert_relation_cardinality(NEW.relation_id);
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER participant_cardinality AFTER INSERT ON world.relation_participant
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION world.check_participant_cardinality();

CREATE TABLE world.occurrence (
  entity_id uuid PRIMARY KEY REFERENCES world.entity,
  event_type_id uuid NOT NULL REFERENCES ontology.concept_version,
  support_id uuid NOT NULL REFERENCES world.support,
  component jsonb NOT NULL,
  provenance_activity_id uuid NOT NULL REFERENCES meta.activity
);
"""


def upgrade() -> None:
    op.execute(SQL)
    op.execute(
        immutable(
            "model.version",
            "belief.release",
            "belief.release_artifact",
            "belief.estimate",
            "belief.input",
            "ops.publication",
            "world.relation",
            "world.relation_participant",
            "world.occurrence",
        )
    )


def downgrade() -> None:
    raise NotImplementedError("scientific history migrations are forward-only")
