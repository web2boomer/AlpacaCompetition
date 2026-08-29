from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class MacroEvent:
    name: str
    releases_at: datetime
    cooldown_minutes: int


# One-off competition calendar. Sources are the BLS, BEA, and Federal Reserve
# release calendars; all values are stored in UTC and reviewed with the spec.
COMPETITION_MACRO_EVENTS = (
    MacroEvent("JOLTS", datetime(2026, 9, 1, 14, 0, tzinfo=UTC), 30),
    MacroEvent("Metropolitan employment", datetime(2026, 9, 2, 14, 0, tzinfo=UTC), 30),
    MacroEvent("Federal Reserve Beige Book", datetime(2026, 9, 2, 18, 0, tzinfo=UTC), 30),
    MacroEvent(
        "Productivity and international trade",
        datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
        90,
    ),
    MacroEvent("Employment Situation", datetime(2026, 9, 4, 12, 30, tzinfo=UTC), 90),
)


def scheduled_macro_event_risk(at: datetime, *, intended_holding_minutes: int = 360) -> bool:
    """Return whether a position opened now would cross a scheduled macro release."""
    if at.tzinfo is None:
        raise ValueError("macro-event clock requires a timezone-aware timestamp")
    now = at.astimezone(UTC)
    intended_exit = now + timedelta(minutes=intended_holding_minutes)
    return any(
        now <= event.releases_at <= intended_exit
        or event.releases_at < now <= event.releases_at + timedelta(minutes=event.cooldown_minutes)
        for event in COMPETITION_MACRO_EVENTS
    )
