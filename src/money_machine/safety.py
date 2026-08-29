import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from money_machine.domain.clock import BASELINE_EQUITY, competition_clock
from money_machine.domain.enums import AccountRole, AppEnvironment
from money_machine.domain.schemas import AccountSnapshot
from money_machine.settings import Settings


@dataclass(frozen=True, slots=True)
class AccountVerification:
    verified: bool
    account_fingerprint: str
    checks: tuple[str, ...]


def verify_account_identity(settings: Settings, account: AccountSnapshot) -> AccountVerification:
    settings.assert_live_credentials_present()
    expected_secret = settings.alpaca_expected_account_id
    if expected_secret is None:  # narrowed by assert_live_credentials_present
        raise ValueError("expected account id is missing")
    expected = expected_secret.get_secret_value()
    identifiers = (account.account_id, account.account_number or "")
    if not any(hmac.compare_digest(identifier, expected) for identifier in identifiers):
        raise ValueError("Alpaca account identity mismatch")
    if not account.is_paper or not settings.alpaca_paper_trade:
        raise ValueError("non-paper Alpaca account rejected")
    if account.status.upper() != "ACTIVE":
        raise ValueError("Alpaca paper account is not active")
    expected_role = (
        AccountRole.DEVELOPMENT
        if settings.app_env is AppEnvironment.DEVELOPMENT
        else AccountRole.COMPETITION
    )
    if settings.account_role is not expected_role:
        raise ValueError("environment/account role mapping rejected")
    fingerprint = hashlib.sha256(expected.encode()).hexdigest()[:12]
    return AccountVerification(
        verified=True,
        account_fingerprint=fingerprint,
        checks=("account_identifier_match", "paper_transport", "active_account", "role_mapping"),
    )


def verify_competition_baseline(account: AccountSnapshot, *, positions: int, orders: int) -> None:
    if account.equity.quantize(Decimal("0.01")) != BASELINE_EQUITY:
        raise ValueError("competition baseline equity is not exactly $100,000.00")
    if positions or orders:
        raise ValueError("competition account baseline requires empty orders and positions")


def new_entry_authorized(account_role: AccountRole, *, now: datetime | None = None) -> bool:
    """Derive entry authority from account role and the immutable competition clock."""
    if account_role is AccountRole.DEVELOPMENT:
        return True
    observed_at = now or datetime.now(UTC)
    return competition_clock(observed_at, has_positions=False).allow_new_entries
