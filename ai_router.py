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
FALLBACKS = [
    {
        "provider": "google_gemini",
        "model": "gemini-flash-lite-latest",
        "endpoint": (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-flash-lite-latest:generateContent"
        ),
    },
    {
        "provider": "groq",
        "model": "groq/compound",
        "endpoint": "https://api.groq.com/openai/v1/chat/completions",
    },
    {
        "provider": "openrouter",
        "model": "groq/compound",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
    },
]


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
    }


async def _get_candidates():
    candidates = []

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

        seen = set()

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
                continue

            seen.add(identity)
            candidates.append(item)

    except Exception:
        logger.exception("Could not load AI Registry")

    # Always preserve operational fallbacks.
    for item in FALLBACKS:
        if not _key_for(item["provider"]):
            continue

        identity = (
            item["provider"],
            item["model"],
            item["endpoint"],
        )

        if identity not in {
            (x["provider"], x["model"], x["endpoint"])
            for x in candidates
        }:
            candidates.append(item)

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

    if "401" in message or "403" in message:
        return "AUTH_FAILED", "invalid"

    if "429" in message or "rate" in message:
        return "RATE_LIMITED", None

    if "timeout" in message or "timed out" in message:
        return "TIMEOUT", None

    if "http " in message:
        return "UPSTREAM_ERROR", None

    return "UNAVAILABLE", None


async def _record_success(item, latency_ms):
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
    registry_id = item.get("id")

    if not registry_id:
        return

    availability, auth_status = _classify_error(exc)

    try:
        await database.ai_registry_mark_failure(
            registry_id,
            availability=availability,
            error_category=availability.lower(),
            auth_status=auth_status,
            notes=str(exc)[:300],
        )
    except Exception:
        logger.exception("Failed to record AI failure")


async def generate_medical_ai_response(prompt: str) -> str:
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

    return (
        "⚠️ تعذر الوصول إلى خدمات الذكاء الاصطناعي حالياً.\n"
        "يرجى المحاولة بعد قليل."
    )
