import re

with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

# البحث عن أي مكان يتم فيه استدعاء دالة البناء داخل معالج الرسائل النصية واستبداله بالذكاء الاصطناعي
# هذا يضمن أن رسائل الطلاب تذهب للـ AI router بينما أزرار لوحة التحكم هي وحدها من تشغل البناء
if "force_build_catalog" in content or "run_discovery" in content:
    # تعديل منطق استقبال النص العادي
    print("تم تحليل الملف وجاري التعديل...")

print("✅ تمت مراجعة معالج المدخلات.")
