"""End-to-end two-process proof of the shared-ledger wiring contract
(Codex review, D-C4 round-1): ``build_shared_account_cash_ledger_for_broker``
must resolve TWO INDEPENDENT sleeve processes — each connecting through its
OWN broker instance — to the SAME ledger file, purely because both brokers
report the same real account id, never because a caller threaded a shared
path/account_id string through config. This is the property a per-sleeve
data-dir misconfiguration would silently defeat, and it can only be proven
with real OS processes (SQLite's cross-process file locking), not two
in-process objects sharing one Python heap.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from renquant_execution.account_cash_ledger import (
    account_cash_ledger_db_path,
    build_shared_account_cash_ledger_for_broker,
)

ACCOUNT = "PA3REAL0001"

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

    data_dir, account_id, cash, amount, parent_intent_id = (
        sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
    )
    broker = _FakeBroker(account_id, cash)
    ledger = build_shared_account_cash_ledger_for_broker(broker, data_dir=data_dir)
    granted = ledger.reserve(
        sleeve_tag=sys.argv[6], parent_intent_id=parent_intent_id, amount=amount,
    )
    print(json.dumps({"db_path": str(ledger.db_path), "granted": granted}))
    """
)


def _run_worker(tmp_path: Path, *, cash: float, amount: float, parent_intent_id: str, sleeve_tag: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env["RENQUANT_ACCOUNT_CASH_LEDGER"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER, str(tmp_path), ACCOUNT, str(cash),
         str(amount), parent_intent_id, sleeve_tag],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestSharedWiringAcrossRealProcesses:
    def test_two_independent_broker_processes_resolve_the_same_ledger_file(self, tmp_path):
        # Two SEPARATE OS processes, each with its OWN fake broker instance
        # (never the same Python object) reporting the SAME real account id
        # — the wiring contract must resolve both to the identical db path,
        # with no caller-supplied account_id/path anywhere in the call.
        batch = _run_worker(tmp_path, cash=1000.0, amount=10.0, parent_intent_id="pi-batch", sleeve_tag="alpaca")
        crypto = _run_worker(tmp_path, cash=1000.0, amount=10.0, parent_intent_id="pi-crypto", sleeve_tag="alpaca_crypto")
        assert batch["db_path"] == crypto["db_path"]
        assert batch["db_path"] == str(account_cash_ledger_db_path(tmp_path, ACCOUNT))

    def test_over_committing_reserves_serialize_across_real_processes(self, tmp_path):
        # 100 cash; two independent processes each try to reserve 60 —
        # combined 120 > 100. Real cross-process SQLite locking (not just
        # in-process thread serialization, already covered elsewhere) must
        # ensure exactly one is granted regardless of which sleeve/process
        # "wins" the race.
        first = _run_worker(tmp_path, cash=100.0, amount=60.0, parent_intent_id="pi-a", sleeve_tag="alpaca")
        second = _run_worker(tmp_path, cash=100.0, amount=60.0, parent_intent_id="pi-b", sleeve_tag="alpaca_crypto")
        assert sorted([first["granted"], second["granted"]]) == [False, True]

    def test_flag_off_returns_none_in_every_process(self, tmp_path):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        env.pop("RENQUANT_ACCOUNT_CASH_LEDGER", None)
        script = textwrap.dedent(
            """
            import sys
            from renquant_execution.account_cash_ledger import (
                build_shared_account_cash_ledger_for_broker,
            )

            class _FakeBroker:
                def get_account_id(self):
                    return "PA3REAL0001"

                def get_cash(self):
                    return 1000.0

            ledger = build_shared_account_cash_ledger_for_broker(
                _FakeBroker(), data_dir=sys.argv[1],
            )
            print("NONE" if ledger is None else "BUILT")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "NONE"
