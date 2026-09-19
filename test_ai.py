import asyncio
import time
import httpx
from dotenv import dotenv_values

env = dotenv_values(".env")
gemini_key = env.get("GEMINI_API_KEY", "").strip()
openrouter_key = env.get("OPENROUTER_API_KEY", "").strip()

models_to_test = [
    {"provider": "Google Gemini", "model": "gemini-flash-lite-latest", "type": "gemini"},
    {"provider": "OpenRouter", "model": "deepseek/deepseek-r1:free", "type": "openrouter"},
    {"provider": "OpenRouter", "model": "meta-llama/llama-3.1-8b-instruct:free", "type": "openrouter"},
    {"provider": "OpenRouter", "model": "meta-llama/llama-3.2-3b-instruct:free", "type": "openrouter"},
    {"provider": "OpenRouter", "model": "mistralai/mistral-7b-instruct:free", "type": "openrouter"}
]

async def test_single(client, m):
    name = f"{m['provider']} ({m['model']})"
    start = time.time()
    try:
        if m["type"] == "gemini":
            if not gemini_key:
                return name, False, "مفتاح GEMINI_API_KEY مفقود", 0
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m['model']}:generateContent?key={gemini_key}"
            payload = {"contents": [{"parts": [{"text": "Medical ping"}]}]}
            resp = await client.post(url, json=payload, timeout=15.0)
            elapsed = round((time.time() - start) * 1000, 1)
            if resp.status_code == 200:
                return name, True, "جاهز ويعمل بنجاح ✅", elapsed
            return name, False, f"فشل (HTTP {resp.status_code})", elapsed

        elif m["type"] == "openrouter":
            if not openrouter_key:
                return name, False, "مفتاح OPENROUTER_API_KEY مفقود", 0
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me/MEDBOT",
                "X-Title": "MEDBOT Diagnostic"
            }
            payload = {
                "model": m["model"],
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 10
            }
            resp = await client.post(url, json=payload, headers=headers, timeout=20.0)
            elapsed = round((time.time() - start) * 1000, 1)
            if resp.status_code == 200:
                return name, True, "جاهز ويعمل بنجاح ✅", elapsed
            return name, False, f"فشل (HTTP {resp.status_code}) - {resp.text[:50]}", elapsed
    except Exception as e:
        elapsed = round((time.time() - start) * 1000, 1)
        return name, False, f"خطأ: {str(e)[:30]}", elapsed

async def run():
    print("\n🔍 جاري إعادة فحص النماذج المجانية المعتمدة...\n" + "━"*55)
    active_count = 0
    async with httpx.AsyncClient() as client:
        tasks = [test_single(client, m) for m in models_to_test]
        results = await asyncio.gather(*tasks)
        for name, ok, msg, lat in results:
            if ok:
                active_count += 1
                print(f"🟢 {name:<45} | {lat:>6}ms | {msg}")
            else:
                print(f"🔴 {name:<45} | {lat:>6}ms | {msg}")

    print("━"*55)
    print(f"📊 الخلاصة: {active_count} من أصل {len(models_to_test)} نماذج تعمل بنجاح.")

if __name__ == "__main__":
    asyncio.run(run())
