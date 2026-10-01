import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="session")
def sample_registry_data() -> dict:
    """Parsed registry.sample.json (seed data for the store tests)."""
    return json.loads((FIXTURES_DIR / "registry.sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sample_scenes_data() -> dict:
    """Parsed scenes.sample.json (seed data for the store tests)."""
    return json.loads((FIXTURES_DIR / "scenes.sample.json").read_text(encoding="utf-8"))
