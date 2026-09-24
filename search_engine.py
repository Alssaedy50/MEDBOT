"""MEDBOT deterministic, intent-aware resource search.

The database is the single source of truth: this module only ever ranks
rows that are actually registered (folders, subjects/blocks, resource
titles). It never fabricates a resource, folder, or link.

Matching is semantic-ish rather than exact-string:

    query -> normalize -> tokenize -> expand abbreviations/synonyms into
    canonical concepts -> score each registered record by (a) direct text
    match, (b) metadata match and (c) concept overlap.

So "CBC" reaches "Complete Blood Count", "Blood Count", "Hematology" and
"Blood Tests" without any of those titles containing the literal token,
while an unrelated query still returns nothing.
"""

import re
import unicodedata

import aiosqlite

import database

DB_NAME = "medbot_v2.sqlite3"


def _db_path() -> str:
    """Effective DB path, identical to the one ``database.py`` opens.

    Prefers an explicit ``search_engine.DB_NAME`` override (tests), otherwise
    defers to ``database.resolve_db_path()`` so both modules cannot drift.
    """
    if DB_NAME != database.DEFAULT_DB_NAME:
        return database.ensure_db_dir(DB_NAME)
    return database.ensure_db_dir()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Intent model: canonical concepts and their surface forms
# ---------------------------------------------------------------------------
#
# Each entry maps a canonical concept to the surface forms (English and
# Arabic) that should be treated as the same intent. Surface forms are
# normalized at import time and indexed both as whole phrases and as
# individual tokens, so "complete blood count", "CBC", "blood count" and
# "تحليل الدم" all resolve to the same concept.

_CONCEPT_SURFACES = {
    "cbc": [
        "cbc", "complete blood count", "blood count", "full blood count",
        "hemogram", "hematology", "haematology", "hematology blood test",
        "blood test", "blood tests", "blood work",
        "تحليل الدم", "تحاليل الدم", "صورة الدم", "فحص الدم", "امراض الدم",
    ],
    "hemoglobin": [
        "hb", "hgb", "hemoglobin", "haemoglobin", "هيموغلوبين", "خضاب",
    ],
    "esr": [
        "esr", "erythrocyte sedimentation rate", "sedimentation rate",
        "سرعة ترسيب الدم", "ترسيب الدم",
    ],
    "crp": [
        "crp", "c-reactive protein", "c reactive protein",
        "بروتين سي التفاعلي",
    ],
    "electrolytes": [
        "electrolytes", "electrolyte", "sodium", "potassium",
        "املاح الدم", "الكهارل", "شوارد الدم",
    ],
    "renal": [
        "renal", "kidney", "kidneys", "renal function", "creatinine",
        "urea", "bun", "gfr", "الكلى", "كلى", "وظائف الكلى", "كرياتينين",
    ],
    "liver": [
        "liver", "hepatic", "liver function", "lft", "lfts", "alt", "ast",
        "bilirubin", "الكبد", "وظائف الكبد", "بيليروبين",
    ],
    "lipid": [
        "lipid", "lipids", "lipid profile", "cholesterol", "ldl", "hdl",
        "triglycerides", "دهون", "الكوليسترول", "دهنيات الدم",
    ],
    "glucose": [
        "glucose", "blood sugar", "fbg", "hba1c", "سكر", "سكري",
        "سكر الدم", "الجلوكوز",
    ],
    "thyroid": [
        "thyroid", "tsh", "t3", "t4", "thyroid function", "الغده الدرقيه",
        "درقيه",
    ],
    "urinalysis": [
        "urinalysis", "urine analysis", "urine test", "تحليل البول", "بول",
    ],
    "anatomy": [
        "anatomy", "تشريح", "علم التشريح",
    ],
    "physiology": [
        "physiology", "فسيولوجيا", "علم وظائف الاعضاء", "وظائف الاعضاء",
    ],
    "pathology": [
        "pathology", "علم الامراض", "باثولوجي",
    ],
    "pharmacology": [
        "pharmacology", "pharma", "drugs", "drug", "فارماكولوجي",
        "علم الادويه", "الادويه",
    ],
    "microbiology": [
        "microbiology", "micro", "بكتيريا", "ميكروبيولوجي",
        "علم الاحياء الدقيقه",
    ],
    "biochemistry": [
        "biochemistry", "biochem", "الكيمياء الحيويه", "كيمياء حيويه",
    ],
    "immunology": [
        "immunology", "immune", "immuno", "مناعه", "علم المناعه",
    ],
    "mcq": [
        "mcq", "mcqs", "multiple choice", "questions", "question",
        "اسئله", "امتحان", "امتحانات", "كويز",
    ],
    "summary": [
        "summary", "summaries", "note", "notes", "ملخص", "ملخصات",
    ],
    "lecture": [
        "lecture", "lectures", "محاضره", "محاضرات", "شرح",
    ],
    "book": [
        "book", "books", "textbook", "كتاب", "كتب", "مرجع",
    ],
    "video": [
        "video", "videos", "فيديو", "مقاطع",
    ],
    "audio": [
        "audio", "record", "recording", "صوتيات", "تسجيل",
    ],
    "block": [
        "block", "blocks", "بلوك", "بلوكات", "موديول", "module",
    ],
    "subject": [
        "subject", "course", "ماده", "مواد", "مقرر",
    ],
}

# Query words that carry no topical intent.
_STOPWORDS = {
    "where", "what", "which", "find", "show", "give", "get", "need",
    "want", "the", "a", "an", "of", "to", "in", "on", "for", "and",
    "or", "is", "are", "do", "does", "can", "i", "me", "my", "please",
    "about", "content", "contents", "material", "materials",
    "اين", "وين", "ما", "ماذا", "هل", "عن", "في", "على", "من", "الى",
    "اريد", "ابحث", "اعطني", "هات", "محتوى", "محتوي", "مواد",
    "عندي", "عند", "كيف", "اماكن", "مكان",
}

# phrase/token -> ordered canonical concepts
_SURFACE_INDEX: dict[str, tuple] = {}
_TOKEN_INDEX: dict[str, tuple] = {}


def _build_indexes() -> None:
    phrase_map: dict[str, list] = {}
    token_map: dict[str, list] = {}

    for concept, surfaces in _CONCEPT_SURFACES.items():
        for surface in surfaces:
            norm = normalize_text(surface)
            if not norm:
                continue

            phrase_map.setdefault(norm, []).append(concept)

            for token in re.findall(r"[a-z0-9\u0600-\u06FF]+", norm):
                if token in _STOPWORDS or len(token) < 2:
                    continue
                token_map.setdefault(token, []).append(concept)

    for mapping, store in (
        (phrase_map, _SURFACE_INDEX),
        (token_map, _TOKEN_INDEX),
    ):
        for key, values in mapping.items():
            # Preserve a deterministic order, drop duplicates.
            store[key] = tuple(dict.fromkeys(values))


_build_indexes()


def _lookup_concepts(text: str) -> set:
    """Return canonical concepts implied by a piece of text."""
    norm = normalize_text(text)

    if not norm:
        return set()

    concepts = set()

    direct = _SURFACE_INDEX.get(norm)
    if direct:
        concepts.update(direct)

    for token in re.findall(r"[a-z0-9\u0600-\u06FF]+", norm):
        if token in _STOPWORDS or len(token) < 2:
            continue

        # Only whole-token matches are indexed, which avoids substring
        # false positives (e.g. "k" inside "kidney").
        token_concepts = _TOKEN_INDEX.get(token)
        if token_concepts:
            concepts.update(token_concepts)

    return concepts


def _query_terms(query_norm: str) -> list:
    return [
        token
        for token in re.findall(r"[a-z0-9\u0600-\u06FF]+", query_norm)
        if len(token) >= 2 and token not in _STOPWORDS
    ]


# ---------------------------------------------------------------------------
# Record scoring
# ---------------------------------------------------------------------------

# Rank buckets, lower is better.
_RANK_EXACT = 0
_RANK_PREFIX = 1
_RANK_TEXT = 2
_RANK_METADATA = 3
_RANK_CONCEPT = 4

_WORD_RE = re.compile(r"[a-z0-9\u0600-\u06FF]+")


def _score_record(
    query_norm: str,
    query_terms: list,
    query_concepts: set,
    primary_text: str,
    secondary_text: str,
    kind_tokens: set,
):
    """Return a rank for one record, or None when it is unrelated.

    ``primary_text`` is the searchable name/title; ``secondary_text`` holds
    description/keywords/ancestor-path text; ``kind_tokens`` are the record's
    own type/node keywords (e.g. summaries, mcq, video).
    """
    primary_norm = normalize_text(primary_text)
    secondary_norm = normalize_text(secondary_text)

    # 1. Direct textual match on the primary name/title.
    if primary_norm and primary_norm == query_norm:
        return _RANK_EXACT

    if primary_norm and query_norm and primary_norm.startswith(query_norm):
        return _RANK_PREFIX

    if primary_norm and query_norm and query_norm in primary_norm:
        return _RANK_TEXT

    # 2. All meaningful query tokens present in the primary title.
    if query_terms and all(term in primary_norm for term in query_terms):
        return _RANK_PREFIX

    # 3. Metadata: description/keywords/path.
    if query_norm and query_norm in secondary_norm:
        return _RANK_METADATA

    if query_terms and any(term in secondary_norm for term in query_terms):
        return _RANK_METADATA

    # 4. Intent/concept overlap (abbreviations & synonyms).
    record_text = f"{primary_norm} {secondary_norm}"

    if query_concepts and query_concepts & _lookup_concepts(record_text):
        return _RANK_CONCEPT

    if query_concepts and query_concepts & kind_tokens:
        return _RANK_CONCEPT

    # 5. Partial-token overlap: at least one reasonably specific token
    #    appears as a whole word in the record text. Keeps recall sane
    #    without matching unrelated rows.
    if query_terms:
        record_words = set(_WORD_RE.findall(record_text))
        if any(term in record_words for term in query_terms):
            return _RANK_CONCEPT

    return None


async def _fetch_records(db: aiosqlite.Connection):
    async with db.execute(
        "SELECT id, parent_id, name, node_type, description, keywords "
        "FROM folders ORDER BY id ASC"
    ) as cur:
        folders = await cur.fetchall()

    async with db.execute(
        "SELECT id, folder_id, title, file_type, description, keywords "
        "FROM content ORDER BY id DESC"
    ) as cur:
        contents = await cur.fetchall()

    return folders, contents


async def search_library(
    keyword: str,
    limit: int = 15,
) -> list:
    """Intent-aware search over registered folders and resources.

    Search scope:
      1. Folder/subject/block names
      2. Resource titles
      3. Registered descriptions/keywords
      4. Abbreviation/synonym concepts (CBC -> Complete Blood Count)

    Ranking: exact -> prefix -> text -> metadata -> concept.

    Result types: FOLDER, EMPTY_FOLDER, CONTENT.
    """

    query = normalize_text(keyword)

    if not query:
        return []

    query_terms = _query_terms(query)
    query_concepts = _lookup_concepts(keyword)

    if not query_terms and not query_concepts:
        return []

    limit = max(1, min(int(limit), 50))

    db = await aiosqlite.connect(_db_path())

    try:
        folders, contents = await _fetch_records(db)

        # Folder content counts, computed once.
        content_counts = {}
        for row in contents:
            content_counts[row[1]] = content_counts.get(row[1], 0) + 1

        # Ancestor path text per folder, resolved once (no N+1 queries).
        folder_by_id = {row[0]: row for row in folders}
        path_cache = {}

        def folder_path(folder_id: int) -> str:
            if folder_id in path_cache:
                return path_cache[folder_id]

            chain = []
            visited = set()
            current = folder_id

            while current and current not in visited:
                visited.add(current)
                row = folder_by_id.get(current)
                if not row:
                    break
                chain.append(row[2])
                current = row[1]

            chain.append("الرئيسية 🏠")
            chain.reverse()
            path_cache[folder_id] = " ⬅️ ".join(chain)
            return path_cache[folder_id]

        results = []

        # ---- Folders -----------------------------------------------------
        for folder_id, parent_id, name, node_type, description, keywords in folders:
            ancestor_text = folder_path(folder_id)
            secondary = " ".join(
                part for part in (description, keywords, ancestor_text) if part
            )
            kind_tokens = {node_type} if node_type else set()

            rank = _score_record(
                query,
                query_terms,
                query_concepts,
                name,
                secondary,
                kind_tokens,
            )

            if rank is None:
                continue

            content_count = content_counts.get(folder_id, 0)

            results.append({
                "type": "FOLDER",
                "result_type": "FOLDER" if content_count else "EMPTY_FOLDER",
                "id": folder_id,
                "folder_id": folder_id,
                "title": name,
                "name": name,
                "path": ancestor_text,
                "content_count": content_count,
                "node_type": node_type,
                "rank": rank,
                "match_field": "folder_name",
            })

        # ---- Content -----------------------------------------------------
        for content_id, folder_id, title, file_type, description, keywords in contents:
            folder_row = folder_by_id.get(folder_id)
            folder_name = folder_row[2] if folder_row else ""

            secondary = " ".join(
                part
                for part in (
                    description,
                    keywords,
                    folder_name,
                    folder_path(folder_id),
                )
                if part
            )
            kind_tokens = {file_type} if file_type else set()

            rank = _score_record(
                query,
                query_terms,
                query_concepts,
                title,
                secondary,
                kind_tokens,
            )

            if rank is None:
                continue

            results.append({
                "type": "CONTENT",
                "result_type": "CONTENT",
                "id": content_id,
                "content_id": content_id,
                "folder_id": folder_id,
                "title": title,
                "name": title,
                "path": folder_path(folder_id),
                "content_count": content_counts.get(folder_id, 0),
                "file_type": file_type,
                "rank": rank,
                "match_field": "content_title",
            })

        # Deterministic ordering: rank -> folders before content -> title -> id.
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


async def search_library_summary(keyword: str, limit: int = 15) -> dict:
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
