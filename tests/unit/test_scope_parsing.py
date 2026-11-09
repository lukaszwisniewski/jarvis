from app.api.webhooks.slack import _parse_scope_and_query


def test_parse_scope_and_query_cf_prefix():
    scope, query = _parse_scope_and_query("cf where is workflow deletion")
    assert scope == "coldfusion"
    assert query == "where is workflow deletion"


def test_parse_scope_and_query_py_prefix():
    scope, query = _parse_scope_and_query("py which endpoint updates group")
    assert scope == "python"
    assert query == "which endpoint updates group"


def test_parse_scope_and_query_no_prefix():
    scope, query = _parse_scope_and_query("how are thumbnails generated")
    assert scope is None
    assert query == "how are thumbnails generated"
