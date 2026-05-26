from __future__ import annotations

import importlib
import sys


def test_execution_import_does_not_pull_training_modules() -> None:
    importlib.import_module("renquant_execution")

    forbidden_prefixes = (
        "renquant_model_gbdt",
        "renquant_model_patchtst",
        "torch",
        "xgboost",
    )
    offenders = sorted(
        name for name in sys.modules
        if name in forbidden_prefixes or name.startswith(forbidden_prefixes)
    )
    assert offenders == []
