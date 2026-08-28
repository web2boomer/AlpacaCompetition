import hashlib
import json
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from money_machine.domain.enums import Action, Regime
from money_machine.domain.schemas import Candidate, ModelDecision, ModelDecisionEnvelope


def safe_model_decision(payload: Any) -> ModelDecisionEnvelope:
    try:
        canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = repr(type(payload))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        decision = ModelDecision.model_validate(payload)
        return ModelDecisionEnvelope(decision=decision, raw_response_hash=digest)
    except (ValidationError, TypeError, ValueError) as exc:
        return ModelDecisionEnvelope(
            decision=ModelDecision.abstention(
                "Invalid model output; deterministic fallback abstained."
            ),
            raw_response_hash=digest,
            validation_error=type(exc).__name__,
        )


class ReplayModelProvider:
    def __init__(self, scripted_payload: dict[str, Any] | None = None) -> None:
        self.scripted_payload = scripted_payload

    async def decide(
        self,
        *,
        candidates: Sequence[Candidate],
        market_context: dict[str, Any],
        portfolio_context: dict[str, Any],
    ) -> ModelDecisionEnvelope:
        del market_context, portfolio_context
        if self.scripted_payload is not None:
            return safe_model_decision(self.scripted_payload)
        if not candidates:
            return safe_model_decision(
                {
                    "regime": "dislocated",
                    "action": "abstain",
                    "candidate_id": None,
                    "confidence": 1.0,
                    "thesis": (
                        "Cash won because no structure passed deterministic data "
                        "and liquidity gates."
                    ),
                    "evidence": ["The precompiled candidate set was empty."],
                    "invalidation": ["Re-evaluate after fresh complete option-chain data arrives."],
                    "maximum_holding_minutes": 0,
                }
            )
        selected = candidates[0]
        regime = {
            Action.INDEX_CONDOR: Regime.CALM,
            Action.CALL_DEBIT_SPREAD: Regime.DIRECTIONAL_UP,
            Action.PUT_DEBIT_SPREAD: Regime.DIRECTIONAL_DOWN,
        }.get(selected.action, Regime.CALM)
        return safe_model_decision(
            {
                "regime": regime,
                "action": selected.action,
                "candidate_id": selected.candidate_id,
                "confidence": 0.84,
                "thesis": (
                    "The highest-ranked eligible structure best fits the replay regime "
                    "and finite risk budget."
                ),
                "evidence": list(selected.gate_evidence[:3]),
                "invalidation": [
                    "Abstain if quotes stale, event risk appears, or deterministic "
                    "risk capacity changes."
                ],
                "maximum_holding_minutes": 360,
            }
        )


class OpenAIModelProvider:
    def __init__(self, *, api_key: str, model: str) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def decide(
        self,
        *,
        candidates: Sequence[Candidate],
        market_context: dict[str, Any],
        portfolio_context: dict[str, Any],
    ) -> ModelDecisionEnvelope:
        safe_candidates = [candidate.model_dump(mode="json") for candidate in candidates]
        prompt = {
            "instruction": (
                "Choose exactly one candidate_id from candidates or abstain. Never invent strikes, "
                "quantity, account, order type, or broker parameters. Confidence "
                "cannot override gates."
            ),
            "market": market_context,
            "portfolio": portfolio_context,
            "candidates": safe_candidates,
        }
        try:
            response = await self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": "You are the constrained candidate selector for Money Machine.",
                    },
                    {"role": "user", "content": json.dumps(prompt, default=str)},
                ],
                text_format=ModelDecision,
            )
            parsed = response.output_parsed
            if parsed is None:
                return safe_model_decision(None)
            return safe_model_decision(parsed.model_dump(mode="json"))
        except Exception as exc:  # provider failures must fail closed
            digest = hashlib.sha256(type(exc).__name__.encode()).hexdigest()
            return ModelDecisionEnvelope(
                decision=ModelDecision.abstention("Model provider unavailable; cycle abstained."),
                raw_response_hash=digest,
                validation_error=type(exc).__name__,
            )
