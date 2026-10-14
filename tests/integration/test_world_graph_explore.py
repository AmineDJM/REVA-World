import json
import pytest
from sqlalchemy import text
from reva.adapters.connectors.registry import builtin_connectors
from reva.evidence.runner import FixtureHttp, run_connector

pytestmark = pytest.mark.db

async def test_explore(db):
    c = builtin_connectors()["un.comtrade.public_exact"]
    rep = await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)
    print(json.dumps({k: v for k, v in rep.as_dict().items() if k != "quality"}, default=str)[:3000])
    with db.owner.connect() as conn:
        for r in conn.execute(text("""SELECT r.layer, r.component, r.existence_uncertainty, sp.temporal_extent, sp.valid_range::text, sp.spatial_spec, sp.resolution, o.value, o.source_flags, c.concept_id
          FROM world.relation r JOIN world.support sp ON sp.id=r.support_id JOIN evidence.observation o ON o.id=r.evidence_observation_id JOIN ontology.concept_version c ON c.id=o.concept_version_id LIMIT 3""")).mappings():
            print(dict(r))
        print(conn.execute(text("SELECT c.concept_id, count(*) FROM evidence.observation o JOIN ontology.concept_version c ON c.id=o.concept_version_id GROUP BY 1")).all())
        print(conn.execute(text("SELECT o.source_flags->>'classificationCode', o.value->'basis'->>'valuation', count(*) FROM evidence.observation o GROUP BY 1,2")).all())
        print(conn.execute(text("SELECT e.label, count(*) FROM world.relation_participant p JOIN world.entity e ON e.id=p.entity_id JOIN world.relation r ON r.id=p.relation_id GROUP BY 1 ORDER BY 2 DESC LIMIT 15")).all())
        print(conn.execute(text("SELECT r.component->>'flow', p.direction, rc.concept_id, count(*) FROM world.relation_participant p JOIN world.relation r ON r.id=p.relation_id JOIN ontology.concept_version rc ON rc.id=p.role_concept_id GROUP BY 1,2,3")).all())
        print(conn.execute(text("SELECT sp.temporal_extent, sp.spatial_spec, o.value, o.source_flags FROM evidence.observation o JOIN world.support sp ON sp.id=o.support_id JOIN ontology.concept_version c ON c.id=o.concept_version_id WHERE c.concept_id like '%world_total' AND o.subject_id=(SELECT entity_id FROM world.alias WHERE provider='iso3166_alpha3' AND external_key='FRA') LIMIT 2")).all())
        print(conn.execute(text("SELECT count(*) FROM world.alias GROUP BY provider, external_key HAVING count(*)>1 LIMIT 3")).all())
        print(conn.execute(text("SELECT a.external_key, e.label FROM world.alias a JOIN world.entity e ON e.id=a.entity_id WHERE provider='comtrade_partner' AND a.entity_id IN (SELECT entity_id FROM world.alias WHERE provider='iso3166_alpha3')")).all())
