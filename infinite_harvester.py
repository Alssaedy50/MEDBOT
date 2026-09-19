import asyncio
import json
import os
import time
import httpx
from dotenv import dotenv_values

env = dotenv_values(".env")
gemini_key = env.get("GEMINI_API_KEY", "").strip()
openrouter_key = env.get("OPENROUTER_API_KEY", "").strip()
groq_key = env.get("GROQ_API_KEY", "").strip()

DB_FILE = "verified_models.json"
TARGET_COUNT = 20
TEST_PROMPT = "Explain Acetazolamide mechanism in glaucoma briefly in 10 words."
IGNORED = ["guard", "whisper", "vision", "embed", "moderation", "vl:free", "vl-", "vl_"]

def load_verified():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_verified(models):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(models, f, ensure_ascii=False, indent=2)
    update_router_file(models)

def update_router_file(models):
    if not models: return
    models_repr = [f"            ('{m['provider']}', '{m['model']}')" for m in models]
    models_code = ",\n".join(models_repr)

    router_code = f'''import asyncio
import logging
import time
import httpx
from typing import Optional
import database as db
import search_engine

logger = logging.getLogger(__name__)

class AIProvider:
    def __init__(self, name: str, model: str, endpoint: str, api_key: str = ""):
        self.name = name
        self.model = model
        self.endpoint = endpoint
        self.api_key = api_key

    async def generate(self, prompt: str, system_prompt: str = None) -> str:
        raise NotImplementedError

class GeminiProvider(AIProvider):
    def __init__(self, api_key: str, model: str):
        super().__init__("google_gemini", model, f"https://generativelanguage.googleapis.com/v1beta/models/{{model}}:generateContent", api_key)

    async def generate(self, prompt: str, system_prompt: str = None) -> str:
        full_prompt = (system_prompt + "\\n\\n" + prompt) if system_prompt else prompt
        payload = {{"contents": [{{"parts": [{{"text": full_prompt}}]}}]}}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{{self.endpoint}}?key={{self.api_key}}", json=payload)
            if resp.status_code == 200:
                parts = resp.json().get("candidates", [])[0].get("content", {{}}).get("parts", [])
                texts = [p.get("text", "") for p in parts if "text" in p]
                return texts[-1].strip() if texts else ""
            raise Exception(f"Gemini error: HTTP {{resp.status_code}}")

class GroqProvider(AIProvider):
    def __init__(self, api_key: str, model: str):
        super().__init__("groq", model, "https://api.groq.com/openai/v1/chat/completions", api_key)

    async def generate(self, prompt: str, system_prompt: str = None) -> str:
        messages = []
        if system_prompt: messages.append({{"role": "system", "content": system_prompt}})
        messages.append({{"role": "user", "content": prompt}})
        headers = {{"Authorization": f"Bearer {{self.api_key}}", "Content-Type": "application/json"}}
        payload = {{"model": self.model, "messages": messages, "max_tokens": 1200, "temperature": 0.2}}
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(self.endpoint, json=payload, headers=headers)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            raise Exception(f"Groq error: HTTP {{resp.status_code}}")

class OpenRouterProvider(AIProvider):
    def __init__(self, api_key: str, model: str):
        super().__init__("openrouter", model, "https://openrouter.ai/api/v1/chat/completions", api_key)

    async def generate(self, prompt: str, system_prompt: str = None) -> str:
        messages = []
        if system_prompt: messages.append({{"role": "system", "content": system_prompt}})
        messages.append({{"role": "user", "content": prompt}})
        headers = {{
            "Authorization": f"Bearer {{self.api_key}}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/MEDBOT",
            "X-Title": "MEDBOT Mega Fleet"
        }}
        payload = {{"model": self.model, "messages": messages, "max_tokens": 1200, "temperature": 0.2}}
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(self.endpoint, json=payload, headers=headers)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            raise Exception(f"OpenRouter [{{self.model}}] error: HTTP {{resp.status_code}}")

class AIRouter:
    def __init__(self, gemini_api_key: str = "", openrouter_api_key: str = "", groq_api_key: str = ""):
        self.providers = []
        verified_pool = [
{models_code}
        ]
        for p_type, p_model in verified_pool:
            if p_type == "google_gemini" and gemini_api_key:
                self.providers.append(GeminiProvider(gemini_api_key, p_model))
            elif p_type == "groq" and groq_api_key:
                self.providers.append(GroqProvider(groq_api_key, p_model))
            elif p_type == "openrouter" and openrouter_api_key:
                self.providers.append(OpenRouterProvider(openrouter_api_key, p_model))

    async def generate_with_failover(self, prompt: str, system_prompt: str = None) -> tuple[Optional[str], Optional[str], float, Optional[str]]:
        last_error = None
        for attempt, provider in enumerate(self.providers):
            try:
                start = time.time()
                response = await provider.generate(prompt, system_prompt)
                elapsed = (time.time() - start) * 1000
                logger.info(f"AI Success from [{{provider.name}} | {{provider.model}}] in {{round(elapsed, 1)}}ms")
                return response, f"{{provider.name}}/{{provider.model}}", round(elapsed, 1), None
            except Exception as e:
                last_error = e
                logger.warning(f"Failover switch: [{{provider.name}} | {{provider.model}}] failed ({{e}}). Trying next model...")
                await asyncio.sleep(0.3)
                continue
        return None, None, 0, f"All AI fleet failed. Last error: {{last_error}}"

async def ai_generate_grounded(user_query: str, gemini_api_key: str, openrouter_api_key: str = "", openai_api_key: str = "") -> tuple[str, str, float, str]:
    from dotenv import dotenv_values
    env = dotenv_values(".env")
    groq_api_key = env.get("GROQ_API_KEY", "").strip()
    search_results = await search_engine.search_library(user_query, limit=5)
    router = AIRouter(gemini_api_key, openrouter_api_key, groq_api_key)
    
    found_resources = ""
    if search_results:
        lines = []
        for r in search_results:
            rtype = r.get("result_type", r.get("type", ""))
            name = r.get("name", r.get("title", ""))
            path = r.get("path", "")
            if rtype == "CONTENT": lines.append(f"- 📄 {{name}} | 📍 {{path}}")
            elif rtype in ("FOLDER", "EMPTY_FOLDER"): lines.append(f"- 📁 {{name}} ({{r.get('content_count', 0)}} ملف) | 📍 {{path}}")
        found_resources = "\\n".join(lines) if lines else "لا توجد ملفات مطابقة في كشاف المنصة."
    else:
        found_resources = "المورد المطلوب غير مسجل حالياً في مكتبة MEDBOT."

    system_prompt = f"""أنت 'المساعد الطبي الأكاديمي الذكي لمنصة MEDBOT' لطلاب كلية الطب البشري.
الهدف الأساسي: تقديم أعلى درجات الجودة، الدقة العلمية، والموثوقية الأكاديمية المبنية على الدليل (Evidence-based).

قواعد الرد:
1. إذا سأل الطالب عن حدود الاستخدام أو ساعات العمل:
   وضح بلباقة أن الخدمة مجانية للطلاب، وتخضع لسعة الاستيعاب اللحظية.

2. شرح المصطلحات والمفاهيم الطبية:
   التزم بالهيكل الأكاديمي التالي بدقة:

🩺 **[المصطلح بالإنجليزية والعربية]**

📖 **English Definition:**
(تعريف أكاديمي قياسي وموثوق باللغة الإنجليزية).

💡 **الشرح بالعربية (Summary & Reasoning):**
• **ما هو (What):** شرح وافٍ وموجز للمفهوم.
• **الآلية الحيوية (Mechanism):** تسلسل الآلية الحيوية أو الإمراضية (A → B → C).
• **الأهمية السريرية والامتحانية (Clinical & Exam Pearl):** نقطة فارقة سريرياً أو فخ امتحاني معتاد.

📍 **المسار في منصة MEDBOT:**
(المسار الدقيق إذا وُجد في الكشاف أدناه، أو اذكر: "الملف غير مسجل حالياً داخل المنصة").

📋 كشاف المنصة المتاح:
{{found_resources}}
"""
    response, provider_name, latency, error = await router.generate_with_failover(user_query, system_prompt)
    return response, provider_name, latency, error
'''
    with open("ai_router.py", "w", encoding="utf-8") as f:
        f.write(router_code)

async def test_single_model(client, provider, model):
    start = time.time()
    try:
        if provider == "google_gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            payload = {"contents": [{"parts": [{"text": TEST_PROMPT}]}]}
            r = await client.post(url, json=payload, timeout=20.0)
            if r.status_code == 200:
                elapsed = round((time.time() - start) * 1000, 1)
                t = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if len(t) > 3: return {"provider": provider, "model": model, "latency": elapsed, "sample": t.replace('\n', ' ')[:40]}
            else: print(f"❌ {model} فشل: HTTP {r.status_code}")

        elif provider == "groq":
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
            payload = {"model": model, "messages": [{"role": "user", "content": TEST_PROMPT}], "max_tokens": 40}
            r = await client.post(url, json=payload, headers=headers, timeout=20.0)
            if r.status_code == 200:
                elapsed = round((time.time() - start) * 1000, 1)
                t = r.json()["choices"][0]["message"]["content"].strip()
                if len(t) > 10 and not t.replace('.','').isdigit():
                    return {"provider": provider, "model": model, "latency": elapsed, "sample": t.replace('\n', ' ')[:40]}
            else: print(f"❌ {model} فشل: HTTP {r.status_code}")

        elif provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me/MEDBOT",
                "X-Title": "MEDBOT Harvester"
            }
            payload = {"model": model, "messages": [{"role": "user", "content": TEST_PROMPT}], "max_tokens": 40}
            r = await client.post(url, json=payload, headers=headers, timeout=20.0)
            if r.status_code == 200:
                elapsed = round((time.time() - start) * 1000, 1)
                t = r.json()["choices"][0]["message"]["content"].strip()
                if len(t) > 5: return {"provider": provider, "model": model, "latency": elapsed, "sample": t.replace('\n', ' ')[:40]}
            elif r.status_code == 429:
                print(f"⚠️ {model} Rate Limit - ننتظر 5 ثوانٍ...")
                await asyncio.sleep(5)
            else:
                print(f"❌ {model} فشل: HTTP {r.status_code}")
    except httpx.TimeoutException:
        print(f"⏳ {model}: TimeOut")
    except Exception as e:
        print(f"⚠️ {model}: Error")
    return None

async def main():
    verified_list = load_verified()
    known_keys = set(f"{m['provider']}:{m['model']}" for m in verified_list)
    print(f"\n📦 الأرشيف يحتوي على {len(verified_list)} نموذج (الهدف: {TARGET_COUNT}).")
    print("🚀 جاري السحب الديناميكي للموديلات من الخوادم مباشرة...")
    print("━" * 80)

    async with httpx.AsyncClient() as client:
        # 1. Gemini
        gemini_model = "gemini-flash-lite-latest"
        if f"google_gemini:{gemini_model}" not in known_keys:
            res = await test_single_model(client, "google_gemini", gemini_model)
            if res:
                verified_list.append(res)
                known_keys.add(f"google_gemini:{gemini_model}")
                save_verified(verified_list)
                print(f"🟢 [{len(verified_list)}/{TARGET_COUNT}] {gemini_model:<35} | {res['latency']}ms")

        # 2. جلب وتجربة كل موديلات Groq الحية
        if groq_key and len(verified_list) < TARGET_COUNT:
            try:
                g_resp = await client.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {groq_key}"}, timeout=8.0)
                if g_resp.status_code == 200:
                    live_groq_models = [m["id"] for m in g_resp.json().get("data", []) if not any(k in m["id"].lower() for k in IGNORED)]
                    for gm in live_groq_models:
                        if len(verified_list) >= TARGET_COUNT: break
                        if f"groq:{gm}" in known_keys: continue
                        res = await test_single_model(client, "groq", gm)
                        if res:
                            verified_list.append(res)
                            known_keys.add(f"groq:{gm}")
                            save_verified(verified_list)
                            print(f"🟢 [{len(verified_list)}/{TARGET_COUNT}] {gm:<35} | {res['latency']}ms")
                        await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Groq API Error: {e}")

        # 3. جلب وتجربة كل موديلات OpenRouter المجانية الحية
        if openrouter_key and len(verified_list) < TARGET_COUNT:
            try:
                or_resp = await client.get("https://openrouter.ai/api/v1/models", timeout=10.0)
                if or_resp.status_code == 200:
                    data = or_resp.json().get("data", [])
                    free_or_models = []
                    for m in data:
                        mid = m["id"]
                        p_prompt = float(m.get("pricing", {}).get("prompt", 1))
                        p_comp = float(m.get("pricing", {}).get("completion", 1))
                        if (mid.endswith(":free") or (p_prompt == 0 and p_comp == 0)) and not any(k in mid.lower() for k in IGNORED):
                            free_or_models.append(mid)

                    print(f"--- فحص {len(free_or_models)} نموذج مجاني تم سحبه للتو من OpenRouter ---")
                    for mid in free_or_models:
                        if len(verified_list) >= TARGET_COUNT: break
                        if f"openrouter:{mid}" in known_keys: continue
                        res = await test_single_model(client, "openrouter", mid)
                        if res:
                            verified_list.append(res)
                            known_keys.add(f"openrouter:{mid}")
                            save_verified(verified_list)
                            print(f"🟢 [{len(verified_list)}/{TARGET_COUNT}] {mid:<35} | {res['latency']}ms")
                        
                        # فاصل 4 ثواني صارم جداً لمنع الـ Rate Limit 429 من OpenRouter
                        await asyncio.sleep(4.0)
            except Exception as e:
                print(f"OpenRouter API Error: {e}")

    print("━" * 80)
    print(f"🎉 الخلاصة: تم توثيق {len(verified_list)} نموذجاً في الأرشيف ودمجها فوراً في ai_router.py!")

if __name__ == "__main__":
    asyncio.run(main())
