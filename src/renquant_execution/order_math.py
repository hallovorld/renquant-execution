"""Pure buy-order sizing math owned by renquant-execution.

OWNERSHIP (S-FRAC v2, D7 gap inventory #1): this module is the single owner
of the cash-cap order-sizing arithmetic. It was first landed on the umbrella
commit path (RenQuant#454) and moved here per that PR's review — the umbrella
is being deprecated and must not gain new execution capability; its
``cap_buy_order_to_cash`` is now a time-bounded compatibility call-site that
delegates to :func:`cap_affordable_qty` and disappears when RunnerAdapter
order math migrates into this repo.

Pure functions only: no broker calls, no I/O, stdlib ``math`` arithmetic on
caller-validated floats. The broker submission rules these sizes must satisfy
(9dp grid, $1 minimum notional, DAY-only TIF) stay pinned in ``broker.py``;
this module reuses ``MIN_FRACTIONAL_NOTIONAL_USD`` from there rather than
duplicating the value.
"""
from __future__ import annotations

import math

from .broker import MIN_FRACTIONAL_NOTIONAL_USD
from .crypto import snap_qty_to_increment

# Fractional order sizing quantizes to the 6-decimal-place quantity grid —
# the renquant-pipeline ``kernel/sizing.py::compute_position_size`` sizing
# convention (finer than needed for the broker's 9dp submission grid, so a
# 6dp size is always submittable). Sizes are FLOORED onto this grid, never
# rounded to nearest: realized notional must stay <= the cash budget.
FRACTIONAL_QTY_GRID = 1_000_000  # 10 ** 6 == 6 decimal places


def cap_affordable_qty(
    price: float,
    cash: float,
    *,
    fractional: bool = False,
    min_fractional_notional: float = MIN_FRACTIONAL_NOTIONAL_USD,
    fee_bps: float = 0.0,
) -> int | float:
    """Largest submittable buy quantity affordable with ``cash`` at ``price``.

    A return of ``0`` (whole-share mode) / ``0.0`` (fractional mode) means
    NO submittable quantity is affordable — the caller must reject the buy
    (the cash-budget-exhausted semantics), never submit a zero or dust order.

    Whole-share mode (``fractional=False``, the default) is the exact legacy
    truncation semantics the umbrella commit path has always had:
    ``int(cash // price)`` shares, and anything below one whole share is a
    reject (returns ``0``). The result is an ``int`` — flag-off callers are
    byte-identity-pinned on both the value and the type.

    Fractional mode (``fractional=True``) floors the affordable quantity onto
    the 6dp sizing grid — ``floor(cash / price * 1e6) / 1e6`` — so a
    cash-capped resize keeps its fractional remainder instead of silently
    snapping to whole shares (the D7 #1 truncation gap). Flooring (never
    round-to-nearest) keeps the realized notional <= ``cash``. A floored
    quantity whose notional lands below ``min_fractional_notional`` (default:
    the ~$1 Alpaca fractional minimum, ``MIN_FRACTIONAL_NOTIONAL_USD``)
    returns ``0.0`` — the fractional analog of the whole-share
    "affordable < 1" reject; the broker would refuse the order anyway, and a
    reject must be explicit rather than a doomed submission.

    ``fee_bps`` (crypto RFC §3.2 E4) makes the size fee-aware: the cash
    budget must cover ``qty * price * (1 + fee_bps/1e4)`` so a taker fee can
    never push the realized outlay past the budget. The default ``0.0``
    leaves every existing (equity, commission-free) caller byte-identical —
    equity order math never gains a fee term; only crypto callers pass a
    non-zero schedule value.

    ``price`` must be finite and positive and ``cash`` finite, else
    ``ValueError`` — sizing on garbage inputs must be loud, not a silent
    zero. Negative ``cash`` is a valid input (an exhausted budget) and
    returns the reject value.
    """
    price_f = float(price)
    cash_f = float(cash)
    fee_f = float(fee_bps)
    if not (math.isfinite(price_f) and price_f > 0.0):
        raise ValueError(f"price must be finite and positive, got {price!r}")
    if not math.isfinite(cash_f):
        raise ValueError(f"cash must be finite, got {cash!r}")
    if not (math.isfinite(fee_f) and fee_f >= 0.0):
        raise ValueError(f"fee_bps must be finite and >= 0, got {fee_bps!r}")
    # fee_bps == 0.0 keeps the historical arithmetic EXACTLY (x * 1.0 == x in
    # IEEE-754): the equity path is byte-identical by construction.
    effective_price = price_f * (1.0 + fee_f / 10_000.0)
    if fractional:
        qty = math.floor(cash_f / effective_price * FRACTIONAL_QTY_GRID) / FRACTIONAL_QTY_GRID
        if qty <= 0.0 or qty * price_f < float(min_fractional_notional):
            return 0.0
        return qty
    shares = int(cash_f // effective_price)
    return shares if shares >= 1 else 0


def cap_affordable_qty_crypto(
    price: float,
    cash: float,
    *,
    min_order_size: float,
    min_trade_increment: float,
    fee_bps: float = 0.0,
) -> float:
    """Largest submittable CRYPTO buy quantity affordable with ``cash``.

    Crypto sizing (crypto RFC §3.2 E4/E6) has no whole-share concept and no
    6dp convention: the affordable quantity — fee-aware, so
    ``qty * price * (1 + fee_bps/1e4) <= cash`` — is floored onto the
    per-pair ``min_trade_increment`` grid (SDK ``Asset`` fields, pinned per
    pair by the strategy repo's snapshot). A floored quantity below
    ``min_order_size`` returns ``0.0``: the broker would refuse the order
    anyway, and a reject must be explicit rather than a doomed submission.

    Never used by equity sizing — equities keep :func:`cap_affordable_qty`
    unchanged.
    """
    price_f = float(price)
    cash_f = float(cash)
    fee_f = float(fee_bps)
    if not (math.isfinite(price_f) and price_f > 0.0):
        raise ValueError(f"price must be finite and positive, got {price!r}")
    if not math.isfinite(cash_f):
        raise ValueError(f"cash must be finite, got {cash!r}")
    if not (math.isfinite(fee_f) and fee_f >= 0.0):
        raise ValueError(f"fee_bps must be finite and >= 0, got {fee_bps!r}")
    min_size_f = float(min_order_size)
    if not (math.isfinite(min_size_f) and min_size_f > 0.0):
        raise ValueError(
            f"min_order_size must be finite and positive, got {min_order_size!r}"
        )
    if cash_f <= 0.0:
        return 0.0
    effective_price = price_f * (1.0 + fee_f / 10_000.0)
    qty = snap_qty_to_increment(cash_f / effective_price, min_trade_increment)
    if qty <= 0.0 or qty < min_size_f:
        return 0.0
    return qty
