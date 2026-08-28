from decimal import ROUND_HALF_UP, Decimal

from money_machine.domain.enums import Action, OptionRight, PositionIntent, Side
from money_machine.domain.schemas import OptionLeg, OptionStructure

MULTIPLIER = Decimal("100")
CENT = Decimal("0.01")


def validate_defined_risk(structure: OptionStructure) -> None:
    legs = structure.legs
    if len(legs) not in {2, 4}:
        raise ValueError("only two-leg spreads and four-leg condors are supported")
    if any(leg.underlying != structure.underlying for leg in legs):
        raise ValueError("all legs must use one underlying")
    if any(leg.expiration != structure.expiration for leg in legs):
        raise ValueError("all legs must use one expiration")
    if len({leg.symbol for leg in legs}) != len(legs):
        raise ValueError("option leg symbols must be unique")
    if any(leg.ratio_qty != 1 for leg in legs):
        raise ValueError("initial playbooks require one-to-one leg ratios")
    if any(
        leg.position_intent not in {PositionIntent.BUY_TO_OPEN, PositionIntent.SELL_TO_OPEN}
        for leg in legs
    ):
        raise ValueError("entry structure legs must open positions")
    calculated = calculate_maximum_loss(structure.strategy, legs, structure.net_price)
    if calculated != structure.maximum_loss.quantize(CENT, rounding=ROUND_HALF_UP):
        raise ValueError("declared maximum loss does not match leg geometry")


def calculate_maximum_loss(
    strategy: Action, legs: tuple[OptionLeg, ...], net_price: Decimal
) -> Decimal:
    if strategy in {Action.INDEX_CONDOR, Action.EARNINGS_CONDOR}:
        return _condor_maximum_loss(legs, net_price)
    if strategy in {Action.CALL_DEBIT_SPREAD, Action.PUT_DEBIT_SPREAD, Action.HEDGE}:
        return _debit_spread_maximum_loss(legs, net_price)
    raise ValueError("unsupported defined-risk strategy")


def _condor_maximum_loss(legs: tuple[OptionLeg, ...], credit: Decimal) -> Decimal:
    if len(legs) != 4:
        raise ValueError("an iron condor requires exactly four legs")
    puts = sorted((leg for leg in legs if leg.right is OptionRight.PUT), key=lambda leg: leg.strike)
    calls = sorted(
        (leg for leg in legs if leg.right is OptionRight.CALL), key=lambda leg: leg.strike
    )
    if len(puts) != 2 or len(calls) != 2:
        raise ValueError("an iron condor requires two put and two call legs")
    if not (
        puts[0].side is Side.BUY
        and puts[1].side is Side.SELL
        and calls[0].side is Side.SELL
        and calls[1].side is Side.BUY
    ):
        raise ValueError("condor wings do not bound both short options")
    if credit <= 0:
        raise ValueError("condor credit must be positive")
    width = max(puts[1].strike - puts[0].strike, calls[1].strike - calls[0].strike)
    loss = (width - credit) * MULTIPLIER
    if loss <= 0:
        raise ValueError("condor credit must be less than wing width")
    return loss.quantize(CENT, rounding=ROUND_HALF_UP)


def _debit_spread_maximum_loss(legs: tuple[OptionLeg, ...], debit: Decimal) -> Decimal:
    if len(legs) != 2:
        raise ValueError("a debit spread requires exactly two legs")
    if legs[0].right is not legs[1].right:
        raise ValueError("a debit spread requires legs with the same option right")
    if {leg.side for leg in legs} != {Side.BUY, Side.SELL}:
        raise ValueError("a debit spread requires one long and one short leg")
    width = abs(legs[0].strike - legs[1].strike)
    if debit <= 0 or debit >= width:
        raise ValueError("debit must be positive and less than spread width")
    return (debit * MULTIPLIER).quantize(CENT, rounding=ROUND_HALF_UP)
