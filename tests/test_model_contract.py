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


def model_selection(candidate_id: str, action: Action) -> ModelDecision:
    return ModelDecision(
        regime="calm",
        action=action,
        candidate_id=candidate_id,
        confidence=0.84,
        thesis="Select the strongest allowed candidate.",
        evidence=("Candidate passed the supplied deterministic gates.",),
        invalidation=("Abstain if deterministic eligibility changes.",),
        maximum_holding_minutes=60,
    )


@pytest.mark.asyncio
async def test_openai_unknown_candidate_id_retries_once_with_exact_allowed_ids(
    replay_candidate,
) -> None:
    provider = OpenAIModelProvider(api_key="test-key", model="gpt-5.6")
    calls = []
    malformed = replay_candidate.candidate_id.replace("_", "-", 1)
    responses = [
        model_selection(malformed, replay_candidate.action),
        model_selection(replay_candidate.candidate_id, replay_candidate.action),
    ]

    async def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id=f"response-{len(calls)}",
            model="gpt-5.6-sol",
            output_parsed=responses[len(calls) - 1],
        )

    provider.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    envelope = await provider.decide(
        candidates=[replay_candidate], market_context={}, portfolio_context={}
    )

    assert len(calls) == 2
    assert envelope.decision.candidate_id == replay_candidate.candidate_id
    assert envelope.selection_attempts == 2
    assert envelope.candidate_id_retry_used is True
    assert envelope.initial_raw_response_hash is not None
    retry_prompt = calls[1]["input"][1]["content"]
    assert '"allowed_candidate_ids": ["' + replay_candidate.candidate_id in retry_prompt
    assert "copied character-for-character" in retry_prompt


@pytest.mark.asyncio
async def test_openai_second_unknown_candidate_id_fails_closed(replay_candidate) -> None:
    provider = OpenAIModelProvider(api_key="test-key", model="gpt-5.6")
    calls = 0

    async def parse(**_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            id=f"response-{calls}",
            model="gpt-5.6-sol",
            output_parsed=model_selection(f"unknown-{calls}", replay_candidate.action),
        )

    provider.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    envelope = await provider.decide(
        candidates=[replay_candidate], market_context={}, portfolio_context={}
    )

    assert calls == 2
    assert envelope.decision.action is Action.ABSTAIN
    assert envelope.decision.candidate_id is None
    assert envelope.validation_error == "UnknownCandidateIdAfterRetry"
    assert envelope.selection_attempts == 2
    assert envelope.candidate_id_retry_used is True
