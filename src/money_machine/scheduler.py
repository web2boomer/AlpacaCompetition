import asyncio
import signal
import socket
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import structlog

from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter
from money_machine.business_reporting import BusinessReportingOrchestrator
from money_machine.domain.clock import FORCED_FLATTEN_STARTS_AT
from money_machine.domain.enums import RunMode
from money_machine.model_provider import DeterministicModelProvider, OpenAIModelProvider
from money_machine.persistence.repository import AuditRepository
from money_machine.ports import ModelProvider
from money_machine.service import AgentService
from money_machine.settings import Settings

logger = structlog.get_logger()


async def run_scheduler(
    settings: Settings,
    repository: AuditRepository,
    *,
    once: bool = False,
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stop.set)
    owner = f"{socket.gethostname()}-{uuid4().hex[:10]}"
    service = AgentService(settings, repository)
    business_reporting = BusinessReportingOrchestrator(settings, repository)
    model = _model(settings)
    broker_confirmed_flat = False
    async with AlpacaMcpV2Adapter(settings) as adapter:
        while not stop.is_set():
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            liquidation_recovery = now >= FORCED_FLATTEN_STARTS_AT and not broker_confirmed_flat
            lease_ttl = 90 if liquidation_recovery else 360
            if not repository.acquire_scheduler_lease(
                name="trading-loop", owner_id=owner, now=now, ttl_seconds=lease_ttl
            ):
                logger.warning("scheduler_lease_unavailable")
                if once:
                    return
            else:
                outcome = await service.run_cycle(
                    adapter=adapter,
                    model=model,
                    now=now,
                    mode=RunMode.LIVE,
                )
                logger.info(
                    "agent_cycle_complete",
                    run_id=outcome.run_id,
                    approved=outcome.approved,
                    submitted=outcome.order_submitted,
                )
                broker_confirmed_flat = bool(
                    outcome.passport.get("account", {}).get("broker_confirmed_flat", False)
                )
                await asyncio.to_thread(
                    business_reporting.report_if_due,
                    now=datetime.now(UTC),
                )
            if once:
                return
            interval = scheduler_interval_seconds(
                datetime.now(UTC), broker_confirmed_flat=broker_confirmed_flat
            )
            delay = interval - (datetime.now(UTC).timestamp() % interval)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)


def _model(settings: Settings) -> ModelProvider:
    if settings.model_provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is missing")
        return OpenAIModelProvider(
            api_key=settings.openai_api_key.get_secret_value(), model=settings.openai_model
        )
    if settings.model_provider in {"deterministic", "replay"}:
        return DeterministicModelProvider()
    raise ValueError(f"unsupported MODEL_PROVIDER: {settings.model_provider}")


def scheduler_interval_seconds(now: datetime, *, broker_confirmed_flat: bool) -> int:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler time must include a timezone")
    liquidation_recovery = (
        now.astimezone(UTC) >= FORCED_FLATTEN_STARTS_AT and not broker_confirmed_flat
    )
    return 60 if liquidation_recovery else 300
