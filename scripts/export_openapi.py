"""Write the API's OpenAPI schema to frontend/openapi.json, the contract the dashboard is typed
against. A backend test fails when the committed file is stale, so the two cannot drift.

  python scripts/export_openapi.py          # rewrite the file
  python scripts/export_openapi.py --check  # exit 1 if it is out of date
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "backend")

from app.api.main import create_app  # noqa: E402
from app.config import Settings  # noqa: E402

TARGET = Path(__file__).resolve().parent.parent / "frontend" / "openapi.json"


def render() -> str:
    app = create_app(
        Settings(database_url="sqlite://", enable_api_docs=True, environment="development")
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    fresh = render()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text() != fresh:
            print(f"{TARGET} is stale; run: python scripts/export_openapi.py")
            sys.exit(1)
        print("openapi.json is up to date")
    else:
        TARGET.write_text(fresh)
        print(f"wrote {TARGET}")
