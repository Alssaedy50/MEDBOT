import os
import re

target_files = ["ai_architect.py", "main.py"]

for fname in target_files:
    fpath = os.path.join(os.path.expanduser("~/MEDBOT"), fname)
    if not os.path.exists(fpath):
        continue

    with open(fpath, "r", encoding="utf-8") as f:
        code = f.read()

    # 1. إزالة الرسالة الهجينة المتناقضة
    broken_str = "❌ تعذر البناء: ✅ تم فرض إعادة بناء وتحديث كافة الأقسام والمواد (نظري وعملي) بنجاح تام!"
    clean_guidance = (
        "⚠️ لم يتم تحديد أي مواد أو تعديلات هيكلية في رسالتك.\n\n"
        "💡 المساعد الذكي مخصص لترتيب وتوليد مواد المنصة، مثال:\n"
        "«أضف مادة التشريح Anatomy لسنة أولى نظري وعملي»\n\n"
        "🔙 للرجوع للقائمة الرئيسية أرسل: /cancel"
    )
    if broken_str in code:
        code = code.replace(broken_str, clean_guidance)

    # 2. تنظيف أي تركيب خاطئ لعبارة تعذر البناء
    code = re.sub(r'❌ تعذر البناء:\s*✅[^\n"\']+', clean_guidance, code)

    with open(fpath, "w", encoding="utf-8") as f:
        f.write(code)

    print(f"✅ تم تصحيح ومعالجة الملف: {fname}")

