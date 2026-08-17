"""O2C 验证语料生成器：中立台账 → 真值 → 双向投递。

用法见 ``deploy/benchmark/README.md`` 步骤 3，配方见 ``docs/BENCHMARK_DATA_PREP.md`` §3。
"""

from __future__ import annotations

from .config import DEFAULT, Recipe
from .ledger import Ledger, build_ledger
from .truth import build_truth, dump_truth

__all__ = ["DEFAULT", "Recipe", "Ledger", "build_ledger", "build_truth", "dump_truth"]
