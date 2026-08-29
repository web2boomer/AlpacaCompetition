from types import SimpleNamespace

import pytest

from money_machine.domain.enums import Action
from money_machine.domain.schemas import ModelDecision
from money_machine.model_provider import (
    OpenAIModelProvider,
    ReplayModelProvider,
    safe_model_decision,
)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"action": "buy_everything"},
        {
            "regime": "calm",
            "action": "index_condor",
            "candidate_id": None,
            "confidence": 2,
            "thesis": "invalid",
            "evidence": [],
            "invalidation": [],
            "maximum_holding_minutes": -1,
        },
        "not-json",
    ],
)
def test_invalid_model_output_becomes_abstention(payload) -> None:
    envelope = safe_model_decision(payload)
    assert envelope.decision.action is Action.ABSTAIN
    assert envelope.validation_error is not None
    assert len(envelope.raw_response_hash) == 64
    assert envelope.provider == "deterministic"


@pytest.mark.asyncio
async def test_empty_candidate_set_abstains() -> None:
    envelope = await ReplayModelProvider().decide(
        candidates=[], market_context={}, portfolio_context={}
    )
    assert envelope.decision.action is Action.ABSTAIN
    assert envelope.validation_error is None
    assert envelope.provider == "replay"


@pytest.mark.asyncio
async def test_openai_response_records_safe_provenance() -> None:
    provider = OpenAIModelProvider(api_key="test-key", model="gpt-5.6")
    decision = ModelDecision.abstention("No eligible candidate passed the hard gates.")

    async def parse(**_kwargs):
        return SimpleNamespace(
            id="resp_private_identifier",
            model="gpt-5.6-sol",
            output_parsed=decision,
        )

    provider.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    envelope = await provider.decide(candidates=[], market_context={}, portfolio_context={})

    assert envelope.provider == "openai"
    assert envelope.model == "gpt-5.6-sol"
    assert envelope.provider_response_id_hash is not None
    assert "resp_private_identifier" not in envelope.provider_response_id_hash
