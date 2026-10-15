"""The demo's own in-process executor must not count as a running worker (it made the next demo wait)."""

from typing import Any

import pytest
from sqlalchemy import text

from reva.cli.demo import LOCAL_WORKER_ID, worker_running

pytestmark = pytest.mark.db


def test_demo_executor_presence_is_not_a_running_worker(db: Any) -> None:
    with db.owner.begin() as c:
        c.execute(text("DELETE FROM ops.worker_presence"))
        c.execute(text("INSERT INTO ops.worker_presence(worker_id) VALUES (:w)"), {"w": LOCAL_WORKER_ID})
    assert worker_running(db.worker) is False
    with db.owner.begin() as c:
        c.execute(text("INSERT INTO ops.worker_presence(worker_id) VALUES ('worker-real-1')"))
    assert worker_running(db.worker) is True
    with db.owner.begin() as c:
        c.execute(text("DELETE FROM ops.worker_presence"))
