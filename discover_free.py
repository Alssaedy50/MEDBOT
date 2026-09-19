import asyncio
import httpx
from dotenv import dotenv_values

env = dotenv_values(".env")
openrouter_key = env.get("OPENROUTER_API_KEY", "").strip()

async def discover():
    if not openrouter_key:
        print("❌ مفتاح OPENROUTER_API_KEY غير موجود في .env")
        return

    print("\n🔍 جاري الاستعلام المباشر من OpenRouter عن النماذج المجانية النشطة حالياً...")
    async with httpx.AsyncClient(timeout=20.0) as client:
        # جلب قائمة جميع النماذج
        resp = await client.get("https://openrouter.ai/api/v1/models")
        if resp.status_code != 200:
            print(f"❌ تعذر جلب قائمة النماذج: HTTP {resp.status_code}")
            return

        data = resp.json().get("data", [])
        # استخراج النماذج التي تنتهي بـ :free أو تسعيرتها 0
        free_models = [
            m["id"] for m in data 
            if m["id"].endswith(":free") or (
                float(m.get("pricing", {}).get("prompt", 1)) == 0 and 
                float(m.get("pricing", {}).get("completion", 1)) == 0
            )
        ]

        print(f"📋 تم العثور على {len(free_models)} نموذج مجاني مسجل. جاري فحص استجابتها الفعلية مع مفتاحك:\n" + "━"*60)

        working_models = []
        headers = {
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/MEDBOT",
            "X-Title": "MEDBOT Discovery"
        }

        for model_id in free_models[:8]:  # فحص أول 8 نماذج مجانية
            payload = {
                "model": model_id,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5
            }
            try:
                test_resp = await client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers, timeout=12.0)
                if test_resp.status_code == 200:
                    print(f"🟢 {model_id:<45} | متاح ويعمل بنجاح ✅")
                    working_models.append(model_id)
                else:
                    err_msg = test_resp.json().get("error", {}).get("message", "")[:40]
                    print(f"🔴 {model_id:<45} | غير متاح ({err_msg})")
            except Exception:
                print(f"🔴 {model_id:<45} | انتهت مهلة الاتصال")

        print("━"*60)
        print(f"🎉 النماذج المجانية النشطة والمستعدة للعمل الفوري: {len(working_models)}")
        if working_models:
            print("النماذج المعتمدة:")
            for wm in working_models:
                print(f"  - '{wm}'")

if __name__ == "__main__":
    asyncio.run(discover())
