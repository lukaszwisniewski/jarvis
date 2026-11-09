from app.workers.tasks import (
    _append_current_state_guardrail,
    _append_source_footer,
    _extract_github_repo,
    _is_current_state_query,
    _source_breakdown_from_context,
    _source_counts_from_context,
)


def test_source_counts_from_context_extracts_github_and_jira():
    context = """
[GITHUB]
Title: file1.py
Snippet: ...
[JIRA]
Title: ticket1
Snippet: ...
[GITHUB]
Title: file2.py
Snippet: ...
""".strip()

    counts = _source_counts_from_context(context)

    assert counts["github"] == 2
    assert counts["jira"] == 1


def test_append_source_footer_appends_counts():
    answer = "Result content"
    context = """
[GITHUB]
Title: users.py
Snippet: ...
URL: https://github.com/example-org/shared-lib/blob/main/models/users.py
Relevance: 0.8
[JIRA]
Title: ticket1
Snippet: ...
""".strip()

    with_footer = _append_source_footer(answer, context)

    assert "Result content" in with_footer
    assert "Source coverage:" in with_footer
    assert "shared-lib" in with_footer
    assert "Jira 1" in with_footer


def test_extract_github_repo_from_url():
    repo = _extract_github_repo(
        "https://github.com/example-org/orders-api/blob/main/src/main.py"
    )
    assert repo == "example-org/orders-api"


def test_source_breakdown_from_context_counts_repos_and_jira():
    context = """
[GITHUB]
Title: one
Snippet: ...
URL: https://github.com/example-org/orders-api/blob/main/a.py
Relevance: 0.9
[GITHUB]
Title: two
Snippet: ...
URL: https://github.com/example-org/shared-lib/blob/main/b.py
Relevance: 0.8
[GITHUB]
Title: three
Snippet: ...
URL: https://github.com/example-org/orders-api/blob/main/c.py
Relevance: 0.7
[JIRA]
Title: t1
Snippet: ...
[JIRA]
Title: t2
Snippet: ...
""".strip()

    repo_counts, jira_count, confluence_count = _source_breakdown_from_context(context)

    assert repo_counts["example-org/orders-api"] == 2
    assert repo_counts["example-org/shared-lib"] == 1
    assert jira_count == 2
    assert confluence_count == 0


def test_is_current_state_query_detects_current_wiring_language():
    assert _is_current_state_query("is this currently wired into the pipeline?")
    assert _is_current_state_query("is it deployed now?")
    assert not _is_current_state_query("how does embedding generation work?")


def test_append_current_state_guardrail_when_no_github_context():
    answer = "Base answer"
    query = "is this currently wired into upload flow?"
    context = """
[JIRA]
Title: ticket
Snippet: details
""".strip()

    guarded = _append_current_state_guardrail(
        answer=answer, query=query, context=context
    )

    assert "Base answer" in guarded
    assert "no GitHub code evidence was retrieved" in guarded


def test_append_current_state_guardrail_not_added_for_non_current_state_query():
    answer = "Base answer"
    query = "how does embedding generation work?"
    context = """
[JIRA]
Title: ticket
Snippet: details
""".strip()

    guarded = _append_current_state_guardrail(
        answer=answer, query=query, context=context
    )

    assert guarded == answer
