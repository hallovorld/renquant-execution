"""Crypto order semantics owned by renquant-execution (crypto RFC D-C1 slice).

Implements the execution-repo additions of the merged crypto trading RFC
(renquant-orchestrator ``doc/design/2026-07-10-crypto-trading-rfc.md`` §2.1
gap table E1-E12, §3.2, §5.1): the asset-class classifier, the crypto TIF
policy (GTC/IOC only — the exact inverse of the equity fractional DAY pin),
the per-pair increment snapshot (min_order_size / min_trade_increment /
price_increment, SDK ``Asset`` fields), the taker/maker fee schedule, and the
explicit no-short guard.

Everything here is a NEW seam next to the existing fractional validators in
``broker.py``. Nothing in this module is consulted by an equity order path:
crypto rules apply only when a symbol is in canonical pair form (``BTC/USD``,
RFC §3.0 symbol policy) or a caller passes ``asset_class="crypto"``
explicitly. Equity behavior stays byte-identical (pinned by regression tests
in ``tests/test_crypto_order_semantics.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
import math
from typing import Any

from .broker import (
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_EQUITY,
    BELOW_MIN_NOTIONAL_STATUS,
    INVALID_CRYPTO_ORDER_STATUS,
    MIN_FRACTIONAL_NOTIONAL_USD,
    PRECISION_EXCEEDS_9DP_STATUS,
    QTY_INTEGRAL_EPS,
    exceeds_9dp,
)

# Alpaca crypto order rules, pinned by the crypto RFC's SDK verification
# (§2.7, alpaca-py 0.43.4):
#   * crypto order types are market / limit / stop_limit ONLY
#     ([VERIFIED] ``alpaca/trading/enums.py:129``; no plain stop, no
#     stop-market — crypto has no LULD);
#   * crypto time-in-force is GTC or IOC ONLY ([VERIFIED]
#     ``alpaca/trading/enums.py:246,249``); DAY is an equity concept and is
#     REJECTED for crypto (E1/E2 — the exact inverse of the equity
#     fractional TIF=DAY pin in ``broker.validate_fractional_order``);
#   * crypto is natively fractional: there is NO whole-share concept and NO
#     fractionable lookup (E5/E6) — sizing lives on the per-pair
#     ``min_trade_increment`` grid instead.
CRYPTO_ORDER_TYPES = frozenset({"market", "limit", "stop_limit"})
CRYPTO_TIME_IN_FORCES = frozenset({"gtc", "ioc"})
#: RFC §3.2 TIF policy: GTC = resting limit / protective stops; IOC =
#: immediate entry. A market order is an immediate entry, so the market
#: default is IOC; the protective stop-limit path pins GTC.
CRYPTO_MARKET_DEFAULT_TIF = "ioc"
CRYPTO_STOP_LIMIT_TIF = "gtc"

# Crypto taker/maker fee schedule defaults, in basis points per side.
# [GUESS: Stage-0 verifies] — the RFC's §2.7 fee row is explicitly
# unverifiable from the SDK (~25 bps taker / ~15 bps maker at tier 0); these
# are config DEFAULTS for sizing/paper math, converted to [VERIFIED] numbers
# by the Stage-0 paper battery's fill receipts, never trusted as ground truth.
DEFAULT_CRYPTO_TAKER_FEE_BPS = 25.0
DEFAULT_CRYPTO_MAKER_FEE_BPS = 15.0

#: Equity commissions are zero on Alpaca — the fee model is crypto-only by
#: construction (E4); equity order math must never gain a fee term.
EQUITY_FEE_BPS = 0.0


def is_crypto_pair(symbol: Any) -> bool:
    """Whether ``symbol`` is a canonical crypto pair (``"BTC/USD"``).

    RFC §3.0 symbol policy: the canonical pair form (slash) is used in
    configs and ALL broker API calls, so a slash in the symbol IS the
    asset-class signal at the execution boundary. Equity tickers can never
    contain ``/``.
    """
    return "/" in str(symbol or "")


def classify_asset_class(symbol: Any, asset_class: str | None = None) -> str:
    """Asset class for an order: explicit ``asset_class`` wins, else the
    pair-form symbol (RFC §3.2 asset-class classifier).

    An explicit-but-inconsistent classification fails loud: declaring an
    equity asset class on a pair-form symbol (or crypto on a plain ticker)
    is a wiring bug, never silently "corrected".
    """
    inferred = ASSET_CLASS_CRYPTO if is_crypto_pair(symbol) else ASSET_CLASS_EQUITY
    if asset_class is None:
        return inferred
    normalized = str(asset_class).strip().lower()
    if normalized not in {ASSET_CLASS_EQUITY, ASSET_CLASS_CRYPTO}:
        raise ValueError(f"unsupported asset_class: {asset_class!r}")
    if normalized != inferred:
        raise ValueError(
            f"asset_class {normalized!r} contradicts symbol {symbol!r} "
            f"(pair-form symbols are crypto, plain tickers are us_equity)"
        )
    return normalized


def validate_crypto_order(
    *,
    order_type: str,
    time_in_force: str,
    qty: float | None = None,
    notional: float | None = None,
) -> tuple[str, str] | None:
    """Validate a crypto order intent (E1/E2 seam, mirror of
    ``broker.validate_fractional_order``).

    Returns ``None`` when the order is submittable, else a
    ``(no_submit_status, reason)`` pair — a violation is never silently
    corrected. The TIF rule is the central E1/E2 assertion: crypto accepts
    GTC/IOC only; DAY (the equity fractional pin) is rejected.
    """
    if (qty is None) == (notional is None):
        return (
            INVALID_CRYPTO_ORDER_STATUS,
            "order must carry exactly one of qty | notional "
            "(both/neither is a broker HTTP 400)",
        )
    order_type_n = str(order_type or "").strip().lower()
    if order_type_n not in CRYPTO_ORDER_TYPES:
        return (
            INVALID_CRYPTO_ORDER_STATUS,
            f"crypto orders support {sorted(CRYPTO_ORDER_TYPES)} order types "
            f"only (SDK crypto matrix), got {order_type!r}",
        )
    tif_n = str(time_in_force or "").strip().lower()
    if tif_n not in CRYPTO_TIME_IN_FORCES:
        return (
            INVALID_CRYPTO_ORDER_STATUS,
            f"crypto orders are TIF GTC|IOC only (E1/E2: DAY is the equity "
            f"fractional pin and does not exist for a 24/7 asset), got "
            f"{time_in_force!r}",
        )
    value = qty if qty is not None else notional
    value_f = float(value)  # type: ignore[arg-type]
    label = "qty" if qty is not None else "notional"
    if not math.isfinite(value_f) or value_f <= 0.0:
        return (
            INVALID_CRYPTO_ORDER_STATUS,
            f"{label} must be finite and positive, got {value!r}",
        )
    if exceeds_9dp(value_f):
        return (
            PRECISION_EXCEEDS_9DP_STATUS,
            f"{label} {value_f!r} exceeds the broker's 9dp grid",
        )
    if notional is not None and value_f < MIN_FRACTIONAL_NOTIONAL_USD:
        # Same $1 broker minimum as the equity notional shape.
        # [GUESS: Stage-0 verifies the crypto notional minimum]
        return (
            BELOW_MIN_NOTIONAL_STATUS,
            f"notional {value_f!r} is below the broker minimum "
            f"${MIN_FRACTIONAL_NOTIONAL_USD}",
        )
    return None


@dataclass(frozen=True)
class CryptoAssetSpec:
    """Per-pair order-grid snapshot (E5/E6/E7 replacement for the equity
    fractionable lookup).

    Field source: SDK ``Asset.min_order_size`` / ``Asset.min_trade_increment``
    / ``Asset.price_increment`` ([VERIFIED] alpaca-py ``trading/models.py:66-68``).
    The strategy repo pins these per pair as an auditable snapshot (RFC §3.1);
    the adapter can also fill them from a live ``get_asset`` lookup.
    """

    symbol: str
    min_order_size: float
    min_trade_increment: float
    price_increment: float

    def __post_init__(self) -> None:
        for name in ("min_order_size", "min_trade_increment", "price_increment"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"CryptoAssetSpec({self.symbol!r}).{name} must be finite "
                    f"and positive, got {value!r}"
                )

    @classmethod
    def from_asset(cls, symbol: str, asset: Any) -> "CryptoAssetSpec":
        """Build a spec from an SDK ``Asset`` row; missing fields fail loud
        (a crypto pair without an order grid is not tradeable, not default-1)."""
        fields: dict[str, float] = {}
        for name in ("min_order_size", "min_trade_increment", "price_increment"):
            raw = getattr(asset, name, None)
            if raw is None:
                raise ValueError(
                    f"asset {symbol!r} has no {name}; refusing to guess a "
                    "crypto order grid"
                )
            fields[name] = float(raw)
        return cls(symbol=str(symbol), **fields)


def snap_qty_to_increment(qty: float, min_trade_increment: float) -> float:
    """Floor ``qty`` onto the ``min_trade_increment`` grid (E6).

    Crypto sizing has NO whole-share concept: quantities are floored DOWN
    onto the per-pair increment grid (never rounded up — realized notional
    must stay <= the intent), using ``Decimal`` so float dust cannot walk the
    result off the broker's grid.
    """
    qty_f = float(qty)
    inc_f = float(min_trade_increment)
    if not math.isfinite(qty_f) or qty_f < 0.0:
        raise ValueError(f"qty must be finite and non-negative, got {qty!r}")
    if not math.isfinite(inc_f) or inc_f <= 0.0:
        raise ValueError(
            f"min_trade_increment must be finite and positive, got {min_trade_increment!r}"
        )
    qty_d = Decimal(str(qty_f))
    inc_d = Decimal(str(inc_f))
    return float((qty_d / inc_d).to_integral_value(rounding=ROUND_FLOOR) * inc_d)


def round_price_to_increment(price: float, price_increment: float) -> float:
    """Round ``price`` to the nearest per-asset ``price_increment`` (E7).

    Replaces the equity 2/4-dp sub-penny rule for crypto: the grid is the
    asset's own ``price_increment``, not a fixed decimal-place convention.
    """
    price_f = float(price)
    inc_f = float(price_increment)
    if not math.isfinite(price_f) or price_f <= 0.0:
        raise ValueError(f"price must be finite and positive, got {price!r}")
    if not math.isfinite(inc_f) or inc_f <= 0.0:
        raise ValueError(
            f"price_increment must be finite and positive, got {price_increment!r}"
        )
    price_d = Decimal(str(price_f))
    inc_d = Decimal(str(inc_f))
    return float((price_d / inc_d).to_integral_value(rounding=ROUND_HALF_UP) * inc_d)


@dataclass(frozen=True)
class CryptoFeeSchedule:
    """Crypto taker/maker fee schedule in bps per side (E4).

    Defaults are the RFC's tier-0 estimate marked [GUESS: Stage-0 verifies]
    (§2.7) — config-driven from the strategy repo in production, empirically
    calibrated from paper-battery fill receipts before any capital decision.
    Equity paths never consult this schedule (equity commissions are zero and
    must stay a no-op — the fee model is crypto-only by construction).
    """

    taker_bps: float = DEFAULT_CRYPTO_TAKER_FEE_BPS
    maker_bps: float = DEFAULT_CRYPTO_MAKER_FEE_BPS

    def __post_init__(self) -> None:
        for name in ("taker_bps", "maker_bps"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"CryptoFeeSchedule.{name} must be finite and >= 0, got {value!r}"
                )

    def fee_bps(self, liquidity: str = "taker") -> float:
        liquidity_n = str(liquidity).strip().lower()
        if liquidity_n == "taker":
            return float(self.taker_bps)
        if liquidity_n == "maker":
            return float(self.maker_bps)
        raise ValueError(f"liquidity must be 'taker' or 'maker', got {liquidity!r}")

    def fee_usd(self, notional: float, liquidity: str = "taker") -> float:
        """Fee in dollars for a fill of ``notional`` (taker = worst case)."""
        notional_f = float(notional)
        if not math.isfinite(notional_f) or notional_f < 0.0:
            raise ValueError(f"notional must be finite and >= 0, got {notional!r}")
        return notional_f * self.fee_bps(liquidity) / 10_000.0


def crypto_no_short_violation(sell_qty: float, held_qty: float) -> str | None:
    """E11 explicit no-short assertion: crypto sell qty <= held qty.

    Returns a human-readable violation reason, or ``None`` when the sell is
    covered. Long-only stays STRUCTURAL for crypto even if equity shorting
    ever lands — no crypto sell may exceed the held quantity.
    """
    sell_f = float(sell_qty)
    held_f = float(held_qty)
    if not math.isfinite(sell_f) or sell_f <= 0.0:
        return f"sell qty must be finite and positive, got {sell_qty!r}"
    if not math.isfinite(held_f) or held_f < 0.0:
        return f"held qty must be finite and >= 0, got {held_qty!r}"
    if sell_f > held_f + QTY_INTEGRAL_EPS:
        return (
            f"crypto is long-only by construction (E11): sell qty {sell_f} "
            f"exceeds held qty {held_f} — no short path exists"
        )
    return None


def assert_crypto_no_short(sell_qty: float, held_qty: float, *, symbol: str = "") -> None:
    """Raise ``ValueError`` on an E11 no-short violation (loud variant)."""
    violation = crypto_no_short_violation(sell_qty, held_qty)
    if violation is not None:
        raise ValueError(f"{symbol or 'crypto order'}: {violation}")


__all__ = [
    "CRYPTO_MARKET_DEFAULT_TIF",
    "CRYPTO_ORDER_TYPES",
    "CRYPTO_STOP_LIMIT_TIF",
    "CRYPTO_TIME_IN_FORCES",
    "CryptoAssetSpec",
    "CryptoFeeSchedule",
    "DEFAULT_CRYPTO_MAKER_FEE_BPS",
    "DEFAULT_CRYPTO_TAKER_FEE_BPS",
    "EQUITY_FEE_BPS",
    "assert_crypto_no_short",
    "classify_asset_class",
    "crypto_no_short_violation",
    "is_crypto_pair",
    "round_price_to_increment",
    "snap_qty_to_increment",
    "validate_crypto_order",
]
