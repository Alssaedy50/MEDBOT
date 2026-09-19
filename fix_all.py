import os
import re

# 1. إصلاح ai_architect.py ومعالجة النصوص الترحيبية وغير المفهومة
for fname in ["ai_architect.py", "main.py"]:
    if os.path.exists(fname):
        with open(fname, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # استبدال رسالة الخطأ المتناقضة برد إرشادي ذكي
        target = "❌ تعذر البناء: ✅ تم فرض إعادة بناء وتحديث كافة الأقسام والمواد (نظري وعملي) بنجاح تام!"
        replacement = (
            "⚠️ لم يتم التعرف على أي مواد أو أقسام طبية في رسالتك.\n\n"
            "💡 هذا القسم مخصص لهيكلة وتعديل مواد المنصة، أرسل تعليمة واضحة مثل:\n"
            "«أضف مادة التشريح لسنة ثانية نظري وعملي»\n\n"
            "🔙 للخروج والعودة للقائمة الرئيسية أرسل: /cancel"
        )
        if target in content:
            content = content.replace(target, replacement)

        # التأكد من إلغاء أي رسائل تعذر قديمة متبقية
        content = content.replace("❌ تعذر البناء: ", "")

        with open(fname, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅ تم ضبط: {fname}")

# 2. إضافة معالج إلغاء فوري في main.py لتصفير الحالة عند طلب /cancel أو /start
with open("main.py", "r", encoding="utf-8", errors="ignore") as f:
    main_code = f.read()

cancel_handler = """
@router.message(F.text.in_(["/cancel", "إلغاء", "/start"]))
async def force_exit_state(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🔄 تم الخروج من وضع المساعد الذكي والعودة للوضع الطبيعي.", parse_mode="Markdown")
"""

# إدراج معالج الإلغاء إذا لم يكن موجوداً
if "force_exit_state" not in main_code and "FSMContext" in main_code:
    # وضعه قبل أي معالجات رسائل عامة
    main_code = main_code.replace("async def", cancel_handler + "\nasync def", 1)
    with open("main.py", "w", encoding="utf-8") as f:
        f.write(main_code)
    print("✅ تمت إضافة معالج تصفير الحالات (Reset Handler).")

