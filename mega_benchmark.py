import asyncio
import time
import httpx
from dotenv import dotenv_values

env = dotenv_values(".env")
gemini_key = env.get("GEMINI_API_KEY", "").strip()
openrouter_key = env.get("OPENROUTER_API_KEY", "").strip()
groq_key = env.get("GROQ_API_KEY", "").strip()

TEST_PROMPT = "Acetazolamide mechanism in glaucoma in 10 words."

async def test_gemini(client, model):
    if not gemini_key: return None
    # تجربة مسارات Google الرسمية
    urls = [
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}",
        f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={gemini_key}"
    ]
    payload = {"contents": [{"parts": [{"text": TEST_PROMPT}]}]}
    for url in urls:
        start = time.time()
        try:
            r = await client.post(url, json=payload, timeout=12.0)
            if r.status_code == 200:
                elapsed = round((time.time() - start) * 1000, 1)
                t = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if len(t) > 3:
                    return {"provider": "google_gemini", "model": model, "latency": elapsed, "sample": t.replace('\n', ' ')[:50]}
        except Exception:
            pass
    return None

async def test_groq(client, model):
    if not groq_key: return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": TEST_PROMPT}], "max_tokens": 50}
    start = time.time()
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=10.0)
        if r.status_code == 200:
            elapsed = round((time.time() - start) * 1000, 1)
            t = r.json()["choices"][0]["message"]["content"].strip()
            if len(t) > 3:
                return {"provider": "groq", "model": model, "latency": elapsed, "sample": t.replace('\n', ' ')[:50]}
    except Exception:
        pass
    return None

async def test_openrouter(client, model):
    if not openrouter_key: return None
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {openrouter_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/MEDBOT",
        "X-Title": "MEDBOT 20 Fleet"
    }
    payload = {"model": model, "messages": [{"role": "user", "content": TEST_PROMPT}], "max_tokens": 50}
    start = time.time()
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=18.0)
        if r.status_code == 200:
            elapsed = round((time.time() - start) * 1000, 1)
            t = r.json()["choices"][0]["message"]["content"].strip()
            if len(t) > 3:
                return {"provider": "openrouter", "model": model, "latency": elapsed, "sample": t.replace('\n', ' ')[:50]}
    except Exception:
        pass
    return None

async def main():
    print("\n🔍 جاري بناء واعتماد أسطول الـ 20 نموذجاً المؤكدة...")
    print("━" * 80)
    passed_models = []

    async with httpx.AsyncClient() as client:
        # 1. قائمة نماذج Gemini الكاملة للمعاينة
        gemini_pool = [
            "gemini-flash-lite-latest",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite-preview-02-05",
            "gemini-1.5-flash",
            "gemini-1.5-flash-latest",
            "gemini-1.5-pro",
            "gemini-1.5-pro-latest"
        ]
        for gm in gemini_pool:
            if len(passed_models) >= 20: break
            res = await test_gemini(client, gm)
            if res:
                passed_models.append(res)
                print(f"🟢 [Gemini   ] {res['model']:<36} | {res['latency']:>6}ms | {res['sample']}")
            await asyncio.sleep(0.3)

        # 2. نماذج Groq الفائقة المؤكدة
        if groq_key:
            try:
                g_resp = await client.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {groq_key}"}, timeout=8.0)
                if g_resp.status_code == 200:
                    groq_live = [m["id"] for m in g_resp.json().get("data", []) if not any(x in m["id"] for x in ["whisper", "prompt-guard"])]
                    for gm in groq_live:
                        if len(passed_models) >= 20: break
                        res = await test_groq(client, gm)
                        if res:
                            passed_models.append(res)
                            print(f"🟢 [Groq     ] {res['model']:<36} | {res['latency']:>6}ms | {res['sample']}")
                        await asyncio.sleep(0.3)
            except Exception:
                pass

        # 3. كافة نماذج OpenRouter المجانية (دون استثناء أي نموذج)
        if openrouter_key:
            try:
                resp = await client.get("https://openrouter.ai/api/v1/models", timeout=12.0)
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    free_models = [
                        m["id"] for m in data 
                        if m["id"].endswith(":free") or (
                            float(m.get("pricing", {}).get("prompt", 1)) == 0 and 
                            float(m.get("pricing", {}).get("completion", 1)) == 0
                        )
                    ]
                    print(f"--- فحص نماذج OpenRouter المجانية ({len(free_models)} نموذج متاح) ---")
                    for mid in free_models:
                        if len(passed_models) >= 20:
                            break
                        res = await test_openrouter(client, mid)
                        if res:
                            passed_models.append(res)
                            print(f"🟢 [OpenRtr  ] {res['model']:<36} | {res['latency']:>6}ms | {res['sample']}")
                        await asyncio.sleep(0.5)
            except Exception:
                pass

    print("━" * 80)
    print(f"🎉 الخلاصة: تم توثيق واجتياز {len(passed_models)} نموذجاً طبياً جاهزاً للعمل!")

    if not passed_models:
        print("❌ لم ينجح أي نموذج.")
        return

    # كتابة ملف ai_router.py
    models_repr = []
    for m in passed_models:
        models_repr.append(f"            ('{m['provider']}', '{m['model']}')")
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
            "X-Title": "MEDBOT 20 Fleet"
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
                logger.info(f"AI Success: [{{provider.name}} | {{provider.model}}] in {{round(elapsed, 1)}}ms")
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

async def run_discovery(gemini_api_key: str = "", openrouter_api_key: str = "", openai_api_key: str = ""):
    pass
'''
    with open("ai_router.py", "w", encoding="utf-8") as f:
        f.write(router_code)
    print("✅ تم تحديث ai_router.py بأسطول النماذج المعتمدة بنجاح!")

if __name__ == "__main__":
    asyncio.run(main())
