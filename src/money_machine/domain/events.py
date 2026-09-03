from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class MacroEvent:
    name: str
    releases_at: datetime
    cooldown_minutes: int


@dataclass(frozen=True, slots=True)
class DirectionalEventWindow:
    accepted: bool
    maximum_holding_minutes: int
    deadline: datetime
    reason: str
    next_event: str | None


# One-off competition calendar. Sources are the BLS, BEA, and Federal Reserve
# release calendars; all values are stored in UTC and reviewed with the spec.
COMPETITION_MACRO_EVENTS = (
    MacroEvent("JOLTS", datetime(2026, 9, 1, 14, 0, tzinfo=UTC), 30),
    MacroEvent("Metropolitan employment", datetime(2026, 9, 2, 14, 0, tzinfo=UTC), 30),
    MacroEvent("Federal Reserve Beige Book", datetime(2026, 9, 2, 18, 0, tzinfo=UTC), 30),
    MacroEvent(
        "Productivity and international trade",
        datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
        75,
    ),
    MacroEvent("Employment Situation", datetime(2026, 9, 4, 12, 30, tzinfo=UTC), 90),
)

DIRECTIONAL_MAX_HOLDING_MINUTES = 45
DIRECTIONAL_EVENT_BUFFER_MINUTES = 15
DIRECTIONAL_MINIMUM_ENTRY_MINUTES = 30


def scheduled_macro_event_risk(at: datetime, *, intended_holding_minutes: int = 360) -> bool:
    """Return whether a position opened now would cross a scheduled macro release."""
    if at.tzinfo is None:
        raise ValueError("macro-event clock requires a timezone-aware timestamp")
    now = at.astimezone(UTC)
    intended_exit = now + timedelta(minutes=intended_holding_minutes)
    return any(
        now <= event.releases_at <= intended_exit
        or event.releases_at < now < event.releases_at + timedelta(minutes=event.cooldown_minutes)
        for event in COMPETITION_MACRO_EVENTS
    )


def directional_event_window(at: datetime) -> DirectionalEventWindow:
    """Return the event-safe holding window for a new directional index spread."""
    if at.tzinfo is None:
        raise ValueError("macro-event clock requires a timezone-aware timestamp")
    now = at.astimezone(UTC)
    for event in COMPETITION_MACRO_EVENTS:
        cooldown_end = event.releases_at + timedelta(minutes=event.cooldown_minutes)
        if event.releases_at <= now < cooldown_end:
            return DirectionalEventWindow(
                accepted=False,
                maximum_holding_minutes=0,
                deadline=cooldown_end,
                reason=f"{event.name} cooldown through {cooldown_end.isoformat()}",
                next_event=event.name,
            )

    next_event = next(
        (event for event in COMPETITION_MACRO_EVENTS if event.releases_at > now), None
    )
    maximum_deadline = now + timedelta(minutes=DIRECTIONAL_MAX_HOLDING_MINUTES)
    event_deadline = (
        next_event.releases_at - timedelta(minutes=DIRECTIONAL_EVENT_BUFFER_MINUTES)
        if next_event is not None
        else maximum_deadline
    )
    deadline = min(maximum_deadline, event_deadline)
    holding_minutes = max(0, int((deadline - now).total_seconds() // 60))
    accepted = holding_minutes >= DIRECTIONAL_MINIMUM_ENTRY_MINUTES
    reason = (
        "directional_45_minute_cap"
        if deadline == maximum_deadline
        else f"event_safe_before_{next_event.name if next_event else 'none'}"
    )
    if not accepted:
        reason = (
            f"insufficient_event_safe_window_before_{next_event.name if next_event else 'none'}"
        )
    return DirectionalEventWindow(
        accepted=accepted,
        maximum_holding_minutes=holding_minutes,
        deadline=deadline,
        reason=reason,
        next_event=next_event.name if next_event else None,
    )
