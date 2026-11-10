import asyncio
import logging
import re

from app.connectors.confluence import ConfluenceConnector
from app.observability.metrics import SOURCE_QUERY_ERRORS
from app.retrieval.models import RetrievalResult
from app.retrieval.providers.base import RetrievalProvider
from app.retrieval.snippets import extract_density_snippet, extract_keywords

logger = logging.getLogger(__name__)


class ConfluenceProvider(RetrievalProvider):
    def __init__(self) -> None:
        self.connector = ConfluenceConnector()

    async def search(self, queries: list[str]) -> list[RetrievalResult]:
        tasks = [self.connector.search(query=query) for query in queries]
        all_raw_groups = await asyncio.gather(*tasks, return_exceptions=True)

        seen_urls: set[str] = set()
        results: list[RetrievalResult] = []

        for raw_pages in all_raw_groups:
            if isinstance(raw_pages, Exception):
                SOURCE_QUERY_ERRORS.labels("confluence").inc()
                logger.warning("confluence query failed: %s", raw_pages)
                continue

            for page in raw_pages:
                content = page.get("content") or {}
                page_id = content.get("id")
                title = content.get("title") or page.get("title") or "Confluence page"

                links = content.get("_links") or {}
                webui = links.get("webui")
                base = links.get("base")
                if webui and base:
                    url = f"{base}{webui}"
                elif webui and self.connector.CONFLUENCE_BASE_URL:
                    url = f"{self.connector.CONFLUENCE_BASE_URL}{webui}"
                else:
                    url = None

                dedupe_key = url or f"page:{page_id or title}"
                if dedupe_key in seen_urls:
                    continue
                seen_urls.add(dedupe_key)

                excerpt = (page.get("excerpt") or "").replace("\n", " ").strip()
                page_body_html = ""
                if page_id:
                    try:
                        page_body_html = await self.connector.get_page_content(
                            str(page_id)
                        )
                    except Exception as exc:
                        logger.warning(
                            "confluence page fetch failed for page_id=%s: %s",
                            page_id,
                            exc,
                        )

                body_text = self._html_to_text(page_body_html)
                space = (content.get("space") or {}).get("name", "")
                version = (content.get("version") or {}).get("number")
                score = self._score_page(
                    title=title,
                    excerpt=excerpt,
                    body_text=body_text,
                    queries=queries,
                )

                snippet_parts = [f"Title: {title}"]
                if space:
                    snippet_parts.append(f"Space: {space}")
                if version is not None:
                    snippet_parts.append(f"Version: {version}")
                if excerpt:
                    snippet_parts.append(f"Excerpt: {excerpt[:800]}")
                if body_text:
                    snippet, _, _, _ = extract_density_snippet(
                        content=body_text,
                        queries=queries,
                        window=800,
                        fallback_length=1600,
                        max_occurrences_per_keyword=5,
                        prefix="... ",
                        suffix=" ...",
                    )
                    snippet_parts.append("Body: " + snippet[:1600])

                logger.debug(
                    "confluence page '%s' score=%s",
                    title,
                    score,
                )

                results.append(
                    RetrievalResult(
                        source="confluence",
                        title=title,
                        snippet="\n".join(snippet_parts),
                        url=url,
                        score=score,
                    )
                )

        return results

    def _score_page(
        self,
        title: str,
        excerpt: str,
        body_text: str,
        queries: list[str],
    ) -> float:
        keywords = extract_keywords(queries)

        title_lower = title.lower()
        excerpt_lower = excerpt.lower()
        body_lower = body_text.lower()
        score = 0.0
        for kw in keywords:
            if kw in title_lower:
                score += 2.5
            elif kw in excerpt_lower:
                score += 1.0
            elif kw in body_lower:
                score += 0.5
        return score

    def _html_to_text(self, html: str) -> str:
        if not html:
            return ""

        # Convert line-breaking tags before stripping remaining tags.
        text = re.sub(
            r"(?i)</?(p|br|li|tr|h[1-6]|div|section|table|td|th)[^>]*>", "\n", html
        )
        text = re.sub(r"<[^>]+>", " ", text)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
        )
        text = re.sub(r"\s+", " ", text)
        return text.strip()
