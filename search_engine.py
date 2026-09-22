import json
import re
import unicodedata
from typing import Any

import aiosqlite

DB_NAME = "medbot_v2.sqlite3"


# ============================================================
# Advanced filters — media types & high-yield medical tags
# ============================================================

# Canonical media buckets exposed to the Telegram filter buttons.
SEARCH_TYPES = ("document", "photo", "audio", "video", "mcq")

TYPE_LABELS = {
    "document": "📄 مستند",
    "photo": "🖼 صورة",
    "audio": "🎧 صوت",
    "video": "🎥 فيديو",
    "mcq": "📝 أسئلة",
}

# High-yield tags recognised inside resource titles. They are matched from the
# stored title only; nothing is invented or inferred.
HIGH_YIELD_TAGS = (
    "#ExamTrap",
    "#ClinicalRelevance",
    "#HighYield",
    "#NBME",
    "#Practical",
)

_TAG_RE = re.compile(r"#([A-Za-z\u0600-\u06FF][A-Za-z0-9_\u0600-\u06FF]*)")

_TYPE_ALIASES = {
    "document": "document",
    "doc": "document",
    "docs": "document",
    "pdf": "document",
    "file": "document",
    "book": "document",
    "photo": "photo",
    "image": "photo",
    "img": "photo",
    "jpg": "photo",
    "jpeg": "photo",
    "png": "photo",
    "webp": "photo",
    "audio": "audio",
    "mp3": "audio",
    "m4a": "audio",
    "wav": "audio",
    "voice": "audio",
    "video": "video",
    "mp4": "video",
    "mkv": "video",
    "mov": "video",
    "mcq": "mcq",
    "quiz": "mcq",
}


def normalize_resource_type(file_type) -> str:
    """Map a stored file_type onto one of the canonical SEARCH_TYPES.

    Unknown types fall back to 'document' so filter buttons never drop a
    resource silently.
    """
    value = str(file_type or "").strip().lower()
    return _TYPE_ALIASES.get(value, "document")


def normalize_search_types(types) -> list:
    """Normalize a caller-supplied type filter into canonical buckets.

    An empty/invalid filter means 'no type restriction'.
    """
    if types is None:
        return []

    if isinstance(types, str):
        types = [types]

    normalized = []

    for item in types:
        bucket = normalize_resource_type(item)
        if bucket not in normalized:
            normalized.append(bucket)

    return normalized


def extract_tags(text) -> list:
    """Extract `#tag` tokens from a title, preserving the leading '#'."""
    if not text:
        return []

    seen = []
    for match in _TAG_RE.findall(str(text)):
        tag = "#" + match
        if tag not in seen:
            seen.append(tag)
    return seen


def _tag_matches(tags: list, wanted: str) -> bool:
    wanted = normalize_text(wanted).lstrip("#")
    return any(normalize_text(tag).lstrip("#") == wanted for tag in tags)



def normalize_text(value: str) -> str:
    """
    Normalize Arabic/English search text without destroying the original
    database values.

    Arabic:
      - remove tashkeel
      - normalize alef variants
      - normalize ya/alef maqsura
      - normalize ta marbuta
    English:
      - lowercase
      - normalize Unicode
      - collapse whitespace
    """
    if not value:
        return ""

    text = unicodedata.normalize("NFKC", str(value)).strip().lower()

    # Remove Arabic tashkeel.
    text = re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]", "", text)

    # Arabic normalization.
    text = text.translate(str.maketrans({
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ة": "ه",
    }))

    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text)

    return text


def _like_pattern(value: str) -> str:
    """
    Escape LIKE wildcards so user input is treated as text.
    """
    value = value.replace("\\", "\\\\")
    value = value.replace("%", "\\%")
    value = value.replace("_", "\\_")
    return value


async def _build_folder_path(db: aiosqlite.Connection, folder_id: int) -> str:
    """
    Build a human-readable folder path.

    Root is represented internally by NULL parent_id.
    Cycle protection is included so corrupted data cannot loop forever.
    """
    parts = []
    current = folder_id
    visited = set()

    while current is not None:
        if current in visited:
            parts.append("[CYCLE]")
            break

        visited.add(current)

        async with db.execute(
            "SELECT parent_id, name FROM folders WHERE id = ?",
            (current,),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            break

        parent_id, name = row
        parts.append(name)
        current = parent_id

    parts.reverse()
    return "الرئيسية 🏠" + ((" ⬅️ " + " ⬅️ ".join(parts)) if parts else "")


async def _folder_content_count(
    db: aiosqlite.Connection,
    folder_id: int,
) -> int:
    async with db.execute(
        "SELECT COUNT(*) FROM content WHERE folder_id = ?",
        (folder_id,),
    ) as cur:
        row = await cur.fetchone()

    return int(row[0] if row else 0)


async def search_library(
    keyword: str,
    limit: int = 15,
    types: list = None,
    tag: str = None,
) -> list[dict[str, Any]]:
    """
    Search Engine v2.

    Search scope:
      1. Folder names
      2. Content titles
      3. Registered MCQ question text (only when 'mcq' is in scope)

    Ranking:
      0 = exact
      1 = prefix
      2 = partial

    Result types:
      FOLDER
      CONTENT
      EMPTY_FOLDER
      MCQ

    Filters:
      types — restrict to one or more canonical media buckets
              (document/photo/audio/video/mcq). Empty means no restriction.
      tag   — restrict content to titles carrying a high-yield tag.

    The database remains the source of truth; nothing is fabricated.
    """

    query = normalize_text(keyword)
    wanted_types = normalize_search_types(types)
    has_type_filter = bool(wanted_types)

    # A tag-only search (no keyword) is allowed and useful.
    if not query and not tag:
        return []

    limit = max(1, min(int(limit), 50))

    # A tag filter narrows the search to tagged resources, so folders and
    # MCQs (which carry no resource tag) are excluded.
    tag_filtered = bool(tag)

    include_folders = (not has_type_filter) and not tag_filtered
    include_content = (not has_type_filter) or any(
        t in wanted_types for t in ("document", "photo", "audio", "video")
    )
    include_mcq = ((not has_type_filter) or ("mcq" in wanted_types)) and not tag_filtered

    db = await aiosqlite.connect(DB_NAME)

    try:
        folders = []
        contents = []

        if include_folders:
            async with db.execute(
                """
                SELECT
                    f.id,
                    f.parent_id,
                    f.name,
                    f.node_type,
                    COUNT(c.id) AS content_count
                FROM folders f
                LEFT JOIN content c ON c.folder_id = f.id
                GROUP BY f.id
                ORDER BY f.id ASC
                """
            ) as cur:
                folders = await cur.fetchall()

        if include_content:
            async with db.execute(
                """
                SELECT
                    c.id,
                    c.folder_id,
                    c.title,
                    c.file_type,
                    (
                        SELECT COUNT(*)
                        FROM content fc
                        WHERE fc.folder_id = c.folder_id
                    ) AS folder_content_count
                FROM content c
                ORDER BY c.id DESC
                """
            ) as cur:
                contents = await cur.fetchall()

        results: list[dict[str, Any]] = []

        # Folder results.
        for folder_id, parent_id, name, node_type, content_count in folders:
            normalized_name = normalize_text(name)

            if not query or normalized_name == query:
                rank = 0
            elif normalized_name.startswith(query):
                rank = 1
            elif query in normalized_name:
                rank = 2
            else:
                continue

            path = await _build_folder_path(db, folder_id)

            result_type = "FOLDER"
            if int(content_count) == 0:
                result_type = "EMPTY_FOLDER"

            results.append({
                "type": result_type,
                "result_type": result_type,
                "id": folder_id,
                "folder_id": folder_id,
                "title": name,
                "name": name,
                "path": path,
                "content_count": int(content_count),
                "node_type": node_type,
                "rank": rank,
                "match_field": "folder_name",
                "tags": [],
            })

        # Content results.
        for content_id, folder_id, title, file_type, folder_content_count in contents:
            bucket = normalize_resource_type(file_type)

            if has_type_filter and bucket not in wanted_types:
                continue

            tags = extract_tags(title)

            if tag and not _tag_matches(tags, tag):
                continue

            normalized_title = normalize_text(title)

            if not query:
                rank = 2
            elif normalized_title == query:
                rank = 0
            elif normalized_title.startswith(query):
                rank = 1
            elif query in normalized_title:
                rank = 2
            else:
                continue

            path = await _build_folder_path(db, folder_id)

            results.append({
                "type": "CONTENT",
                "result_type": "CONTENT",
                "id": content_id,
                "content_id": content_id,
                "folder_id": folder_id,
                "title": title,
                "name": title,
                "path": path,
                "content_count": int(folder_content_count),
                "file_type": file_type,
                "media_type": bucket,
                "tags": tags,
                "rank": rank,
                "match_field": "content_title",
            })

        # Registered MCQ results.
        if include_mcq:
            async with db.execute(
                """
                SELECT q.id, q.folder_id, q.question_text, q.options_json
                FROM mcq_questions q
                ORDER BY q.id ASC
                """
            ) as cur:
                mcq_rows = await cur.fetchall()

            for qid, q_folder_id, question_text, options_json in mcq_rows:
                normalized_question = normalize_text(question_text)

                if not query:
                    rank = 2
                elif normalized_question == query:
                    rank = 0
                elif normalized_question.startswith(query):
                    rank = 1
                elif query in normalized_question:
                    rank = 2
                else:
                    continue

                try:
                    option_count = len(json.loads(options_json))
                except Exception:
                    option_count = 0

                path = await _build_folder_path(db, q_folder_id)

                results.append({
                    "type": "MCQ",
                    "result_type": "MCQ",
                    "id": qid,
                    "mcq_id": qid,
                    "folder_id": q_folder_id,
                    "title": question_text,
                    "name": question_text,
                    "path": path,
                    "content_count": option_count,
                    "file_type": "mcq",
                    "media_type": "mcq",
                    "tags": extract_tags(question_text),
                    "rank": rank,
                    "match_field": "mcq_question",
                })

        # Deterministic ordering:
        # exact → prefix → partial → folders before content → title → id.
        results.sort(
            key=lambda item: (
                item["rank"],
                0 if item["result_type"] in ("FOLDER", "EMPTY_FOLDER") else 1,
                normalize_text(item["title"]),
                item["id"],
            )
        )

        return results[:limit]

    finally:
        await db.close()


async def search_library_summary(
    keyword: str,
    limit: int = 15,
    types: list = None,
    tag: str = None,
) -> dict[str, Any]:
    """
    Stable API for Telegram/AI layers.
    """
    results = await search_library(keyword, limit, types=types, tag=tag)

    return {
        "query": keyword,
        "normalized_query": normalize_text(keyword),
        "types": normalize_search_types(types),
        "tag": tag,
        "result_count": len(results),
        "results": results,
    }


if __name__ == "__main__":
    import asyncio
    import json
    import sys

    query = " ".join(sys.argv[1:]).strip()

    if not query:
        print("Usage: python search_engine.py <query>")
        raise SystemExit(2)

    output = asyncio.run(search_library_summary(query))
    print(json.dumps(output, ensure_ascii=False, indent=2))
