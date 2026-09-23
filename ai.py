import os
import socket
import asyncio
import logging
import time
from random import SystemRandom

import httpx
from dotenv import load_dotenv

import database
import search_engine
from medical_sources import search_pubmed, build_source_context

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

SYSTEM_PROMPT = """أنت المساعد الطبي الذكي لمنصة MEDBOT لطلاب الطب والعلوم الصحية.

الهدف: تقديم إجابة طبية قصيرة، مباشرة، علمياً موثوقة وسريعة.

القواعد الإلزامية:
1. أجب عن السؤال مباشرة دون مقدمات أو حشو.
2. اجعل الإجابة مختصرة: أهم النقاط فقط، ويفضل 3-7 نقاط عند مناسبة ذلك.
3. اذكر المصطلحات الطبية الأساسية باللغة الإنجليزية مع شرح عربي مختصر عند الحاجة.
4. لا تخترع أي معلومة أو مرجع أو مصدر.
5. عند ذكر مصدر، استخدم مصدراً طبياً موثوقاً فقط، مثل NCBI/NIH أو إرشادات الجمعيات الطبية أو المراجع الطبية الأكاديمية المعروفة.
6. لا تنسب معلومة إلى مصدر لم تتحقق منها.
7. إذا لم تكن متأكداً من معلومة، قل بوضوح إنك غير متأكد بدلاً من التخمين.
8. لا تطيل الشرح إلا إذا طلب المستخدم التفصيل.
9. إذا كان السؤال عن تشخيص أو علاج شخصي، اجعل الإجابة تعليمية ومختصرة واذكر أنها لا تغني عن تقييم الطبيب.
10. لا تقدم تشخيصاً شخصياً أو وصفة علاجية شخصية بناءً على معلومات محدودة.

11. استخدم المصادر التي يزوّدك بها النظام فقط عند ذكر مصدر تم التحقق منه.
12. لا تخترع PMID أو رابطاً أو اسم مصدر.
13. إذا وُجدت مصادر موثقة، اجعل الإجابة مبنية عليها قدر الإمكان.
14. في نهاية الإجابة أضف سطراً قصيراً بعنوان "Source:" يتضمن عنوان المصدر وPMID فقط.
15. إذا لم توجد مصادر موثقة، لا تقل إنك تحققت من مصدر؛ أجب بحذر ولا تنشئ مرجعاً من نفسك.
"""

MEDBOT_ASSISTANT_PROMPT = """أنت مساعد منصة MEDBOT، وهي منصة تعليمية طبية على Telegram.

دورك محدد وليس دور طبيب أو شات طبي عام:
- التعريف بمنصة MEDBOT وشرح طريقة استخدامها.
- فهم ما يريده الطالب داخل MEDBOT.
- مساعدته في الوصول إلى الموارد المسجّلة في المكتبة (أقسام/ملفات).

قواعد إلزامية:
1. اعتمد فقط على "نتائج البحث" المزوّدة من قاعدة بيانات MEDBOT في سياق الرسالة.
2. ممنوع تماماً اختراع ملف أو قسم أو مسار أو رابط أو محاضرة أو مورد.
3. إذا كانت نتائج البحث فارغة، أو لم يوجد المورد المطلوب، فرد حرفياً بهذه الجملة دون إضافة:
الموارد المطلوبة غير مسجلة حالياً في MEDBOT.
4. لا تكرر شجرة MEDBOT كاملة ولا تخمّن موارد غير مذكورة في السياق.
5. اجعل الإجابة قصيرة: عنوان المورد ومساره الفعلي فقط.
6. إذا سأل الطالب عن طريقة استخدام MEDBOT، اشرح الاستخدام باختصار دون اختلاق موارد.
"""

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
OR_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# Exact refusal string required by the MEDBOT specification.
NOT_REGISTERED_MESSAGE = (
    "الموارد المطلوبة غير مسجلة حالياً في MEDBOT."
)

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

# The verified active pool is expensive to build: provider discovery plus up
# to six health probes, all network round-trips. Rebuilding it on every user
# message is what made /start-adjacent AI replies slow. Cache the resulting
# pool for a short TTL (and on failure keep serving the previous pool).
_CANDIDATE_CACHE = {"pool": None}
_CANDIDATE_CACHE_AT = 0.0
CANDIDATE_TTL_SECONDS = 300
_CANDIDATE_LOCK = asyncio.Lock()


FALLBACKS = ()


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

    logger.warning(
        "Gemini discovery unavailable; no speculative fallback models will be used"
    )
    return []


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
    """Discover text-generation models that are explicitly free on OpenRouter.

    A model is accepted only when:
    - it is a text-capable model,
    - it is not an excluded modality,
    - and OpenRouter reports zero prompt/completion pricing OR
      the model ID explicitly uses the :free suffix.
    """
    if not OR_KEY:
        return []

    now = time.time()
    cached = _DISCOVERY_CACHE.get("openrouter")

    if (
        cached
        and now - _DISCOVERY_LAST_RUN.get("openrouter", 0)
        < DISCOVERY_TTL_SECONDS
    ):
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
            "audio",
            "transcribe",
            "speech",
        )

        for model in data.get("data", []):
            model_id = str(model.get("id") or "").strip()

            if not model_id:
                continue

            lowered = model_id.lower()

            if any(term in lowered for term in excluded_terms):
                continue

            architecture = model.get("architecture") or {}
            input_modalities = architecture.get("input_modalities") or []

            if input_modalities and "text" not in input_modalities:
                continue

            pricing = model.get("pricing") or {}
            prompt_price = pricing.get("prompt")
            completion_price = pricing.get("completion")

            explicitly_free = lowered.endswith(":free")

            try:
                pricing_free = (
                    prompt_price is not None
                    and completion_price is not None
                    and float(prompt_price) == 0.0
                    and float(completion_price) == 0.0
                )
            except (TypeError, ValueError):
                pricing_free = False

            if not (explicitly_free or pricing_free):
                continue

            discovered.append({
                "provider": "openrouter",
                "model": model_id,
                "endpoint": (
                    "https://openrouter.ai/api/v1/chat/completions"
                ),
            })

        if discovered:
            _DISCOVERY_CACHE["openrouter"] = discovered
            _DISCOVERY_LAST_RUN["openrouter"] = now

            logger.info(
                "OpenRouter free discovery found %d models",
                len(discovered),
            )

            return discovered

        logger.warning(
            "OpenRouter discovery returned no explicitly free models"
        )

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
        "content-safety",
        "safety",
        "prompt-guard",
        "guard",
        "classifier",
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
    """Build a request pool using VERIFIED models only.

    Provider diversity is preserved where possible, then the remaining
    verified models are randomized to avoid deterministic preference for
    the first model returned by discovery.
    """
    verified = [
        item
        for item in candidates
        if item.get("availability") == "VERIFIED"
        and item.get("provider")
        and item.get("model")
        and item.get("endpoint")
        and not _is_in_cooldown(item)
        and not (
            item.get("provider") == "openrouter"
            and not str(item.get("model")).lower().endswith(":free")
        )
    ]

    if not verified:
        logger.warning("AI active pool: no VERIFIED models available")
        return []

    max_pool = ACTIVE_POOL_SIZE
    min_per_provider = 2

    rng = SystemRandom()

    groups = {}
    for item in verified:
        groups.setdefault(item["provider"], []).append(item)

    # Randomize within each provider first.
    for items in groups.values():
        rng.shuffle(items)

    pool = []
    seen = set()

    def add(item):
        key = _model_key(item)

        if key in seen or len(pool) >= max_pool:
            return False

        seen.add(key)
        pool.append(item)
        return True

    # Preserve provider diversity.
    providers = list(groups.keys())
    rng.shuffle(providers)

    for provider in providers:
        items = groups[provider]

        for item in items[:min_per_provider]:
            if len(pool) >= max_pool:
                break
            add(item)

    # Fill remaining slots randomly from all remaining VERIFIED models.
    remaining = []

    for items in groups.values():
        remaining.extend(items[min_per_provider:])

    rng.shuffle(remaining)

    for item in remaining:
        if len(pool) >= max_pool:
            break
        add(item)

    logger.info(
        "AI active pool contains %d VERIFIED models from %d providers",
        len(pool),
        len({item["provider"] for item in pool}),
    )

    return pool


async def _refresh_discovered_models(candidates):
    """Probe a small rotating sample while preserving provider diversity.

    The old implementation probed the first two DISCOVERED models globally.
    Because discovery is ordered by provider, that could spend the entire
    probe budget on Gemini and never verify Groq/OpenRouter.

    We therefore select up to two candidates per provider, with a hard
    global limit, prioritizing models that have never been tested or were
    tested least recently.
    """
    discovered = [
        item
        for item in candidates
        if item.get("availability") == "DISCOVERED"
        and not _is_in_cooldown(item)
        and item.get("provider")
    ]

    if not discovered:
        return candidates

    try:
        rows = await database.ai_registry_get_all()
        last_test_by_id = {
            row[0]: row[11] if len(row) > 11 else None
            for row in rows
        }
    except Exception:
        logger.exception("Could not load AI registry test timestamps")
        last_test_by_id = {}

    groups = {}
    for item in discovered:
        groups.setdefault(item["provider"], []).append(item)

    for items in groups.values():
        items.sort(
            key=lambda item: (
                last_test_by_id.get(item.get("id")) is not None,
                last_test_by_id.get(item.get("id")) or "",
            )
        )

    # Keep probing bounded to protect provider quotas while ensuring that
    # available providers get an opportunity to enter the VERIFIED pool.
    MAX_PROBES_PER_PROVIDER = 2
    MAX_TOTAL_PROBES = 6

    providers = list(groups.keys())
    rng = SystemRandom()
    rng.shuffle(providers)

    selected = []

    # First pass: one candidate from every discovered provider.
    for provider in providers:
        if len(selected) >= MAX_TOTAL_PROBES:
            break

        items = groups[provider]
        if items:
            selected.append(items[0])

    # Second pass: one additional candidate per provider.
    for provider in providers:
        if len(selected) >= MAX_TOTAL_PROBES:
            break

        items = groups[provider]
        if len(items) >= MAX_PROBES_PER_PROVIDER:
            selected.append(items[1])

    logger.info(
        "AI probe rotation: selected=%d providers=%d",
        len(selected),
        len(providers),
    )

    for item in selected:
        await _probe_model(item)

    return candidates

async def _get_candidates():
    """Discover, register, verify, and build the active AI pool.

    Lifecycle:
        DISCOVERED -> probe -> VERIFIED -> active pool

    AVAILABLE/UNKNOWN/DISCOVERED models are never used directly.

    The resulting pool is cached for ``CANDIDATE_TTL_SECONDS`` so a burst of
    user messages does not re-run discovery and health probes on every call.
    Callers never mutate the returned pool.
    """
    global _CANDIDATE_CACHE_AT

    now = time.time()
    cached_pool = _CANDIDATE_CACHE.get("pool")

    if cached_pool and now - _CANDIDATE_CACHE_AT < CANDIDATE_TTL_SECONDS:
        return list(cached_pool)

    async with _CANDIDATE_LOCK:
        # Re-check inside the lock: another coroutine may have refreshed it.
        now = time.time()
        cached_pool = _CANDIDATE_CACHE.get("pool")

        if cached_pool and now - _CANDIDATE_CACHE_AT < CANDIDATE_TTL_SECONDS:
            return list(cached_pool)

        pool = await _build_candidates_uncached()

        if pool:
            _CANDIDATE_CACHE["pool"] = pool
            _CANDIDATE_CACHE_AT = time.time()
        elif not cached_pool:
            # Nothing verified yet: do not cache an empty pool, so the next
            # request can retry instead of being stuck with no providers.
            _CANDIDATE_CACHE_AT = 0.0

        return list(pool if pool else (cached_pool or []))


async def _build_candidates_uncached():
    candidates = []

    # --------------------------------------------------------
    # A. Fresh provider discovery
    # --------------------------------------------------------
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as discovery_client:
            discovered = await _discover_gemini_models(discovery_client)
            discovered += await _discover_groq_models(discovery_client)
            discovered += await _discover_openrouter_models(
                discovery_client
            )

        for item in discovered:
            if _is_model_suitable_for_medbot(item):
                item["availability"] = item.get(
                    "availability",
                    "DISCOVERED",
                )
                candidates.append(item)

    except Exception:
        logger.exception("AI discovery stage failed")

    # --------------------------------------------------------
    # B. Merge persisted registry health
    # --------------------------------------------------------
    try:
        rows = await database.ai_registry_get_healthy()

        seen = {
            (
                x["provider"],
                x["model"],
                x["endpoint"],
            )
            for x in candidates
        }

        for row in rows:
            item = _registry_row_to_provider(row)

            if not item["provider"] or not item["model"]:
                continue

            if not _key_for(item["provider"]):
                continue

            if not _is_model_suitable_for_medbot(item):
                continue

            # OpenRouter registry rows do not persist pricing metadata.
            # Therefore only explicit :free models may survive from
            # persisted registry data into the active candidate pipeline.
            if (
                item["provider"] == "openrouter"
                and not str(item["model"]).lower().endswith(":free")
            ):
                continue

            identity = (
                item["provider"],
                item["model"],
                item["endpoint"],
            )

            if identity in seen:
                for existing in candidates:
                    existing_identity = (
                        existing["provider"],
                        existing["model"],
                        existing["endpoint"],
                    )

                    if existing_identity == identity:
                        existing.update(item)
                        break

                continue

            seen.add(identity)
            candidates.append(item)

    except Exception:
        logger.exception("Could not load AI Registry")

    # --------------------------------------------------------
    # C. Register candidates without promoting them.
    # --------------------------------------------------------
    candidates = await _ensure_candidate_registry_ids(candidates)

    # --------------------------------------------------------
    # D. Probe a very small number of unverified candidates.
    # --------------------------------------------------------
    candidates = await _refresh_discovered_models(candidates)

    # --------------------------------------------------------
    # E. Only VERIFIED models are allowed into the pool.
    # --------------------------------------------------------
    verified = [
        item
        for item in candidates
        if item.get("availability") == "VERIFIED"
        and _key_for(item.get("provider"))
        and not _is_in_cooldown(item)
    ]

    active_pool = await _build_active_pool(verified)

    logger.info(
        "AI candidate pipeline: discovered=%d verified=%d active=%d",
        len(candidates),
        len(verified),
        len(active_pool),
    )

    return active_pool


async def _gemini_request(client, item, prompt, system_prompt=SYSTEM_PROMPT):
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
                "parts": [{"text": system_prompt}]
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


async def _openai_compatible_request(client, item, prompt, system_prompt=SYSTEM_PROMPT):
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
                {"role": "system", "content": system_prompt},
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


async def _request(client, item, prompt, system_prompt=SYSTEM_PROMPT):
    if item["provider"] == "google_gemini":
        return await _gemini_request(client, item, prompt, system_prompt)

    if item["provider"] in {"groq", "openrouter"}:
        return await _openai_compatible_request(client, item, prompt, system_prompt)

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


def _result_line(item: dict) -> str:
    title = item.get("title") or item.get("name") or "بدون عنوان"
    path = item.get("path") or "بدون مسار"
    kind = "قسم" if item.get("result_type") in ("FOLDER", "EMPTY_FOLDER") else "مورد"
    return f"- [{kind}] {title} | المسار: {path}"


def build_library_context(results: list) -> str:
    """Render deterministic search results into a compact grounding context."""
    if not results:
        return "لا توجد نتائج مطابقة في قاعدة بيانات MEDBOT."

    return "\n".join(_result_line(item) for item in results)


class GroundingValidator:
    """Reject/handle answers that are not grounded in registered MEDBOT data.

    The validator is intentionally conservative: when the model returns an
    empty answer, or the deterministic search found nothing, the caller is
    told to fall back to the exact refusal message. It never rewrites a
    grounded answer or invents content.
    """

    def allows(self, answer: str) -> bool:
        return bool((answer or "").strip())


async def _search_medbot(query: str) -> list:
    try:
        response = await search_engine.search_library_summary(query, limit=10)
    except Exception:
        logger.exception("MEDBOT search failed during AI grounding")
        return []

    if not isinstance(response, dict):
        return []

    results = response.get("results", [])
    return results if isinstance(results, list) else []


async def generate_medbot_assistant_response(
    prompt: str,
    user_id: int = None,
) -> str:
    """MEDBOT-grounded assistant.

    Pipeline:
        prompt
        -> deterministic SQLite search
        -> grounding context (registered folders/content only)
        -> AI Router / provider
        -> grounding validation
        -> Telegram text

    The AI never receives the full MEDBOT tree and never invents resources.
    """
    prompt = (prompt or "").strip()

    if not prompt:
        return "⚠️ يرجى كتابة سؤال واضح."

    results = await _search_medbot(prompt)

    # No registered resource -> deterministic refusal, no model call.
    if not results:
        return NOT_REGISTERED_MESSAGE

    context = build_library_context(results)

    candidates = await _get_candidates()

    if not candidates:
        # Fall back to deterministic results rather than hallucinating.
        return (
            "📚 *نتائج البحث داخل MEDBOT*\n\n"
            f"{context}\n\n"
            "⚠️ خدمة الذكاء الاصطناعي غير متاحة حالياً."
        )

    grounded_prompt = (
        f"نتائج البحث داخل MEDBOT (المصدر الوحيد المسموح):\n"
        f"{context}\n\n"
        f"طلب المستخدم:\n{prompt}\n\n"
        "اعرض للمستخدم المورد/القسم المناسب مع مساره الفعلي فقط. "
        "لا تخترع أي مورد غير مذكور أعلاه."
    )

    validator = GroundingValidator()

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for item in candidates:
            provider = item["provider"]

            try:
                started = time.perf_counter()

                answer = await _request(
                    client,
                    item,
                    grounded_prompt,
                    MEDBOT_ASSISTANT_PROMPT,
                )

                if not validator.allows(answer):
                    raise RuntimeError("Grounding validator rejected empty answer")

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
                    "MEDBOT assistant success provider=%s model=%s latency_ms=%s",
                    provider,
                    item["model"],
                    latency_ms,
                )

                return answer

            except Exception as exc:
                logger.warning(
                    "MEDBOT assistant provider failed provider=%s model=%s error=%s",
                    provider,
                    item["model"],
                    exc,
                )

                await _record_failure(item, exc)

    # All providers failed: return grounded deterministic results.
    return (
        "📚 *نتائج البحث داخل MEDBOT*\n\n"
        f"{context}\n\n"
        "⚠️ تعذر الوصول إلى خدمة الذكاء الاصطناعي؛ النتائج أعلاه مأخوذة "
        "مباشرة من قاعدة بيانات MEDBOT."
    )


async def generate_medical_ai_response(prompt: str, user_id: int = None) -> str:
    # REAL MEDICAL SOURCE RETRIEVAL
    # NCBI PubMed is queried before generation so the model receives
    # actual source records instead of inventing references.
    sources = await search_pubmed(prompt, limit=3)
    source_context = build_source_context(sources)

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

                grounded_prompt = (
                    f"User question:\n{prompt}\n\n"
                    f"Verified medical source context from NCBI PubMed:\n"
                    f"{source_context}\n\n"
                    "Answer the user's question briefly and accurately. "
                    "Use only the supplied source context for source claims. "
                    "Do not invent citations, PMID numbers, URLs, or sources."
                )

                answer = await _request(
                    client,
                    item,
                    grounded_prompt,
                )

                # Add the verified PubMed source programmatically.
                # The model must not be trusted to invent or format citations.
                if sources:
                    verified_source = sources[0]
                    source_line = (
                        f"\n\nSource: {verified_source['title']} "
                        f"(PMID: {verified_source['pmid']})"
                    )

                    # Remove any model-generated Source line so the final
                    # citation always comes from the verified PubMed record.
                    # Remove every model-generated citation/source line.
                    # The application, not the model, owns the final citation.
                    source_patterns = (
                        "source:",
                        "source：",
                        "المصدر:",
                        "المصدر：",
                        "sources:",
                        "references:",
                        "reference:",
                    )

                    lines = answer.splitlines()
                    cleaned_lines = []

                    for line in lines:
                        normalized_line = line.strip().lower()

                        if any(
                            normalized_line.startswith(pattern)
                            for pattern in source_patterns
                        ):
                            continue

                        cleaned_lines.append(line)

                    answer = "\n".join(cleaned_lines).strip()

                    if answer:
                        answer += source_line

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
