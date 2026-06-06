"""Transaction-cost model.

Costs are the single biggest reason retail systematic strategies that look
great in backtest die in live trading. This module makes costs *mandatory*
and expressed in return terms so the engine can subtract them on every
position change.
"""
from __future__ import annotations

from dataclasses import dataclass
import warnings


@dataclass(frozen=True)
class CostModel:
    """All costs expressed as a fraction of notional, applied per side.

    spread_frac:     half-spread paid when crossing the book, per side.
                     e.g. EURUSD 0.6 pip spread on price 1.1000 -> 0.00006/1.1
                     ~ 5.5e-5; you pay half on entry, half on exit, so put the
                     *half-spread* here (~2.7e-5).
    slippage_frac:   extra adverse fill beyond the spread, per side.
    commission_frac: broker commission per side as fraction of notional.
                     e.g. $7 per $100k round-turn -> 3.5e-5 per side.
    """

    spread_frac: float
    slippage_frac: float = 0.0
    commission_frac: float = 0.0

    @property
    def per_side(self) -> float:
        return self.spread_frac + self.slippage_frac + self.commission_frac

    def __post_init__(self) -> None:
        if self.per_side <= 0:
            warnings.warn(
                "CostModel has zero cost. A frictionless backtest is a lie for "
                "any retail account. Real results will be worse. Set realistic "
                "spread/slippage/commission before trusting anything.",
                stacklevel=2,
            )


# A deliberately *pessimistic* default for a retail FX major. Better to be
# surprised on the upside in live than the downside.
RETAIL_FX_MAJOR = CostModel(
    spread_frac=3.0e-5,      # ~0.6 pip half-spread on a major
    slippage_frac=1.0e-5,    # assume you get filled a bit worse than mid
    commission_frac=3.5e-5,  # ~$7/100k round-turn ECN-style, split per side
)
