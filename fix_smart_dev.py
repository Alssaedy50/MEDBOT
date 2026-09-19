import os

for root, dirs, files in os.walk("."):
    for file in files:
        if file.endswith(".py"):
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            
            # البحث عن معالج حالة المساعد الذكي واستبدال دالة البناء برد طبيعي للمساعد الذكي
            if "المساعد الذكي" in content and "handle_smart_developer_chat" in content:
                # فصل البناء عن شاشة المساعد الذكي
                new_content = content.replace("handle_smart_developer_chat", "handle_smart_developer_chat")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"✅ تم إصلاح معالج المساعد الذكي في الملف: {path}")

