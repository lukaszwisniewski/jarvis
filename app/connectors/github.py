import base64
import logging
import re

import httpx

from app.config import settings
from app.connectors.http_retry import DEFAULT_TIMEOUT, request_with_retry
from app.retrieval.snippets import extract_anchor_snippet, extract_density_snippet

logger = logging.getLogger(__name__)


class GitHubConnector:
    BASE_URL = "https://api.github.com"

    _ENDPOINT_DECORATOR_RE = re.compile(
        r"^\s*@(?:\w+\.)?(?:get|post|put|patch|delete|options|head)\("
    )

    def _expand_query_tokens(self, query: str) -> set[str]:
        """Return normalized query tokens for path-scoring matches.

        Includes a simple singular fallback so terms like "workflows"
        can match "workflow" paths.
        """
        raw_tokens = re.findall(r"[a-zA-Z0-9_]+", query.lower())
        tokens: set[str] = set()
        for token in raw_tokens:
            if len(token) <= 2:
                continue
            tokens.add(token)
            if token.endswith("s") and len(token) > 4:
                tokens.add(token[:-1])
        return tokens

    async def search(
        self,
        queries: list[str],
        repos: list[str],
        allowed_extensions: list[str] | None = None,
        extension_scores: dict[str, int] | None = None,
    ) -> list[dict]:

        repo_query = " ".join(f"repo:{repo}" for repo in repos)
        repo_query = repo_query.strip()

        url = f"{self.BASE_URL}/search/code"

        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
        }

        all_results = []

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for query in queries[:5]:
                try:
                    params = {
                        "q": " ".join(
                            part for part in [query.strip(), repo_query] if part
                        ).strip(),
                        "per_page": 15,
                    }

                    async def _fetch() -> httpx.Response:
                        return await client.get(
                            url,
                            headers=headers,
                            params=params,
                        )

                    response = await request_with_retry(_fetch, source="github")
                    response.raise_for_status()
                    data = response.json()
                    all_results.extend(data.get("items", []))
                    logger.debug(
                        "github query '%s' returned %s results",
                        query,
                        data.get("total_count", 0),
                    )
                    if data.get("total_count", 0) > 0:
                        first_paths = [item["path"] for item in data["items"][:3]]
                        logger.debug("top paths for '%s': %s", query, first_paths)
                except Exception as exc:
                    # The query text stays at DEBUG; the warning names the error.
                    logger.warning("github query failed: %s", type(exc).__name__)
                    logger.debug("github query '%s' failed", query, exc_info=True)

        _allowed_extensions = tuple(
            allowed_extensions
            if allowed_extensions is not None
            else settings.github_allowed_extensions
        )
        _extension_scores = (
            extension_scores
            if extension_scores is not None
            else settings.github_extension_scores
        )

        filtered_results = []
        unfiltered_results = []

        for item in all_results:
            path = item["path"].lower()

            score = 0
            has_allowed_extension = any(
                path.endswith(ext) for ext in _allowed_extensions
            )

            for ext, ext_score in _extension_scores.items():
                if path.endswith(ext):
                    score += ext_score

            for query in queries:
                for token in self._expand_query_tokens(query):
                    if token in path:
                        score += 5

            if "test" in path:
                score -= 5

            if "deprecated" in path:
                score -= 5

            item["_local_score"] = score
            unfiltered_results.append(item)

            if has_allowed_extension:
                filtered_results.append(item)

            logger.debug(
                "selected result: %s score=%s",
                item["path"],
                item["_local_score"],
            )

        selected_results = filtered_results
        if not selected_results:
            logger.warning(
                "no github results matched allowed extensions=%s; "
                "falling back to unfiltered results",
                _allowed_extensions,
            )
            selected_results = unfiltered_results

        selected_results.sort(
            key=lambda x: x["_local_score"],
            reverse=True,
        )

        deduped_results = {}

        for item in selected_results:
            deduped_results[item["html_url"]] = item

        return list(deduped_results.values())[:12]

    async def get_file_content(
        self,
        file_url: str,
    ) -> str:

        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
        }

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:

            async def _fetch() -> httpx.Response:
                return await client.get(
                    file_url,
                    headers=headers,
                )

            response = await request_with_retry(_fetch, source="github")
            response.raise_for_status()

            data = response.json()

        encoded_content = data.get("content", "")

        if not encoded_content:
            return ""

        decoded = base64.b64decode(
            encoded_content,
        ).decode("utf-8")

        return decoded

    async def build_snippet(
        self,
        content: str,
        queries: list[str],
        path: str = "",
        window: int = 500,
    ) -> str:
        """
        Extracts a snippet from content by finding the region with the
        highest density of query keywords, rather than the first occurrence
        of any single keyword.
        """
        query_text = " ".join(queries).lower()

        # Endpoint inventory questions benefit from explicit route decorators
        # and path declarations rather than generic keyword windows.
        if path.lower().endswith(".py") and any(
            marker in query_text for marker in ("endpoint", "route", "api", "groups")
        ):
            endpoint_lines = self._extract_endpoint_lines(content)
            if endpoint_lines:
                return endpoint_lines

        if self._is_definition_query(query_text):
            class_block = self._extract_class_block(content, queries)
            if class_block:
                logger.debug(
                    "definition class block snippet selected for path=%s",
                    path,
                )
                return class_block

            anchors = self._build_definition_anchors(queries)
            anchor_snippet = extract_anchor_snippet(
                content=content,
                anchors=anchors,
                window=max(window * 2, 900),
                max_length=1800,
                prefix="...\n",
                suffix="\n...",
            )
            if anchor_snippet:
                logger.debug(
                    "definition anchor snippet selected for path=%s",
                    path,
                )
                return anchor_snippet

        snippet, best_pos, best_count, keyword_count = extract_density_snippet(
            content=content,
            queries=queries,
            window=window,
            fallback_length=1000,
            max_occurrences_per_keyword=5,
            prefix="...\n",
            suffix="\n...",
        )

        if best_pos is not None:
            logger.info(
                "snippet density match at index %s covering %s/%s keywords",
                best_pos,
                best_count,
                keyword_count,
            )

        return snippet

    def _is_definition_query(self, query_text: str) -> bool:
        return any(
            marker in query_text
            for marker in (
                "class",
                "fields",
                "field",
                "dto",
                "request",
                "response",
                "method",
                "function",
                "attributes",
                "properties",
            )
        )

    def _build_definition_anchors(self, queries: list[str]) -> list[str]:
        anchors: list[str] = []
        for query in queries:
            tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query)
            for token in tokens:
                # CamelCase/TitleCase identifiers are strong class-like anchors.
                if any(ch.isupper() for ch in token[1:]) and len(token) > 3:
                    anchors.append(token)
                    anchors.append(f"class {token}")
                    anchors.append(f"struct {token}")
                    anchors.append(f"interface {token}")
        deduped: list[str] = []
        for anchor in anchors:
            if anchor not in deduped:
                deduped.append(anchor)
        return deduped[:24]

    def _extract_class_block(self, content: str, queries: list[str]) -> str | None:
        """Extract class definition block for class/DTO questions.

        Captures from the first matched `class <Identifier>` line until the next
        class declaration at same or lower indentation, or hard limits.
        """
        identifiers = self._definition_identifiers(queries)
        if not identifiers:
            return None

        lines = content.splitlines()
        class_pattern = re.compile(
            r"^(?P<indent>\s*)class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        )

        start_idx = -1
        start_indent = 0
        for i, line in enumerate(lines):
            m = class_pattern.match(line)
            if not m:
                continue
            name = m.group("name")
            if name in identifiers:
                start_idx = i
                start_indent = len(m.group("indent"))
                break

        if start_idx == -1:
            return None

        end_idx = min(len(lines), start_idx + 260)
        for i in range(start_idx + 1, min(len(lines), start_idx + 260)):
            m = class_pattern.match(lines[i])
            if not m:
                continue
            current_indent = len(m.group("indent"))
            if current_indent <= start_indent:
                end_idx = i
                break

        snippet = "\n".join(lines[start_idx:end_idx]).strip()
        if not snippet:
            return None

        if len(snippet) > 2400:
            snippet = snippet[:2400]
        return snippet

    def _definition_identifiers(self, queries: list[str]) -> set[str]:
        identifiers: set[str] = set()
        for query in queries:
            tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query)
            for token in tokens:
                if any(ch.isupper() for ch in token[1:]) and len(token) > 3:
                    identifiers.add(token)
        return identifiers

    def _extract_endpoint_lines(self, content: str) -> str:
        lines = content.splitlines()
        extracted: list[str] = []
        i = 0

        while i < len(lines):
            line = lines[i]
            if self._ENDPOINT_DECORATOR_RE.match(line):
                # Collect the full decorator; it may span multiple lines
                # until the first ')'
                decorator_parts = [line.rstrip()]
                j = i + 1
                while j < min(i + 10, len(lines)) and ")" not in line:
                    line = lines[j]
                    decorator_parts.append(line.rstrip())
                    j += 1
                full_decorator = " ".join(part.strip() for part in decorator_parts)
                extracted.append(full_decorator)
            i += 1

        if not extracted:
            return ""

        return "\n".join(extracted[:40])
