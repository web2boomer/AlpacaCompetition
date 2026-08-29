from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr, ValidationError

from money_machine.domain.clock import STARTS_AT
from money_machine.domain.enums import AccountRole
from money_machine.domain.schemas import AccountSnapshot
from money_machine.safety import new_entry_authorized, verify_account_identity
from money_machine.settings import Settings


def configured_settings(**updates) -> Settings:
    values = {
        "app_env": "development",
        "account_role": "development",
        "alpaca_api_key": SecretStr("present"),
        "alpaca_secret_key": SecretStr("present"),
        "alpaca_expected_account_id": SecretStr("paper-account"),
        "alpaca_paper_trade": True,
        "apca_api_base_url": "https://paper-api.alpaca.markets",
    }
    values.update(updates)
    return Settings(**values)


def account(**updates) -> AccountSnapshot:
    values = {
        "account_id": "paper-account",
        "status": "ACTIVE",
        "is_paper": True,
        "equity": Decimal("100000"),
        "cash": Decimal("100000"),
        "buying_power": Decimal("200000"),
        "portfolio_value": Decimal("100000"),
    }
    values.update(updates)
    return AccountSnapshot(**values)


@pytest.mark.parametrize(
    ("app_env", "account_role", "valid"),
    [
        ("development", "development", True),
        ("production", "competition", True),
        ("development", "competition", False),
        ("production", "development", False),
    ],
)
def test_environment_account_role_mapping(app_env: str, account_role: str, valid: bool) -> None:
    if valid:
        configured_settings(app_env=app_env, account_role=account_role)
    else:
        with pytest.raises(ValidationError, match="mapping"):
            configured_settings(app_env=app_env, account_role=account_role)


@pytest.mark.parametrize(
    "updates",
    [
        {"alpaca_paper_trade": False},
        {"apca_api_base_url": "https://api.alpaca.markets"},
        {"apca_api_base_url": "http://paper-api.alpaca.markets"},
        {"apca_api_base_url": "https://paper-api.alpaca.markets.attacker.invalid"},
    ],
)
def test_live_or_lookalike_endpoints_rejected(updates) -> None:
    with pytest.raises(ValidationError):
        configured_settings(**updates)


def test_forbidden_execution_enabled_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_ENABLED", "true")
    with pytest.raises(ValidationError, match="forbidden"):
        configured_settings()


def test_account_identity_verified_without_exposing_id() -> None:
    verification = verify_account_identity(configured_settings(), account())
    assert verification.verified
    assert verification.account_fingerprint != "paper-account"


def test_account_number_is_an_accepted_exact_identifier() -> None:
    verification = verify_account_identity(
        configured_settings(), account(account_id="internal-uuid", account_number="paper-account")
    )
    assert verification.verified


def test_account_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        verify_account_identity(configured_settings(), account(account_id="wrong"))


def test_non_paper_account_rejected() -> None:
    with pytest.raises(ValueError, match="non-paper"):
        verify_account_identity(configured_settings(), account(is_paper=False))


def test_competition_entry_authority_is_derived_from_scoring_window() -> None:
    assert not new_entry_authorized(AccountRole.COMPETITION, now=STARTS_AT - timedelta(seconds=1))
    assert new_entry_authorized(AccountRole.COMPETITION, now=STARTS_AT)


def test_development_entry_authority_is_not_bound_to_competition_clock() -> None:
    assert new_entry_authorized(AccountRole.DEVELOPMENT, now=STARTS_AT - timedelta(days=30))
    assert new_entry_authorized(AccountRole.DEVELOPMENT, now=STARTS_AT + timedelta(days=30))
