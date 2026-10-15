"""The formatter must leave the digest-pinned autonomy template closure byte-identical.

``[tool.ruff.format].exclude`` lists exactly the Python files of
``eurostat_template.template_closure()``; a mismatch would let ``ruff format`` silently change the pinned
``template_sha256`` (disabling T1 auto-promotion) or leave newly added closure modules unprotected.
"""

import tomllib
from pathlib import Path

from reva.adapters.connectors import eurostat_template as et

REPO = Path(__file__).resolve().parents[2]


def test_formatter_exclusions_equal_the_pinned_template_closure() -> None:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    excluded = set(cfg["tool"]["ruff"]["format"]["exclude"])
    closure = {
        "src/" + name.replace(".", "/") + ".py" for name, _ in et.template_closure() if not name.startswith("file:")
    }
    assert excluded == closure
