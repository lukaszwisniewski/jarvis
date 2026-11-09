from app.llm.endpoint_inventory import build_endpoint_inventory_response


def test_build_endpoint_inventory_response_endpoint_query_deduplicates_and_counts():
    context = """
[GITHUB]
Title: services/orders-api/api/resources/orders/routes.py
Snippet: @router.get("/orders")
@router.get("/orders")
@router.post("/orders")
URL: https://example/1
Relevance: 0.9
[GITHUB]
Title: services/orders-api/api/resources/orders/items/items.py
Snippet: @router.delete("/orders/{order_id}/items/{item_id}")
@router.post("/orders/{order_id}/items")
URL: https://example/2
Relevance: 0.8
[JIRA]
Title: unrelated ticket
Snippet: Key: PROJ-1
Relevance: 0.1
""".strip()

    answer = build_endpoint_inventory_response(
        query="How many endpoints do we have and where are they defined?",
        context=context,
    )

    assert answer is not None
    assert "*4 endpoints*" in answer
    assert "routes.py" in answer
    assert "items.py" in answer
    assert "/orders" in answer


def test_build_endpoint_inventory_response_returns_none_for_non_endpoint_query():
    context = """
[GITHUB]
Title: app/main.py
Snippet: def run():
Relevance: 0.2
""".strip()

    answer = build_endpoint_inventory_response(
        query="Who owns this project?",
        context=context,
    )

    assert answer is None


def test_build_endpoint_inventory_response_returns_none_when_no_endpoints_found():
    context = """
[GITHUB]
Title: services/orders-api/api/resources/orders/routes.py
Snippet: class OrderService:
    pass
Relevance: 0.9
""".strip()

    answer = build_endpoint_inventory_response(
        query="list endpoints in orders",
        context=context,
    )

    assert answer is None
