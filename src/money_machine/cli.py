import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from money_machine.acceptance import run_production_acceptance
from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter
from money_machine.adapters.replay import ReplayAlpacaAdapter
from money_machine.business_reporting import BusinessReportBuilder, BusinessReportingOrchestrator
from money_machine.development_acceptance import run_development_round_trip
from money_machine.domain.clock import EOD_EQUITY_SNAPSHOT_AT
from money_machine.domain.enums import RunMode
from money_machine.logging_config import configure_logging
from money_machine.model_provider import ReplayModelProvider
from money_machine.persistence.database import Database, normalize_database_url
from money_machine.persistence.repository import AuditRepository
from money_machine.safety import configured_account_fingerprint
from money_machine.scheduler import run_scheduler
from money_machine.service import AgentService
from money_machine.settings import Settings, load_local_environment


def main() -> None:
    parser = argparse.ArgumentParser(prog="money-machine")
    parser.add_argument("--env-file", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("replay", help="run the canonical offline decision cycle")
    serve = subparsers.add_parser("serve", help="serve the public dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    db = subparsers.add_parser("db", help="database migration commands")
    db.add_argument("action", choices=["upgrade", "current"])
    scheduler = subparsers.add_parser("scheduler", help="run the guarded five-minute loop")
    scheduler.add_argument("--once", action="store_true")
    subparsers.add_parser("acceptance", help="run read-only production acceptance checks")
    subparsers.add_parser("mcp-read-check", help="verify guarded Alpaca MCP V2 reads")
    subparsers.add_parser(
        "mission-control-report-dry-run",
        help="print the current persisted Mission Control report without sending",
    )
    subparsers.add_parser(
        "mission-control-report",
        help="submit one due persisted report to Mission Control",
    )
    subparsers.add_parser(
        "competition-performance-export",
        help="print the deterministic read-only official performance evidence",
    )
    round_trip = subparsers.add_parser(
        "development-round-trip",
        help="open and close one bounded spread in the development paper account",
    )
    round_trip.add_argument(
        "--confirm-paper-order",
        action="store_true",
        help="confirm that two real Alpaca paper orders may be submitted",
    )
    kill = subparsers.add_parser("kill-switch", help="set the persistent entry kill switch")
    kill.add_argument("state", choices=["on", "off", "status"])
    args = parser.parse_args()
    if args.env_file:
        load_local_environment(args.env_file)
    settings = Settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    repository = AuditRepository(database)

    if args.command == "db":
        _migration(args.action, settings.database_url)
    elif args.command == "replay":
        _migration("upgrade", settings.database_url)
        _print_json(asyncio.run(_replay(settings, repository)))
    elif args.command == "serve":
        import uvicorn

        from money_machine.web import create_app

        uvicorn.run(create_app(settings, database), host=args.host, port=args.port)
    elif args.command == "scheduler":
        asyncio.run(run_scheduler(settings, repository, once=args.once))
    elif args.command == "acceptance":
        report = asyncio.run(run_production_acceptance(settings, database, repository))
        _print_json(report.safe_dict())
        if not report.passed:
            raise SystemExit(2)
    elif args.command == "mcp-read-check":
        _print_json(asyncio.run(_mcp_read_check(settings)))
    elif args.command == "mission-control-report-dry-run":
        business_report = BusinessReportBuilder(
            repository,
            interval_minutes=settings.mission_control_reporting_interval_minutes,
            account_fingerprint=configured_account_fingerprint(settings),
        ).build(
            now=datetime.now(UTC),
            environment=settings.mission_control_environment or settings.app_env.value,
        )
        if business_report is None:
            raise SystemExit("no completed official equity period is available")
        _print_json(business_report.as_payload())
    elif args.command == "mission-control-report":
        result = BusinessReportingOrchestrator(settings, repository).report_if_due(
            now=datetime.now(UTC)
        )
        if result is None:
            raise SystemExit("no report was delivered; inspect the redacted reporting warning")
        _print_json({"report_id": result.event_id, "duplicate": result.duplicate})
    elif args.command == "competition-performance-export":
        performance = repository.competition_performance_summary(
            account_fingerprint=configured_account_fingerprint(settings),
            now=EOD_EQUITY_SNAPSHOT_AT,
        )
        if performance["result_status"] != "final_eod_snapshot":
            raise SystemExit("the authoritative Thursday EOD snapshot is not available")
        _print_json(performance)
    elif args.command == "development-round-trip":
        if not args.confirm_paper_order:
            raise SystemExit("development round trip requires --confirm-paper-order")
        _migration("upgrade", settings.database_url)
        _print_json(asyncio.run(_development_round_trip(settings, repository)))
    elif args.command == "kill-switch":
        if args.state == "status":
            _print_json(repository.latest_operational_state())
        else:
            repository.set_kill_switch(active=args.state == "on", now=datetime.now(UTC))
            _print_json({"kill_switch": args.state, "persistent": True})


async def _replay(settings: Settings, repository: AuditRepository) -> dict[str, Any]:
    replay_settings = settings.model_copy(update={"run_mode": RunMode.REPLAY})
    adapter = ReplayAlpacaAdapter()
    outcome = await AgentService(replay_settings, repository).run_cycle(
        adapter=adapter,
        model=ReplayModelProvider(),
        now=adapter.observed_at,
        mode=RunMode.REPLAY,
    )
    return {
        "run_id": outcome.run_id,
        "created": outcome.created,
        "approved": outcome.approved,
        "order_submitted": outcome.order_submitted,
        "result_label": outcome.passport.get("result_label"),
    }


async def _mcp_read_check(settings: Settings) -> dict[str, Any]:
    settings.assert_live_credentials_present()
    async with AlpacaMcpV2Adapter(settings) as adapter:
        account = await adapter.account()
        from money_machine.safety import verify_account_identity

        verify_account_identity(settings, account)
        results = await asyncio.gather(
            adapter.market_clock(),
            adapter.portfolio_history(),
            adapter.underlying_snapshot("SPY"),
            adapter.option_chain("SPY"),
            adapter.activities(),
        )
        open_orders, positions = await asyncio.gather(
            adapter.orders(status="open"), adapter.positions()
        )
    return {
        "account_identity": "verified",
        "paper_account": account.is_paper,
        "market_clock": "passed" if results[0] else "failed",
        "portfolio_history": "passed" if results[1] else "failed",
        "stock_snapshot": "passed" if results[2] else "failed",
        "option_chain": "passed" if results[3] else "failed",
        "fill_activities": "passed",
        "open_orders": len(open_orders),
        "open_positions": len(positions),
        "orders_submitted_by_check": 0,
    }


async def _development_round_trip(
    settings: Settings, repository: AuditRepository
) -> dict[str, Any]:
    settings.assert_live_credentials_present()
    async with AlpacaMcpV2Adapter(settings) as adapter:
        report = await run_development_round_trip(settings, repository, adapter)
    return report.safe_dict()


def _migration(action: str, database_url: str) -> None:
    config_path = Path.cwd() / "alembic.ini"
    if not config_path.is_file():
        config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
    config = Config(str(config_path))
    normalized_url = normalize_database_url(database_url)
    config.set_main_option("sqlalchemy.url", normalized_url.replace("%", "%%"))
    if action == "upgrade":
        command.upgrade(config, "head")
    else:
        command.current(config)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
