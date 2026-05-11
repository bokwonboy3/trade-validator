"""Base for all specialist agents — shared invocation + failure handling."""
from __future__ import annotations

from typing import Any

from agents.client import AgentClient, AgentClientError
from agents.types import SpecialistOutput


def run_specialist(
    *,
    client: AgentClient,
    name: str,
    system_prompt: str,
    user_content: str,
    expected_keys: list[str],
) -> SpecialistOutput:
    """Generic specialist runner.

    Wraps `client.call_structured` and converts failures to a SpecialistOutput
    with `failed=True` (rather than raising). This is the foundation for
    graceful degradation — if one specialist fails, others still run and the
    meta-judge sees the failure flag.

    Validates that the response includes ``expected_keys`` plus the required
    meta fields (`confidence`, `rationale`).
    """
    try:
        body = client.call_structured(
            system_prompt=system_prompt,
            user_content=user_content,
        )
    except AgentClientError as e:
        return SpecialistOutput(name=name, failed=True, failure_reason=str(e))

    missing = [k for k in (*expected_keys, "confidence", "rationale") if k not in body]
    if missing:
        return SpecialistOutput(
            name=name,
            failed=True,
            failure_reason=f"response missing keys: {missing}",
        )

    findings = {k: body[k] for k in expected_keys}
    try:
        confidence = int(body["confidence"])
    except (TypeError, ValueError):
        return SpecialistOutput(
            name=name,
            failed=True,
            failure_reason=f"confidence not int: {body.get('confidence')!r}",
        )
    confidence = max(1, min(10, confidence))
    rationale = str(body["rationale"])[:500]
    return SpecialistOutput(
        name=name,
        findings=findings,
        confidence=confidence,
        rationale=rationale,
    )
