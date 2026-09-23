"""MCQ session logic for MEDBOT.

This module owns grading and session bookkeeping only. Question content always
comes from the `mcq_questions` table, which is populated exclusively by an
explicit admin registration. Nothing here generates or infers medical facts.
"""


def normalize_index(value) -> int:
    """Return a non-negative integer index, or -1 when unusable."""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return -1

    if index < 0:
        return -1

    return index


def is_correct(question: dict, chosen_index) -> bool:
    """Grade one answer against the stored correct index.

    A malformed question (missing/out-of-range correct index) grades as False
    rather than raising, so a corrupt record can never crash a quiz session.
    """
    if not isinstance(question, dict):
        return False

    correct = normalize_index(question.get("correct_index"))
    chosen = normalize_index(chosen_index)

    if correct < 0 or chosen < 0:
        return False

    options = question.get("options") or []
    if correct >= len(options):
        return False

    return chosen == correct


def fresh_session(questions: list) -> dict:
    """Start a quiz session for a list of questions."""
    questions = list(questions or [])

    return {
        "idx": 0,
        "score": 0,
        "answered": 0,
        "total": len(questions),
        "questions": questions,
        "done": len(questions) == 0,
    }


def current_question(session: dict):
    """Return the active question, or None when the session is finished."""
    if not isinstance(session, dict):
        return None

    questions = session.get("questions") or []
    idx = normalize_index(session.get("idx"))

    if idx < 0 or idx >= len(questions):
        return None

    return questions[idx]


def record_answer(session: dict, chosen_index) -> dict:
    """Grade the current question, updating the score.

    Returns a result dict describing the outcome. Calling this twice for the
    same question is prevented by the caller moving the index forward, and it
    never raises on malformed input.
    """
    question = current_question(session)

    if question is None:
        return {
            "ok": False,
            "correct": False,
            "correct_index": -1,
            "explanation": "",
            "score": session.get("score", 0),
            "answered": session.get("answered", 0),
            "total": session.get("total", 0),
        }

    correct = is_correct(question, chosen_index)

    if correct:
        session["score"] = session.get("score", 0) + 1

    session["answered"] = session.get("answered", 0) + 1

    correct_index = normalize_index(question.get("correct_index"))

    return {
        "ok": True,
        "correct": correct,
        "correct_index": correct_index,
        "correct_option": (
            question.get("options")[correct_index]
            if 0 <= correct_index < len(question.get("options") or [])
            else ""
        ),
        "explanation": question.get("explanation") or "",
        "score": session["score"],
        "answered": session["answered"],
        "total": session.get("total", 0),
    }


def advance(session: dict) -> bool:
    """Move to the next question. Returns False when the quiz is over."""
    if not isinstance(session, dict):
        return False

    questions = session.get("questions") or []
    next_idx = normalize_index(session.get("idx")) + 1

    if next_idx >= len(questions):
        session["done"] = True
        return False

    session["idx"] = next_idx
    return True


def score_summary(session: dict) -> dict:
    """Final report data for a finished session."""
    total = int(session.get("total", 0) or 0)
    score = int(session.get("score", 0) or 0)

    if total <= 0:
        percentage = 0.0
    else:
        percentage = (score / total) * 100.0

    if percentage >= 75:
        assessment = "أداء سريري ممتاز جداً 🩺🔥"
    elif percentage >= 50:
        assessment = "مستوى جيد، يحتاج مراجعة للتفاصيل الدقيقة 📖"
    else:
        assessment = "يحتاج مراجعة شاملة للمادة 📚"

    return {
        "score": score,
        "total": total,
        "percentage": round(percentage, 1),
        "assessment": assessment,
    }


def build_option_buttons(question: dict, question_key) -> list:
    """Build (label, callback_data) pairs for the answer options."""
    options = (question or {}).get("options") or []
    letters = "ABCDEFGH"

    buttons = []

    for index, option in enumerate(options):
        letter = letters[index] if index < len(letters) else str(index + 1)
        buttons.append(
            (
                f"{letter}) {str(option)[:40]}",
                f"mcq_answer:{question_key}:{index}",
            )
        )

    return buttons
