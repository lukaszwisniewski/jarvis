import asyncio
import logging

from app.connectors.jira import JiraConnector
from app.retrieval.models import RetrievalResult
from app.retrieval.providers.base import RetrievalProvider

logger = logging.getLogger(__name__)


class JiraProvider(RetrievalProvider):
    def __init__(self) -> None:
        self.connector = JiraConnector()

    async def search(self, queries: list[str]) -> list[RetrievalResult]:
        tasks = [self.connector.search(query=query) for query in queries]
        all_raw_groups = await asyncio.gather(*tasks, return_exceptions=True)

        seen_keys: set[str] = set()
        results: list[RetrievalResult] = []

        for raw_issues in all_raw_groups:
            if isinstance(raw_issues, Exception):
                logger.warning("jira query failed: %s", raw_issues)
                continue
            for issue in raw_issues:
                key = issue.get("key", "")
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                fields = issue.get("fields", {})
                summary = fields.get("summary", "")
                description = self._extract_text(fields.get("description"))
                status = fields.get("status", {}).get("name", "")
                assignee = (fields.get("assignee") or {}).get("displayName", "")
                comments = self._extract_comments(fields.get("comment"))
                url = f"{self.connector.JIRA_BASE_URL}/browse/{key}" if key else None
                score = self._score_issue(summary, description, queries)

                snippet_parts = [
                    f"Key: {key}",
                    f"Status: {status}",
                    f"Assignee: {assignee}",
                    f"Summary: {summary}",
                    f"Description: {description[:800]}",
                ]
                if comments:
                    snippet_parts.append(f"Comments: {comments[:400]}")

                results.append(
                    RetrievalResult(
                        source="jira",
                        title=summary or key,
                        snippet="\n".join(snippet_parts),
                        url=url,
                        score=score,
                    )
                )

        return results

    def _score_issue(self, summary: str, description: str, queries: list[str]) -> float:
        keywords: set[str] = set()
        for query in queries:
            for word in query.lower().split():
                if len(word) > 2:
                    keywords.add(word)
        summary_lower = summary.lower()
        description_lower = description.lower()
        score = 0.0
        for kw in keywords:
            if kw in summary_lower:
                score += 3.0
            elif kw in description_lower:
                score += 1.0
        return score

    def _extract_comments(self, comment_field: dict | None) -> str:
        if not comment_field:
            return ""
        comments = comment_field.get("comments", [])
        parts = []
        for c in comments[-3:]:  # last 3 comments
            body = self._extract_text(c.get("body"))
            if body:
                author = (c.get("author") or {}).get("displayName", "")
                parts.append(f"{author}: {body[:200]}")
        return " | ".join(parts)

    def _extract_text(self, node: dict | list | str | None) -> str:
        if node is None:
            return ""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return " ".join(self._extract_text(item) for item in node)
        if isinstance(node, dict):
            text = ""
            if "text" in node:
                text += node["text"] + " "
            if "content" in node:
                text += self._extract_text(node["content"])
            return text.strip()
        return ""
