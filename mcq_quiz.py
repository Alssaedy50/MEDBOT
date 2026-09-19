import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

MEDICAL_QUIZ_BANK = [
    {
        "id": 1,
        "subject": "Medical Biochemistry & Genetics",
        "question": "A 2-day-old male neonate presents with severe lethargy, vomiting, and hyperammonemia. Laboratory evaluation reveals respiratory alkalosis, markedly elevated urinary orotic acid, and undetectable citrulline. Which enzyme deficiency is the most likely diagnosis?",
        "options": [
            "Carbamoyl Phosphate Synthetase I (CPS I)",
            "Ornithine Transcarbamylase (OTC)",
            "Argininosuccinate Synthetase",
            "Phenylalanine Hydroxylase"
        ],
        "correct_idx": 1,
        "pearl": (
            "<b>Clinical Pearl & Mechanism:</b>\n"
            "• <b>OTC Deficiency</b> هو أكثر اضطرابات دورة اليوريا شيوعاً (X-linked recessive).\n"
            "• <b>المسار (Pathway):</b> نقص OTC ➔ تراكم Carbamoyl phosphate ➔ تسربه للسيتوبلازم ➔ تحفيز تصنيع Pyrimidine ➔ <b>ارتفاع حمض الأوروتيك (Orotic acid)</b> مع <b>فرط أمونيا (Hyperammonemia)</b> حاد.\n"
            "• <b>Exam Trap:</b> يختلف عن Orotic Aciduria (نقص UMP Synthase) الذي يسبب Megaloblastic anemia مع مستوى أمونيا طبيعي تماماً."
        )
    },
    {
        "id": 2,
        "subject": "Cardiovascular Pharmacology",
        "question": "A 64-year-old male with Heart Failure with reduced Ejection Fraction (HFrEF) is started on Sacubitril/Valsartan (ARNI). What is the primary therapeutic mechanism of Sacubitril?",
        "options": [
            "Inhibition of Neprilysin, preventing degradation of Natriuretic Peptides (ANP/BNP)",
            "Competitive blockade of Angiotensin II Type 1 (AT1) receptors",
            "Direct inhibition of Myocardial Na+/K+-ATPase pump",
            "Non-selective beta-adrenergic receptor blockade"
        ],
        "correct_idx": 0,
        "pearl": (
            "<b>Clinical Pearl & Pharmacology:</b>\n"
            "• <b>Sacubitril</b> يثبط إنزيم <b>Neprilysin</b> فيمنع تكسير الـ Natriuretic Peptides (ANP, BNP).\n"
            "• <b>النتيجة:</b> زيادة cGMP ➔ توسع وعائي، إدرار الصوديوم، وتثبيط الـ Ventricular remodeling.\n"
            "• <b>فخ سريري:</b> يتطلب فترة إيقاف (Washout period) مدتها 36 ساعة بعد أدوية ACE inhibitors لتفادي حدوث Angioedema خطيرة."
        )
    },
    {
        "id": 3,
        "subject": "Systemic Pathology",
        "question": "Which specific morphological pattern of tissue necrosis classically occurs in cerebral tissue following an acute ischemic stroke?",
        "options": [
            "Coagulative necrosis",
            "Liquefactive necrosis",
            "Caseous necrosis",
            "Fibrinoid necrosis"
        ],
        "correct_idx": 1,
        "pearl": (
            "<b>Clinical Pearl & Pathology:</b>\n"
            "• النسيج الدماغي (CNS) يستجيب للاحتشاء الإقفاري بـ <b>Liquefactive necrosis</b> بخلاف باقي الأعضاء الصلبة (Coagulative).\n"
            "• <b>الآلية:</b> نشاط الإنزيمات التحللية المفرزة من الـ <b>Microglia</b> والـ Lysosomes يؤدي لتميع النسيج وتكوين تجويف كيسي (Cystic cavity)."
        )
    }
]

def build_quiz_keyboard(q_idx: int) -> InlineKeyboardMarkup:
    q = MEDICAL_QUIZ_BANK[q_idx]
    letters = ["A", "B", "C", "D"]
    buttons = [
        [InlineKeyboardButton(text=f"{letters[i]}) {opt}", callback_data=f"mcq:ans:{q_idx}:{i}")]
        for i, opt in enumerate(q["options"])
    ]
    buttons.append([InlineKeyboardButton(text="❌ إنهاء الاختبار", callback_data="mcq:exit")])
    return InlineKeyboardMarkup(buttons)

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["quiz_idx"] = 0
    context.user_data["quiz_score"] = 0
    context.user_data["quiz_total"] = len(MEDICAL_QUIZ_BANK)
    await show_question(update, context, 0)

async def show_question(update: Update, context: ContextTypes.DEFAULT_TYPE, q_idx: int):
    q = MEDICAL_QUIZ_BANK[q_idx]
    total = len(MEDICAL_QUIZ_BANK)
    text = (
        f"🩺 <b>اختبار سريري تفاعلي (Medical MCQ Quiz)</b>\n"
        f"📚 <b>المادة:</b> <code>{html.escape(q['subject'])}</code>\n"
        f"📊 <b>السؤال:</b> {q_idx + 1} من {total}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"❓ <b>{html.escape(q['question'])}</b>"
    )
    kb = build_quiz_keyboard(q_idx)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def quiz_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]

    if action == "exit":
        context.user_data.pop("quiz_idx", None)
        context.user_data.pop("quiz_score", None)
        await query.edit_message_text("🚪 تم إنهاء الاختبار. للبدء مجدداً أرسل أمر /quiz.")
        return

    if action == "restart":
        context.user_data["quiz_idx"] = 0
        context.user_data["quiz_score"] = 0
        await show_question(update, context, 0)
        return

    if action == "ans":
        q_idx = int(parts[2])
        chosen_idx = int(parts[3])
        q = MEDICAL_QUIZ_BANK[q_idx]
        correct_idx = q["correct_idx"]
        is_correct = (chosen_idx == correct_idx)

        if is_correct:
            context.user_data["quiz_score"] = context.user_data.get("quiz_score", 0) + 1
            status_header = "✅ <b>إجابة صحيحة! أحسنت دكتور 👏</b>"
        else:
            status_header = "❌ <b>إجابة غير صحيحة!</b>"

        feedback = (
            f"{status_header}\n\n"
            f"🎯 <b>الإجابة الصحيحة:</b> <code>{html.escape(q['options'][correct_idx])}</code>\n\n"
            f"{q['pearl']}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        next_idx = q_idx + 1
        btns = []
        if next_idx < len(MEDICAL_QUIZ_BANK):
            btns.append([InlineKeyboardButton(text="السؤال التالي ⬅️", callback_data=f"mcq:next:{next_idx}")])
        else:
            btns.append([InlineKeyboardButton(text="🏁 عرض النتيجة والتقرير", callback_data="mcq:finish")])
        await query.edit_message_text(feedback, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")
        return

    if action == "next":
        next_idx = int(parts[2])
        context.user_data["quiz_idx"] = next_idx
        await show_question(update, context, next_idx)
        return

    if action == "finish":
        score = context.user_data.get("quiz_score", 0)
        total = context.user_data.get("quiz_total", len(MEDICAL_QUIZ_BANK))
        pct = (score / total) * 100 if total > 0 else 0
        assessment = "أداء سريري ممتاز جداً 🩺🔥" if pct >= 75 else "مستوى جيد، يحتاج مراجعة للتفاصيل الدقيقة 📖"
        summary = (
            f"🏁 <b>التقرير النهائي للاختبار السريري</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>الطالب:</b> {html.escape(query.from_user.full_name or 'دكتور')}\n"
            f"🎯 <b>النتيجة:</b> {score} من {total}\n"
            f"📈 <b>النسبة:</b> {pct:.1f}%\n"
            f"💬 <b>التقييم:</b> {assessment}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(text="🔄 إعادة الاختبار", callback_data="mcq:restart")],
            [InlineKeyboardButton(text="🚪 خروج", callback_data="mcq:exit")]
        ])
        await query.edit_message_text(summary, reply_markup=kb, parse_mode="HTML")
        return

def register_quiz_handlers(app):
    app.add_handler(CommandHandler("quiz", start_quiz))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(كويز|اختبار|MCQ|mcq)$"), start_quiz))
    app.add_handler(CallbackQueryHandler(quiz_callback_handler, pattern=r"^mcq:"))
