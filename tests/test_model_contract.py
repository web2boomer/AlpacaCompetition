from decimal import Decimal
from types import SimpleNamespace

import pytest

from money_machine.domain.enums import Action, ExecutionState, RiskReason
from money_machine.domain.risk import evaluate_risk
from money_machine.domain.schemas import ModelDecision, RiskContext
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


@pytest.mark.asyncio
async def test_openai_prompt_treats_supplied_directional_candidates_as_gate_passed(
    replay_candidate,
) -> None:
    provider = OpenAIModelProvider(api_key="test-key", model="gpt-5.6")
    calls = []

    async def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id="response-directional",
            model="gpt-5.6-sol",
            output_parsed=ModelDecision.abstention("No regime fit."),
        )

    provider.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    await provider.decide(candidates=[replay_candidate], market_context={}, portfolio_context={})

    prompt = calls[0]["input"][1]["content"]
    assert "already passed its strategy-specific deterministic construction gates" in prompt
    assert "richness_ratio is not an eligibility gate" in prompt
    assert "rejection evidence for condors" in prompt


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
async def test_openai_second_unknown_candidate_id_uses_standard_tier_top_ranked_fallback(
    replay_candidate,
) -> None:
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
    assert envelope.decision.action is replay_candidate.action
    assert envelope.decision.candidate_id == replay_candidate.candidate_id
    assert envelope.decision.confidence == float(replay_candidate.minimum_confidence)
    assert envelope.validation_error == "DeterministicTopRankedSelectionFallback"
    assert envelope.selection_attempts == 2
    assert envelope.candidate_id_retry_used is True
    assert envelope.deterministic_fallback_used is True
    assert envelope.selection_provenance == "deterministic_top_ranked"
    assert envelope.initial_raw_response_hash is not None
    assert envelope.retry_raw_response_hash is not None


@pytest.mark.asyncio
async def test_openai_first_abstention_gets_exact_corrective_retry(replay_candidate) -> None:
    provider = OpenAIModelProvider(api_key="test-key", model="gpt-5.6")
    calls = []
    responses = [
        ModelDecision.abstention("Invented event and richness veto."),
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
    assert envelope.selection_provenance == "corrective_retry"
    assert envelope.deterministic_fallback_used is False
    assert envelope.initial_raw_response_hash is not None
    assert envelope.retry_raw_response_hash is not None
    assert "You are the ranker, not the gatekeeper" in calls[1]["input"][1]["content"]
    assert "Do not abstain" in calls[1]["input"][1]["content"]


@pytest.mark.asyncio
async def test_openai_double_abstention_cannot_veto_eligible_directional(
    directional_candidate,
) -> None:
    provider = OpenAIModelProvider(api_key="test-key", model="gpt-5.6")
    calls = 0
    directional = directional_candidate

    async def parse(**_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            id=f"response-{calls}",
            model="gpt-5.6-sol",
            output_parsed=ModelDecision.abstention(
                "Invented event overlap and richness threshold."
            ),
        )

    provider.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    envelope = await provider.decide(
        candidates=[directional], market_context={}, portfolio_context={}
    )

    assert calls == 2
    assert envelope.decision.action is directional.action
    assert envelope.decision.candidate_id == directional.candidate_id
    assert envelope.decision.confidence == 0.72
    assert envelope.decision.maximum_holding_minutes == 60
    assert envelope.selection_provenance == "deterministic_top_ranked"

    context = RiskContext(
        now="2026-08-28T15:05:00Z",
        execution_state=ExecutionState.FULL_EXECUTION,
        equity=Decimal("100000"),
        start_of_day_equity=Decimal("100000"),
        peak_equity=Decimal("100000"),
        total_open_defined_loss=Decimal("0"),
        index_cluster_defined_loss=Decimal("0"),
        open_alpha_structures=0,
        pending_underlyings=frozenset(),
        open_underlyings=frozenset(),
        kill_switch_active=False,
        reconciliation_clean=True,
    )
    risk = evaluate_risk(envelope.decision, directional, context)
    tier = next(check for check in risk.checks if check.name == "effective_per_structure_percent")
    assert tier.actual == "0.015"

    blocked = evaluate_risk(
        envelope.decision,
        directional,
        context.model_copy(update={"reconciliation_clean": False}),
    )
    assert not blocked.approved
    assert RiskReason.RECONCILIATION in blocked.reason_codes


@pytest.mark.asyncio
async def test_openai_empty_tuple_keeps_model_abstention() -> None:
    provider = OpenAIModelProvider(api_key="test-key", model="gpt-5.6")
    calls = 0

    async def parse(**_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            id="response-empty",
            model="gpt-5.6-sol",
            output_parsed=ModelDecision.abstention("No candidates."),
        )

    provider.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    envelope = await provider.decide(candidates=[], market_context={}, portfolio_context={})

    assert calls == 1
    assert envelope.decision.action is Action.ABSTAIN
    assert envelope.selection_attempts == 1
    assert not envelope.deterministic_fallback_used
