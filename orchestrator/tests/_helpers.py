"""Small helpers shared across the test suite.

Plain functions only. Shared fixtures live in conftest.py (or a
``fixtures_<area>.py`` module that conftest imports), so a test file never has
to import another test file.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_script(path: str | Path, name: str) -> ModuleType:
    """Import a stand-alone script (``scripts/…``, ``monitoring/…``) by file path.

    Works for files without a ``.py`` suffix (``scripts/jess-coder``). The
    module is registered in ``sys.modules`` under ``name`` BEFORE it runs:
    ``@dataclass`` with ``from __future__ import annotations`` looks its module
    up by name. Each call executes the file afresh, so pick a ``name`` unique
    to the test file.
    """
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def metric_value(metric, sample: str = "", **labels: str) -> float:
    """Current value of a prometheus_client metric, summed over matching series.

    ``sample`` picks the sample by suffix: ``""`` is the metric's own value
    (a gauge, or a counter's ``_total``), ``"_count"`` / ``"_sum"`` read a
    histogram or summary (which have no own value: ``sample`` is required).
    ``labels`` filter series by subset match and the matching series are
    summed: ``metric_value(COUNTER)`` is the total over every label set, and a
    label you leave out is not checked, so name every label when the test is
    about which series moved. Reads through ``metric.collect()``, never the
    global registry. A series that was never created reads as 0.0.
    """
    total = 0.0
    for family in metric.collect():
        if not sample and family.type in ("histogram", "summary"):
            raise ValueError(f"{family.name} is a {family.type}: pass sample='_count', '_sum' or '_bucket'")
        wanted = {family.name + sample} if sample else {family.name, family.name + "_total"}
        for s in family.samples:
            if s.name in wanted and all(s.labels.get(k) == str(v) for k, v in labels.items()):
                total += s.value
    return total
