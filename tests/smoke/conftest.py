"""Live smoke tests: the tool's live clients, as they are, against recorded exchanges (valxel_sdlc.cassettes).

    pytest tests/smoke                         replay the cassettes (CI, every pull request)
    VALXEL_SMOKE_RECORD=1 pytest tests/smoke   against the dev services; rewrites the cassettes
                                               (VALXEL_SMOKE_GITHUB_TOKEN, VALXEL_SMOKE_GITHUB_REPO,
                                               VALXEL_LLM_GATEWAY_URL, VALXEL_LLM_GATEWAY_API_KEY)
    VALXEL_SMOKE_SEED=1 pytest tests/smoke     against tests/smoke/fakes.py: the first cassettes,
                                               from the services' documented responses

A cassette's `_meta.recorded` says which it is; `seeded` ones are replaced by the first recording.
"""

import importlib.util
import os
from pathlib import Path

import pytest

from valxel_sdlc.cassettes import Cassette

# The live extra (pip install -e ".[dev,live]"): without it, there are no live clients to test.
collect_ignore_glob = [] if importlib.util.find_spec("anthropic") else ["test_*.py"]

HERE = Path(__file__).parent / "cassettes"
RECORD = os.environ.get("VALXEL_SMOKE_RECORD") == "1"
SEED = os.environ.get("VALXEL_SMOKE_SEED") == "1"


@pytest.fixture
def cassette(request):
    c = Cassette(HERE / f"{request.node.name}.json", record=RECORD or SEED)
    c.meta = {"test": request.node.nodeid, "recorded": RECORD,
              **({"seeded": "from the services' documented responses (tests/smoke/fakes.py); "
                            "re-record against the dev services"} if SEED else {})}
    yield c
    c.done()
    c.save()
