import re
from collections import defaultdict

_ENDPOINT_QUERY_MARKERS = (
    "endpoint",
    "endpoints",
    "route",
    "routes",
    "api",
)

_PY_DECORATOR_RE = re.compile(
    r"@(?:\w+\.)?(?P<method>get|post|put|patch|delete|options|head)\((?P<args>[^)]*)",
    re.IGNORECASE,
)
_QUOTED_PATH_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_PATH_LIKE_RE = re.compile(r"['\"](/[^'\"]*)['\"]")


def _is_endpoint_inventory_query(query: str) -> bool:
    lowered = query.lower()
    return any(marker in lowered for marker in _ENDPOINT_QUERY_MARKERS)


def _extract_sections(context: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_source = ""
    current_title = ""
    snippet_lines: list[str] = []

    for line in context.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_source or current_title or snippet_lines:
                sections.append(
                    {
                        "source": current_source,
                        "title": current_title,
                        "snippet": "\n".join(snippet_lines).strip(),
                    }
                )
            current_source = stripped.strip("[]")
            current_title = ""
            snippet_lines = []
            continue

        if stripped.startswith("Title: "):
            current_title = stripped.replace("Title: ", "", 1)
            continue

        if stripped.startswith("Snippet: "):
            snippet_lines.append(stripped.replace("Snippet: ", "", 1))
            continue

        if stripped.startswith("URL: ") or stripped.startswith("Relevance: "):
            continue

        if snippet_lines:
            snippet_lines.append(line)

    if current_source or current_title or snippet_lines:
        sections.append(
            {
                "source": current_source,
                "title": current_title,
                "snippet": "\n".join(snippet_lines).strip(),
            }
        )

    return sections


def _extract_path_from_args(args: str) -> str:
    # Prefer an explicit path argument (starts with "/")
    path_match = _PATH_LIKE_RE.search(args)
    if path_match:
        return path_match.group(1)
    # Fall back to any quoted string, but reject ones that look like tag/model names
    for match in _QUOTED_PATH_RE.finditer(args):
        candidate = match.group(1)
        if not any(
            skip in candidate for skip in ("tags", "model", "Depends", "response", " ")
        ):
            return candidate
    return "(see file)"


def _extract_endpoints(snippet: str, file_path: str) -> list[dict[str, str]]:
    endpoints: list[dict[str, str]] = []

    for match in _PY_DECORATOR_RE.finditer(snippet):
        method = match.group("method").upper()
        args = match.group("args")
        path = _extract_path_from_args(args) if args.strip() else "(see file)"
        endpoints.append(
            {
                "method": method,
                "path": path,
                "file": file_path,
            }
        )

    return endpoints


def build_endpoint_inventory_response(query: str, context: str) -> str | None:
    if not _is_endpoint_inventory_query(query) or not context.strip():
        return None

    sections = _extract_sections(context)
    github_sections = [s for s in sections if s["source"].upper() == "GITHUB"]

    extracted: list[dict[str, str]] = []
    for section in github_sections:
        file_path = section["title"]
        snippet = section["snippet"]
        extracted.extend(_extract_endpoints(snippet, file_path))

    # Dedup only when path is known (path with actual value, not "(see file)")
    # Unknown-path endpoints are deduplicated by (method, file, occurrence_index)
    # to avoid collapsing multiple real endpoints that share the same method
    deduped: list[dict[str, str]] = []
    seen_known: set[tuple[str, str, str]] = set()
    for endpoint in extracted:
        if endpoint["path"] != "(see file)":
            key = (endpoint["method"], endpoint["path"], endpoint["file"])
            if key in seen_known:
                continue
            seen_known.add(key)
        deduped.append(endpoint)

    rows = sorted(
        deduped,
        key=lambda row: (
            row["file"],
            row["path"],
            row["method"],
        ),
    )

    if not rows:
        return None

    # Group by file for Slack-friendly mrkdwn output
    # (Slack does not render markdown tables)
    by_file: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_file[row["file"]].append(row)

    lines = [
        f"*{len(rows)} endpoints* across {len(by_file)} files, sir.",
        "",
    ]

    for file_path in sorted(by_file.keys()):
        file_rows = by_file[file_path]
        short_path = re.sub(
            r"^[^/]+/[^/]+/", "", file_path
        )  # strip "repo/top-level-dir/"
        lines.append(f"*{short_path}* — {len(file_rows)} endpoint(s)")
        for row in sorted(file_rows, key=lambda r: (r["method"], r["path"])):
            path_display = (
                row["path"]
                if row["path"] != "(see file)"
                else "_(path requires router prefix — see file)_"
            )
            lines.append(f"  • `{row['method']}` {path_display}")
        lines.append("")

    lines.append(f"_Total: {len(rows)} (deduplicated by method + path + file)_")

    return "\n".join(lines)
