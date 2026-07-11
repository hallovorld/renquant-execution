"""End-to-end two-process proof of the shared-ledger wiring contract
(Codex review, D-C4 round-1/round-2): ``build_shared_account_cash_ledger_for_broker``
must resolve TWO INDEPENDENT sleeve processes — each connecting through its
OWN broker instance — to the SAME ledger file, purely because both brokers
report the same real account id AND the data root is resolved by a single
non-caller-supplied function, never because a caller threaded a shared
path/account_id string through config. Round-1 derived account_id from the
broker but still accepted an arbitrary ``data_dir`` argument (Codex correctly
flagged this as still allowing two sleeves to diverge onto independent
per-account databases); round-2 removes ``data_dir`` from the function
signature entirely — there is no parameter through which a per-sleeve path
could be threaded, by construction, not by convention. This can only be
proven with real OS processes (SQLite's cross-process file locking), not two
in-process objects sharing one Python heap.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from renquant_execution.account_cash_ledger import (
    ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE,
    account_cash_ledger_data_dir,
    account_cash_ledger_db_path,
    build_shared_account_cash_ledger_for_broker,
)

ACCOUNT = "PA3REAL0001"


class _FakeBroker:
    def __init__(self, account_id="PA3REAL0001", cash=1000.0):
        self._account_id = account_id
        self._cash = cash

    def get_account_id(self):
        return self._account_id

    def get_cash(self):
        return self._cash


def test_data_dir_is_not_an_accepted_parameter():
    """Round-2 (Codex re-review of cba1dd9): the API surface itself must
    make a per-sleeve path structurally impossible to pass — not merely
    discouraged by convention. Passing data_dir is a TypeError, not a
    silently-honored override."""
    with pytest.raises(TypeError, match="data_dir"):
        build_shared_account_cash_ledger_for_broker(
            _FakeBroker(), data_dir="/tmp/some-per-sleeve-path"  # noqa: S108
        )


_WORKER = textwrap.dedent(
    """
    import json, sys
    from renquant_execution.account_cash_ledger import (
        build_shared_account_cash_ledger_for_broker,
    )

    class _FakeBroker:
        \"\"\"A distinct object per process — simulating a sleeve's OWN
        broker connection, never a shared Python instance.\"\"\"

        def __init__(self, account_id, cash):
            self._account_id = account_id
            self._cash = cash

        def get_account_id(self):
            return self._account_id

        def get_cash(self):
            return self._cash

    account_id, cash, amount, parent_intent_id, sleeve_tag = (
        sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4], sys.argv[5]
    )
    broker = _FakeBroker(account_id, cash)
    ledger = build_shared_account_cash_ledger_for_broker(broker)
    granted = ledger.reserve(
        sleeve_tag=sleeve_tag, parent_intent_id=parent_intent_id, amount=amount,
    )
    print(json.dumps({"db_path": str(ledger.db_path), "granted": granted}))
    """
)


def _run_worker(
    *, cash: float, amount: float, parent_intent_id: str, sleeve_tag: str,
    data_dir_override: "str | None" = None, extra_env: "dict | None" = None,
) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env["RENQUANT_ACCOUNT_CASH_LEDGER"] = "1"
    if data_dir_override is not None:
        env[ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE] = data_dir_override
    else:
        env.pop(ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE, None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER, ACCOUNT, str(cash), str(amount),
         parent_intent_id, sleeve_tag],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestSharedWiringAcrossRealProcesses:
    def test_two_independent_broker_processes_resolve_the_same_ledger_file(self, tmp_path):
        # Two SEPARATE OS processes, each with its OWN fake broker instance
        # (never the same Python object) reporting the SAME real account id
        # — the wiring contract must resolve both to the identical db path,
        # with NO caller-supplied account_id/path anywhere in the call.
        override = str(tmp_path)
        batch = _run_worker(cash=1000.0, amount=10.0, parent_intent_id="pi-batch",
                             sleeve_tag="alpaca", data_dir_override=override)
        crypto = _run_worker(cash=1000.0, amount=10.0, parent_intent_id="pi-crypto",
                              sleeve_tag="alpaca_crypto", data_dir_override=override)
        assert batch["db_path"] == crypto["db_path"]
        assert batch["db_path"] == str(account_cash_ledger_db_path(tmp_path, ACCOUNT))

    def test_over_committing_reserves_serialize_across_real_processes(self, tmp_path):
        # 100 cash; two independent processes each try to reserve 60 —
        # combined 120 > 100. Real cross-process SQLite locking (not just
        # in-process thread serialization, already covered elsewhere) must
        # ensure exactly one is granted regardless of which sleeve/process
        # "wins" the race.
        override = str(tmp_path)
        first = _run_worker(cash=100.0, amount=60.0, parent_intent_id="pi-a",
                             sleeve_tag="alpaca", data_dir_override=override)
        second = _run_worker(cash=100.0, amount=60.0, parent_intent_id="pi-b",
                              sleeve_tag="alpaca_crypto", data_dir_override=override)
        assert sorted([first["granted"], second["granted"]]) == [False, True]

    def test_unrelated_per_sleeve_env_divergence_cannot_move_the_ledger_path(self, tmp_path, monkeypatch):
        # Round-2 proof: the OLD vulnerable vector was a caller-supplied
        # data_dir; the data root is now resolved by account_cash_ledger_
        # data_dir(), which consults NOTHING but the one override hook. Two
        # sleeves with deliberately DIFFERENT RENQUANT_REPO_ROOT / cwd /
        # RENQUANT_SUBREPO_ROOT (all real per-deployment variables elsewhere
        # in this codebase) must still resolve to the IDENTICAL canonical
        # path — proving that vector is closed, not just less likely. HOME
        # is pinned to a tmp dir (shared by both workers, standing in for
        # "the one real machine both sleeves run on") so this test never
        # touches the actual operator's home directory.
        monkeypatch.delenv(ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE, raising=False)
        fake_home = str(tmp_path / "home")
        batch = _run_worker(
            cash=1000.0, amount=10.0, parent_intent_id="pi-batch-env", sleeve_tag="alpaca",
            extra_env={"HOME": fake_home,
                       "RENQUANT_REPO_ROOT": str(tmp_path / "batch_repo_root"),
                       "RENQUANT_SUBREPO_ROOT": str(tmp_path / "batch_subrepo")},
        )
        crypto = _run_worker(
            cash=1000.0, amount=10.0, parent_intent_id="pi-crypto-env", sleeve_tag="alpaca_crypto",
            extra_env={"HOME": fake_home,
                       "RENQUANT_REPO_ROOT": str(tmp_path / "crypto_repo_root_DIFFERENT"),
                       "RENQUANT_SUBREPO_ROOT": str(tmp_path / "crypto_subrepo_DIFFERENT")},
        )
        assert batch["db_path"] == crypto["db_path"]
        assert batch["db_path"] == str(
            account_cash_ledger_db_path(
                Path(fake_home) / ".renquant" / "account_cash_ledger", ACCOUNT
            )
        )

    def test_flag_off_returns_none_in_every_process(self, tmp_path):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        env.pop("RENQUANT_ACCOUNT_CASH_LEDGER", None)
        env[ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE] = str(tmp_path)
        script = textwrap.dedent(
            """
            from renquant_execution.account_cash_ledger import (
                build_shared_account_cash_ledger_for_broker,
            )

            class _FakeBroker:
                def get_account_id(self):
                    return "PA3REAL0001"

                def get_cash(self):
                    return 1000.0

            ledger = build_shared_account_cash_ledger_for_broker(_FakeBroker())
            print("NONE" if ledger is None else "BUILT")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "NONE"


class TestCanonicalDataDirResolver:
    def test_override_hook_wins(self, tmp_path):
        env = {ACCOUNT_CASH_LEDGER_DATA_DIR_OVERRIDE: str(tmp_path)}
        assert account_cash_ledger_data_dir(env=env) == tmp_path.resolve()

    def test_default_is_fixed_home_scoped_path_independent_of_repo_root(self):
        env = {"RENQUANT_REPO_ROOT": "/some/other/deployment/root"}
        assert account_cash_ledger_data_dir(env=env) == (
            Path.home() / ".renquant" / "account_cash_ledger"
        )
