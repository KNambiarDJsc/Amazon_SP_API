"""The dashboard is typed against frontend/openapi.json; it must describe the API as it is."""

import json
from pathlib import Path

from app.api.main import create_app
from app.config import Settings

COMMITTED = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"


def test_committed_openapi_contract_matches_the_api():
    live = create_app(Settings(database_url="sqlite://", enable_api_docs=True)).openapi()
    committed = json.loads(COMMITTED.read_text())
    assert committed == live, (
        "frontend/openapi.json is stale: run `python scripts/export_openapi.py`, then "
        "`npm run gen:api` in frontend/ and commit both"
    )
