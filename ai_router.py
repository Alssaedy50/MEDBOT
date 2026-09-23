"""AI Router — compatibility facade over the single MEDBOT AI implementation.

Historically MEDBOT had two divergent AI modules (``ai_router.py`` and
``ai.py``) with duplicated provider adapters, error classification, and
failover logic. That duplication risked the two copies drifting apart.

``ai.py`` is now the single source of truth for:

    - provider discovery
    - provider adapters (Gemini / Groq / OpenRouter)
    - error classification and failover
    - grounding validation
    - MEDBOT-grounded assistant responses

This module re-exports that implementation so existing callers (for example
``ai_discovery.py``) keep working without maintaining a second copy.

Do not add provider logic here — extend ``ai.py`` instead.
"""

import httpx

from ai import (  # noqa: F401  (re-exported for existing callers)
    NOT_REGISTERED_MESSAGE,
    SYSTEM_PROMPT,
    MEDBOT_ASSISTANT_PROMPT,
    TIMEOUT,
    GroundingValidator,
    build_library_context,
    generate_medical_ai_response,
    generate_medbot_assistant_response,
    _get_candidates,
    _gemini_request,
    _openai_compatible_request,
    _request,
    _classify_error,
    _record_success,
    _record_failure,
)

__all__ = [
    "httpx",
    "NOT_REGISTERED_MESSAGE",
    "SYSTEM_PROMPT",
    "MEDBOT_ASSISTANT_PROMPT",
    "TIMEOUT",
    "GroundingValidator",
    "build_library_context",
    "generate_medical_ai_response",
    "generate_medbot_assistant_response",
    "_get_candidates",
    "_gemini_request",
    "_openai_compatible_request",
    "_request",
    "_classify_error",
    "_record_success",
    "_record_failure",
]
