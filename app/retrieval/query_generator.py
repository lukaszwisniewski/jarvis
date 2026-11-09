import json
import logging
import re

from app.config import settings
from app.llm.client import ask_llm

logger = logging.getLogger(__name__)


def _extract_keywords(query: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9_]+", query.lower())
    stopwords = set(settings.retrieval_query_stopwords)
    return [token for token in tokens if len(token) > 2 and token not in stopwords]


def _deterministic_queries(query: str) -> list[str]:
    lowered = query.lower()
    tokens = set(re.findall(r"[a-zA-Z0-9_]+", lowered))
    keywords = _extract_keywords(query)
    endpoint_keywords = set(settings.retrieval_endpoint_intent_keywords)
    architecture_keywords = set(settings.retrieval_architecture_intent_keywords)
    table_keywords = set(settings.retrieval_table_intent_keywords)
    table_entity_excludes = set(settings.retrieval_table_entity_exclude_keywords)

    is_endpoint_query = any(token in tokens for token in endpoint_keywords)
    is_table_query = any(token in tokens for token in table_keywords)
    is_architecture_query = any(token in tokens for token in architecture_keywords)

    entities = [token for token in keywords if token not in table_entity_excludes]
    primary_entity = entities[0] if entities else "domain"

    base = [" ".join(keywords[:4]).strip()] if keywords else []
    if is_endpoint_query:
        base.extend(
            [
                "APIRouter",
                "@router.get @router.post @router.put @router.delete",
                "@app.get @app.post @app.put @app.delete",
            ]
        )
    if is_architecture_query:
        base.extend(
            [
                "README architecture process flow",
            ]
        )
    if is_table_query:
        base.extend(
            [
                f"SELECT FROM JOIN {primary_entity}",
                f"queryExecute FROM {primary_entity}",
                f"DAO {primary_entity} table",
                f"INSERT INTO UPDATE {primary_entity}",
            ]
        )

    cleaned = [item for item in base if item]
    deduped: list[str] = []
    for item in cleaned:
        if item not in deduped:
            deduped.append(item)
    return deduped[:5]


def generate_queries(
    query: str,
) -> list[str]:

    prompt = f"""
Generate up to 5 short GitHub code search queries
for this engineering question.

Rules:
- focus on code terminology
- prefer nouns and technical terms
- avoid explanations
- each query should be short
- return JSON list only

Question:
{query}
"""

    response = ask_llm(query=prompt, fast=True)

    logger.info(
        "raw llm query response: %s",
        response,
    )

    cleaned = response.replace("```json", "").replace("```", "").strip()

    llm_queries: list[str] = []
    try:
        loaded = json.loads(cleaned)
        if isinstance(loaded, list):
            llm_queries = [str(item).strip() for item in loaded if str(item).strip()]
    except json.JSONDecodeError:
        logger.warning(
            "query generation JSON parse failed, using deterministic fallback"
        )

    fallback_queries = _deterministic_queries(query)
    endpoint_keywords = set(settings.retrieval_endpoint_intent_keywords)
    architecture_keywords = set(settings.retrieval_architecture_intent_keywords)
    table_keywords = set(settings.retrieval_table_intent_keywords)

    lowered_query = query.lower()
    tokens = set(re.findall(r"[a-zA-Z0-9_]+", lowered_query))
    endpoint_intent = any(token in tokens for token in endpoint_keywords)
    architecture_intent = any(token in tokens for token in architecture_keywords)
    table_intent = any(token in tokens for token in table_keywords)

    combined: list[str] = []
    ordered_candidates = (
        fallback_queries + llm_queries
        if endpoint_intent or architecture_intent or table_intent
        else llm_queries + fallback_queries
    )
    for candidate in ordered_candidates:
        if candidate and candidate not in combined:
            combined.append(candidate)

    if not combined:
        combined = fallback_queries or [query.strip()]

    return combined[:5]
