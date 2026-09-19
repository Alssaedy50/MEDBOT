import re
import unicodedata
from typing import Any

import aiosqlite

DB_NAME = "medbot_v2.sqlite3"


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
) -> list[dict[str, Any]]:
    """
    Search Engine v1.

    Search scope:
      1. Folder names
      2. Content titles

    Ranking:
      0 = exact
      1 = prefix
      2 = partial

    Result types:
      FOLDER
      CONTENT
      EMPTY_FOLDER

    The database remains the source of truth.
    """

    query = normalize_text(keyword)

    if not query:
        return []

    limit = max(1, min(int(limit), 50))
    escaped = _like_pattern(query)

    db = await aiosqlite.connect(DB_NAME)

    try:
        # Fetch all candidate folders/content records. The dataset is
        # intentionally small at this stage, and normalization is performed
        # in Python so Arabic matching is reliable without changing stored data.
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

            if normalized_name == query:
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
            })

        # Content results.
        for content_id, folder_id, title, file_type, folder_content_count in contents:
            normalized_title = normalize_text(title)

            if normalized_title == query:
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
                "rank": rank,
                "match_field": "content_title",
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


async def search_library_summary(keyword: str, limit: int = 15) -> dict[str, Any]:
    """
    Stable API for Telegram/AI layers.
    """
    results = await search_library(keyword, limit)

    return {
        "query": keyword,
        "normalized_query": normalize_text(keyword),
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
