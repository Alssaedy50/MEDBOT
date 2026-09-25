import os
import re
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

UNIFIED_ASSISTANT_PROMPT = """أنت المساعد الطبي لمنصة MEDBOT، منصة تعليمية طبية على Telegram.

هذا المسار مخصّص للأسئلة الطبية/العلمية فقط.

أسلوب الإجابة الطبية (إلزامي):
- اكتب أولاً إجابة أكاديمية نموذجية *باللغة الإنجليزية*، بأسلوب مرجعي دقيق
  (تعريف، ثم النقاط الأساسية، ثم الأهمية السريرية عند الحاجة)، في فقرة إلى
  ثلاث فقرات قصيرة.
- ثم أضف قسماً منفصلاً يشرح المعنى *بالعربية* بإيجاز مفيد، شرحاً أميناً غير
  مشوّه للمعنى، دون ترجمة حرفية طويلة.
- استخدم هذا الترتيب حرفياً:

  🩺 <الموضوع>
  **English (academic):**
  <الإجابة الأكاديمية بالإنجليزية>
  **العربية — شرح مختصر:**
  <الشرح العربي الموجز>

قواعد إلزامية:
1. ابدأ بالجواب المباشر ثم تفسير موجز؛ لا مقدّمات ولا حشو ولا تكرار.
2. لا تُطل: أهم النقاط فقط، ولا فقرات إنشائية عامة.
3. اعتمد على المصادر الموثوقة المزوّدة (NCBI PubMed) عند وجودها؛ لا تخترع
   معلومة أو مرجعاً أو PMID، ولا تنسب معلومة إلى مصدر لم يُزوَّد لك.
4. إن كانت المعلومة غير مؤكدة أو الأدلة غير كافية، قل ذلك بوضوح وقدّم أأمن
   إجابة دقيقة بدلاً من التخمين أو ادّعاء اليقين.
5. اجعل الإجابة تعليمية، ولا تقدّم تشخيصاً شخصياً أو وصفة علاجية شخصية؛
   واذكر أن الأمر لا يغني عن تقييم الطبيب عند الحاجة فقط.
6. أي كلام عن المنصة أو مواردها أو مساراتها ممنوع في هذا المسار؛ حقائق المنصة
   تُعالَج في مسار الموارد المخصّص، ولا تُشتق من معرفتك الطبية.
7. اذكر المراجع (PMID) فقط إن وُجدت فعلاً في المصادر المزوّدة.
"""

GENERAL_ASSISTANT_PROMPT = """أنت مساعد منصة MEDBOT التعليمية على Telegram.

هذا المسار للأسئلة العامة غير الطبية.

قواعد إلزامية:
1. أجب عن السؤال مباشرة وبقدر ما يكفي لفهمه؛ لا تُطل ولا تكثر التفاصيل.
2. لا تكرّر الفكرة نفسها ولا تعيد صياغة ما قلته.
3. لا تضف تنبيهات أو تحذيرات أو اعتذارات غير مطلوبة.
4. لا تكتب فقرات عامة طويلة؛ توقّف عندما تكون حاجة المستخدم للمعلومة قد لُبّيت.
5. لا تتحدّث عن موارد المنصة أو أقسامها إطلاقاً؛ حقائق المنصة في مسارها
   المخصّص فقط.
6. لا تخترع أي معلومة، وإذا لم تكن متأكداً قل ذلك باختصار.
"""

# MODE 1 — platform resource search. The model is handed the complete current
# catalog (every registered section/resource with its real path and id) and may
# reason over it freely, but it may only ever name an entity that exists there.
# It never receives — and never returns — a Telegram callback or an id, so the
# backend alone decides what is reachable.
PLATFORM_SEARCH_PROMPT = """أنت محرّك البحث والتنقّل في منصة MEDBOT التعليمية على Telegram.

هذه ليست محادثة طبية. مهمتك الوحيدة: فهم طلب الطالب البحثي/التنقّلي ثم تحديد
الموارد المسجّلة فعلاً في المنصة التي تطابق طلبه.

سياقك هو "دليل المنصة" أدناه، وهو القائمة الكاملة الحالية للأقسام والموارد
المسجّلة. يجوز لك التفكير داخله بحرية: فسّر العامية والأخطاء والاختصارات
والمرادفات، واستنتج ما الذي يقصده الطالب فعلاً بالرجوع إلى عناصر حقيقية موجودة
في الدليل.

قواعد إلزامية:
1. لا يجوز لك اختلاق أي قسم أو مادة أو ملف أو مسار غير موجود في الدليل.
   كل عنصر تذكره يجب أن يقابل عنصراً مسجّلاً فعلاً في الدليل.
2. لا تذكر مساراً أو قسماً لمجرد أن معرفتك الطبية/الأكاديمية توحي بوجوده.
   الدليل وحده هو مصدر حقيقة المنصة.
3. إذا لم يوجد أي عنصر مطابق فعلاً، قل بوضوح إنه لا يوجد مورد مسجّل مطابق،
   ولا تقترح بديلاً غير موجود.
4. إذا وجدت عنصراً واحداً مطابقاً، اعرضه بمساره الفعلي.
   وإذا وجدت أكثر من عنصر مطابق، اعرض العناصر المطابقة فقط (وليس كل الدليل).
5. اجعل الرد قصيراً وعملياً: جملة تمهيدية قصيرة ثم أسماء الموارد المطابقة
   ومساراتها. لا محاضرات ولا شرح طويل ولا معلومات طبية عامة.
6. لا تنشئ أزراراً ولا روابط ولا معرّفات ولا أهداف تنقّل ولا callbacks؛ النظام
   الخلفي هو من يبني الوصول من عناصر مسجّلة بعد التحقق منها.
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

# Hard cap on generated tokens. Bilingual answers are short by design, so the
# cap bounds worst-case generation latency (a runaway long answer is the other
# big latency contributor) without truncating a normal reply.
MAX_OUTPUT_TOKENS = 900

# --- Local output guard ---------------------------------------------------
# A model can degenerate and emit the same line, paragraph or table row many
# times. The guard is purely local (no network, no DB) and never caps the
# answer length: a legitimately long reply passes untouched. It only collapses
# a unit that is substantial (>= REPETITION_MIN_UNIT_CHARS) and clearly
# duplicated. A single regeneration is attempted at most once, and only when a
# repetition signature survives the local fix, so there is no loop.
REPETITION_MIN_UNIT_CHARS = 24
REPETITION_MIN_REPEATS = 3

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

    # Probes are independent HTTP round-trips; run them concurrently so the
    # refresh costs one probe round-trip instead of the sum of up to six.
    if selected:
        await asyncio.gather(
            *(_probe_model(item) for item in selected),
            return_exceptions=True,
        )

    return candidates


async def warm_ai_pool() -> None:
    """Warm the discovery/probe cache so the first student reply is fast.

    Called once at startup. It is best-effort: a failure here never affects
    serving, and the normal on-demand path still runs if it fails.
    """
    try:
        await _get_candidates()
    except Exception:
        logger.exception("AI pool warm-up failed")


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
            # The three providers are independent; running them concurrently
            # keeps a slow/unreachable provider from serializing the others
            # (previously up to 3x the slowest discovery round-trip).
            gemini, groq, openrouter = await asyncio.gather(
                _discover_gemini_models(discovery_client),
                _discover_groq_models(discovery_client),
                _discover_openrouter_models(discovery_client),
                return_exceptions=True,
            )

        discovered = []
        for provider_result in (gemini, groq, openrouter):
            if isinstance(provider_result, list):
                discovered += provider_result
            else:
                logger.warning(
                    "Provider discovery raised: %s", provider_result
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
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
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
            "max_tokens": MAX_OUTPUT_TOKENS,
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


# Cap on the platform catalog injected into a single prompt. The unified
# assistant may read the whole library, but an unbounded dump could exceed
# the provider's context window, so it is truncated on a line boundary.
CATALOG_MAX_CHARS = 8000

_NODE_LABELS = {
    "book": "📚 كتاب",
    "books": "📚 كتب",
    "video": "🎥 فيديو",
    "audio": "🎧 صوتي",
    "mcq": "📝 MCQ",
    "summary": "📑 ملخص",
    "summaries": "📑 ملخصات",
    "image": "🖼 صورة",
    "photo": "🖼 صورة",
    "document": "📄 مستند",
    "doc": "📄 مستند",
    "general": "📁 قسم",
}

_FILE_LABELS = {
    "photo": "🖼 صورة",
    "image": "🖼 صورة",
    "video": "🎥 فيديو",
    "audio": "🎧 صوتي",
    "mcq": "📝 MCQ",
    "quiz": "📝 MCQ",
    "pdf": "📕 PDF",
    "document": "📄 مستند",
}


def build_library_context(results: list) -> str:
    """Render deterministic search results into a compact grounding context."""
    if not results:
        return "لا توجد نتائج مطابقة في قاعدة بيانات MEDBOT."

    return "\n".join(_result_line(item) for item in results)


# Max number of direct-access buttons offered under one answer.
MAX_RESULT_ACTIONS = 5


def _action_label(item: dict) -> str:
    """Row label that disambiguates same-named matches by their real id."""
    result_type = item.get("result_type")
    title = item.get("title") or item.get("name") or "بدون عنوان"
    item_id = item.get("id")
    if result_type in ("FOLDER", "EMPTY_FOLDER"):
        return f"📂 {title} (#{item_id})"
    return f"📄 {title} (#{item_id})"


def build_result_actions(results: list) -> list:
    """Build direct-access buttons for the matched folders/resources.

    Each action opens the exact registered item (``folder:<id>`` /
    ``file:<id>``) so the student reaches it in one tap instead of walking the
    whole tree. Ids come straight from the deterministic search, so a button
    can never point at something that is not registered.
    """
    actions = []
    seen = set()

    for item in results or []:
        item_id = item.get("id")
        result_type = item.get("result_type")

        if item_id is None:
            continue

        if result_type in ("FOLDER", "EMPTY_FOLDER"):
            callback = f"folder:{item_id}"
        elif result_type == "CONTENT":
            callback = f"file:{item_id}"
        else:
            continue

        if callback in seen:
            continue
        seen.add(callback)

        actions.append({"label": _action_label(item), "callback": callback})

        if len(actions) >= MAX_RESULT_ACTIONS:
            break

    return actions


def _escape_md_link_text(value: str) -> str:
    return (value or "").replace("[", "(").replace("]", ")").strip()


def build_sources_footer(sources: list) -> str:
    """Render a compact 'trusted sources' footer with verifiable links.

    Only real PubMed records (with a PMID and a URL) are rendered, so the
    footer is the model's factual anchor and never an invented citation.
    """
    usable = [
        source
        for source in (sources or [])
        if source.get("url") and source.get("pmid")
    ]

    if not usable:
        return ""

    lines = ["", "—", "🔬 *مصادر موثوقة (NCBI PubMed):*"]

    for source in usable[:3]:
        title = _escape_md_link_text(source.get("title") or "PubMed record")
        if len(title) > 90:
            title = title[:90].rstrip() + "…"
        lines.append(
            f"• [{title}]({source['url']}) · PMID: {source['pmid']}"
        )

    return "\n".join(lines)


def _ensure_sources_footer(answer: str, footer: str) -> str:
    """Append the sources footer unless the model already listed the sources.

    Keeps the answer verifiable without duplicating a citation block the model
    produced itself.
    """
    answer = (answer or "").rstrip()

    if not footer:
        return answer

    lowered = answer.lower()

    if "pubmed" in lowered or "pmid" in lowered:
        return answer

    return f"{answer}{footer}"


# ---------------------------------------------------------------------------
# Local output guard (anti-repetition)
# ---------------------------------------------------------------------------
# Purely local post-processing for model output. It fixes the common
# "same line / paragraph / table row over and over" degeneration without any
# network or DB call, and without imposing a length cap: long answers are
# valid and must pass. See REPETITION_* constants for the thresholds.

_WORD_RE = re.compile(r"[A-Za-z0-9\u0600-\u06FF]+")


def _unit_signature(unit: str) -> str:
    """A stable key for a line/paragraph, normalised for comparison.

    Case, surrounding whitespace, punctuation and Arabic diacritics are
    ignored so that "* Item" and "- item." count as the same unit.
    """
    normalized = search_engine.normalize_text(unit or "")
    words = _WORD_RE.findall(normalized)
    return " ".join(words)


def _is_table_line(line: str) -> bool:
    stripped = (line or "").strip()
    return (
        stripped.startswith("|")
        or stripped.startswith("+-")
        or stripped.startswith("|:")
    ) or (stripped.count("|") >= 2)


def _collapse_repeated_units(text: str):
    """Collapse runs of >= REPETITION_MIN_REPEATS identical adjacent units.

    Units are lines, and additionally whole paragraphs for blocks that are not
    table rows. A long, varied answer is returned unchanged. Returns
    ``(cleaned_text, repeats_found)``.
    """
    if not text:
        return text, 0

    lines = text.split("\n")
    collapsed: list[str] = []
    repeats = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        signature = _unit_signature(line)

        # A substantial, non-table line that repeats itself back-to-back.
        if (
            signature
            and len(line.strip()) >= REPETITION_MIN_UNIT_CHARS
            and not _is_table_line(line)
        ):
            run = 1
            while (
                index + run < len(lines)
                and _unit_signature(lines[index + run]) == signature
            ):
                run += 1

            if run >= REPETITION_MIN_REPEATS:
                collapsed.append(line)
                repeats += run - 1
                index += run
                continue

        # A table row repeated with the same column layout.
        if _is_table_line(line):
            cells = [
                _unit_signature(cell) for cell in line.strip().strip("|").split("|")
            ]
            run = 1
            while index + run < len(lines) and _is_table_line(lines[index + run]):
                next_cells = [
                    _unit_signature(cell)
                    for cell in lines[index + run].strip().strip("|").split("|")
                ]
                if next_cells != cells:
                    break
                run += 1

            if run >= REPETITION_MIN_REPEATS:
                collapsed.append(line)
                repeats += run - 1
                index += run
                continue

        collapsed.append(line)
        index += 1

    cleaned = "\n".join(collapsed)

    # Whole-paragraph repetition (blank-line separated), e.g. the same
    # explanation block pasted several times in a row.
    paragraphs = cleaned.split("\n\n")
    if len(paragraphs) > REPETITION_MIN_REPEATS:
        deduped: list[str] = []
        para_repeats = 0
        index = 0
        while index < len(paragraphs):
            paragraph = paragraphs[index]
            signature = _unit_signature(paragraph)
            if signature and len(paragraph.strip()) >= REPETITION_MIN_UNIT_CHARS:
                run = 1
                while (
                    index + run < len(paragraphs)
                    and _unit_signature(paragraphs[index + run]) == signature
                ):
                    run += 1
                if run >= REPETITION_MIN_REPEATS:
                    deduped.append(paragraph)
                    para_repeats += run - 1
                    index += run
                    continue
            deduped.append(paragraph)
            index += 1

        if para_repeats:
            cleaned = "\n\n".join(deduped)
            repeats += para_repeats

    return cleaned, repeats


def _has_repetition(text: str) -> bool:
    """Heuristic: does the answer still contain obviously duplicated units?

    Used only to decide whether a single regeneration is worth attempting. It
    is deliberately conservative so a genuinely long, varied answer is never
    flagged.
    """
    if not text:
        return False

    lines = [
        line
        for line in text.split("\n")
        if len(line.strip()) >= REPETITION_MIN_UNIT_CHARS
    ]
    if len(lines) < REPETITION_MIN_REPEATS:
        return False

    seen: dict[str, int] = {}
    for line in lines:
        signature = _unit_signature(line)
        if not signature:
            continue
        seen[signature] = seen.get(signature, 0) + 1

    if any(count >= REPETITION_MIN_REPEATS for count in seen.values()):
        return True

    paragraphs = [
        p for p in text.split("\n\n")
        if len(p.strip()) >= REPETITION_MIN_UNIT_CHARS
    ]
    for paragraph in paragraphs:
        if paragraphs.count(paragraph) >= REPETITION_MIN_REPEATS:
            return True

    # The whole answer is one block repeated: detect a period in the word
    # sequence (covers "answer pasted twice/thrice", however it is spaced).
    words = _unit_signature(text).split()
    if len(words) >= 2 * REPETITION_MIN_REPEATS:
        for period in range(1, len(words) // 2 + 1):
            block = words[:period]
            repeats = 0
            for start in range(0, len(words) - period + 1, period):
                if words[start:start + period] != block:
                    break
                repeats += 1
            # A short period (< 4 words) matches any normal prose; only treat
            # a substantial repeated block as degeneration.
            if repeats >= REPETITION_MIN_REPEATS and period * repeats >= len(words) // 2:
                if period >= 3:
                    return True

    return False


def _guard_answer(text: str) -> str:
    """Repair obvious local repetition in a generated answer.

    Never truncates a legitimate long answer: only repeated units are removed.
    """
    cleaned, _ = _collapse_repeated_units(text or "")
    return cleaned or (text or "")


REPETITION_RETRY_INSTRUCTION = (
    "\n\nمهم جداً: أعد صياغة الإجابة كاملة مرة واحدة دون أي تكرار. "
    "لا تعِد نفس السطر أو الفقرة أو صف الجدول، واذكر كل فكرة مرة واحدة فقط."
)


def build_platform_catalog(folders, contents, paths) -> str:
    """Render the full registered MEDBOT tree into a compact text catalog.

    The unified assistant is allowed to read and compare the whole platform
    (every section and resource with its real path) so it can answer
    navigation questions and point at the exact place of a resource. Only
    registered data is rendered — nothing is invented — and the result is
    bounded by ``CATALOG_MAX_CHARS`` so an enormous library can never blow
    the prompt size.
    """
    lines = []

    for row in folders:
        folder_id, name, node_type = row[0], row[2], row[3]
        path = paths.get(folder_id) or "الرئيسية 🏠"
        kind = _NODE_LABELS.get(str(node_type or "").lower(), "📁 قسم")
        suffix = " (يستقبل مساهمات)" if len(row) > 4 and row[4] else ""
        lines.append(f"- [{kind}] {name} | المسار: {path}{suffix}")

    for row in contents:
        folder_id, title, file_type = row[1], row[2], row[3]
        path = paths.get(folder_id) or "الرئيسية 🏠"
        kind = _FILE_LABELS.get(str(file_type or "").lower(), "📄 مورد")
        lines.append(f"- [{kind}] {title} | داخل: {path}")

    if not lines:
        return "قاعدة البيانات لا تحتوي أي أقسام أو موارد مسجلة بعد."

    catalog = "\n".join(lines)

    if len(catalog) > CATALOG_MAX_CHARS:
        catalog = catalog[:CATALOG_MAX_CHARS].rsplit("\n", 1)[0]
        catalog += "\n… (تم اختصار دليل المنصة لطوله)"

    return catalog


# ---------------------------------------------------------------------------
# Intent classification (deterministic, no network, no extra DB round-trip)
# ---------------------------------------------------------------------------
# The unified assistant answers four kinds of request, each with a different
# source of truth and a different latency budget:
#
#   overview  -> "what exists on MEDBOT?"  -> answered from registered data only
#   resource  -> "where is X?"             -> deterministic search of the registry
#   medical   -> a medical/scientific question -> verified answer + PubMed
#   general   -> any other question        -> direct, concise answer
#
# Classifying first is what keeps a navigation question from being answered
# with the model's own idea of a medical-school curriculum.

INTENT_OVERVIEW = "overview"
INTENT_RESOURCE = "resource"
INTENT_MEDICAL = "medical"
INTENT_GENERAL = "general"

# Canonical medical concepts (from the search engine's concept table) that mark
# a question as medical/scientific rather than platform navigation.
MEDICAL_CONCEPT_KEYS = frozenset({
    "cbc", "hemoglobin", "esr", "crp", "electrolytes", "renal", "liver",
    "lipid", "glucose", "thyroid", "urinalysis", "anatomy", "physiology",
    "pathology", "pharmacology", "microbiology", "biochemistry", "immunology",
    "histology",
})

# Extra medical keywords that are not part of the synonym table.
_MEDICAL_KEYWORDS = frozenset({
    "دواء", "ادويه", "علاج", "مرض", "امراض", "اعراض", "عرض", "تشخيص",
    "فيروس", "عدوى", "التهاب", "سرطان", "لقاح", "جرعه", "مضاعفات",
    "قلب", "دم", "كبد", "كليه", "رئه", "دماغ", "عصب", "عضله", "عظم",
    "هرمون", "هرمونات", "مناعه", "بكتيريا", "سكر", "ضغط", "تنفس",
    "جهاز", "خليه", "خلايا", "انزيم", "بروتين", "فيتامين", "دوره",
    "دورة", "حيض", "حمل", "ورم", "تضخم", "قصور", "انسداد", "جراحه",
    "disease", "treatment", "symptom", "symptoms", "diagnosis", "infection",
    "cancer", "vaccine", "drug", "drugs", "dose", "therapy", "organ",
    "cell", "cells", "enzyme", "protein", "vitamin", "hormone", "cardiac",
    "clinical", "physiological", "pathological", "syndrome", "disorder",
    "potential", "receptor", "neuron", "muscle", "nerve", "blood",
})

# Filler words that carry no subject on their own, used only to decide whether
# a platform-structure question names a specific subject.
_GENERIC_TOKENS = frozenset({
    "هي", "هو", "هما", "هم", "هن", "حاليا", "الان", "الموجود", "الموجوده",
    "كل", "جميع", "list", "show", "available", "current", "currently", "now",
})


def _bare_token(token: str) -> str:
    """Drop a leading Arabic definite article, keeping the root otherwise.

    ``normalize_text`` keeps the ``ال`` prefix, so "الأقسام" and "أقسام" would
    otherwise be different signals. Words whose ``ال`` is part of the root
    (e.g. "التهاب") are matched as written, so they are still present in the
    token sets returned by :func:`_match_tokens`.
    """
    if token.startswith("ال") and len(token) > 3:
        return token[2:]
    return token


def _match_tokens(text: str) -> set:
    """Tokens used for keyword matching: each token plus its article-less form."""
    tokens = set(search_engine.meaningful_terms(text))

    for token in list(tokens):
        bare = _bare_token(token)
        if bare != token:
            tokens.add(bare)

    return tokens


def _bare_tokens(text: str) -> set:
    """Article-less tokens, used to tell a specific subject from bare filler."""
    return {_bare_token(token) for token in search_engine.meaningful_terms(text)}

# Nouns that refer to the platform itself or its structure.
_PLATFORM_NOUNS = frozenset({
    "اقسام", "قسم", "فروع", "فرع", "محتوي", "محتوى", "محتويات", "مواد",
    "مقرر", "مقررات", "بوت", "منصه", "شجره", "تصنيف", "قائمه",
    "sections", "section", "subjects", "subject", "blocks", "block",
    "content", "contents", "bot", "platform", "medbot", "dictionary",
    "structure", "hierarchy", "tree", "categories", "category",
})

# Tokens that signal an access/location question rather than an enumeration.
# "أين"/"وين" are search stop words, so these are matched against the raw
# normalized tokens rather than the intent-carrying ones.
_WHERE_TOKENS = frozenset({
    "وين", "اين", "where", "مسار", "path", "مكان", "اماكن", "افتح",
    "open", "find", "locate", "reach", "اصل", "اوصل", "اجد", "القي", "ألقى",
})

_WHERE_PHRASES = ("كيف اصل", "كيف اوصل", "how to reach", "how do i find")

# Tokens that signal an existence question ("does X exist?").
_EXISTENCE_TOKENS = frozenset({
    "يوجد", "موجود", "موجوده", "متوفر", "متوفره", "متاح", "متاحه",
    "exist", "exists", "available", "there",
})

_EXISTENCE_PHRASES = ("is there", "are there", "do you have")


def classify_intent(prompt: str) -> str:
    """Classify a student message into one of the four MEDBOT intents.

    Deterministic and local: it only looks at the message text, so it adds no
    latency and can never disagree with the registry search (both use the same
    ``search_engine`` normalization/concept table).
    """
    raw = (prompt or "").strip()

    if not raw:
        return INTENT_GENERAL

    norm = search_engine.normalize_text(raw)
    tokens = _match_tokens(raw)
    # Full normalized token set, including search stop words such as "أين".
    all_tokens = set(norm.split())
    concepts = search_engine.implied_concepts(raw)

    is_medical = bool(concepts & MEDICAL_CONCEPT_KEYS) or bool(
        tokens & _MEDICAL_KEYWORDS
    )
    has_platform_noun = bool(tokens & _PLATFORM_NOUNS)

    # "Where do I find X?" / "how do I reach X?" -> registry lookup.
    if (
        all_tokens & _WHERE_TOKENS
        or tokens & _WHERE_TOKENS
        or any(p in norm for p in _WHERE_PHRASES)
    ):
        return INTENT_RESOURCE

    # A platform noun plus a specific subject is a lookup for that subject;
    # with no subject left it is a bare "what exists" enumeration.
    if has_platform_noun:
        if is_medical:
            return INTENT_RESOURCE

        residual = (
            _bare_tokens(raw)
            - _PLATFORM_NOUNS
            - _GENERIC_TOKENS
            - _EXISTENCE_TOKENS
        )
        if residual:
            return INTENT_RESOURCE
        return INTENT_OVERVIEW

    # "Does X exist?" -> a named lookup; absence is answered deterministically.
    existence = bool(tokens & _EXISTENCE_TOKENS) or any(
        p in norm for p in _EXISTENCE_PHRASES
    )
    if existence and (tokens - _EXISTENCE_TOKENS - _GENERIC_TOKENS):
        return INTENT_RESOURCE

    if is_medical:
        return INTENT_MEDICAL

    return INTENT_GENERAL


# Bounds for the deterministic registry overview (a full-tree answer must stay
# Telegram-sized and readable).
OVERVIEW_MAX_DEPTH = 6
OVERVIEW_MAX_CHARS = 3500


def build_registry_overview(folders) -> str:
    """Render the registered section hierarchy — and nothing else.

    This is the ONLY answer source for "what exists on MEDBOT?" questions. It
    walks the real ``folders`` rows, so a section the admin never added can
    never appear, and a future addition shows up automatically.
    """
    if not folders:
        return "📚 لا توجد أقسام مسجلة حالياً في MEDBOT."

    children = {}
    for row in folders:
        children.setdefault(row[1], []).append(row)

    lines = ["📚 *الأقسام المتوفرة حالياً في MEDBOT:*", ""]

    def walk(parent_id, depth):
        if depth > OVERVIEW_MAX_DEPTH:
            return
        rows = sorted(
            children.get(parent_id, []),
            key=lambda r: (search_engine.normalize_text(r[2]), r[0]),
        )
        for row in rows:
            lines.append("  " * (depth - 1) + f"• {row[2]}")
            walk(row[0], depth + 1)

    walk(None, 1)

    text = "\n".join(lines)

    if len(text) > OVERVIEW_MAX_CHARS:
        text = text[:OVERVIEW_MAX_CHARS].rsplit("\n", 1)[0]
        text += "\n… (توجد أقسام إضافية داخل المنصة)"

    return text


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


async def _fetch_pubmed_sources(prompt: str) -> list:
    """Fetch verified PubMed sources without ever raising into the caller."""
    try:
        return await search_pubmed(prompt, limit=2)
    except Exception:
        logger.exception("PubMed retrieval failed in unified assistant")
        return []


async def _load_registry():
    """Load the registered rows, degrading to empty on failure.

    Never raises into the caller: a database hiccup must become an honest
    "nothing registered" answer, not an invented one.
    """
    try:
        return await database.get_searchable_records()
    except Exception:
        logger.exception("Could not load MEDBOT registry for the AI assistant")
        return [], [], {}


# Structural words that describe a place in a hierarchy rather than a subject.
# A query made only of these must match a registered title exactly (or by whole
# phrase), otherwise "First Year" would be answered with "Second Year" just
# because both contain "year".
_GENERIC_SUBJECT_TOKENS = frozenset({
    "first", "second", "third", "fourth", "fifth", "sixth", "year", "years",
    "level", "stage", "semester", "term", "block", "blocks", "module",
    "section", "subject", "course",
    "سنه", "سنوات", "مستوي", "مرحله", "فصل", "ترم", "بلوك", "بلوكات",
    "دراسي", "دراسيه",
})


def _genuine_registry_matches(prompt: str, results: list) -> list:
    """Keep only the hits that really correspond to what the student named.

    The deterministic search is intentionally recall-oriented, so a query like
    "First Year" can surface "Second Year" through the shared word "year". A
    hit counts only if a subject concept matches, a specific (non-structural)
    subject word appears in the hit, or a structural phrase is matched whole.
    Returns the genuinely-matching rows (possibly empty), so a "does X exist?"
    question is never answered with an unrelated registered neighbour.
    """
    concepts = search_engine.implied_concepts(prompt)
    subject = (
        _bare_tokens(prompt)
        - _PLATFORM_NOUNS
        - _EXISTENCE_TOKENS
        - _WHERE_TOKENS
        - _GENERIC_TOKENS
    )
    specific = subject - _GENERIC_SUBJECT_TOKENS
    structural = subject & _GENERIC_SUBJECT_TOKENS

    matched = []

    for item in results or []:
        title = search_engine.normalize_text(
            item.get("title") or item.get("name") or ""
        )
        path = search_engine.normalize_text(item.get("path") or "")
        blob = f"{title} {path}"

        if concepts and concepts & search_engine.implied_concepts(blob):
            matched.append(item)
        elif specific and any(term in blob for term in specific):
            matched.append(item)
        elif not specific and structural and all(
            term in blob for term in structural
        ):
            matched.append(item)

    return matched


def _medical_prompt(prompt: str, sources: list) -> str:
    source_context = (
        build_source_context(sources)
        if sources
        else "لا توجد مصادر PubMed متاحة لهذا السؤال؛ إن لم تكن متأكداً فاذكر ذلك."
    )
    return (
        "مصادر طبية موثّقة (NCBI PubMed) — استخدمها كمصدر وحيد لأي ادّعاء مصدر:\n"
        f"{source_context}\n\n"
        f"سؤال الطالب:\n{prompt}"
    )


def _general_prompt(prompt: str) -> str:
    return f"سؤال الطالب:\n{prompt}"


async def _provider_failover(
    grounded_prompt: str,
    system_prompt: str,
    candidates: list,
    user_id,
    label: str,
    sources_footer: str = "",
) -> str:
    """Send one grounded prompt through the candidate pool with failover.

    Returns the answer text, or ``""`` when every provider failed. Keeps the
    usage/health bookkeeping in one place so each intent shares the same
    provider contract.
    """
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
                    system_prompt,
                )

                if not validator.allows(answer):
                    raise RuntimeError("Validator rejected empty answer")

                # Local anti-repetition guard: fix the common "same line /
                # paragraph / table row repeated" degeneration without any
                # network or DB call. It never caps answer length, so a
                # legitimately long reply is untouched. Only when an obvious
                # repetition survives the local repair do we regenerate — once
                # for this candidate, never in a loop.
                answer = _guard_answer(answer)

                if _has_repetition(answer):
                    logger.info(
                        "%s repetition detected provider=%s; regenerating once",
                        label,
                        provider,
                    )
                    try:
                        retry = await _request(
                            client,
                            item,
                            grounded_prompt + REPETITION_RETRY_INSTRUCTION,
                            system_prompt,
                        )
                        if validator.allows(retry):
                            retry = _guard_answer(retry)
                            if not _has_repetition(retry):
                                answer = retry
                    except Exception as retry_exc:
                        logger.warning(
                            "%s repetition regeneration failed provider=%s error=%s",
                            label,
                            provider,
                            retry_exc,
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
                    "%s success provider=%s model=%s latency_ms=%s",
                    label,
                    provider,
                    item["model"],
                    latency_ms,
                )

                return _ensure_sources_footer(answer, sources_footer)

            except Exception as exc:
                logger.warning(
                    "%s provider failed provider=%s model=%s error=%s",
                    label,
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

    return ""


async def generate_medbot_unified_result(
    prompt: str,
    user_id: int = None,
) -> dict:
    """Backward-compatible auto-routing entry point (legacy callers only).

    The platform now has TWO explicit modes, and this function exists only so
    single-textbox / CLI callers keep working: it classifies the message and
    delegates to the matching workflow.

        overview | resource -> MODE 1 platform resource search
        medical  | general  -> MODE 2 AI chat

    It is a thin dispatcher, not a merged workflow: neither mode is re-merged,
    and the Telegram UI selects a mode explicitly. New callers should call
    ``generate_platform_search_result`` or ``generate_ai_chat_result``.
    """
    prompt = (prompt or "").strip()

    if not prompt:
        return {"text": "⚠️ يرجى كتابة سؤال واضح.", "actions": []}

    intent = classify_intent(prompt)
    logger.info("Assistant intent=%s", intent)

    if intent in (INTENT_OVERVIEW, INTENT_RESOURCE):
        return await generate_platform_search_result(prompt, user_id=user_id)

    return await generate_ai_chat_result(prompt, user_id=user_id)


# ---------------------------------------------------------------------------
# MODE 1 — PLATFORM RESOURCE SEARCH (registry facts + verified access)
# ---------------------------------------------------------------------------
# Separate workflow from AI Chat. It is allowed to read the COMPLETE current
# platform catalog so it can resolve natural-language Arabic/English resource
# queries, but it may never produce a platform fact that is not a real
# registered row, and the backend builds every access button from verified ids.
#
# Fast path: when the deterministic engine already finds the named item(s), the
# answer is rendered from registered rows and no model is called at all. The
# model is used only for the genuinely hard natural-language query — the way to
# search the whole catalog when token overlap found nothing — and even then the
# backend keeps only what it can verify against a real id.

PLATFORM_SEARCH_NO_MATCH = (
    "لم أجد أي مورد أو قسم مسجّل في MEDBOT يطابق طلبك.\n"
    "جرّب كتابة اسم المادة أو القسم بشكل آخر."
)


def _search_subject_tokens(prompt: str) -> set:
    """The content tokens of a resource query, with all search noise removed.

    ``search_engine.meaningful_terms`` already drops stop words and generic
    platform nouns (محتوى/موارد/بيانات/مواد); here the leftover question/
    navigation tokens are dropped too, so what remains is the named subject.
    """
    subject = _bare_tokens(prompt)
    return subject - _PLATFORM_NOUNS - _GENERIC_TOKENS - _EXISTENCE_TOKENS


async def _platform_search_results(prompt: str) -> list:
    """Deterministic registry search followed by a verified catalog fallback.

    Returns only real ``search_engine`` result rows. The fallback asks the
    model to resolve a natural-language query against the full catalog, then
    discards anything that cannot be matched back to a registered row.
    """
    results = await _search_medbot(prompt)
    matches = _genuine_registry_matches(prompt, results)

    if matches:
        return matches

    return await _catalog_fallback_matches(prompt)


async def _catalog_fallback_matches(prompt: str) -> list:
    """Query the full catalog with the model, keeping only verifiable hits.

    Only runs for a genuinely harder natural-language question (the
    deterministic pass found nothing). If no provider is reachable, or the
    model names nothing that maps back to a registered row, the result is
    empty and the caller answers with the honest no-match message.
    """
    folders, contents, paths = await _load_registry()

    if not folders and not contents:
        return []

    subject = _search_subject_tokens(prompt)

    if not subject:
        return []

    candidates = await _get_candidates()

    if not candidates:
        return []

    catalog = build_platform_catalog(folders, contents, paths)
    grounded_prompt = (
        "دليل المنصة (المصدر الوحيد المسموح لمعرفة ما هو مسجّل):\n"
        f"{catalog}\n\n"
        f"طلب الطالب:\n{prompt}\n\n"
        "اذكر فقط الموارد/الأقسام المطابقة لطلب الطالب من الدليل أعلاه، مع "
        "مسار كل منها. لا تذكر أي شيء غير موجود في الدليل."
    )

    answer = await _provider_failover(
        grounded_prompt,
        PLATFORM_SEARCH_PROMPT,
        candidates,
        None,
        "Platform search",
    )

    if not answer:
        return []

    return _verify_catalog_answer(answer, folders, contents, paths)


def _verify_catalog_answer(answer, folders, contents, paths) -> list:
    """Keep only catalog rows the model's answer can be verified against.

    The model's text is a *claim*; a row becomes reachable only when we can
    confirm it from the registry itself. We iterate the REAL catalog rows (so an
    invented entity can never be produced) and keep a row only when the model
    explicitly named it. This is what lets the model bridge colloquial Arabic to
    an English title while still never minting a platform entity: the model can
    only ever *select* a row that already exists, never create one.
    """
    rows = _catalog_rows_as_results(folders, contents, paths)
    normalized_answer = search_engine.normalize_text(answer)

    verified = []
    for item in rows:
        title = search_engine.normalize_text(
            item.get("title") or item.get("name") or ""
        )
        if title and title in normalized_answer:
            verified.append(item)

    return verified[:MAX_RESULT_ACTIONS]


def _catalog_rows_as_results(folders, contents, paths) -> list:
    """Render the full catalog as search-shaped result rows (no synthesis).

    Every row is a real folder/content record with its true id, name and
    breadcrumb; only the columns the rest of the assistant expects are added.
    """
    results = []

    for row in folders:
        folder_id, parent_id, name, node_type = row[0], row[1], row[2], row[3]
        content_count = sum(1 for c in contents if c[1] == folder_id)
        results.append({
            "type": "FOLDER",
            "result_type": "FOLDER" if content_count else "EMPTY_FOLDER",
            "id": folder_id,
            "folder_id": folder_id,
            "title": name,
            "name": name,
            "path": paths.get(folder_id) or "الرئيسية 🏠",
            "content_count": content_count,
            "node_type": node_type,
        })

    for row in contents:
        content_id, folder_id, title, file_type = row[0], row[1], row[2], row[3]
        results.append({
            "type": "CONTENT",
            "result_type": "CONTENT",
            "id": content_id,
            "content_id": content_id,
            "folder_id": folder_id,
            "title": title,
            "name": title,
            "path": paths.get(folder_id) or "الرئيسية 🏠",
            "file_type": file_type,
        })

    return results


def _platform_search_line(item: dict) -> str:
    is_folder = item.get("result_type") in ("FOLDER", "EMPTY_FOLDER")
    icon = "📂" if is_folder else "📄"
    title = item.get("title") or item.get("name") or "بدون عنوان"
    path = item.get("path") or "الرئيسية 🏠"
    label = "المسار" if is_folder else "داخل"
    return f"{icon} *{title}*\n   {label}: {path}"


def build_platform_search_answer(results: list) -> str:
    """Short, discovery-oriented answer rendered only from verified rows."""
    if not results:
        return PLATFORM_SEARCH_NO_MATCH

    if len(results) == 1:
        return (
            "وجدت لك مورداً مرتبطاً بطلبك:\n\n"
            + _platform_search_line(results[0])
        )

    lines = ["وجدت عدة موارد مرتبطة بطلبك:", ""]

    for item in results[:MAX_RESULT_ACTIONS]:
        lines.append(_platform_search_line(item))

    return "\n".join(lines)


async def generate_platform_search_result(
    prompt: str,
    user_id: int = None,
) -> dict:
    """MODE 1: find and reach real MEDBOT resources.

    Pipeline (never invents a platform entity):
        query -> intent/concept resolution (local, free)
              -> deterministic registry search (fast path, no model)
              -> [fallback] full-catalog read + verified-id matching
              -> short answer + one-tap buttons from VERIFIED registry ids

    Returns ``{"text": str, "actions": [{"label", "callback"}, ...]}`` where
    every callback is ``folder:<id>`` / ``file:<id>`` built from a real row.
    """
    prompt = (prompt or "").strip()

    if not prompt:
        return {"text": "⚠️ يرجى كتابة ما تبحث عنه.", "actions": []}

    # A bare enumeration ("ما هي الأقسام الموجودة؟") is answered from the
    # registered hierarchy alone: no search, no model, nothing inventable.
    if classify_intent(prompt) == INTENT_OVERVIEW:
        folders, _contents, _paths = await _load_registry()
        return {"text": build_registry_overview(folders), "actions": []}

    matches = await _platform_search_results(prompt)

    if not matches:
        return {"text": PLATFORM_SEARCH_NO_MATCH, "actions": []}

    return {
        "text": build_platform_search_answer(matches),
        "actions": build_result_actions(matches),
    }


# ---------------------------------------------------------------------------
# MODE 2 — AI CHAT (conversational knowledge, never the platform registry)
# ---------------------------------------------------------------------------
# Independent from platform search: it answers questions, it does not navigate.
# A medical question gets an English academic answer plus a concise Arabic
# explanation and PubMed grounding; anything else is answered naturally and
# concisely, with no PubMed and no registry access at all.

async def generate_ai_chat_result(
    prompt: str,
    user_id: int = None,
) -> dict:
    """MODE 2: conversational AI, separate from platform navigation.

    Returns ``{"text": str, "actions": []}``. Actions are always empty: chat
    never produces platform navigation, so it can never expose platform
    structure or invent a resource.
    """
    prompt = (prompt or "").strip()

    if not prompt:
        return {"text": "⚠️ يرجى كتابة سؤال واضح.", "actions": []}

    intent = classify_intent(prompt)
    logger.info("AI chat intent=%s", intent)

    if not _is_medical_question(prompt, intent):
        # A general question: no PubMed, no registry, one concise answer.
        candidates = await _get_candidates()

        if not candidates:
            return {"text": _chat_no_provider_answer(), "actions": []}

        answer = await _provider_failover(
            _general_prompt(prompt),
            GENERAL_ASSISTANT_PROMPT,
            candidates,
            user_id,
            "General assistant",
        )

        if not answer:
            return {"text": _chat_no_provider_answer(), "actions": []}

        return {"text": answer, "actions": []}

    # A medical question: PubMed grounding + the bilingual answer contract.
    # Deliberately no registry search: AI Chat must not surface MEDBOT's
    # platform structure for a medical question.
    candidates, sources = await asyncio.gather(
        _get_candidates(),
        _fetch_pubmed_sources(prompt),
        return_exceptions=True,
    )

    if isinstance(candidates, BaseException):
        candidates = []
    if isinstance(sources, BaseException):
        sources = []

    if not candidates:
        return {"text": _chat_no_provider_answer(), "actions": []}

    answer = await _provider_failover(
        _medical_prompt(prompt, sources),
        UNIFIED_ASSISTANT_PROMPT,
        candidates,
        user_id,
        "Medical assistant",
        build_sources_footer(sources),
    )

    if not answer:
        return {"text": _chat_no_provider_answer(), "actions": []}

    return {"text": answer, "actions": []}


def _is_medical_question(prompt: str, intent: str) -> bool:
    """Whether an AI-Chat message is a medical/scientific question.

    A question like "اشرح لي دورة القلب" names a concept that is both a
    registered directory and a medical topic; in AI Chat the first person is
    medical, so any recognized medical concept counts as medical. Everything
    else is a general question.
    """
    if intent == INTENT_MEDICAL:
        return True

    return bool(search_engine.implied_concepts(prompt) & MEDICAL_CONCEPT_KEYS)


def _chat_no_provider_answer() -> str:
    return (
        "⚠️ تعذر الوصول إلى خدمة الذكاء الاصطناعي حالياً؛ "
        "يرجى المحاولة بعد قليل."
    )


async def generate_medbot_unified_response(
    prompt: str,
    user_id: int = None,
) -> str:
    """Backward-compatible text-only wrapper (legacy/CLI callers only).

    It auto-routes exactly like ``generate_medbot_unified_result``. The
    Telegram layer does not use either dispatcher: it selects one of the two
    explicit modes (``generate_platform_search_result`` /
    ``generate_ai_chat_result``).
    """
    result = await generate_medbot_unified_result(prompt, user_id=user_id)
    return result.get("text", "")


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

                # Legacy /ask-era path: apply the same local anti-repetition
                # repair (no network, no length cap).
                answer = _guard_answer(answer)

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
