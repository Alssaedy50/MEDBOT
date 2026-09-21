#!/usr/bin/env python3

import asyncio
import logging
import os
import sys
import time

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as db
import ai_router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("ai_discovery")


async def discover_and_verify():
    """
    Verify the current AI Registry using the active ai_router architecture.

    Important:
    - Does NOT delete Registry records.
    - Does NOT recreate the Registry.
    - Does NOT require AIRouter.
    - Uses the current _get_candidates() implementation.
    """

    await db.init_db()

    candidates = await ai_router._get_candidates()

    print("\n=== MEDBOT AI DISCOVERY / VERIFICATION ===\n")
    print(f"Candidate count: {len(candidates)}")

    if not candidates:
        print("\nNO USABLE AI CANDIDATES FOUND.")
        print("Check API keys and Registry health.")
        return []

    results = []

    async with ai_router.httpx.AsyncClient(
        timeout=ai_router.TIMEOUT
    ) as client:

        for index, item in enumerate(candidates, start=1):
            provider = item.get("provider")
            model = item.get("model")
            registry_id = item.get("id")

            print(
                f"\n[{index}/{len(candidates)}] "
                f"{provider}/{model}"
            )

            prompt = (
                "MEDBOT health verification. "
                "Reply with exactly: MEDBOT_OK"
            )

            started = time.perf_counter()

            try:
                if provider == "google_gemini":
                    response = await ai_router._gemini_request(
                        client,
                        item,
                        prompt,
                    )
                elif provider in {"groq", "openrouter"}:
                    response = await ai_router._openai_compatible_request(
                        client,
                        item,
                        prompt,
                    )
                else:
                    raise RuntimeError(
                        f"Unsupported provider: {provider}"
                    )

                latency_ms = round(
                    (time.perf_counter() - started) * 1000,
                    1,
                )

                response_clean = (response or "").strip()

                print("   Availability: VERIFIED")
                print(f"   Latency: {latency_ms} ms")
                print(f"   Response: {response_clean[:120]}")

                if registry_id is not None:
                    await db.ai_registry_mark_success(
                        registry_id,
                        latency_ms=latency_ms,
                        notes="verified by MEDBOT discovery",
                    )

                    await db.ai_usage_record(
                        registry_id=registry_id,
                        latency_ms=latency_ms,
                        success=True,
                    )

                result = {
                    "id": registry_id,
                    "provider": provider,
                    "model": model,
                    "availability": "VERIFIED",
                    "latency_ms": latency_ms,
                    "success": True,
                }

            except Exception as exc:
                latency_ms = round(
                    (time.perf_counter() - started) * 1000,
                    1,
                )

                error_text = str(exc)

                print("   Availability: FAILED")
                print(f"   Latency: {latency_ms} ms")
                print(f"   Error: {error_text[:300]}")

                if registry_id is not None:
                    # Keep failure classification conservative.
                    category = "UPSTREAM_ERROR"

                    lowered = error_text.lower()

                    if "timeout" in lowered:
                        availability = "TIMEOUT"
                        category = "TIMEOUT"
                    elif "401" in lowered or "403" in lowered:
                        availability = "AUTH_FAILED"
                        category = "AUTH_FAILED"
                    elif "429" in lowered:
                        availability = "RATE_LIMITED"
                        category = "RATE_LIMITED"
                    elif "connection" in lowered:
                        availability = "NETWORK_ERROR"
                        category = "NETWORK_ERROR"
                    else:
                        availability = "UPSTREAM_ERROR"

                    try:
                        await db.ai_registry_mark_failure(
                            registry_id,
                            availability=availability,
                            error_category=category,
                            notes=error_text[:500],
                        )
                    except Exception:
                        logger.exception(
                            "Could not update failure state for registry id %s",
                            registry_id,
                        )

                    try:
                        await db.ai_usage_record(
                            registry_id=registry_id,
                            latency_ms=latency_ms,
                            success=False,
                            error_category=category,
                        )
                    except Exception:
                        logger.exception(
                            "Could not record failed AI usage"
                        )

                result = {
                    "id": registry_id,
                    "provider": provider,
                    "model": model,
                    "availability": "FAILED",
                    "latency_ms": latency_ms,
                    "success": False,
                    "error": error_text,
                }

            results.append(result)

    verified = sum(
        1 for item in results
        if item.get("success")
    )

    failed = len(results) - verified

    print("\n==========================================")
    print("DISCOVERY SUMMARY")
    print("==========================================")
    print(f"Candidates tested : {len(results)}")
    print(f"Verified          : {verified}")
    print(f"Failed            : {failed}")

    print("\n=== VERIFIED MODELS ===")

    for item in results:
        if item.get("success"):
            print(
                f"PASS  {item['provider']}/{item['model']} "
                f"({item['latency_ms']} ms)"
            )

    print("\n=== FAILED MODELS ===")

    for item in results:
        if not item.get("success"):
            print(
                f"FAIL  {item['provider']}/{item['model']} "
                f"-> {item.get('error', '')[:200]}"
            )

    return results


async def main():
    load_dotenv()

    print("==========================================")
    print("MEDBOT AI DISCOVERY")
    print("==========================================")

    print(
        f"GEMINI_API_KEY: "
        f"{'SET' if os.getenv('GEMINI_API_KEY') else 'MISSING'}"
    )

    print(
        f"GROQ_API_KEY: "
        f"{'SET' if os.getenv('GROQ_API_KEY') else 'MISSING'}"
    )

    print(
        f"OPENROUTER_API_KEY: "
        f"{'SET' if os.getenv('OPENROUTER_API_KEY') else 'MISSING'}"
    )

    results = await discover_and_verify()

    print("\n=== CURRENT HEALTHY REGISTRY ===")

    healthy = await db.ai_registry_get_healthy()

    print(f"Healthy count: {len(healthy)}")

    for row in healthy:
        print(
            f"ID={row[0]} | "
            f"{row[1]}/{row[2]} | "
            f"availability={row[4]} | "
            f"auth={row[5]} | "
            f"latency={row[6]} | "
            f"success_rate={row[7]}"
        )

    print("\n=== AI DISCOVERY COMPLETE ===")

    return results


if __name__ == "__main__":
    asyncio.run(main())
