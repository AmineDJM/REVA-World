"""Builder of ``registry/entities/country_codes_extra.json`` (extra provider code schemes).

The ALLC crosswalk (``country_crosswalk.json``) stays as is. Extra schemes are derived only
from archived provider reference bytes or quoted provider documentation:

* ``fips`` — FIPS 10-4 country codes from the GeoNames ``countryInfo.txt`` dump (column
  ``fips``; CC BY 4.0), archived under ``registry/atlas/reference/``: a code maps when its
  row's ISO3 is a crosswalk entity key.
* ``eurostat`` — ISO 3166 alpha-2 except Greece ``EL``, United Kingdom ``UK`` and Kosovo
  ``XK`` (Eurostat Glossary: Country codes, quoted below); ``GR``/``GB`` are excluded.

Provider-specific non-ISO codes for other providers (e.g. a Kosovo code used by one
provider) are added by the atlas agent that observed them, as a scheme with ``base: iso3``
in ``registry/atlas/geo_aliases/<agent>.yaml`` with its evidence — never guessed here.

Run ``uv run python -m reva.atlas.country_codes`` to rebuild; the unit test re-derives the
file from the archive and compares.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from reva.ontology.registry import REGISTRY_ROOT

GEONAMES_FILE = REGISTRY_ROOT / "atlas" / "reference" / "geonames_countryInfo.txt"
GEONAMES_URL = "https://download.geonames.org/export/dump/countryInfo.txt"
GEONAMES_RETRIEVED_AT = "2026-10-15T22:07:54Z"
EUROSTAT_GLOSSARY = {
    "url": "https://ec.europa.eu/eurostat/statistics-explained/index.php?title=Glossary:Country_codes",
    "retrieved_at": "2026-10-15T22:08:10Z",
    "page_sha256": "21f838afc2c5fb60142cdb70e2e23887a1f3d558fd2bce5c613ca5bb59272c17",
    "quotes": ["Greece (EL)", "United Kingdom (UK)", "Kosovo* (XK"],
}
OUT = REGISTRY_ROOT / "entities" / "country_codes_extra.json"


def fips_from_geonames(data: bytes, keys: set[str]) -> tuple[dict[str, str], list[dict[str, str]]]:
    mapped: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    for line in data.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        iso2, iso3, fips, name = cols[0], cols[1], cols[3], cols[4]
        if not fips:
            continue
        if iso3 not in keys:
            skipped.append({"fips": fips, "geonames_iso3": iso3, "name": name, "reason": "iso3 not a crosswalk entity"})
            continue
        if fips in mapped and mapped[fips] != iso3:
            raise ValueError(f"GeoNames fips {fips} maps to {mapped[fips]} and {iso3}")
        mapped[fips] = iso3
        _ = iso2
    return dict(sorted(mapped.items())), skipped


def build(root: Path = REGISTRY_ROOT) -> dict[str, Any]:
    crosswalk = json.loads((root / "entities" / "country_crosswalk.json").read_text())
    keys = {e["key"] for e in crosswalk["entities"]}
    data = GEONAMES_FILE.read_bytes()
    fips, skipped = fips_from_geonames(data, keys)
    return {
        "schema": "reva-country-codes-extra/1",
        "review_status": "machine_derived_pending_maintainer_review",
        "built_by": "reva.atlas.country_codes (V3, agent ATLASCORE)",
        "note": "Extends registry/entities/country_crosswalk.json (unchanged). Targets are crosswalk entity keys.",
        "schemes": {
            "eurostat": {
                "base": "iso2",
                "exclude": ["GR", "GB"],
                "aliases": {"EL": "GRC", "UK": "GBR", "XK": "XKX"},
                "source": {"kind": "provider_documentation", **EUROSTAT_GLOSSARY},
            },
            "fips": {
                "base": None,
                "aliases": fips,
                "source": {
                    "kind": "archived_reference",
                    "file": "registry/atlas/reference/geonames_countryInfo.txt",
                    "url": GEONAMES_URL,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "retrieved_at": GEONAMES_RETRIEVED_AT,
                    "licence": "CC BY 4.0 (GeoNames)",
                    "column": "fips",
                    "skipped": skipped,
                },
            },
        },
    }


def main() -> None:  # pragma: no cover - maintenance entry point
    OUT.write_text(json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":  # pragma: no cover
    main()
