from app.retrieval.query_generator import generate_queries


def test_generate_queries_prioritizes_table_fallback_terms(monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.query_generator.ask_llm",
        lambda query, fast=True: '["workflow setup"]',
    )

    queries = generate_queries("what are the main tables involved with workflows?")

    assert any("SELECT FROM JOIN" in q for q in queries)
    assert any("queryExecute FROM" in q for q in queries)


def test_generate_queries_keeps_llm_queries_for_non_table_question(monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.query_generator.ask_llm",
        lambda query, fast=True: '["thumbnail generation", "image resize service"]',
    )

    queries = generate_queries("how does thumbnail generation work")

    assert "thumbnail generation" in queries
    assert "image resize service" in queries
