import os

for root, dirs, files in os.walk("."):
    for file in files:
        if file.endswith(".py"):
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            
            # البحث عن أي معالج نصي داخل حالة المساعد الذكي يستدعي دالة البناء واستبداله برد تفاعلي سليم
            if "print("Smart Dev Input Processed") # تم فصل البناء" in content and ("state" in content or "State" in content):
                # استبدال استدعاء البناء برد تفاعلي يخبر المستخدم بأنه تم استلام الرسالة بنجاح
                new_content = content.replace(
                    "await message.answer('🤖 **المساعد الذكي:** تم استلام تعليماتك بنجاح وجاري معالجتها برمجياً...'); return
# ", 
                    "await message.answer('🤖 **المساعد الذكي:** تم استلام تعليماتك بنجاح وجاري معالجتها برمجياً...'); return
# ('🤖 **المساعد الذكي:** تم استلام تعليماتك بنجاح وجاري معالجتها برمجياً...'); return\n# "
                )
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"✅ تم تصحيح معالج حالة المساعد الذكي في: {path}")

