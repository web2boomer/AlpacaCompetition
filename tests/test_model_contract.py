import pytest

from money_machine.domain.enums import Action
from money_machine.model_provider import ReplayModelProvider, safe_model_decision


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


@pytest.mark.asyncio
async def test_empty_candidate_set_abstains() -> None:
    envelope = await ReplayModelProvider().decide(
        candidates=[], market_context={}, portfolio_context={}
    )
    assert envelope.decision.action is Action.ABSTAIN
    assert envelope.validation_error is None
