import os

files_to_check = ["ai_architect.py", "main.py"]

for fname in files_to_check:
    if os.path.exists(fname):
        with open(fname, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        
        # تصحيح الرد في حال كانت الرسالة مجرد ترحيب أو غير مفهومة للمهندس الذكي
        if "❌ تعذر البناء: ✅ تم فرض" in content or "تم فرض إعادة بناء" in content:
            content = content.replace(
                "❌ تعذر البناء: ✅ تم فرض إعادة بناء وتحديث كافة الأقسام والمواد (نظري وعملي) بنجاح تام!",
                "⚠️ لم يتم التعرف على مواد أو أقسام في رسالتك.\n💡 يرجى كتابة تعليمات واضحة للهيكلة، مثل:\n`أضف مادة علم الأدوية لسنة ثالثة نظري وعملي`"
            )
            with open(fname, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"✅ تم تصحيح الرد في: {fname}")

print("✅ تمت المعالجة بنجاح.")
