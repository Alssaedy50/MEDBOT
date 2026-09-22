import html
import logging
import re
from typing import List, Dict

import httpx

logger = logging.getLogger(__name__)

NCBI_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

SOURCE_TIMEOUT = httpx.Timeout(12.0, connect=5.0)


def _clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


async def search_pubmed(query: str, limit: int = 3) -> List[Dict]:
    query = (query or "").strip()

    if not query:
        return []

    try:
        async with httpx.AsyncClient(timeout=SOURCE_TIMEOUT) as client:
            # Convert natural-language questions into a focused
            # PubMed query. Remove common English question/grammar words
            # so queries such as "What is the cardiac cycle?" become
            # "cardiac cycle".
            normalized = re.sub(r"[^a-zA-Z0-9\\s-]", " ", query)
            normalized = re.sub(r"\\s+", " ", normalized).strip()

            stop_words = {
                "what", "is", "are", "was", "were", "the", "a", "an",
                "of", "to", "in", "on", "for", "and", "or", "how",
                "why", "when", "where", "which", "who", "does", "do",
                "can", "could", "would", "should", "explain", "define",
                "definition", "please", "tell", "me", "about"
            }

            terms = [
                part for part in normalized.split()
                if len(part) >= 3 and part.lower() not in stop_words
            ]

            if terms:
                focused_query = " AND ".join(f'"{term}"' for term in terms[:8])
            else:
                focused_query = normalized

            response = await client.get(
                NCBI_ESEARCH,
                params={
                    "db": "pubmed",
                    "term": focused_query,
                    "retmode": "json",
                    "retmax": max(limit * 3, 9),
                    "sort": "relevance",
                },
            )
            response.raise_for_status()

            data = response.json()
            ids = data.get("esearchresult", {}).get("idlist", [])

            if not ids:
                return []

            fetch = await client.get(
                NCBI_EFETCH,
                params={
                    "db": "pubmed",
                    "id": ",".join(ids),
                    "retmode": "xml",
                },
            )
            fetch.raise_for_status()

            xml = fetch.text

    except Exception as exc:
        logger.warning("PubMed retrieval failed: %s", exc)
        return []

    records = []

    for article in re.findall(
        r"<PubmedArticle>(.*?)</PubmedArticle>",
        xml,
        flags=re.DOTALL,
    ):
        pmid_match = re.search(
            r"<PMID[^>]*>(.*?)</PMID>",
            article,
            flags=re.DOTALL,
        )
        title_match = re.search(
            r"<ArticleTitle>(.*?)</ArticleTitle>",
            article,
            flags=re.DOTALL,
        )
        abstract_parts = re.findall(
            r"<AbstractText[^>]*>(.*?)</AbstractText>",
            article,
            flags=re.DOTALL,
        )

        if not pmid_match or not title_match:
            continue

        pmid = _clean_text(pmid_match.group(1))
        title = _clean_text(title_match.group(1))
        abstract = _clean_text(" ".join(abstract_parts))

        records.append(
            {
                "pmid": pmid,
                "title": title,
                "abstract": abstract[:2500],
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "source_type": "PubMed",
            }
        )

    # Rank records by medical concept relevance.
    # Give title matches much more weight than abstract-only matches.
    query_terms = [
        term.lower()
        for term in re.findall(r"[a-zA-Z0-9-]{3,}", query)
        if term.lower() not in stop_words
    ]

    def relevance_score(record):
        title = record.get("title", "").lower()
        abstract = record.get("abstract", "").lower()

        score = 0

        for term in query_terms:
            if term in title:
                score += 5
            elif term in abstract:
                score += 1

        # Strong bonus when the important multi-word concept appears
        # together in the title.
        if len(query_terms) >= 2:
            phrase = " ".join(query_terms)
            if phrase in title:
                score += 15

        return score

    records.sort(key=relevance_score, reverse=True)

    # Do not return records that have no meaningful overlap with
    # the requested medical concepts.
    records = [
        record for record in records
        if relevance_score(record) > 0
    ]

    return records[:limit]


def build_source_context(sources: List[Dict]) -> str:
    if not sources:
        return (
            "لم يتم العثور على مصدر PubMed متاح للتحقق من هذه الإجابة. "
            "لا تدّعِ أن الإجابة تم التحقق منها من مصدر."
        )

    blocks = []

    for index, source in enumerate(sources, 1):
        blocks.append(
            f"[SOURCE {index}]\n"
            f"Title: {source['title']}\n"
            f"PMID: {source['pmid']}\n"
            f"Abstract: {source['abstract']}\n"
            f"URL: {source['url']}"
        )

    return "\n\n".join(blocks)
