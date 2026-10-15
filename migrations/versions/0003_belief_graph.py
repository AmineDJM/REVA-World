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
DECLARE r belief.release; o evidence.observation; prior_cutoff timestamptz;
BEGIN
  SELECT rel.* INTO r FROM belief.estimate e JOIN belief.release rel ON rel.id = e.release_id
   WHERE e.id = NEW.estimate_id;
  IF NEW.observation_id IS NOT NULL THEN
    SELECT * INTO o FROM evidence.observation WHERE id = NEW.observation_id;
    -- temporal: only records committed at or before the release cutoff
    IF o.recorded_at > r.knowledge_cutoff THEN
      RAISE EXCEPTION 'observation % recorded after release cutoff', o.id;
    END IF;
    -- (6) eligible only for concepts/scopes approved for that connector version;
    -- evidence.observation_is_eligible is the single authority shared with
    -- belief selection (R5-03): an unknown support year never satisfies a
    -- years-scoped decision.
    IF NOT evidence.observation_is_eligible(o.id, r.knowledge_cutoff) THEN
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
DECLARE r belief.release; bad text;
BEGIN
  -- The identity value is allocated before this trigger runs, so the caller
  -- must hold the publication lock (single bigint key: classid 0, objid
  -- 7262011, objsubid 1) before INSERT for sequence = commit order.
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks WHERE locktype = 'advisory' AND pid = pg_catalog.pg_backend_pid()
                 AND classid = 0 AND objid = 7262011 AND objsubid = 1 AND mode = 'ExclusiveLock' AND granted) THEN
    RAISE EXCEPTION 'publication advisory lock (7262011) not held by this transaction';
  END IF;
  -- (R-19, R5-08) It must also stay held until commit. Re-acquiring the same key
  -- at transaction scope is re-entrant for this backend (never waits, never
  -- warns) and pins the hold to commit even if the caller held it at session
  -- level and releases that early. No unlock probe: it would release a caller's
  -- session hold and log a WARNING on every publication.
  PERFORM pg_catalog.pg_advisory_xact_lock(7262011);
  -- (R5-07) Seal race: estimate/input/artifact/relation writers hold the
  -- release's lock shared (belief.reject_if_published) until they commit; the
  -- publisher waits for them here, and every check below runs in a fresh
  -- READ COMMITTED snapshot that sees their committed rows.
  IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'release publication must run at READ COMMITTED isolation (got %)',
      pg_catalog.current_setting('transaction_isolation');
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('reva.release:' || NEW.release_id::text, 0));
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

-- (R-08, R5-14) published_at is server time; only a session whose login owns
-- the table may supply it (session_user, never current_user: see
-- meta.force_recorded_at). Other supplied values are rejected, not replaced.
CREATE FUNCTION ops.force_published_at() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE server_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_roles r ON r.oid = c.relowner
             WHERE c.oid = TG_RELID AND r.rolname = session_user) THEN
    RETURN NEW;
  END IF;
  IF NEW.published_at IS NOT NULL
     AND abs(extract(epoch FROM (NEW.published_at - server_now))) > 1 THEN
    RAISE EXCEPTION 'published_at is server-generated: a caller-supplied value is not accepted (%)',
      NEW.published_at USING ERRCODE = '42501';
  END IF;
  NEW.published_at := server_now;
  RETURN NEW;
END $$;
CREATE TRIGGER a00_server_published_at BEFORE INSERT ON ops.publication
  FOR EACH ROW EXECUTE FUNCTION ops.force_published_at();

-- (R-02) A published release is sealed: nothing can be appended to it.
-- (R5-07) Writers take the release's lock shared (held until commit) before the
-- check; ops.check_publication takes it exclusively. Either the writer commits
-- first (and the publisher, waiting, validates its rows) or the publication
-- commits first (and the writer, waiting, sees it in a fresh snapshot and
-- fails). Snapshot freshness after the wait requires READ COMMITTED.
CREATE FUNCTION belief.reject_if_published() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rel uuid;
BEGIN
  IF TG_TABLE_NAME = 'input' THEN
    SELECT e.release_id INTO rel FROM belief.estimate e WHERE e.id = NEW.estimate_id;
  ELSE
    rel := NEW.release_id;
  END IF;
  IF rel IS NULL THEN
    RETURN NEW;  -- world.relation without a release (evidence-backed edge)
  END IF;
  IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'rows of release % must be written at READ COMMITTED isolation (got %)',
      rel, pg_catalog.current_setting('transaction_isolation');
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended('reva.release:' || rel::text, 0));
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
-- (R5-07) Release-pinned graph edges are sealed with their release.
CREATE TRIGGER release_sealed BEFORE INSERT ON world.relation
  FOR EACH ROW WHEN (NEW.release_id IS NOT NULL) EXECUTE FUNCTION belief.reject_if_published();
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
