import os
import httpx
import asyncio
from dotenv import load_dotenv

# تمرير مسار الملف صراحة لتجنب استدعاء find_dotenv() في بايثون 3.14
load_dotenv(dotenv_path="./.env")

groq_key = os.getenv("GROQ_API_KEY", "").strip()
or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

async def test():
    async with httpx.AsyncClient(timeout=15.0) as client:
        print("=== 1. اختبار Groq ===")
        if groq_key:
            try:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": "hi"}]}
                )
                print(f"Status: {r.status_code}\nResponse: {r.text[:200]}\n")
            except Exception as e:
                print(f"Groq Error: {e}\n")
        else:
            print("Groq Key مفقود في .env\n")

        print("=== 2. اختبار OpenRouter ===")
        if or_key:
            try:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {or_key}", "HTTP-Referer": "https://telegram.org"},
                    json={"model": "meta-llama/llama-3.3-70b-instruct:free", "messages": [{"role": "user", "content": "hi"}]}
                )
                print(f"Status: {r.status_code}\nResponse: {r.text[:200]}\n")
            except Exception as e:
                print(f"OpenRouter Error: {e}\n")
        else:
            print("OpenRouter Key مفقود في .env\n")

        print("=== 3. اختبار Gemini ===")
        if gemini_key:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
                r = await client.post(url, json={"contents": [{"parts": [{"text": "hi"}]}]})
                print(f"Status: {r.status_code}\nResponse: {r.text[:200]}\n")
            except Exception as e:
                print(f"Gemini Error: {e}\n")
        else:
            print("Gemini Key مفقود في .env\n")

asyncio.run(test())
