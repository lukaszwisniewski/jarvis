import httpx

from app.config import settings
from app.connectors.http_retry import DEFAULT_TIMEOUT, request_with_retry


class JiraConnector:
    JIRA_BASE_URL = settings.jira_base_url

    async def search(
        self,
        query: str,
    ) -> list[dict]:

        url = f"{self.JIRA_BASE_URL}/rest/api/3/search/jql"

        headers = {
            "Accept": "application/json",
        }

        safe_query = query.replace('"', '\\"').replace("\n", " ").strip()

        params = {
            "jql": f'text ~ "{safe_query}"',
            "maxResults": 5,
            "fields": "summary,description,status,assignee,comment",
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

            response = await request_with_retry(_fetch)
            response.raise_for_status()

            data = response.json()

        return data.get("issues", [])
