import os
import socket
import logging
import time

import httpx
from dotenv import load_dotenv

import database

# Force IPv4 to avoid IPv6/network issues in Termux.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(
        host,
        port,
        socket.AF_INET,
        type,
        proto,
        flags,
    )


socket.getaddrinfo = _ipv4

load_dotenv(dotenv_path="./.env")

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """أنت المساعد الطبي الذكي لمنصة MEDBOT.
أجب بدقة علمية موثوقة لطلاب الطب والعلوم الصحية.
القواعد:
1. اذكر المصطلحات الطبية باللغة الإنجليزية واشرحها بالعربية بوضوح.
2. نسق الإجابة بنقاط واضحة ومباشرة دون حشو.
3. لا تخترع معلومات أو مراجع.
4. إذا كان السؤال يحتاج تشخيصاً أو علاجاً شخصياً، وضّح أن الإجابة تعليمية وليست بديلاً عن الطبيب.
"""

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
OR_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Fallback models used only when Registry data is unavailable
# or the registered provider cannot be reached.
# Last-known-good models verified against the current MEDBOT keys.
# Discovery may temporarily timeout, so these remain available as a safety net.
KNOWN_GOOD_MODELS = {
    "google_gemini": [
        "gemini-flash-lite-latest",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ],
}

_DISCOVERY_CACHE = {}
_DISCOVERY_LAST_RUN = {}
DISCOVERY_TTL_SECONDS = 900


FALLBACKS = (
    [
    {
        "provider": "google_gemini",
        "model": model,
        "endpoint": (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        ),
    }
    for model in KNOWN_GOOD_MODELS["google_gemini"]
]
    + [
        {
            "provider": "groq",
            "model": "groq/compound",
            "endpoint": "https://api.groq.com/openai/v1/chat/completions",
        },
        {
            "provider": "openrouter",
            "model": "nex-agi/nex-n2.5-pro:free",
            "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        },
    ]
)


def _key_for(provider: str) -> str:
    if provider == "google_gemini":
        return GEMINI_KEY
    if provider == "groq":
        return GROQ_KEY
    if provider == "openrouter":
        return OR_KEY
    return ""


def _is_healthy(availability: str) -> bool:
    return availability in {"VERIFIED", "AVAILABLE"}


def _registry_row_to_provider(row):
    return {
        "id": row[0],
        "provider": row[1],
        "model": row[2],
        "endpoint": row[3],
        "availability": row[4],
        "auth_status": row[5] if len(row) > 5 else None,
        "latency_ms": row[6] if len(row) > 6 else None,
        "success_rate": row[7] if len(row) > 7 else None,
    }



async def _discover_gemini_models(client):
    """Discover currently exposed Gemini text-generation models."""
    if not GEMINI_KEY:
        return []

    now = time.time()
    cached = _DISCOVERY_CACHE.get("google_gemini")

    if cached and now - _DISCOVERY_LAST_RUN.get("google_gemini", 0) < DISCOVERY_TTL_SECONDS:
        return cached

    endpoint = "https://generativelanguage.googleapis.com/v1beta/models"

    try:
        response = await client.get(
            endpoint,
            params={"key": GEMINI_KEY, "pageSize": 100},
            timeout=httpx.Timeout(12.0, connect=5.0),
        )
        response.raise_for_status()

        data = response.json()
        discovered = []

        for model in data.get("models", []):
            name = model.get("name", "")
            methods = model.get("supportedGenerationMethods", [])

            if not name or "generateContent" not in methods:
                continue

            model_id = name.rsplit("/", 1)[-1]

            discovered.append({
                "provider": "google_gemini",
                "model": model_id,
                "endpoint": (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model_id}:generateContent"
                ),
            })

        if discovered:
            _DISCOVERY_CACHE["google_gemini"] = discovered
            _DISCOVERY_LAST_RUN["google_gemini"] = now
            logger.info(
                "Gemini discovery found %d text-generation models",
                len(discovered),
            )
            return discovered

        logger.warning("Gemini discovery returned no usable models")

    except Exception as exc:
        logger.warning("Gemini discovery failed: %s", exc)

    return [
        {
            "provider": "google_gemini",
            "model": model,
            "endpoint": (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent"
            ),
        }
        for model in KNOWN_GOOD_MODELS["google_gemini"]
    ]


async def _discover_groq_models(client):
    if not GROQ_KEY:
        return []

    now = time.time()
    cached = _DISCOVERY_CACHE.get("groq")

    if cached and now - _DISCOVERY_LAST_RUN.get("groq", 0) < DISCOVERY_TTL_SECONDS:
        return cached

    endpoint = "https://api.groq.com/openai/v1/models"

    try:
        response = await client.get(
            endpoint,
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            timeout=httpx.Timeout(12.0, connect=5.0),
        )
        response.raise_for_status()

        data = response.json()
        discovered = []

        excluded_prefixes = (
            "whisper",
            "canopylabs/orpheus",
            "meta-llama/llama-prompt-guard",
        )

        for model in data.get("data", []):
            model_id = model.get("id", "")

            if not model_id or model_id.lower().startswith(excluded_prefixes):
                continue

            discovered.append({
                "provider": "groq",
                "model": model_id,
                "endpoint": "https://api.groq.com/openai/v1/chat/completions",
            })

        if discovered:
            _DISCOVERY_CACHE["groq"] = discovered
            _DISCOVERY_LAST_RUN["groq"] = now
            logger.info(
                "Groq discovery found %d text models",
                len(discovered),
            )
            return discovered

        logger.warning("Groq discovery returned no usable models")

    except Exception as exc:
        logger.warning("Groq discovery failed: %s", exc)

    return []


async def _discover_openrouter_models(client):
    if not OR_KEY:
        return []

    now = time.time()
    cached = _DISCOVERY_CACHE.get("openrouter")

    if cached and now - _DISCOVERY_LAST_RUN.get("openrouter", 0) < DISCOVERY_TTL_SECONDS:
        return cached

    endpoint = "https://openrouter.ai/api/v1/models"

    try:
        response = await client.get(
            endpoint,
            headers={
                "Authorization": f"Bearer {OR_KEY}",
                "HTTP-Referer": "https://medbot.local",
                "X-Title": "MEDBOT",
            },
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        response.raise_for_status()

        data = response.json()
        discovered = []

        excluded_terms = (
            "embedding",
            "whisper",
            "rerank",
            "moderation",
            "tts",
            "image",
            "vision",
        )

        for model in data.get("data", []):
            model_id = model.get("id", "")

            if not model_id:
                continue

            lowered = model_id.lower()

            if any(term in lowered for term in excluded_terms):
                continue

            architecture = model.get("architecture") or {}
            input_modalities = architecture.get("input_modalities") or []

            if input_modalities and "text" not in input_modalities:
                continue

            discovered.append({
                "provider": "openrouter",
                "model": model_id,
                "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            })

        if discovered:
            _DISCOVERY_CACHE["openrouter"] = discovered
            _DISCOVERY_LAST_RUN["openrouter"] = now
            logger.info(
                "OpenRouter discovery found %d candidate models",
                len(discovered),
            )
            return discovered

        logger.warning("OpenRouter discovery returned no usable models")

    except Exception as exc:
        logger.warning("OpenRouter discovery failed: %s", exc)

    return []


async def _ensure_candidate_registry_ids(candidates):
    for item in candidates:
        provider = item.get("provider")
        model = item.get("model")
        endpoint = item.get("endpoint")

        if not provider or not model or not endpoint:
            continue

        try:
            registry_id = await database.ai_registry_ensure(
                provider=provider,
                model=model,
                endpoint=endpoint,
                availability="DISCOVERED",
                auth_status="valid" if _key_for(provider) else None,
                capabilities="text_generation",
            )

            if registry_id:
                item["id"] = registry_id
                item["availability"] = item.get("availability") or "DISCOVERED"

        except Exception:
            logger.exception(
                "Failed to register AI candidate: %s/%s",
                provider,
                model,
            )

    return candidates


def _is_model_suitable_for_medbot(item):
    provider = item.get("provider", "")
    model = item.get("model", "").lower()

    if not model:
        return False

    excluded = (
        "tts",
        "image",
        "embedding",
        "whisper",
        "rerank",
        "moderation",
        "prompt-guard",
        "audio",
        "transcribe",
        "speech",
    )

    if any(term in model for term in excluded):
        return False

    if provider == "google_gemini":
        # Keep general Gemini/Gemma text models.
        return (
            model.startswith("gemini-")
            or model.startswith("gemma-")
        )

    if provider == "groq":
        return True

    if provider == "openrouter":
        return True

    return False



ACTIVE_POOL_SIZE = 12
DISCOVERED_PER_PROVIDER = 2



async def _probe_model(item):
    """Lightweight health probe for a discovered model."""
    probe_prompt = "أجب بكلمة واحدة: ما هو تعريف الحمى؟"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            started = time.perf_counter()

            answer = await _request(
                client,
                item,
                probe_prompt,
            )

            latency_ms = round(
                (time.perf_counter() - started) * 1000,
                1,
            )

        if not answer or not answer.strip():
            raise RuntimeError("EMPTY_MODEL_RESPONSE")

        registry_id = item.get("id")

        if registry_id:
            await database.ai_registry_mark_success(
                registry_id,
                latency_ms=latency_ms,
                notes="health probe succeeded",
            )

        item["availability"] = "VERIFIED"
        item["latency_ms"] = latency_ms
        item["success_rate"] = 1.0

        _mark_model_success(item)

        logger.info(
            "AI probe succeeded: %s/%s in %sms",
            item.get("provider"),
            item.get("model"),
            latency_ms,
        )

        return True

    except Exception as exc:
        await _record_failure(item, exc)

        logger.warning(
            "AI probe failed: %s/%s: %s",
            item.get("provider"),
            item.get("model"),
            exc,
        )

        return False


async def _build_active_pool(candidates):
    """Build a balanced request-time pool from broad discovery."""

    if not candidates:
        return []

    MAX_POOL = ACTIVE_POOL_SIZE
    MIN_PER_PROVIDER = 2

    groups = {}

    for item in candidates:
        provider = item.get("provider", "")
        groups.setdefault(provider, []).append(item)

    def score(item):
        availability = item.get("availability", "DISCOVERED")

        return (
            0 if availability == "VERIFIED" else
            1 if availability == "AVAILABLE" else
            2,
            -(item.get("success_rate") or 0.0),
            item.get("latency_ms") or 999999,
        )

    for items in groups.values():
        items.sort(key=score)

    pool = []
    seen = set()

    def add(item):
        key = _model_key(item)

        if key in seen:
            return False

        seen.add(key)
        pool.append(item)
        return True

    # First guarantee representation from every provider that has candidates.
    for provider, items in groups.items():
        for item in items[:MIN_PER_PROVIDER]:
            if len(pool) >= MAX_POOL:
                break
            add(item)

    # Then fill remaining slots globally by health/performance.
    remaining = []

    for provider, items in groups.items():
        for item in items[MIN_PER_PROVIDER:]:
            remaining.append(item)

    remaining.sort(key=score)

    for item in remaining:
        if len(pool) >= MAX_POOL:
            break
        add(item)

    logger.info(
        "AI active pool contains %d/%d models",
        len(pool),
        len(candidates),
    )

    return pool



async def _refresh_discovered_models(candidates):
    """Probe a tiny rotating sample of the least recently tested models."""
    discovered = [
        item
        for item in candidates
        if item.get("availability") == "DISCOVERED"
    ]
    if not discovered:
        return candidates

    try:
        rows = await database.ai_registry_get_all()
        last_test_by_id = {
            row[0]: row[13] if len(row) > 13 else None
            for row in rows
        }
    except Exception:
        logger.exception("Could not load AI registry test timestamps")
        last_test_by_id = {}

    discovered.sort(
        key=lambda item: (
            last_test_by_id.get(item.get("id")) is not None,
            last_test_by_id.get(item.get("id")) or "",
        )
    )

    # At most two probes per refresh to protect provider quotas.
    for item in discovered[:2]:
        await _probe_model(item)

    return candidates

async def _get_candidates():
    candidates = []

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as discovery_client:
            discovered = await _discover_gemini_models(discovery_client)
            discovered += await _discover_groq_models(discovery_client)
            discovered += await _discover_openrouter_models(discovery_client)

        for item in discovered:
            if _is_model_suitable_for_medbot(item):
                candidates.append(item)

    except Exception:
        logger.exception("AI discovery stage failed")

    try:
        rows = await database.ai_registry_get_healthy()

        # Prefer VERIFIED over merely AVAILABLE.
        rows = sorted(
            rows,
            key=lambda r: (
                0 if r[4] == "VERIFIED" else 1,
                -(r[7] or 0.0),
                r[6] if r[6] is not None else 999999,
            ),
        )

        seen = {
            (x["provider"], x["model"], x["endpoint"])
            for x in candidates
        }

        for row in rows:
            item = _registry_row_to_provider(row)

            if not item["provider"] or not item["model"]:
                continue

            if not _is_healthy(item["availability"]):
                continue

            if not _key_for(item["provider"]):
                continue

            identity = (
                item["provider"],
                item["model"],
                item["endpoint"],
            )

            if identity in seen:
                # Merge verified/available registry health into the
                # freshly discovered candidate instead of discarding it.
                for existing in candidates:
                    if (
                        existing["provider"],
                        existing["model"],
                        existing["endpoint"],
                    ) == identity:
                        existing.update(item)
                        existing["availability"] = item["availability"]
                        existing["id"] = item.get("id")
                        existing["latency_ms"] = row[6]
                        existing["success_rate"] = row[7]
                        break
                continue

            seen.add(identity)
            item["latency_ms"] = row[6]
            item["success_rate"] = row[7]
            candidates.append(item)

    except Exception:
        logger.exception("Could not load AI Registry")

    # Always preserve operational fallbacks.
    seen = {
        (x["provider"], x["model"], x["endpoint"])
        for x in candidates
    }

    for item in FALLBACKS:
        if not _key_for(item["provider"]):
            continue

        identity = (
            item["provider"],
            item["model"],
            item["endpoint"],
        )

        if identity not in seen:
            candidates.append(item)
            seen.add(identity)

    candidates = _filter_runtime_healthy(candidates)
    candidates = await _ensure_candidate_registry_ids(candidates)

    candidates = await _build_active_pool(candidates)

    logger.info(
        "AI candidate pool contains %d active models",
        len(candidates),
    )

    return candidates


async def _gemini_request(client, item, prompt):
    endpoint = item["endpoint"]
    if not endpoint:
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{item['model']}:generateContent"
        )

    response = await client.post(
        endpoint,
        params={"key": GEMINI_KEY},
        json={
            "system_instruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
            },
        },
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    text = ""
    candidates = data.get("candidates") or []

    if candidates:
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        text = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict)
        ).strip()

    if not text:
        raise RuntimeError("Gemini returned an empty response")

    return text


async def _openai_compatible_request(client, item, prompt):
    provider = item["provider"]

    if provider == "groq":
        api_key = GROQ_KEY
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        endpoint = item["endpoint"]
    elif provider == "openrouter":
        api_key = OR_KEY
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://telegram.org",
            "X-Title": "MEDBOT",
        }
        endpoint = item["endpoint"]
    else:
        raise RuntimeError(f"Unsupported OpenAI-compatible provider: {provider}")

    response = await client.post(
        endpoint,
        headers=headers,
        json={
            "model": item["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"{provider} HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"{provider} returned an invalid response"
        ) from exc

    if not text:
        raise RuntimeError(f"{provider} returned an empty response")

    return text


async def _request(client, item, prompt):
    if item["provider"] == "google_gemini":
        return await _gemini_request(client, item, prompt)

    if item["provider"] in {"groq", "openrouter"}:
        return await _openai_compatible_request(client, item, prompt)

    raise RuntimeError(f"Unsupported AI provider: {item['provider']}")


def _classify_error(exc: Exception):
    message = str(exc).lower()

    if (
        "key limit exceeded" in message
        or "total limit" in message
        or "quota exceeded" in message
        or "insufficient_quota" in message
    ):
        return "QUOTA_LIMITED", None, "QUOTA_LIMITED"

    if "api_key_invalid" in message or "invalid api key" in message:
        return "AUTH_FAILED", "invalid", "AUTH_FAILED"

    if "401" in message:
        return "AUTH_FAILED", "invalid", "AUTH_FAILED"

    if "429" in message or "rate" in message:
        return "RATE_LIMITED", None, "RATE_LIMITED"

    if "timeout" in message or "timed out" in message:
        return "TIMEOUT", None, "TIMEOUT"

    if (
        "no address associated with hostname" in message
        or "name or service not known" in message
        or "temporary failure in name resolution" in message
        or "nodename nor servname provided" in message
        or "network is unreachable" in message
        or "connection reset" in message
        or "connecterror" in message
        or "connecttimeout" in message
    ):
        return "NETWORK_ERROR", None, "NETWORK_ERROR"

    if "403" in message:
        return "AUTH_FAILED", "forbidden", "AUTH_FAILED"

    if "http " in message:
        return "UPSTREAM_ERROR", None, "UPSTREAM_ERROR"

    return "UNAVAILABLE", None, "UNAVAILABLE"


from collections import defaultdict

_MODEL_FAILURES = defaultdict(int)
_MODEL_COOLDOWN_UNTIL = {}
_MODEL_LAST_ERROR = {}
_MODEL_SUCCESS_COUNT = defaultdict(int)

COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 900

def _model_key(item):
    return (
        item.get("provider", ""),
        item.get("model", ""),
        item.get("endpoint", ""),
    )


def _is_in_cooldown(item):
    key = _model_key(item)
    until = _MODEL_COOLDOWN_UNTIL.get(key, 0.0)

    if until <= time.time():
        _MODEL_COOLDOWN_UNTIL.pop(key, None)
        return False

    return True


def _mark_model_success(item):
    key = _model_key(item)
    _MODEL_FAILURES[key] = 0
    _MODEL_LAST_ERROR.pop(key, None)
    _MODEL_COOLDOWN_UNTIL.pop(key, None)
    _MODEL_SUCCESS_COUNT[key] += 1


def _mark_model_failure(item, error_category):
    key = _model_key(item)

    _MODEL_FAILURES[key] += 1
    _MODEL_LAST_ERROR[key] = error_category

    failures = _MODEL_FAILURES[key]
    cooldown = min(
        COOLDOWN_SECONDS * (2 ** max(0, failures - 1)),
        MAX_COOLDOWN_SECONDS,
    )

    _MODEL_COOLDOWN_UNTIL[key] = time.time() + cooldown


def _filter_runtime_healthy(items):
    return [
        item
        for item in items
        if not _is_in_cooldown(item)
    ]




async def _record_success(item, latency_ms):
    _mark_model_success(item)

    registry_id = item.get("id")
    if not registry_id:
        return

    try:
        await database.ai_registry_mark_success(
            registry_id,
            latency_ms=latency_ms,
            notes="verified by MEDBOT router",
        )
    except Exception:
        logger.exception("Failed to record AI success")


async def _record_failure(item, exc):
    availability, auth_status, error_category = _classify_error(exc)

    _mark_model_failure(item, error_category)

    registry_id = item.get("id")
    if not registry_id:
        return

    try:
        await database.ai_registry_mark_failure(
            registry_id,
            availability=availability,
            error_category=error_category,
            auth_status=auth_status,
            notes=str(exc)[:300],
        )
    except Exception:
        logger.exception("Failed to record AI failure")


async def generate_medical_ai_response(prompt: str, user_id: int = None) -> str:
    prompt = (prompt or "").strip()

    if not prompt:
        return "⚠️ يرجى كتابة سؤال واضح."

    candidates = await _get_candidates()

    if not candidates:
        return "⚠️ لا توجد خدمة ذكاء اصطناعي متاحة حالياً."

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for item in candidates:
            provider = item["provider"]

            try:
                started = time.perf_counter()

                answer = await _request(
                    client,
                    item,
                    prompt,
                )

                latency_ms = round(
                    (time.perf_counter() - started) * 1000,
                    1,
                )

                registry_id = item.get("id")
                if registry_id:
                    try:
                        await database.ai_usage_record(
                            registry_id=registry_id,
                            user_id=user_id,
                            latency_ms=latency_ms,
                            success=True,
                        )
                    except Exception:
                        logger.exception("Failed to record AI usage success")

                await _record_success(item, latency_ms)

                logger.info(
                    "AI success provider=%s model=%s latency_ms=%s",
                    provider,
                    item["model"],
                    latency_ms,
                )

                return answer

            except Exception as exc:
                logger.warning(
                    "AI provider failed provider=%s model=%s error=%s",
                    provider,
                    item["model"],
                    exc,
                )

                await _record_failure(item, exc)

                registry_id = item.get("id")
                if registry_id:
                    try:
                        _, _, error_category = _classify_error(exc)
                        await database.ai_usage_record(
                            registry_id=registry_id,
                            user_id=user_id,
                            success=False,
                            error_category=error_category,
                        )
                    except Exception:
                        logger.exception("Failed to record AI usage failure")

    return (
        "⚠️ تعذر الوصول إلى خدمات الذكاء الاصطناعي حالياً.\n"
        "يرجى المحاولة بعد قليل."
    )
