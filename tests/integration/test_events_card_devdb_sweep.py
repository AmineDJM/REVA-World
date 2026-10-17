"""No event card may 500: a read-only sweep of the dev database (APIFIX, docs/dev/v3_apifix.md §1).

``GET /v1/events/icb:crisis:196`` and ``/v1/events/navco21:408`` answered 500 (UICHECK, docs/dev/v3_uicheck.md §4.1):
records recognised by a *marker* dataset kind declared for any source (``source_key: null`` in
``registry/events/taxonomy.yaml``: CAUSDATA, A-CONTENTION, A-ECRISES, A-COLONIAL, CAUSMORE) put ``None`` into
``EventCard.dataset_kind.source_key``. The card now names the source of the marker statement that typed the record.

This test reads the configured database (``REVA_DB_NAME``, the dev DB ``reva`` locally) as ``reva_api`` inside a
read-only transaction and builds the card of one record of every kind present there, exactly as the route does
(``reva.events.view.event_card`` then the route's own response models). Sample:

* every (entity source, entity kind) of ``kg.entity_current`` (events and non-events: a non-event must be a 404,
  never a crash);
* every (claim source, relation) of ``kg.causal_claim_current``, both ends (the UI asks the card of each end);
* every dataset kind of the taxonomy: one record per id prefix, one per (marker source, coded value);
* the ids UICHECK reported, and ``Q8698`` (claims cite it, no KG entity: ``None`` = 404).

Skipped when the database is unreachable or holds no live causal claims (a fresh CI database).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, text

from reva.adapters.postgres.engine import make_engine
from reva.settings import Settings

pytestmark = [pytest.mark.db]

REPORTED = ["icb:crisis:196", "navco21:408", "Q8698", "Q5167679", "Q361", "cow:war:139", "ties:case:1990122101"]


@pytest.fixture(scope="module")
def conn() -> Iterator[Connection]:
    settings = Settings(_env_file=Path(__file__).parents[2] / ".env")  # type: ignore[call-arg]
    if settings.reva_migration_database_url is None:
        pytest.skip("no database configured")
    eng = make_engine("api", settings)
    try:
        c = eng.connect()
    except Exception as e:  # pragma: no cover - environment
        eng.dispose()
        pytest.skip(f"database not reachable: {type(e).__name__}")
    try:
        c.execute(text("SET default_transaction_read_only = on"))
        try:
            n = c.execute(text("SELECT count(*) FROM kg.causal_claim_current WHERE origin = 'live'")).scalar_one()
        except Exception:
            pytest.skip("database has no causal schema")
        if not n:
            pytest.skip("no live causal claims in the configured database (not the dev DB)")
        c.commit()  # keeps the session setting (SET is transactional); every card runs in its own transaction
        assert c.execute(text("SHOW default_transaction_read_only")).scalar_one() == "on"
        c.rollback()
        yield c
    finally:
        c.close()
        eng.dispose()


def _sample(c: Connection) -> list[str]:
    from reva.events.taxonomy import load_taxonomy

    ids: list[str] = list(REPORTED)
    ids += c.execute(
        text("SELECT min(qid) FROM kg.entity_current WHERE origin = 'live' GROUP BY source_key, kind")
    ).scalars()
    for col in ("cause_id", "effect_id"):
        ids += c.execute(
            text(f"SELECT min({col}) FROM kg.causal_claim_current WHERE origin = 'live' GROUP BY source_key, relation")
        ).scalars()
    tax = load_taxonomy()
    for d in tax.dataset_kinds:
        if d.id_prefix:
            like = d.id_prefix.replace("\\", "\\\\").replace("_", r"\_").replace("%", r"\%") + "%"
            ids += c.execute(
                text("SELECT min(qid) FROM kg.entity_current WHERE origin = 'live' AND qid LIKE :p"), {"p": like}
            ).scalars()
        if d.marker_property:
            ids += c.execute(
                text(
                    """SELECT min(subject_qid) FROM kg.statement_current WHERE origin = 'live' AND property_pid = :p
                        GROUP BY source_key, value::text"""
                ),
                {"p": d.marker_property},
            ).scalars()
    return list(dict.fromkeys(i for i in ids if i))


def test_no_event_card_fails_on_the_dev_db(conn: Connection) -> None:
    from reva.api.routes.events import EventCard, EventCardResponse
    from reva.events import view as ev

    ids = _sample(conn)
    conn.rollback()
    assert len(ids) > 50, ids
    failures: dict[str, str] = {}
    cards = nulls = 0
    marker_sources: set[str] = set()
    t0 = time.monotonic()
    for eid in ids:
        try:
            with conn.begin():
                card = ev.event_card(conn, eid, origin="live")
                if card is None:
                    nulls += 1
                    continue
                model = EventCard(**card)
                EventCardResponse(data=model, context_mode="evidence", origin="live", modality="reported_observation")
                cards += 1
                if model.dataset_kind is not None and not model.dataset_kind.id_prefix:
                    marker_sources.add(model.dataset_kind.source_key)
        except Exception as e:  # the sweep collects every failure
            failures[eid] = f"{type(e).__name__}: {str(e)[:300]}"
    elapsed = time.monotonic() - t0
    assert not failures, failures
    assert cards > 30, (cards, nulls)
    # marker-recognised records name the source that typed them (the 500 of icb:crisis:196 / navco21:408)
    assert {"icb.data", "harvard.dataverse.navco"} <= marker_sources, marker_sources
    print(f"event cards: {len(ids)} ids, {cards} cards, {nulls} not events, {elapsed:.1f} s")


def test_reported_cards_name_their_source(conn: Connection) -> None:
    from reva.events import view as ev

    with conn.begin():
        icb = ev.event_card(conn, "icb:crisis:196", origin="live")
        navco = ev.event_card(conn, "navco21:408", origin="live")
        depression = ev.event_card(conn, "Q8698", origin="live")
    assert icb is not None and icb["dataset_kind"]["source_key"] == "icb.data"
    assert navco is not None and navco["dataset_kind"]["source_key"] == "harvard.dataverse.navco"
    if depression is not None:  # becomes a card once the KG holds Q8698 (APIFIX §6)
        assert depression["id"] == "Q8698"
