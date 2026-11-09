from abc import ABC, abstractmethod

from app.retrieval.models import RetrievalResult


class RetrievalProvider(ABC):
    """
    Abstract base class for retrieval providers.
    All providers must implement the search() method returning normalized
    RetrievalResult objects.
    """

    @abstractmethod
    async def search(
        self,
        queries: list[str],
    ) -> list[RetrievalResult]:
        pass
