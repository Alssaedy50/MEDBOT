"""Centralised localization (i18n) for MEDBOT.

One table of canonical keys -> {"ar": ..., "en": ...}. Callers resolve text
with `t(key, lang)`; they never branch on the language themselves, so adding
a language is a one-place change (extend `_TRANSLATIONS` and
`database.SUPPORTED_LANGUAGES`) rather than a hunt for scattered `if lang`
conditions.

Arabic wording is written natively and professionally, not transliterated.
Unknown keys fall back to the key itself and never raise, so a missing
translation degrades to something visible instead of crashing a handler.

Terminology (kept consistent across the interface):
    المنصة / Platform            الإعدادات / Settings
    إدارة المنصة / Administration   المصادر / Resources
    القسم / Section              الفرع / Branch
    القسم الأب / Parent          الجذر / Root
    البحث / Search               المواضيع / Topics
    الإشعارات / Notifications    المساهمات / Contributions
    الصلاحيات / Permissions      نقل الملكية / Ownership Transfer
    سجل التدقيق / Audit Log
"""

DEFAULT_LANGUAGE = "ar"


_TRANSLATIONS = {
    # ---- Generic -------------------------------------------------------
    "home": {"ar": "🏠 الرئيسية", "en": "🏠 Home"},
    "back": {"ar": "⬅️ رجوع", "en": "⬅️ Back"},
    "cancel": {"ar": "❌ إلغاء", "en": "❌ Cancel"},
    "unauthorized": {"ar": "🔒 غير مصرح.", "en": "🔒 Unauthorized."},
    "done": {"ar": "✅ تم بنجاح.", "en": "✅ Done."},
    "not_found": {"ar": "⚠️ العنصر غير موجود.", "en": "⚠️ Not found."},
    "admin_only": {
        "ar": "🔒 هذه المنطقة مخصصة للمشرفين المعتمدين فقط.",
        "en": "🔒 This area is for authorized admins only.",
    },
    "cancel_operation": {"ar": "❌ تم إلغاء العملية الجارية.", "en": "❌ Operation cancelled."},
    "no_operation": {"ar": "ℹ️ لا توجد عملية قيد التنفيذ.", "en": "ℹ️ No operation in progress."},

    # ---- Main menu / welcome ------------------------------------------
    "menu_resources": {"ar": "📚 موارد المنصة", "en": "📚 Resources"},
    "menu_assistant": {"ar": "🤖 المساعد", "en": "🤖 Assistant"},
    "menu_contributions": {"ar": "📤 مساهمات الطلاب", "en": "📤 Student Contributions"},
    "menu_my_contributions": {"ar": "📄 مساهماتي", "en": "📄 My Contributions"},
    "menu_account": {"ar": "📊 حسابي", "en": "📊 My Account"},
    "menu_contact": {"ar": "📬 تواصل مع المنصة", "en": "📬 Contact"},
    "menu_about": {"ar": "ℹ️ عن المنصة", "en": "ℹ️ About"},
    "menu_admin": {"ar": "🛠 إدارة المنصة", "en": "🛠 Administration"},
    "menu_topics": {"ar": "🧭 المواضيع", "en": "🧭 Topics"},
    "menu_language": {"ar": "🌐 اللغة", "en": "🌐 Language"},

    "welcome": {
        "ar": "🩺 *{platform}*\n\nمرحباً بك دكتور {name}.\n\n"
              "منصة أكاديمية طبية تساعدك على الوصول إلى الموارد المسجلة "
              "والبحث فيها واستخدام المساعد الذكي ضمن محتوى المنصة.\n\n"
              "📊 *رصيد الذكاء الاصطناعي اليوم:* {remaining}/{limit}\n\n"
              "اختر الخدمة التي تريد استخدامها:",
        "en": "🩺 *{platform}*\n\nWelcome, Dr. {name}.\n\n"
              "A medical academic platform for reaching and searching the "
              "registered resources and using the assistant within the "
              "platform content.\n\n"
              "📊 *Daily AI balance:* {remaining}/{limit}\n\n"
              "Choose a service:",
    },
    "choose_service": {"ar": "اختر الخدمة التي تريد استخدامها:", "en": "Choose a service:"},

    # ---- Account / language -------------------------------------------
    "account_title": {"ar": "📊 *حسابي*", "en": "📊 *My Account*"},
    "account_language": {"ar": "🌐 اللغة", "en": "🌐 Language"},
    "language_title": {
        "ar": "🌐 *اختيار اللغة*\n\nاختر لغة واجهة المنصة.",
        "en": "🌐 *Language*\n\nChoose the platform interface language.",
    },
    "language_saved": {
        "ar": "✅ تم حفظ اللغة: 🇸🇦 العربية",
        "en": "✅ Language saved: 🇬🇧 English",
    },

    # ---- Search / topics ---------------------------------------------
    "topics_title": {
        "ar": "🧭 *مواضيع البحث*\n\nاختر موضوعاً للوصول السريع إلى الموارد المسجلة:",
        "en": "🧭 *Search Topics*\n\nChoose a topic for quick access to registered resources:",
    },
    "topics_empty": {
        "ar": "ℹ️ لا توجد مواضيع مسجلة حالياً.",
        "en": "ℹ️ No topics registered yet.",
    },
    "topic_open_title": {
        "ar": "🧭 *{name}*\n\nاختر القسم الذي تريد فتحه:",
        "en": "🧭 *{name}*\n\nChoose a section to open:",
    },
    "topic_no_sections": {
        "ar": "ℹ️ لا توجد أقسام مرتبطة بهذا الموضوع بعد.",
        "en": "ℹ️ No sections linked to this topic yet.",
    },
    "search_empty": {
        "ar": "المورد المطلوب غير مسجل حالياً في MEDBOT.",
        "en": "The requested resource is not currently registered in MEDBOT.",
    },

    # ---- Administration ----------------------------------------------
    "admin_panel_title": {"ar": "🛠 *إدارة المنصة*", "en": "🛠 *Administration*"},
    "admin_pending_contributions": {"ar": "📤 المساهمات المعلقة", "en": "📤 Pending contributions"},
    "admin_open_messages": {"ar": "📬 رسائل الطلاب غير المغلقة", "en": "📬 Open student messages"},
    "admin_choose_action": {"ar": "اختر الإجراء:", "en": "Choose an action:"},

    "admin_sections": {"ar": "🗂 إدارة الأقسام والفروع", "en": "🗂 Manage sections & branches"},
    "admin_contributions": {"ar": "📥 مراجعة المساهمات", "en": "📥 Review contributions"},
    "admin_messages": {"ar": "📬 رسائل الطلاب", "en": "📬 Student messages"},
    "admin_ai": {"ar": "🤖 سجل الذكاء الاصطناعي", "en": "🤖 AI Registry"},
    "admin_runtime": {"ar": "📊 حالة التشغيل", "en": "📊 Runtime"},
    "admin_admins": {"ar": "👥 إدارة المشرفين", "en": "👥 Admins"},
    "admin_audit": {"ar": "📜 سجل التدقيق", "en": "📜 Audit Log"},
    "admin_settings": {"ar": "⚙️ إعدادات المنصة", "en": "⚙️ Settings"},
    "admin_notifications": {"ar": "🔔 الإشعارات", "en": "🔔 Notifications"},
    "admin_topics": {"ar": "🧭 مواضيع البحث", "en": "🧭 Search Topics"},

    # ---- Ownership / permissions -------------------------------------
    "ownership_transfer": {"ar": "👑 نقل الملكية", "en": "👑 Ownership Transfer"},
    "ownership_title": {
        "ar": "👑 *نقل الملكية*\n\nاختر المشرف الذي تريد نقل ملكية المنصة إليه:",
        "en": "👑 *Ownership Transfer*\n\nChoose the admin to transfer ownership to:",
    },
    "ownership_none": {
        "ar": "ℹ️ لا يوجد مشرف آخر يمكن نقل الملكية إليه.",
        "en": "ℹ️ No other admin is available to receive ownership.",
    },
    "ownership_confirm": {
        "ar": "⚠️ سيصبح الحساب <code>{target}</code> المالك الجديد، "
              "وسيتحوّل دورك إلى مشرف.\n\nهل تريد المتابعة؟",
        "en": "⚠️ Account <code>{target}</code> will become the new owner and "
              "your role will change to admin.\n\nContinue?",
    },
    "permissions_title": {"ar": "🔐 *الصلاحيات*", "en": "🔐 *Permissions*"},
    "role_title": {"ar": "👑 *الدور*", "en": "👑 *Role*"},
    "permissions_grant": {"ar": "✅ منح", "en": "✅ Grant"},
    "permissions_revoke": {"ar": "⛔ إلغاء", "en": "⛔ Revoke"},

    # ---- Sections / branches -----------------------------------------
    "section_parent": {"ar": "القسم الأب", "en": "Parent"},
    "section_root": {"ar": "الجذر", "en": "Root"},
    "move_title": {
        "ar": "🚚 *نقل القسم*\n\n📁 القسم: <b>{name}</b>\n\n"
              "اختر القسم الأب الجديد (الجذر أو أي قسم):",
        "en": "🚚 *Move section*\n\n📁 Section: <b>{name}</b>\n\n"
              "Choose the new parent (Root or any section):",
    },
    "move_to_root": {"ar": "🏠 نقل إلى الجذر", "en": "🏠 Move to Root"},
    "move_here": {"ar": "✅ النقل إلى هنا", "en": "✅ Move here"},
    "move_self": {"ar": "لا يمكن نقل القسم إلى نفسه", "en": "A section cannot move into itself"},
    "move_descendant": {
        "ar": "لا يمكن نقل قسم إلى داخل أحد تفرعاته",
        "en": "A section cannot move into one of its descendants",
    },
    "move_ok": {"ar": "✅ تم نقل القسم بنجاح.", "en": "✅ Section moved successfully."},

    # ---- Settings ------------------------------------------------------
    "settings_title": {"ar": "⚙️ *إعدادات المنصة*", "en": "⚙️ *Platform Settings*"},
    "settings_prompt": {
        "ar": "أرسل النص الجديد في رسالة، أو اضغط ❌ إلغاء.",
        "en": "Send the new text in a message, or press ❌ Cancel.",
    },
    "settings_saved": {"ar": "✅ تم حفظ الإعداد.", "en": "✅ Setting saved."},
    "settings_invalid": {"ar": "⚠️ نص غير صالح.", "en": "⚠️ Invalid text."},

    # ---- Notifications -------------------------------------------------
    "notifications_title": {"ar": "🔔 *الإشعارات*", "en": "🔔 *Notifications*"},
    "notifications_prompt": {
        "ar": "أرسل نص الإشعار الذي تريد إرساله إلى جميع المستخدمين.\n\n"
              "لإلغاء العملية أرسل /cancel.",
        "en": "Send the notification body to broadcast to all users.\n\n"
              "Send /cancel to abort.",
    },
    "notifications_sending": {"ar": "⏳ جارٍ إرسال الإشعار...", "en": "⏳ Sending notification..."},
    "notifications_sent": {
        "ar": "✅ تم إرسال الإشعار إلى {delivered} من {recipients} مستخدم.",
        "en": "✅ Notification delivered to {delivered} of {recipients} users.",
    },
    "notifications_history": {"ar": "📜 سجل الإشعارات", "en": "📜 Notification history"},
    "notifications_new": {"ar": "✍️ إرسال إشعار", "en": "✍️ Send notification"},
    "notifications_empty": {"ar": "ℹ️ لم يتم إرسال أي إشعار بعد.", "en": "ℹ️ No notifications sent yet."},
}


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Resolve `key` for `lang`, formatting any `kwargs`.

    Never raises: an unknown key returns the key, and a bad format string
    returns the raw template, so a translation gap can never break a handler.
    """
    entry = _TRANSLATIONS.get(key)
    if entry is None:
        return key

    template = entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or key

    if not kwargs:
        return template

    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def available_languages() -> tuple:
    """Languages the i18n layer can render."""
    languages = set()
    for entry in _TRANSLATIONS.values():
        languages.update(entry.keys())
    return tuple(sorted(languages))
