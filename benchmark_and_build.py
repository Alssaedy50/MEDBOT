import asyncio
import time
import httpx
from dotenv import dotenv_values

env = dotenv_values(".env")
gemini_key = env.get("GEMINI_API_KEY", "").strip()
openrouter_key = env.get("OPENROUTER_API_KEY", "").strip()

GEMINI_CANDIDATES = [
    "gemini-flash-lite-latest",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite-preview-02-05"
]

OPENROUTER_FREE_CANDIDATES = [
    "inclusionai/ling-3.0-flash-sante:free",
    "nex-agi/nex-n2.5-pro:free",
    "nex-agi/nex-n2.5-mini:free",
    "inclusionai/ling-3.0-flash-vl:free",
    "dots-studio/dots-3-note-preview:free",
    "liquid/lfm-2.5-2.6b:free"
]

TEST_PROMPT = "Explain the pharmacological mechanism of Acetazolamide in glaucoma briefly."

async def test_gemini(client, model):
    if not gemini_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
    payload = {"contents": [{"parts": [{"text": TEST_PROMPT}]}]}
    start = time.time()
    try:
        resp = await client.post(url, json=payload, timeout=20.0)
        elapsed = round((time.time() - start) * 1000, 1)
        if resp.status_code == 200:
            return {"provider": "google_gemini", "model": model, "latency": elapsed}
    except Exception:
        pass
    return None

async def test_openrouter(client, model):
    if not openrouter_key:
        return None
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {openrouter_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/MEDBOT",
        "X-Title": "MEDBOT Clinical Benchmark"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 300
    }
    start = time.time()
    try:
        resp = await client.post(url, json=payload, headers=headers, timeout=25.0)
        elapsed = round((time.time() - start) * 1000, 1)
        if resp.status_code == 200:
            return {"provider": "openrouter", "model": model, "latency": elapsed}
    except Exception:
        pass
    return None

async def main():
    print("\n🩺 جاري فحص واختبار التشكيلة الأكاديمية الرسمية...")
    print("━" * 65)
    
    passed_models = []
    async with httpx.AsyncClient() as client:
        for gm in GEMINI_CANDIDATES:
            res = await test_gemini(client, gm)
            if res:
                print(f"🟢 Google Gemini [{res['model']:<32}] | {res['latency']:>6}ms | اجتاز الفحص")
                passed_models.append(res)
            else:
                print(f"🔴 Google Gemini [{gm:<32}] | غير متاح")
            await asyncio.sleep(0.5)

        for om in OPENROUTER_FREE_CANDIDATES:
            res = await test_openrouter(client, om)
            if res:
                print(f"🟢 OpenRouter    [{res['model']:<32}] | {res['latency']:>6}ms | اجتاز الفحص")
                passed_models.append(res)
            else:
                print(f"🔴 OpenRouter    [{om:<32}] | غير متاح")
            await asyncio.sleep(1.0)

    print("━" * 65)
    print(f"🎉 إجمالي النماذج المعتمدة الجاهزة: {len(passed_models)}")
    
    if not passed_models:
        print("⚠️ لم ينجح أي نموذج.")
        return

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
        super().__init__(
            name="google_gemini",
            model=model,
            endpoint=f"https://generativelanguage.googleapis.com/v1beta/models/{{model}}:generateContent",
            api_key=api_key
        )

    async def generate(self, prompt: str, system_prompt: str = None) -> str:
        full_prompt = (system_prompt + "\\n\\n" + prompt) if system_prompt else prompt
        payload = {{"contents": [{{"parts": [{{"text": full_prompt}}]}}]}}
        async with httpx.AsyncClient(timeout=35.0) as client:
            url = f"{{self.endpoint}}?key={{self.api_key}}"
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                data = response.json()
                parts = data.get("candidates", [])[0].get("content", {{}}).get("parts", [])
                texts = [p.get("text", "") for p in parts if "text" in p]
                return texts[-1].strip() if texts else ""
            else:
                raise Exception(f"Gemini error: HTTP {{response.status_code}}")

class OpenRouterProvider(AIProvider):
    def __init__(self, api_key: str, model: str):
        super().__init__(
            name="openrouter",
            model=model,
            endpoint="https://openrouter.ai/api/v1/chat/completions",
            api_key=api_key
        )

    async def generate(self, prompt: str, system_prompt: str = None) -> str:
        messages = []
        if system_prompt:
            messages.append({{"role": "system", "content": system_prompt}})
        messages.append({{"role": "user", "content": prompt}})
        
        headers = {{
            "Authorization": f"Bearer {{self.api_key}}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/MEDBOT",
            "X-Title": "MEDBOT Medical Assistant"
        }}
        payload = {{
            "model": self.model,
            "messages": messages,
            "max_tokens": 1200,
            "temperature": 0.2
        }}
        async with httpx.AsyncClient(timeout=35.0) as client:
            response = await client.post(self.endpoint, json=payload, headers=headers)
            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"].strip()
            else:
                raise Exception(f"OpenRouter [{{self.model}}] error: HTTP {{response.status_code}}")

class AIRouter:
    def __init__(self, gemini_api_key: str = "", openrouter_api_key: str = "", openai_api_key: str = ""):
        self.providers = []
        verified_pool = [
{models_code}
        ]
        for p_type, p_model in verified_pool:
            if p_type == "google_gemini" and gemini_api_key:
                self.providers.append(GeminiProvider(gemini_api_key, p_model))
            elif p_type == "openrouter" and openrouter_api_key:
                self.providers.append(OpenRouterProvider(openrouter_api_key, p_model))

    async def generate_with_failover(self, prompt: str, system_prompt: str = None) -> tuple[Optional[str], Optional[str], float, Optional[str]]:
        last_error = None
        for attempt, provider in enumerate(self.providers):
            try:
                start = time.time()
                response = await provider.generate(prompt, system_prompt)
                elapsed = (time.time() - start) * 1000
                logger.info(f"AI Response from [{{provider.name}} | {{provider.model}}] in {{round(elapsed, 1)}}ms")
                return response, f"{{provider.name}}/{{provider.model}}", round(elapsed, 1), None
            except Exception as e:
                last_error = e
                logger.warning(f"Failover switch: [{{provider.name}} | {{provider.model}}] failed ({{e}}). Next model...")
                await asyncio.sleep(0.5)
                continue
        return None, None, 0, f"All AI providers failed. Last error: {{last_error}}"

async def ai_generate_grounded(user_query: str, gemini_api_key: str, openrouter_api_key: str = "", openai_api_key: str = "") -> tuple[str, str, float, str]:
    search_results = await search_engine.search_library(user_query, limit=5)
    router = AIRouter(gemini_api_key, openrouter_api_key, openai_api_key)
    
    found_resources = ""
    if search_results:
        lines = []
        for r in search_results:
            rtype = r.get("result_type", r.get("type", ""))
            name = r.get("name", r.get("title", ""))
            path = r.get("path", "")
            if rtype == "CONTENT":
                lines.append(f"- 📄 {{name}} | 📍 {{path}}")
            elif rtype in ("FOLDER", "EMPTY_FOLDER"):
                cc = r.get("content_count", 0)
                lines.append(f"- 📁 {{name}} ({{cc}} ملف) | 📍 {{path}}")
        found_resources = "\\n".join(lines) if lines else "لا توجد ملفات مطابقة في كشاف المنصة."
    else:
        found_resources = "المورد المطلوب غير مسجل حالياً في مكتبة MEDBOT."

    system_prompt = f"""أنت 'المساعد الطبي الأكاديمي الذكي لمنصة MEDBOT' لطلاب كلية الطب البشري.

الهدف الأساسي: تقديم أعلى درجات الجودة، الدقة العلمية، والموثوقية الأكاديمية المبنية على الدليل (Evidence-based).

قواعد الرد:
1. إذا سأل الطالب عن حدود الاستخدام أو ساعات العمل:
   وضح بلباقة أن الخدمة متاحة ومجانية دائماً للطلاب، وتخضع لسعة الاستيعاب اللحظية.

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
    print("✅ تم تحديث ai_router.py بنجاح بالنماذج الفعالة!")

if __name__ == "__main__":
    asyncio.run(main())
