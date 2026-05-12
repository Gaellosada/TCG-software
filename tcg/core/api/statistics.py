"""Statistics router — equity-curve performance statistics endpoint.

POST /api/statistics accepts a YYYYMMDD-dated equity curve plus an
optional annualized risk-free rate and returns the full nested
:class:`StatisticsSuite`.

This is the reusable backend half of the Statistics component shared
between the Portfolio page and the Signals ResultsView.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from fastapi import APIRouter
from pydantic import BaseModel, Field

from tcg.engine.statistics import compute_statistics
from tcg.types.errors import ValidationError
from tcg.types.statistics import StatisticsSuite

router = APIRouter(prefix="/api", tags=["statistics"])


_DEFAULT_RISK_FREE_RATE = 0.04


class StatisticsRequest(BaseModel):
    dates: list[int] = Field(..., description="YYYYMMDD integer dates")
    equity: list[float] = Field(..., description="Equity values; same length as dates")
    risk_free_rate: float | None = Field(
        default=None,
        description="Annualized risk-free rate as a decimal. Default 0.04.",
    )


def _serialize_suite(suite: StatisticsSuite) -> dict[str, Any]:
    """Project the nested suite to the locked JSON shape.

    The dataclass uses ``return_`` (trailing underscore) because
    ``return`` is a Python keyword; the API contract calls the field
    ``return``. Convert here.
    """
    return {
        "return": {
            "total_return": suite.return_.total_return,
            "cagr": suite.return_.cagr,
            "excess_return": suite.return_.excess_return,
            "annualized_volatility": suite.return_.annualized_volatility,
            "best_day": suite.return_.best_day,
            "worst_day": suite.return_.worst_day,
            "best_month": suite.return_.best_month,
            "worst_month": suite.return_.worst_month,
        },
        "risk_adjusted": {
            "sharpe_ratio": suite.risk_adjusted.sharpe_ratio,
            "sortino_ratio": suite.risk_adjusted.sortino_ratio,
            "calmar_ratio": suite.risk_adjusted.calmar_ratio,
        },
        "tail": {
            "var_95": suite.tail.var_95,
            "var_99": suite.tail.var_99,
            "cvar_5": suite.tail.cvar_5,
            "skewness": suite.tail.skewness,
            "kurtosis": suite.tail.kurtosis,
        },
        "drawdown": {
            "max_drawdown": suite.drawdown.max_drawdown,
            "avg_drawdown": suite.drawdown.avg_drawdown,
            "current_drawdown": suite.drawdown.current_drawdown,
            "longest_drawdown_days": suite.drawdown.longest_drawdown_days,
            "time_underwater_days": suite.drawdown.time_underwater_days,
        },
        "risk_free_rate_used": suite.risk_free_rate_used,
        "num_observations": suite.num_observations,
    }


@router.post("/statistics")
async def compute_statistics_endpoint(body: StatisticsRequest) -> dict[str, Any]:
    if len(body.dates) != len(body.equity):
        raise ValidationError(
            f"dates length {len(body.dates)} != equity length {len(body.equity)}"
        )
    if len(body.equity) < 2:
        raise ValidationError("equity must have at least 2 observations")
    if any(v <= 0 for v in body.equity):
        raise ValidationError("equity values must all be strictly positive")

    rf = body.risk_free_rate if body.risk_free_rate is not None else _DEFAULT_RISK_FREE_RATE

    dates_arr = np.asarray(body.dates, dtype=np.int64)
    equity_arr = np.asarray(body.equity, dtype=np.float64)

    try:
        suite = compute_statistics(dates_arr, equity_arr, rf)
    except ValueError as exc:
        # Engine guards (e.g. monotonic-positive equity) become 400s.
        raise ValidationError(str(exc)) from exc

    return _serialize_suite(suite)
