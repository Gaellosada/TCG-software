"""Per-root futures/option contract multipliers for futures-notional option sizing.

SIGNED OFF 2026-07-07 (``workspace/tasks/option-futures-notional-sizing/output/
multiplier-config-signed-off.md``).

Semantics: the CONSUMER reads a live ``dim_instrument.contract_size`` FIRST (per
contract, both FUT and OPT).  This table is the FALLBACK, used only when the live
value is NULL.  The ``verified`` flag mirrors the ``STRIKE_FACTOR_VERIFIED`` pattern
(``tcg.data.options._strike_factor``) so provisional roots (BTC/ETH/FX) are visibly
unconfirmed.

``m_opt`` is the OPTION contract multiplier — it turns the option premium move
(in POINTS) into a dollar P&L.  ``m_fut`` is the FUTURES contract multiplier — it
turns the reference future price (in POINTS) into the notional that is the sizing
DENOMINATOR.  They DIFFER for VIX (option=100 on the VIX index, future=1000 on the
VX future); getting that split right is the whole point of the ``m_fut != m_opt``
distinction — a hardcoded single multiplier would mis-size VIX by 10x.

This module lives in ``tcg.types`` (no dependencies) so both ``tcg.engine`` (the
sizing formula) and ``tcg.core`` (the live-first resolution) can consume it without
crossing an import-linter boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RootMultipliers:
    """Signed-off fallback multipliers for one root."""

    m_fut: float
    m_opt: float
    verified: bool


# Keyed by ROOT (the collection with its ``OPT_`` / ``FUT_`` prefix stripped —
# both share the same root, e.g. ``OPT_SP_500`` and ``FUT_SP_500`` → ``SP_500``).
FUTURES_NOTIONAL_MULTIPLIERS: dict[str, RootMultipliers] = {
    "SP_500": RootMultipliers(m_fut=50.0, m_opt=50.0, verified=True),
    "NASDAQ_100": RootMultipliers(m_fut=20.0, m_opt=20.0, verified=True),
    "GOLD": RootMultipliers(m_fut=100.0, m_opt=100.0, verified=True),
    # The ONLY root where fut and opt multipliers differ: VX future = $1000/pt,
    # VIX-index option = $100/pt (Cboe-confirmed).
    "VIX": RootMultipliers(m_fut=1000.0, m_opt=100.0, verified=True),
    # Provisional (user sign-off; contract_size NULL in dwh so the fallback bites).
    "BTC": RootMultipliers(m_fut=5.0, m_opt=5.0, verified=False),
    "ETH": RootMultipliers(m_fut=50.0, m_opt=50.0, verified=False),
    # FX: valid ONLY if the dwh price series is raw USD/EUR (resp. USD/JPY).  If the
    # dwh price is rescaled the multiplier must be rescaled by the same factor —
    # hence unverified until the price scale is confirmed against a live row.
    "EURUSD": RootMultipliers(m_fut=125_000.0, m_opt=125_000.0, verified=False),
    "JPYUSD": RootMultipliers(m_fut=12_500_000.0, m_opt=12_500_000.0, verified=False),
    "T_NOTE_10_Y": RootMultipliers(m_fut=1000.0, m_opt=1000.0, verified=True),
    "T_BOND": RootMultipliers(m_fut=1000.0, m_opt=1000.0, verified=True),
}


def root_from_collection(collection: str) -> str:
    """Strip the ``OPT_`` / ``FUT_`` prefix to the shared root.

    ``OPT_SP_500`` → ``SP_500``; ``FUT_VIX`` → ``VIX``.  A collection without a
    known prefix is returned unchanged (so a bare root also works).
    """
    for prefix in ("OPT_", "FUT_"):
        if collection.startswith(prefix):
            return collection[len(prefix) :]
    return collection


def futures_collection_for_option(option_collection: str) -> str:
    """Map an ``OPT_<root>`` collection to its ``FUT_<root>`` twin BY NAME.

    Guardrail Sign 3: derive the futures reference by name substitution, NOT via
    ``dim_instrument.underlying_id`` (which points at messy weekly roots).
    """
    return f"FUT_{root_from_collection(option_collection)}"


@dataclass(frozen=True)
class ResolvedMultipliers:
    """Outcome of live-first / config-fallback multiplier resolution for one root.

    ``m_fut`` / ``m_opt`` are ``NaN`` when a value could NOT be sourced (neither a
    live ``contract_size`` nor a config entry) — the consumer must then apply the
    tail policy (carry-forward + surfaced diagnostic), NEVER a silent ``1.0``.
    """

    m_fut: float
    m_opt: float
    m_fut_source: str  # "live" | "config" | "missing"
    m_opt_source: str  # "live" | "config" | "missing"
    verified: bool
    diagnostic: str | None

    @property
    def is_complete(self) -> bool:
        """True iff both multipliers were sourced (finite & > 0)."""
        return (
            math.isfinite(self.m_fut)
            and self.m_fut > 0.0
            and math.isfinite(self.m_opt)
            and self.m_opt > 0.0
        )


def _finite_positive(v: float | None) -> bool:
    return v is not None and math.isfinite(v) and v > 0.0


def resolve_multipliers(
    root: str,
    *,
    live_m_fut: float | None = None,
    live_m_opt: float | None = None,
) -> ResolvedMultipliers:
    """Combine a live ``contract_size`` read (if any) with the signed-off config.

    Rule (Guardrail Sign 2): live value FIRST when finite & > 0; else the per-root
    config fallback; else ``NaN`` + a diagnostic (never a silent ``1.0``).  A live
    value that DISAGREES with the config is used verbatim (live wins) but the
    disagreement is surfaced in ``diagnostic`` (do not silently override live).

    ``root`` is the bare root (``SP_500`` / ``VIX``); pass ``root_from_collection``
    output.  Pure — no I/O — so the caller performs the live ``contract_size`` read
    and hands the value in.
    """
    cfg = FUTURES_NOTIONAL_MULTIPLIERS.get(root)
    diags: list[str] = []

    def _one(name: str, live: float | None, cfg_val: float | None) -> tuple[float, str]:
        if _finite_positive(live):
            src = "live"
            val = float(live)  # type: ignore[arg-type]
            if cfg_val is not None and abs(cfg_val - val) > 1e-9:
                diags.append(
                    f"{name} live={val:g} disagrees with signed-off config="
                    f"{cfg_val:g} for root {root!r} (using live)"
                )
            return val, src
        if cfg_val is not None:
            return float(cfg_val), "config"
        diags.append(
            f"no {name} multiplier for root {root!r} (neither a live contract_size "
            f"nor a signed-off config entry) — tail carry-forward applies"
        )
        return math.nan, "missing"

    m_fut, src_fut = _one("m_fut", live_m_fut, None if cfg is None else cfg.m_fut)
    m_opt, src_opt = _one("m_opt", live_m_opt, None if cfg is None else cfg.m_opt)

    verified = cfg.verified if cfg is not None else False
    if (
        cfg is not None
        and not verified
        and (src_fut != "missing" or src_opt != "missing")
    ):
        diags.append(
            f"root {root!r} multipliers are PROVISIONAL (unverified in the "
            f"signed-off table) — confirm against exchange specs before trusting"
        )

    return ResolvedMultipliers(
        m_fut=m_fut,
        m_opt=m_opt,
        m_fut_source=src_fut,
        m_opt_source=src_opt,
        verified=verified,
        diagnostic="; ".join(diags) if diags else None,
    )
