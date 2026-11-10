import logging

import httpx

from app.config import settings
from app.connectors.http_retry import DEFAULT_TIMEOUT, request_with_retry

logger = logging.getLogger(__name__)


class ConfluenceConnector:
    CONFLUENCE_BASE_URL = settings.confluence_base_url.rstrip("/")

    async def search(
        self,
        query: str,
    ) -> list[dict]:

        if not self.CONFLUENCE_BASE_URL:
            return []

        url = f"{self.CONFLUENCE_BASE_URL}/wiki/rest/api/search"

        headers = {
            "Accept": "application/json",
        }

        safe_query = query.replace('"', '\\"').replace("\n", " ").strip()

        params = {
            "cql": f'text ~ "{safe_query}" AND type = "page"',
            "limit": 5,
            "expand": "content.space,content.version",
        }

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:

            async def _fetch() -> httpx.Response:
                return await client.get(
                    url,
                    headers=headers,
                    auth=(
                        settings.jira_email,
                        settings.jira_token,
                    ),
                    params=params,
                )

            response = await request_with_retry(_fetch, source="confluence")
            response.raise_for_status()

            data = response.json()

        results = data.get("results", [])
        logger.debug("confluence query '%s' returned %s results", query, len(results))
        return results

    async def get_page_content(self, page_id: str) -> str:
        """Fetch full Confluence page body in storage format."""
        if not self.CONFLUENCE_BASE_URL or not page_id:
            return ""

        url = f"{self.CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}"
        headers = {"Accept": "application/json"}
        params = {"expand": "body.storage"}

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:

            async def _fetch() -> httpx.Response:
                return await client.get(
                    url,
                    headers=headers,
                    auth=(settings.jira_email, settings.jira_token),
                    params=params,
                )

            response = await request_with_retry(_fetch, source="confluence")
            response.raise_for_status()
            data = response.json()

        return data.get("body", {}).get("storage", {}).get("value", "")
