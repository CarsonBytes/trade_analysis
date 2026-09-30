"""Session-wide hermetic env for `pytest` runs (3.3).

conftest.py is imported by pytest before any test module, so the isolation in
dashboard/tests/hermetic.py is in force for every test without each module
having to remember it. Script-style runs (`python -m dashboard.tests.X`) get
no conftest, hence the explicit import in the modules that can reach
log_usage() / the host journal.
"""
import pathlib
import sys

_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dashboard.tests import hermetic  # noqa: E402  (applies on import)

hermetic.apply()
