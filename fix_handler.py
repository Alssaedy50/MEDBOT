import os

with open("main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
skip = False
for line in lines:
    # إزالة أي توجيه خاطئ يجعل الرسائل العادية تنفذ البناء الإجباري
    if "force_build_catalog" in line and "message" in line:
        continue
    new_lines.append(line)

with open("main.py", "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("✅ تمت مراجعة معالج الرسائل وتصحيحه!")
