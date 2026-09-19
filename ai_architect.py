import json
import logging
import re
import httpx
from dotenv import dotenv_values
import database as db

logger = logging.getLogger(__name__)

async def get_current_tree_context() -> str:
    conn = await db.get_db()
    async with conn.execute("SELECT id, parent_id, name, node_type FROM folders ORDER BY id ASC") as cur:
        rows = await cur.fetchall()
    await conn.close()
    lines = []
    for fid, pid, name, ntype in rows:
        lines.append(f"- ID: {fid} | Parent: {pid} | Name: {name} | Type: {ntype}")
    return "\n".join(lines) if lines else "قاعدة البيانات فارغة حالياً."

async def parse_structure_prompt(user_prompt: str) -> tuple[list, str]:
    env = dotenv_values(".env")
    gemini_key = env.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        return None, "مفتاح GEMINI_API_KEY غير متوفر في ملف .env."

    lower_prompt = user_prompt.strip().lower()
    if lower_prompt in ["مرحبا", "مرحباً", "السلام عليكم", "هلا", "أهلاً", "hi", "hello"]:
        return None, "👋 أهلاً بك في منصة MEDBOT الطبية! يرجى إرسال طلبك الهيكلي أو البحث عما تريده لنقوم بمساعدتك."

    current_tree = await get_current_tree_context()

    system_prompt = f"""أنت مهندس قواعد بيانات ذكي لمنصة MEDBOT الطبية.
مهمتك: تحليل شجرة الأقسام الحالية المرفقة، ثم ترجمة طلب المالك إلى مصفوفة JSON تحتوي على الأقسام الجديدة التي يجب إنشاؤها وربطها بالمعرف الصحيح (target_pid).

📂 شجرة قاعدة البيانات الحالية:
{current_tree}

📋 قواعد التحليل:
1. إذا طلب المالك "في كل مادة من مواد البلوك" أو "في كل مادة طولية" أو عبارة تعميم لإضافة (نظري / عملي):
   ابحث في الشجرة عن جميع المواد التابعة للمجلدات (مثل 'بلوكات' أو 'مواد طولية').
   لكل مادة، تحقق من أبنائها الحاليين:
   - إذا لم يكن لديها مجلد "نظري"، أضف قسماً باسم "نظري" وحدد `target_pid` برقم المادة.
   - إذا لم يكن لديها مجلد "عملي" (أو Practical)، أضف قسماً باسم "عملي" وحدد `target_pid` برقم المادة.
2. عدم التكرار: إذا كان القسم موجوداً مسبقاً تحت المادة، لا تضفه في المصفوفة إطلاقاً.
3. node_type: "general" للأقسام العامة والنظري والعملي، "book" للملازم، "mcq" للأسئلة، "summary" للملخصات.
4. accepts_contributions: 0 دائماً إلا إذا نص صراحة على مساهمات الطلاب فاجعله 1.
5. إذا كان الإدخال تحية أو كلاماً عاماً لا يتضمن طلباً هيكلياً، أرجع مصفوفة فارغة [].

أرجع فقط مصفوفة JSON صالحة ومباشرة دون أي نصوص أخرى:
[
  {
    "target_pid": 17,
    "name": "نظري",
    "node_type": "general",
    "accepts_contributions": 0,
    "children": []
  }
]
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"parts": [{"text": system_prompt + f"\n\nطلب المالك:\n{user_prompt}"}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json"
        }
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                return None, f"خطأ من محرك الذكاء الاصطناعي (كود {resp.status_code})"
            data = resp.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if "```" in raw_text:
                parts = raw_text.split("```")
                raw_text = parts[1] if len(parts) > 1 else raw_text
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            raw_text = raw_text.strip()
            parsed_tree = json.loads(raw_text)
            if isinstance(parsed_tree, list):
                if not parsed_tree:
                    return None, "👋 أهلاً بك! تم تلقي رسالتك بنجاح. كيف يمكنني مساعدتك في إدارة أقسام ومحتويات MEDBOT الطبية اليوم؟"
                return parsed_tree, ""
            return None, "لم يُرجع النموذج مصفوفة أقسام صالحة."
    except Exception as e:
        logger.error(f"AI Architect Error: {e}")
        return None, f"تعذر تحليل الطلب حالياً بسبب خطأ داخلي."