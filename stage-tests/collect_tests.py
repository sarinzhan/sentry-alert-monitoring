"""Dumps the test inventory as JSON: node id, docstring, flow/step link, markers.

Run standalone (`python collect_tests.py`) or by server.py; pytest's own
collection output is swallowed so stdout is pure JSON.
"""

import contextlib
import inspect
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent


def collect() -> list[dict]:
    tests: list[dict] = []

    class Collector:
        def pytest_collection_finish(self, session):
            for item in session.items:
                flow_marker = item.get_closest_marker("flow")
                needs_marker = item.get_closest_marker("needs")
                func = getattr(item, "function", None)
                tests.append({
                    "node_id": item.nodeid,
                    "name": item.name,
                    "doc": inspect.getdoc(func) or "",
                    "flow": flow_marker.args[0] if flow_marker and flow_marker.args else None,
                    "step": flow_marker.kwargs.get("step") if flow_marker else None,
                    "needs": list(needs_marker.args) if needs_marker else [],
                    "destructive": item.get_closest_marker("destructive") is not None,
                })

    with contextlib.redirect_stdout(io.StringIO()):
        rc = pytest.main(
            ["--collect-only", "-q", "--override-ini=addopts=", str(ROOT)],
            plugins=[Collector()],
        )
    if rc not in (0, 5):  # 5 = no tests collected
        raise SystemExit(rc)
    return tests


if __name__ == "__main__":
    json.dump(collect(), sys.stdout, ensure_ascii=False)
