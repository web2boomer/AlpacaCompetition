import asyncio
import signal
import socket
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import structlog

from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter
from money_machine.domain.enums import RunMode
from money_machine.model_provider import OpenAIModelProvider, ReplayModelProvider
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
    model = _model(settings)
    async with AlpacaMcpV2Adapter(settings) as adapter:
        while not stop.is_set():
            now = datetime.now(UTC)
            if not repository.acquire_scheduler_lease(name="trading-loop", owner_id=owner, now=now):
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
            if once:
                return
            delay = 300 - (datetime.now(UTC).timestamp() % 300)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)


def _model(settings: Settings) -> ModelProvider:
    if settings.model_provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is missing")
        return OpenAIModelProvider(
            api_key=settings.openai_api_key.get_secret_value(), model=settings.openai_model
        )
    return ReplayModelProvider()
