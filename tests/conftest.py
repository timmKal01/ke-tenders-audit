import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ke_tenders_audit.audit_log import LOG_DIR  # noqa: E402
from ke_tenders_audit.cases import CASES_DIR  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def remove_test_cases():
    yield
    for folder in CASES_DIR.glob("test-*"):
        shutil.rmtree(folder, ignore_errors=True)
    for log in LOG_DIR.glob("test-*.jsonl"):
        log.unlink(missing_ok=True)
