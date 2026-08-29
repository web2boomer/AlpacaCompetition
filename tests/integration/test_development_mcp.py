from pathlib import Path

import pytest

from money_machine.adapters.alpaca_mcp import AlpacaMcpV2Adapter
from money_machine.safety import verify_account_identity
from money_machine.settings import Settings, load_local_environment


@pytest.mark.integration
@pytest.mark.asyncio
async def test_development_mcp_authentication_and_reads() -> None:
    env_file = Path(".env.development.local")
    legacy_env_file = Path(".env.competition.local")
    if not env_file.exists() and legacy_env_file.exists():
        env_file = legacy_env_file
    if not env_file.exists():
        pytest.skip(".env.development.local is missing")
    load_local_environment(env_file)
    settings = Settings()
    if settings.account_role.value != "development":
        pytest.skip("development account role is not configured")
    async with AlpacaMcpV2Adapter(settings) as adapter:
        account = await adapter.account()
        verification = verify_account_identity(settings, account)
        clock = await adapter.market_clock()
        history = await adapter.portfolio_history()
        snapshot = await adapter.underlying_snapshot("SPY")
        chain = await adapter.option_chain("SPY")
        open_orders = await adapter.orders(status="open")
        positions = await adapter.positions()
    assert verification.verified
    assert account.is_paper
    assert clock
    assert history
    assert snapshot.spot > 0
    assert chain
    assert not open_orders
    assert not positions
