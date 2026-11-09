import asyncio

from app.config import settings
from app.connectors.github import GitHubConnector
from app.retrieval.models import RetrievalResult
from app.retrieval.providers.base import RetrievalProvider


def _build_profiles() -> dict[str, dict]:
    """Build runtime profiles.

    Default behavior uses global GitHub settings.
    Explicit scope selects ColdFusion or Python profile settings, each with
    fallback to global values if profile-specific settings are empty.
    """
    default_profile = {
        "repos": settings.github_repos,
        "allowed_extensions": settings.github_allowed_extensions,
        "extension_scores": settings.github_extension_scores,
    }

    return {
        "default": default_profile,
        "coldfusion": {
            "repos": settings.github_cf_repos or settings.github_repos,
            "allowed_extensions": (
                settings.github_cf_allowed_extensions
                or settings.github_allowed_extensions
            ),
            "extension_scores": (
                settings.github_cf_extension_scores or settings.github_extension_scores
            ),
        },
        "python": {
            "repos": settings.github_py_repos or settings.github_repos,
            "allowed_extensions": (
                settings.github_py_allowed_extensions
                or settings.github_allowed_extensions
            ),
            "extension_scores": (
                settings.github_py_extension_scores or settings.github_extension_scores
            ),
        },
    }


class GitHubProvider(RetrievalProvider):
    """
    Retrieval provider for GitHub using the GitHubConnector.
    Converts raw connector results into normalized RetrievalResult objects.

    When ``scope`` is supplied ("coldfusion" or "python"), the provider uses
    that profile directly. Otherwise it uses the global default profile.
    """

    def __init__(self, query: str = "", scope: str | None = None) -> None:
        self.connector = GitHubConnector()
        profiles = _build_profiles()
        profile_key = scope if scope in {"coldfusion", "python"} else "default"
        profile = profiles[profile_key]
        self._repos: list[str] = profile["repos"]
        self._allowed_extensions: list[str] = profile["allowed_extensions"]
        self._extension_scores: dict[str, int] = profile["extension_scores"]

    async def search(
        self,
        queries: list[str],
    ) -> list[RetrievalResult]:
        raw_results = await self.connector.search(
            queries=queries,
            repos=self._repos,
            allowed_extensions=self._allowed_extensions,
            extension_scores=self._extension_scores,
        )

        semaphore = asyncio.Semaphore(3)

        async def build_result(item: dict) -> RetrievalResult:
            async with semaphore:
                content = await self.connector.get_file_content(item["url"])
                snippet = await self.connector.build_snippet(
                    content=content,
                    queries=queries,
                    path=item["path"],
                )
                return RetrievalResult(
                    source="github",
                    title=item["path"],
                    snippet=snippet,
                    url=item["html_url"],
                    score=item[
                        "_local_score"
                    ],  # use computed local score, not GitHub API score
                )

        tasks = [build_result(item) for item in raw_results]
        return await asyncio.gather(*tasks)
