"""Python mirror of ``v8Links`` (ui/src/v8/contract.ts): the console's stable deep-link scheme."""

from __future__ import annotations

from urllib.parse import quote, urlencode


def _enc(s: str) -> str:
    # encodeURIComponent keeps A-Z a-z 0-9 - _ . ! ~ * ' ( )
    return quote(s, safe="-_.!~*'()")


def entity(kind: str, id_: str) -> str:
    return f"/v8/fiche/{_enc(kind)}/{_enc(id_)}"


def series(table: str, params: dict[str, str] | None = None) -> str:
    return f"/v8/monde?{urlencode({'table': table, **(params or {})})}"


def ask(q: str) -> str:
    return f"/v8/demander?{urlencode({'q': q})}"


def decision(ref: str | None = None) -> str:
    return f"/v8/decision?{urlencode({'ref': ref})}" if ref else "/v8/decision"


def scoreboard(track: str | None = None) -> str:
    return f"/v8/scores?track={_enc(track)}" if track else "/v8/scores"


def path(from_: str, to: str) -> str:
    return f"/v8/liens?{urlencode({'from': from_, 'to': to})}"


def board(id_: str) -> str:
    return f"/v8/tableaux/{_enc(id_)}"
