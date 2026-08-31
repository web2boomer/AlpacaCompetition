from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from money_machine.domain.enums import Action, OptionRight, PositionIntent, Side
from money_machine.domain.events import scheduled_macro_event_risk
from money_machine.domain.options import MULTIPLIER, calculate_maximum_loss
from money_machine.domain.schemas import (
    Candidate,
    OptionLeg,
    OptionQuote,
    OptionStructure,
    UnderlyingSnapshot,
)

MIN_VOLUME = 25
MIN_OPEN_INTEREST = 200
MAX_QUOTE_AGE_SECONDS = 90
MIN_RICHNESS_RATIO = Decimal("1.20")
MIN_IMPLIED_MOVE_PCT = Decimal("0.003")
MAX_IMPLIED_MOVE_PCT = Decimal("0.08")
MAX_STRUCTURE_SPREAD_TO_EDGE = Decimal("0.45")
MIN_CREDIT = Decimal("0.20")
DIRECTIONAL_TREND_THRESHOLD = Decimal("0.004")
DIRECTIONAL_MIN_CONFIDENCE = Decimal("0.72")
CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class CandidateBuildReport:
    candidates: tuple[Candidate, ...]
    rejections: dict[str, tuple[str, ...]]


def build_candidates(
    snapshots: list[UnderlyingSnapshot],
    chains: dict[str, list[OptionQuote]],
    now: datetime,
) -> CandidateBuildReport:
    candidates: list[Candidate] = []
    rejections: dict[str, tuple[str, ...]] = {}
    for snapshot in snapshots:
        snapshot = snapshot.model_copy(
            update={"event_risk": snapshot.event_risk or scheduled_macro_event_risk(now)}
        )
        symbol_candidates: list[Candidate] = []
        reasons: list[str] = []
        chain = chains.get(snapshot.symbol, [])
        condor = _build_condor(snapshot, chain, now, reasons)
        if condor:
            symbol_candidates.append(condor)
        if snapshot.symbol in {"SPY", "QQQ"}:
            directional = _build_directional(snapshot, chain, now, reasons)
            if directional:
                symbol_candidates.append(directional)
        if not symbol_candidates:
            rejections[snapshot.symbol] = tuple(dict.fromkeys(reasons)) or (
                "no eligible defined-risk structure",
            )
        candidates.extend(symbol_candidates)
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.candidate_id))
    return CandidateBuildReport(candidates=tuple(candidates), rejections=rejections)


def _build_condor(
    snapshot: UnderlyingSnapshot,
    chain: list[OptionQuote],
    now: datetime,
    reasons: list[str],
) -> Candidate | None:
    richness = snapshot.richness_ratio
    if richness < MIN_RICHNESS_RATIO:
        reasons.append(f"richness {richness:.2f} below {MIN_RICHNESS_RATIO}")
        return None
    if not MIN_IMPLIED_MOVE_PCT <= snapshot.implied_move_pct <= MAX_IMPLIED_MOVE_PCT:
        reasons.append("implied move outside conservative sanity range")
        return None
    if snapshot.event_risk:
        reasons.append("scheduled macro event overlaps intended holding period")
        return None
    expiry_groups = _fresh_expiry_groups(chain, now)
    if not expiry_groups:
        reasons.append("option chain is empty, incomplete, or stale")
        return None
    expiration, quotes = min(expiry_groups.items(), key=lambda item: item[0])
    move = snapshot.spot * snapshot.implied_move_pct
    width = Decimal("3") if snapshot.symbol == "IWM" else Decimal("5")
    short_put = _nearest(quotes, OptionRight.PUT, snapshot.spot - move)
    short_call = _nearest(quotes, OptionRight.CALL, snapshot.spot + move)
    if short_put is None or short_call is None:
        reasons.append("missing short strikes around implied move")
        return None
    long_put = _nearest_exact_side(quotes, OptionRight.PUT, short_put.strike - width, lower=True)
    long_call = _nearest_exact_side(
        quotes, OptionRight.CALL, short_call.strike + width, lower=False
    )
    selected = (long_put, short_put, short_call, long_call)
    if any(quote is None for quote in selected):
        reasons.append("missing protective wing for condor")
        return None
    complete = tuple(quote for quote in selected if quote is not None)
    if not _liquid(complete):
        reasons.append("fresh quote, daily volume, or available depth liquidity gate failed")
        return None
    legs = (
        _leg(complete[0], Side.BUY),
        _leg(complete[1], Side.SELL),
        _leg(complete[2], Side.SELL),
        _leg(complete[3], Side.BUY),
    )
    midpoint_credit = sum(
        (leg.midpoint if leg.side is Side.SELL else -leg.midpoint for leg in legs),
        Decimal("0"),
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    executable_credit = (
        complete[1].bid + complete[2].bid - complete[0].ask - complete[3].ask
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    structure_spread = (midpoint_credit - executable_credit).copy_abs().quantize(CENT)
    if midpoint_credit < MIN_CREDIT:
        reasons.append("expected condor credit is below minimum")
        return None
    if structure_spread > midpoint_credit * MAX_STRUCTURE_SPREAD_TO_EDGE:
        reasons.append("structure spread consumes too much expected credit")
        return None
    maximum_loss = calculate_maximum_loss(Action.INDEX_CONDOR, legs, midpoint_credit)
    maximum_profit = (midpoint_credit * MULTIPLIER).quantize(CENT)
    structure = OptionStructure(
        strategy=Action.INDEX_CONDOR,
        underlying=snapshot.symbol,
        expiration=expiration,
        legs=legs,
        net_price=midpoint_credit,
        maximum_loss=maximum_loss,
        maximum_profit=maximum_profit,
        is_credit=True,
    )
    age = max(_age_seconds(now, quote.observed_at) for quote in complete)
    score = (richness * Decimal("100") - structure_spread * Decimal("10")).quantize(CENT)
    candidate_id = _candidate_id(snapshot.symbol, Action.INDEX_CONDOR, expiration, legs)
    return Candidate(
        candidate_id=candidate_id,
        action=Action.INDEX_CONDOR,
        structure=structure,
        score=score,
        expected_credit_or_debit=midpoint_credit,
        structure_spread=structure_spread,
        richness_ratio=richness,
        data_age_seconds=age,
        event_risk=False,
        liquidity_passed=True,
        gate_evidence=(
            f"implied/realized move ratio {richness:.2f}",
            f"four-leg midpoint credit ${midpoint_credit:.2f}",
            f"defined maximum loss ${maximum_loss:.2f} per contract",
            f"structure spread ${structure_spread:.2f}",
        ),
    )


def _build_directional(
    snapshot: UnderlyingSnapshot,
    chain: list[OptionQuote],
    now: datetime,
    reasons: list[str],
) -> Candidate | None:
    if abs(snapshot.trend_return_pct) < DIRECTIONAL_TREND_THRESHOLD:
        return None
    if snapshot.event_risk:
        reasons.append("event risk vetoed directional structure")
        return None
    expiry_groups = _fresh_expiry_groups(chain, now)
    if not expiry_groups:
        reasons.append("directional chain is empty, incomplete, or stale")
        return None
    expiration, quotes = min(expiry_groups.items(), key=lambda item: item[0])
    bullish = snapshot.trend_return_pct > 0
    right = OptionRight.CALL if bullish else OptionRight.PUT
    action = Action.CALL_DEBIT_SPREAD if bullish else Action.PUT_DEBIT_SPREAD
    width = Decimal("5")
    long_quote = _nearest(quotes, right, snapshot.spot)
    if long_quote is None:
        reasons.append("missing near-the-money directional long leg")
        return None
    short_target = long_quote.strike + width if bullish else long_quote.strike - width
    short_quote = _nearest_exact_side(quotes, right, short_target, lower=not bullish)
    if short_quote is None or not _liquid((long_quote, short_quote)):
        reasons.append("directional spread liquidity gate failed")
        return None
    legs = (_leg(long_quote, Side.BUY), _leg(short_quote, Side.SELL))
    debit = (long_quote.midpoint - short_quote.midpoint).quantize(CENT, rounding=ROUND_HALF_UP)
    if debit <= 0 or debit >= width:
        reasons.append("directional debit is outside defined-risk bounds")
        return None
    maximum_loss = calculate_maximum_loss(action, legs, debit)
    maximum_profit = ((width - debit) * MULTIPLIER).quantize(CENT)
    if maximum_profit / maximum_loss < Decimal("0.70"):
        reasons.append("directional reward-to-risk below 0.70")
        return None
    structure_spread = (long_quote.spread + short_quote.spread).quantize(CENT)
    age = max(_age_seconds(now, quote.observed_at) for quote in (long_quote, short_quote))
    structure = OptionStructure(
        strategy=action,
        underlying=snapshot.symbol,
        expiration=expiration,
        legs=legs,
        net_price=debit,
        maximum_loss=maximum_loss,
        maximum_profit=maximum_profit,
        is_credit=False,
    )
    return Candidate(
        candidate_id=_candidate_id(snapshot.symbol, action, expiration, legs),
        action=action,
        structure=structure,
        score=(abs(snapshot.trend_return_pct) * Decimal("10000")).quantize(CENT),
        expected_credit_or_debit=debit,
        structure_spread=structure_spread,
        richness_ratio=snapshot.richness_ratio,
        data_age_seconds=age,
        event_risk=False,
        liquidity_passed=True,
        direction_agrees=True,
        minimum_confidence=DIRECTIONAL_MIN_CONFIDENCE,
        gate_evidence=(
            f"deterministic trend {snapshot.trend_return_pct:.2%}",
            f"defined debit ${debit:.2f}",
            f"maximum loss ${maximum_loss:.2f} per contract",
            f"reward/risk {(maximum_profit / maximum_loss):.2f}",
        ),
    )


def _fresh_expiry_groups(
    chain: list[OptionQuote], now: datetime
) -> dict[datetime, list[OptionQuote]]:
    grouped: dict[datetime, list[OptionQuote]] = defaultdict(list)
    for quote in chain:
        if quote.expiration <= now or _age_seconds(now, quote.observed_at) > MAX_QUOTE_AGE_SECONDS:
            continue
        grouped[quote.expiration].append(quote)
    return dict(grouped)


def _nearest(quotes: list[OptionQuote], right: OptionRight, target: Decimal) -> OptionQuote | None:
    matches = [quote for quote in quotes if quote.right is right]
    return (
        min(matches, key=lambda quote: (abs(quote.strike - target), quote.strike))
        if matches
        else None
    )


def _nearest_exact_side(
    quotes: list[OptionQuote],
    right: OptionRight,
    target: Decimal,
    *,
    lower: bool,
) -> OptionQuote | None:
    matches = [
        quote
        for quote in quotes
        if quote.right is right and (quote.strike <= target if lower else quote.strike >= target)
    ]
    return min(matches, key=lambda quote: abs(quote.strike - target)) if matches else None


def _liquid(quotes: tuple[OptionQuote, ...]) -> bool:
    return all(_quote_is_liquid(quote) for quote in quotes)


def _quote_is_liquid(quote: OptionQuote) -> bool:
    if quote.bid <= 0 or quote.ask <= quote.bid:
        return False
    if quote.volume is None or quote.volume < MIN_VOLUME:
        return False
    # Alpaca's option snapshot has no open-interest field. When another normalized
    # source supplies OI, keep the existing conservative floor; absence is not zero.
    if quote.open_interest is not None and quote.open_interest < MIN_OPEN_INTEREST:
        return False
    # Alpaca quote depth is useful corroboration when present, but older normalized
    # fixtures and sources may omit it. A supplied empty side is never considered liquid.
    if quote.bid_size is not None and quote.bid_size <= 0:
        return False
    return quote.ask_size is None or quote.ask_size > 0


def _leg(quote: OptionQuote, side: Side) -> OptionLeg:
    return OptionLeg(
        symbol=quote.symbol,
        underlying=quote.underlying,
        expiration=quote.expiration,
        right=quote.right,
        strike=quote.strike,
        side=side,
        position_intent=(
            PositionIntent.BUY_TO_OPEN if side is Side.BUY else PositionIntent.SELL_TO_OPEN
        ),
        bid=quote.bid,
        ask=quote.ask,
        volume=quote.volume,
        open_interest=quote.open_interest,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
    )


def _candidate_id(
    symbol: str, action: Action, expiration: datetime, legs: tuple[OptionLeg, ...]
) -> str:
    strikes = "-".join(str(leg.strike).replace(".", "p") for leg in legs)
    return f"{symbol.lower()}-{action.value}-{expiration:%Y%m%d}-{strikes}"


def _age_seconds(now: datetime, observed_at: datetime) -> int:
    return max(0, int((now.astimezone(UTC) - observed_at.astimezone(UTC)).total_seconds()))
