import hashlib
import json
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from money_machine.domain.enums import Action, Regime
from money_machine.domain.schemas import Candidate, ModelDecision, ModelDecisionEnvelope


def safe_model_decision(
    payload: Any,
    *,
    provider: str = "deterministic",
    model: str | None = None,
    provider_response_id: str | None = None,
) -> ModelDecisionEnvelope:
    try:
        canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = repr(type(payload))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        decision = ModelDecision.model_validate(payload)
        return ModelDecisionEnvelope(
            decision=decision,
            raw_response_hash=digest,
            provider=provider,
            model=model,
            provider_response_id_hash=(
                hashlib.sha256(provider_response_id.encode()).hexdigest()
                if provider_response_id
                else None
            ),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        return ModelDecisionEnvelope(
            decision=ModelDecision.abstention(
                "Invalid model output; deterministic fallback abstained."
            ),
            raw_response_hash=digest,
            validation_error=type(exc).__name__,
            provider=provider,
            model=model,
            provider_response_id_hash=(
                hashlib.sha256(provider_response_id.encode()).hexdigest()
                if provider_response_id
                else None
            ),
        )


class DeterministicModelProvider:
    provider_name = "deterministic"

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
            return safe_model_decision(self.scripted_payload, provider=self.provider_name)
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
                },
                provider=self.provider_name,
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
                    "The highest-ranked eligible structure best fits the current regime "
                    "and finite risk budget."
                ),
                "evidence": list(selected.gate_evidence[:3]),
                "invalidation": [
                    "Abstain if quotes stale, event risk appears, or deterministic "
                    "risk capacity changes."
                ],
                "maximum_holding_minutes": 360,
            },
            provider=self.provider_name,
        )


class ReplayModelProvider(DeterministicModelProvider):
    """Offline replay name retained for fixtures and explicit replay mode."""

    provider_name = "replay"


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
        allowed_candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
        prompt = {
            "instruction": (
                "Every supplied candidate already passed its strategy-specific deterministic "
                "construction gates. Choose exactly one candidate_id from candidates or abstain "
                "only when none fits the observed regime. For call_debit_spread and "
                "put_debit_spread, richness_ratio is not an eligibility gate and rejection "
                "evidence for condors or other structures must not be applied to them. Never "
                "invent strikes, quantity, account, order type, or broker parameters. Confidence "
                "cannot override gates."
            ),
            "market": market_context,
            "portfolio": portfolio_context,
            "candidates": safe_candidates,
        }

        async def request_decision(request_prompt: dict[str, Any]) -> ModelDecisionEnvelope:
            response = await self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": "You are the constrained candidate selector for Money Machine.",
                    },
                    {"role": "user", "content": json.dumps(request_prompt, default=str)},
                ],
                text_format=ModelDecision,
            )
            parsed = response.output_parsed
            if parsed is None:
                return safe_model_decision(
                    None,
                    provider="openai",
                    model=str(response.model),
                    provider_response_id=str(response.id),
                )
            return safe_model_decision(
                parsed.model_dump(mode="json"),
                provider="openai",
                model=str(response.model),
                provider_response_id=str(response.id),
            )

        try:
            first = await request_decision(prompt)
            if not candidates or (
                first.decision.action is not Action.ABSTAIN
                and first.decision.candidate_id in allowed_candidate_ids
            ):
                return first
            retry_prompt = {
                **prompt,
                "instruction": (
                    "The previous response did not select an eligible candidate. Every candidate "
                    "listed here already passed deterministic candidate, event, liquidity, "
                    "directional-confirmation, and portfolio-eligibility gates. You are the "
                    "ranker, not the gatekeeper: choose exactly one candidate_id from "
                    "allowed_candidate_ids, copied character-for-character. Do not abstain, "
                    "invent a threshold, alter punctuation, or choose an excluded candidate."
                ),
                "allowed_candidate_ids": allowed_candidate_ids,
                "previous_invalid_candidate_id": first.decision.candidate_id,
            }
            retry = await request_decision(retry_prompt)
            retry_metadata = {
                "selection_attempts": 2,
                "candidate_id_retry_used": True,
                "initial_raw_response_hash": first.raw_response_hash,
                "retry_raw_response_hash": retry.raw_response_hash,
                "selection_provenance": "corrective_retry",
            }
            if (
                retry.decision.action is not Action.ABSTAIN
                and retry.decision.candidate_id in allowed_candidate_ids
            ):
                return retry.model_copy(update=retry_metadata)
            selected = candidates[0]
            fallback_confidence = float(selected.minimum_confidence)
            fallback_regime = {
                Action.CALL_DEBIT_SPREAD: Regime.DIRECTIONAL_UP,
                Action.PUT_DEBIT_SPREAD: Regime.DIRECTIONAL_DOWN,
                Action.INDEX_CONDOR: Regime.CALM,
            }.get(selected.action, Regime.CALM)
            return retry.model_copy(
                update={
                    **retry_metadata,
                    "decision": ModelDecision(
                        regime=fallback_regime,
                        action=selected.action,
                        candidate_id=selected.candidate_id,
                        confidence=fallback_confidence,
                        thesis=(
                            "Deterministic selector fallback chose the highest-ranked candidate "
                            "after two invalid model selections."
                        ),
                        evidence=(
                            "The candidate passed every deterministic pre-auction gate.",
                            "Authoritative risk evaluation remains required before any order.",
                        ),
                        invalidation=(
                            "Any authoritative risk failure retains cash and submits no order.",
                        ),
                        maximum_holding_minutes=selected.maximum_holding_minutes or 60,
                    ),
                    "validation_error": "DeterministicTopRankedSelectionFallback",
                    "deterministic_fallback_used": True,
                    "selection_provenance": "deterministic_top_ranked",
                }
            )
        except Exception as exc:  # provider failures must fail closed
            digest = hashlib.sha256(type(exc).__name__.encode()).hexdigest()
            return ModelDecisionEnvelope(
                decision=ModelDecision.abstention("Model provider unavailable; cycle abstained."),
                raw_response_hash=digest,
                validation_error=type(exc).__name__,
                provider="openai",
                model=self.model,
            )
