#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db
import ai_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ai_discovery")

async def main():
    load_dotenv()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    await db.init_db()
    router = ai_router.AIRouter(gemini_key, openrouter_key, openai_key)
    results = await router.discover_and_register()
    print("\n=== AI Provider Discovery Results ===\n")
    for r in results:
        status_icon = {
            "AVAILABLE": "AVAILABLE", "VERIFIED": "VERIFIED",
            "RATE_LIMITED": "RATE_LIMITED", "TIMEOUT": "TIMEOUT",
            "AUTH_FAILED": "AUTH_FAILED",
            "UPSTREAM_ERROR": "UPSTREAM_ERROR", "UNAVAILABLE": "UNAVAILABLE"
        }.get(r.get("availability", ""), "UNKNOWN")
        print(f"{status_icon} {r['provider']}/{r['model']}")
        print(f"   Availability: {r.get('availability', 'unknown')}")
        print(f"   Latency: {r.get('latency_ms', 'N/A')}ms")
        print(f"   Auth: {r.get('auth_status', 'unknown')}")
        if r.get("error"):
            print(f"   Error: {r['error']}")
        print()
    all_registry = await db.ai_registry_get_all()
    print(f"\n=== Registry Entries: {len(all_registry)} ===\n")
    for entry in all_registry:
        print(entry)
    return results

if __name__ == "__main__":
    asyncio.run(main())