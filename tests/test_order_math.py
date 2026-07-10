"""Pins for the cash-cap sizing math (order_math.cap_affordable_qty).

Ported/adapted from the umbrella suite that first pinned this behavior
(RenQuant#454 ``tests/test_runner_execmath*.py``) when the implementation
moved here per that PR's review — renquant-execution owns the math, so it
owns the pins. Two layers, same shape as the originals:

* worked EXAMPLES (the exact D7 #1 gap cases);
* seeded-grid INVARIANTS — no ``hypothesis`` dependency (matches the
  umbrella suite's hermetic-CI reasoning): each property sweeps a
  deterministic 4000-case ``random.Random(SEED)`` grid spanning the
  afford-all / resize / reject regime boundaries, so failures are
  replayable from the printed inputs.

THE flag-off contract: whole-share mode is byte-identical to the frozen
legacy umbrella truncation — same value, same ``int`` type, same reject
boundary — pinned against a verbatim frozen copy over the full grid.
"""
from __future__ import annotations

import math
import random

import pytest

from renquant_execution.broker import MIN_FRACTIONAL_NOTIONAL_USD
from renquant_execution.order_math import FRACTIONAL_QTY_GRID, cap_affordable_qty

SEED = 0x5EED
N = 4000  # cases per property — large enough to exercise the grid corners


def _cases(seed_offset: int, n: int = N):
    """Deterministic stream of (cash, price) pairs spanning the interesting
    regime boundaries (afford-all / resize / reject / exhausted). Same
    generator shape as the umbrella grid this test is ported from."""
    rng = random.Random(SEED + seed_offset)
    for _ in range(n):
        cash = rng.choice([
            0.0, rng.uniform(0, 5), rng.uniform(5, 1_000),
            rng.uniform(1_000, 250_000),
        ])
        price = rng.choice([
            rng.uniform(0.01, 5), rng.uniform(5, 500), rng.uniform(500, 4000),
        ])
        yield cash, price


def _legacy_whole_share_cap(price: float, cash: float) -> int:
    """FROZEN copy of the legacy umbrella whole-share cap semantics
    (``adapters/runner_execmath.py::cap_buy_order_to_cash`` pre-D7 #1):
    ``int(cash // price)`` affordable shares, reject (0) below one share.
    The byte-identity grid below demands whole-share mode return EXACTLY
    this — value and ``int`` type — so flag-off drift fails here, not in
    a live forensic."""
    affordable = int(float(cash) // float(price))
    return affordable if affordable >= 1 else 0


class TestWholeShareMode:
    """Default mode — exact legacy umbrella truncation semantics."""

    def test_worked_examples(self):
        assert cap_affordable_qty(10.0, 55.0) == 5
        assert cap_affordable_qty(100.0, 50.0) == 0  # < 1 share -> reject
        assert cap_affordable_qty(100.0, 100.0) == 1
        assert cap_affordable_qty(3.0, 100.0) == 33

    def test_byte_identical_to_frozen_legacy_over_grid(self):
        """THE flag-off regression pin (4000 seeded cases): default call and
        explicit ``fractional=False`` both reproduce the frozen legacy
        result exactly — value, reject boundary, and ``int`` type."""
        for cash, price in _cases(11):
            want = _legacy_whole_share_cap(price, cash)
            for kwargs in ({}, {"fractional": False}):
                got = cap_affordable_qty(price, cash, **kwargs)
                assert got == want, (cash, price, kwargs, got, want)
                assert type(got) is int, (cash, price, kwargs, type(got))

    def test_negative_cash_is_reject_not_negative_qty(self):
        assert cap_affordable_qty(10.0, -50.0) == 0

    def test_never_overspends_over_grid(self):
        for cash, price in _cases(12):
            qty = cap_affordable_qty(price, cash)
            assert qty * price <= cash + 1e-6, (cash, price, qty)


class TestFractionalMode:
    """S-FRAC v2 stage 2 — 6dp-floored fractional sizing (D7 #1)."""

    def test_capped_to_6dp_floor(self):
        # floor(100/3 * 1e6)/1e6 = 33.333333 — floored, never rounded up.
        assert cap_affordable_qty(3.0, 100.0, fractional=True) == 33.333333

    def test_sub_one_share_slice_is_admitted(self):
        # The exact D7 #1 gap: legacy truncation turned 0.5 affordable
        # shares into 0 -> reject. Fractional mode admits the 0.5 slice.
        qty = cap_affordable_qty(100.0, 50.0, fractional=True)
        assert qty == 0.5
        assert qty * 100.0 == 50.0

    def test_below_min_notional_rejects(self):
        # floor6(0.5/10) = 0.05 shares -> $0.50 notional < the ~$1 broker
        # fractional minimum -> reject (0.0), never a dust order.
        assert cap_affordable_qty(10.0, 0.5, fractional=True) == 0.0

    def test_min_notional_default_is_the_broker_constant(self):
        """The default threshold IS ``broker.MIN_FRACTIONAL_NOTIONAL_USD``
        (imported, not a duplicated literal): notional exactly at the
        constant is admitted, epsilon below it rejects."""
        price = 10.0
        at_min = MIN_FRACTIONAL_NOTIONAL_USD  # $1.00 -> 0.1 shares
        assert cap_affordable_qty(price, at_min, fractional=True) == pytest.approx(
            at_min / price)
        below = MIN_FRACTIONAL_NOTIONAL_USD - 0.01
        assert cap_affordable_qty(price, below, fractional=True) == 0.0

    def test_min_notional_override(self):
        # min=0 admits the dust slice the default rejects ...
        assert cap_affordable_qty(
            10.0, 0.5, fractional=True, min_fractional_notional=0.0) == 0.05
        # ... and a stricter minimum rejects what the default admits.
        assert cap_affordable_qty(
            10.0, 2.0, fractional=True, min_fractional_notional=5.0) == 0.0

    def test_negative_cash_is_reject(self):
        assert cap_affordable_qty(10.0, -50.0, fractional=True) == 0.0

    def test_never_overspends_over_grid(self):
        """The core money-safety invariant: the floored 6dp quantity never
        spends past cash (within the caller's 1e-6 slack)."""
        for cash, price in _cases(21):
            qty = cap_affordable_qty(price, cash, fractional=True)
            assert qty * price <= cash + 1e-6, (
                f"OVERSPEND(frac) cash={cash} price={price} qty={qty}")

    def test_lands_on_6dp_grid_and_clears_min_notional(self):
        """An admitted fractional quantity is EXACTLY the floored-grid value
        (the definition, bit-for-bit) and its notional clears the broker
        minimum. The looser grid-proximity check applies only where a 6dp
        grid point is float-representable (scaled qty < 2^40 or so —
        beyond that, double ulp exceeds the 1e-3 tolerance by construction,
        not by a bug)."""
        for cash, price in _cases(22):
            qty = cap_affordable_qty(price, cash, fractional=True)
            if qty == 0.0:
                continue
            want = math.floor(cash / price * FRACTIONAL_QTY_GRID) / FRACTIONAL_QTY_GRID
            assert qty == want, (cash, price, qty, want)
            scaled = qty * FRACTIONAL_QTY_GRID
            if scaled < 2 ** 40:
                assert abs(scaled - round(scaled)) < 1e-3, (
                    f"off the 6dp grid: {qty} (cash={cash}, price={price})")
            assert qty * price >= MIN_FRACTIONAL_NOTIONAL_USD - 1e-9, (
                f"dust admitted: {qty} x {price}")

    def test_reject_implies_below_min_notional(self):
        """A 0.0 return is justified: the floored affordable notional is
        below the broker minimum (never a spurious reject)."""
        for cash, price in _cases(23):
            qty = cap_affordable_qty(price, cash, fractional=True)
            if qty != 0.0:
                continue
            floored = math.floor(cash / price * FRACTIONAL_QTY_GRID) / FRACTIONAL_QTY_GRID
            assert floored * price < MIN_FRACTIONAL_NOTIONAL_USD, (
                cash, price, floored)

    def test_monotone_nondecreasing_in_cash(self):
        """More budget can only ever buy at least as much — guards against
        a non-monotone rounding regression (e.g. off-by-one in the floor)."""
        rng = random.Random(SEED + 24)
        for _ in range(N):
            price = rng.uniform(0.5, 800)
            c1 = rng.uniform(0, 200_000)
            c2 = c1 + rng.uniform(0, 50_000)  # c2 >= c1
            q1 = cap_affordable_qty(price, c1, fractional=True)
            q2 = cap_affordable_qty(price, c2, fractional=True)
            assert q2 >= q1, (
                f"non-monotone(frac): cash {c1}->{c2} gave qty {q1}->{q2} "
                f"(price={price})")


class TestInputValidation:
    """Garbage sizing inputs must be loud (ValueError), never a silent 0 —
    the caller distinguishes 'bad order' from 'budget exhausted'."""

    @pytest.mark.parametrize("fractional", [False, True])
    def test_bad_price_raises(self, fractional):
        for price in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                cap_affordable_qty(price, 100.0, fractional=fractional)

    @pytest.mark.parametrize("fractional", [False, True])
    def test_non_finite_cash_raises(self, fractional):
        for cash in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                cap_affordable_qty(10.0, cash, fractional=fractional)
