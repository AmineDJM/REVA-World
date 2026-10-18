"""V8API boards (document validation, rule rules, alert evaluation) and change-feed helpers (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from reva.v8.boards import alerts as A
from reva.v8.boards.models import BoardCreate, BoardUpdate, rule_problem
from reva.v8.changes import feed as F

ITEMS: list[dict[str, Any]] = [
    {"kind": "series", "uid": "i1", "added_at": "t", "concept": "x.c", "geo": "FRA", "label": "Inflation FR",
     "table": "imf.weo"},
    {"kind": "forecast", "uid": "i2", "added_at": "t", "questionId": "fq_0123456789abcdef01234567", "label": "Q"},
    {"kind": "note", "uid": "i3", "added_at": "t", "markdown": "# hello"},
    {"kind": "entity", "uid": "i4", "added_at": "t", "entityKind": "country", "id": "FRA", "label": "France"},
]  # fmt: skip


def test_board_document_validates_like_the_ui_model() -> None:
    b = BoardCreate.model_validate({"title": "Énergie", "items": ITEMS, "rules": []})
    assert [i.kind for i in b.items] == ["series", "forecast", "note", "entity"]
    rules = [{"id": "r1", "kind": "threshold", "item_uid": "i1", "op": ">", "value": "2.5", "created_at": "t"}]
    BoardUpdate.model_validate({"title": "x", "items": ITEMS, "rules": rules, "version": 1})
    with pytest.raises(ValidationError):  # resolution only on forecasts
        BoardCreate.model_validate(
            {"title": "x", "items": ITEMS, "rules": [{"id": "r", "kind": "resolution", "item_uid": "i1",
                                                     "created_at": "t"}]})  # fmt: skip
    with pytest.raises(ValidationError):  # duplicate uid
        BoardCreate.model_validate({"title": "x", "items": [ITEMS[0], ITEMS[0]]})
    with pytest.raises(ValidationError):  # values are never stored in a board: unknown fields refused
        BoardCreate.model_validate({"title": "x", "items": [{**ITEMS[0], "value": "3.1"}]})


def test_rule_problem_mirrors_model_ts() -> None:
    b = BoardCreate.model_validate({"title": "x", "items": ITEMS})
    s, f, n = b.items[0], b.items[1], b.items[2]
    assert rule_problem("threshold", ">", "2,5", s) is not None  # decimal with a dot expected
    assert rule_problem("threshold", None, "2.5", s) == "opérateur manquant"
    assert rule_problem("threshold", ">=", "2.5", s) is None
    assert rule_problem("resolution", None, None, f) is None
    assert rule_problem("new_data", None, None, n) is not None
    assert rule_problem("new_data", None, None, None) == "élément introuvable sur ce tableau"


def _change(kind: str, refs: list[dict[str, str]], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return F.item(kind, f"{kind}:1", datetime(2026, 10, 17, tzinfo=UTC), "t", refs=refs, detail=detail)


def test_alerts_against_the_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "entity_sources", lambda conn, items: {})
    board = {
        "items": ITEMS,
        "rules": [
            {"id": "r1", "kind": "new_data", "item_uid": "i1"},
            {"id": "r2", "kind": "revision", "item_uid": "i1"},
            {"id": "r3", "kind": "resolution", "item_uid": "i2"},
            {"id": "r4", "kind": "threshold", "item_uid": "i1", "op": ">", "value": "2"},
            {"id": "r5", "kind": "new_data", "item_uid": "i4"},
        ],
    }
    changes = [
        _change("ingest", [{"type": "table", "id": "imf.weo"}], {"revised_tables": []}),
        _change("ingest", [{"type": "table", "id": "imf.weo"}, {"type": "geo", "id": "FRA"}],
                {"revised_tables": ["imf.weo"]}),  # fmt: skip
        _change("question_resolved", [{"type": "question", "id": "fq_0123456789abcdef01234567"}]),
        _change("ingest", [{"type": "table", "id": "wb.wdi"}]),
    ]
    out = A.evaluate(None, board, changes)  # type: ignore[arg-type]
    by = {}
    for a in out:
        by.setdefault(a["rule_id"], []).append(a)
    assert len(by["r1"]) == 2 and len(by["r2"]) == 1 and len(by["r3"]) == 1 and len(by["r5"]) == 1
    # no atlas query open: the threshold is reported not evaluable, never compared with 0
    assert by["r4"][0]["status"] == "not_evaluable" and by["r4"][0]["observed"] is None


def test_threshold_uses_latest_observed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "entity_sources", lambda conn, items: {})
    monkeypatch.setattr(
        A, "latest_observed", lambda q, c, g, h: {"value": "2.31", "period": "2024", "source": "imf.weo.PCPIPCH"}
    )
    board = {"items": ITEMS[:1], "rules": [{"id": "r", "kind": "threshold", "item_uid": "i1", "op": ">", "value": "2"}]}
    (a,) = A.evaluate(None, board, [], atlas=lambda: object())  # type: ignore[arg-type]
    assert a["status"] == "fired" and a["observed"]["value"] == "2.31"
    board["rules"][0]["value"] = "3"
    (a,) = A.evaluate(None, board, [], atlas=lambda: object())  # type: ignore[arg-type]
    assert a["status"] == "not_fired"
    monkeypatch.setattr(A, "latest_observed", lambda q, c, g, h: None)
    (a,) = A.evaluate(None, board, [], atlas=lambda: object())  # type: ignore[arg-type]
    assert a["status"] == "not_evaluable" and a["reason"] == "no_observed_value"


def test_cursor_roundtrip_and_item_shape() -> None:
    c = F.encode_cursor("2026-10-17T00:00:00Z", "ingest:x")
    assert F.decode_cursor(c) == ("2026-10-17T00:00:00Z", "ingest:x")
    it = F.item("ingest", "i", datetime(2026, 10, 17, 1, tzinfo=UTC), "T", counts={"rows": 3, "x": None})
    assert it["at"] == "2026-10-17T01:00:00Z" and it["counts"] == {"rows": 3}
    assert set(F.KIND_FR) == set(F.CHANGE_KINDS)


def test_owner_decisions_only_inside_the_window() -> None:
    now = datetime.now(UTC)
    far = F.owner_decisions(now - timedelta(days=3650), now - timedelta(days=3600))
    assert far == []
    items = F.owner_decisions(now - timedelta(days=3650), now + timedelta(days=1))
    assert all(i["kind"] == "owner_decision" and i["severity"] == "action" for i in items)
