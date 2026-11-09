from pydantic import BaseModel


class RetrievalResult(BaseModel):
    """Normalized result returned by a retrieval provider.

    Attributes:
        source: Name of the provider that produced the result.
        title: Result title or heading.
        snippet: Short excerpt or summary text.
        url: Source URL, if available.
        score: Optional relevance score used by downstream ranking.
    """

    source: str
    title: str
    snippet: str
    url: str | None = None
    score: float | None = None
