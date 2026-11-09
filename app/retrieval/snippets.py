def extract_keywords(queries: list[str]) -> set[str]:
    keywords: set[str] = set()
    for query in queries:
        for word in query.lower().split():
            if len(word) > 2:
                keywords.add(word)
                if word.endswith("s") and len(word) > 4:
                    keywords.add(word[:-1])
    return keywords


def extract_density_snippet(
    content: str,
    queries: list[str],
    window: int,
    fallback_length: int,
    max_occurrences_per_keyword: int = 5,
    prefix: str = "...\n",
    suffix: str = "\n...",
) -> tuple[str, int | None, int, int]:
    """Return a keyword-density snippet and diagnostic stats.

    Returns:
        snippet, best_pos, best_count, keyword_count
    """
    lowered = content.lower()
    keywords = extract_keywords(queries)

    if not keywords:
        fallback = content[:fallback_length]
        last_newline = fallback.rfind("\n")
        trimmed = fallback[:last_newline] if last_newline != -1 else fallback
        return trimmed, None, 0, 0

    candidate_positions: list[int] = []
    for word in keywords:
        start = 0
        for _ in range(max_occurrences_per_keyword):
            idx = lowered.find(word, start)
            if idx == -1:
                break
            candidate_positions.append(idx)
            start = idx + 1

    if not candidate_positions:
        fallback = content[:fallback_length]
        last_newline = fallback.rfind("\n")
        trimmed = fallback[:last_newline] if last_newline != -1 else fallback
        return trimmed, None, 0, len(keywords)

    best_pos = candidate_positions[0]
    best_count = 0

    for pos in candidate_positions:
        win_start = max(0, pos - window)
        win_end = min(len(lowered), pos + window)
        window_text = lowered[win_start:win_end]
        count = sum(1 for kw in keywords if kw in window_text)
        if count > best_count:
            best_count = count
            best_pos = pos

    start = max(0, best_pos - window)
    end = min(len(content), best_pos + window)
    snippet = content[start:end]
    if start > 0:
        snippet = prefix + snippet
    if end < len(content):
        snippet = snippet + suffix

    return snippet, best_pos, best_count, len(keywords)


def extract_anchor_snippet(
    content: str,
    anchors: list[str],
    window: int,
    max_length: int,
    prefix: str = "...\n",
    suffix: str = "\n...",
) -> str | None:
    """Return a snippet centered around the first matching anchor.

    Anchors are checked in order of priority. Matching is case-insensitive.
    """
    if not content or not anchors:
        return None

    lowered = content.lower()
    best_idx: int | None = None

    for anchor in anchors:
        needle = anchor.lower().strip()
        if not needle:
            continue
        idx = lowered.find(needle)
        if idx != -1:
            best_idx = idx
            break

    if best_idx is None:
        return None

    start = max(0, best_idx - window)
    end = min(len(content), best_idx + window)
    snippet = content[start:end]
    if start > 0:
        snippet = prefix + snippet
    if end < len(content):
        snippet = snippet + suffix

    return snippet[:max_length]
