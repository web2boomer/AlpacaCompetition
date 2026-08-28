import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter
from money_machine.domain.clock import BASELINE_EQUITY, competition_clock
from money_machine.domain.enums import AccountRole, AppEnvironment
from money_machine.persistence.database import Database
from money_machine.persistence.repository import AuditRepository
from money_machine.safety import COMPETITION_GO_LIVE_AUTHORIZED, verify_account_identity
from money_machine.settings import Settings


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    passed: bool
    read_only: bool
    checks: tuple[AcceptanceCheck, ...]

    def safe_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "read_only": self.read_only,
            "checks": [asdict(check) for check in self.checks],
        }


def _safe_exception_shape(exc: BaseException) -> str:
    """Describe nested failures without exposing broker payloads or credentials."""
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple):
        children = ",".join(_safe_exception_shape(child) for child in nested)
        return f"{type(exc).__name__}[{children}]"
    return type(exc).__name__


async def run_production_acceptance(
    settings: Settings, database: Database, repository: AuditRepository
) -> AcceptanceReport:
    checks: list[AcceptanceCheck] = []

    def add(name: str, passed: bool, ok: str, failed: str) -> None:
        checks.append(AcceptanceCheck(name, passed, ok if passed else failed))

    add("database", database.healthcheck(), "reachable", "unreachable")
    competition_configuration = (
        settings.app_env is AppEnvironment.PRODUCTION
        and settings.account_role is AccountRole.COMPETITION
    )
    add(
        "competition_configuration",
        competition_configuration,
        "production/competition mapping",
        "environment is not mapped to the competition role",
    )
    try:
        settings.assert_live_credentials_present()
        add("environment_variables", True, "required variables present", "")
    except ValueError:
        add("environment_variables", False, "", "required variables missing")
        return AcceptanceReport(False, True, tuple(checks))

    try:
        async with AlpacaMcpV2Adapter(settings) as adapter:
            (
                account_result,
                clock_result,
                orders_result,
                positions_result,
                history_result,
                activities_result,
            ) = await asyncio.gather(
                adapter.account(),
                adapter.market_clock(),
                adapter.orders(status="all"),
                adapter.positions(),
                adapter.portfolio_history(),
                adapter.activities(),
                return_exceptions=True,
            )
            reads = {
                "account": account_result,
                "clock": clock_result,
                "orders": orders_result,
                "positions": positions_result,
                "portfolio_history": history_result,
                "fill_activities": activities_result,
            }
            for name, result in reads.items():
                if isinstance(result, BaseException):
                    add(
                        f"alpaca_{name}_read",
                        False,
                        "",
                        f"failed ({type(result).__name__})",
                    )

            if not isinstance(account_result, BaseException):
                try:
                    verification = verify_account_identity(settings, account_result)
                    add(
                        "account_identity",
                        verification.verified,
                        "verified paper account",
                        "rejected",
                    )
                except ValueError:
                    add("account_identity", False, "", "rejected")
                add(
                    "baseline_equity",
                    account_result.equity == BASELINE_EQUITY,
                    "exactly $100,000.00",
                    "not exactly $100,000.00",
                )

            positions = [] if isinstance(positions_result, BaseException) else positions_result
            orders = [] if isinstance(orders_result, BaseException) else orders_result
            if not isinstance(positions_result, BaseException):
                add("empty_positions", not positions, "empty", "broker positions present")
            if not isinstance(orders_result, BaseException):
                add("empty_orders", not orders, "empty", "broker order history/state present")
            if not isinstance(history_result, BaseException):
                add(
                    "portfolio_history",
                    bool(history_result),
                    "read succeeded",
                    "read returned no data",
                )
            if not isinstance(activities_result, BaseException):
                add("fill_activities", True, "read succeeded", "")

            now_text = (
                None if isinstance(clock_result, BaseException) else clock_result.get("timestamp")
            )
            if isinstance(now_text, str):
                from datetime import datetime

                now = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
                clock_state = competition_clock(now, has_positions=bool(positions)).state
                add(
                    "competition_clock",
                    clock_state.value == "full_execution",
                    "full execution window",
                    f"state is {clock_state.value}",
                )
            elif not isinstance(clock_result, BaseException):
                add("competition_clock", False, "", "clock timestamp missing")
            stock_result, chain_result = await asyncio.gather(
                adapter.underlying_snapshot("SPY"),
                adapter.option_chain("SPY"),
                return_exceptions=True,
            )
            if isinstance(stock_result, BaseException):
                add(
                    "alpaca_stock_snapshot_read",
                    False,
                    "",
                    f"failed ({type(stock_result).__name__})",
                )
            else:
                add(
                    "stock_snapshot",
                    stock_result.spot > 0,
                    "SPY read succeeded",
                    "SPY read failed",
                )
            if isinstance(chain_result, BaseException):
                add(
                    "alpaca_option_chain_read",
                    False,
                    "",
                    f"failed ({type(chain_result).__name__})",
                )
            else:
                add(
                    "option_chain",
                    bool(chain_result),
                    "SPY chain read succeeded",
                    "SPY chain empty",
                )
    except Exception as exc:
        add("alpaca_mcp_v2", False, "", f"read path failed ({_safe_exception_shape(exc)})")

    operational_state = repository.latest_operational_state()
    add(
        "kill_switch_available",
        "kill_switch_active" in operational_state,
        "persistent state available",
        "persistent state unavailable",
    )
    add(
        "reconciliation",
        bool(operational_state.get("reconciliation_clean", True)),
        "clean",
        "not clean",
    )
    add(
        "development_round_trip",
        repository.development_round_trip_verified(),
        "verified",
        "no development-account round-trip evidence recorded",
    )
    add(
        "go_live_authorization",
        COMPETITION_GO_LIVE_AUTHORIZED,
        "version-controlled authorization present",
        "intentionally blocked pending explicit authorization",
    )
    return AcceptanceReport(all(check.passed for check in checks), True, tuple(checks))
