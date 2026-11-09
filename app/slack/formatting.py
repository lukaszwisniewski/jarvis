def format_response(
    query: str,
    answer: str,
    user_id: str,
) -> dict:
    """Format a Jarvis answer as a Slack Block Kit message.

    Block Kit is Slack's structured layout system — richer than plain text,
    supports markdown, dividers, and context footers.

    Args:
        query: The original question the user asked.
        answer: The LLM-generated answer.
        user_id: Slack user ID of the requesting user.

    Returns:
        A dict ready to POST to Slack's response_url.
    """
    return {
        "response_type": "ephemeral",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "verbatim": True,
                    "text": f"*Query from <@{user_id}>:* {query}",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "verbatim": True,
                    "text": answer,
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":robot_face: Jarvis — powered by Anthropic Claude",
                    }
                ],
            },
        ],
    }
