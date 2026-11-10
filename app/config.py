import json
from typing import Literal

from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    """Runtime configuration for the JARVIS application.

    Values are loaded from environment variables and a local .env file. The
    settings include Slack credentials, Anthropic configuration, provider
    integration details, Redis settings, and retrieval tuning values.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    anthropic_api_key: str | None = None

    # Model selection — use haiku for cheap/fast steps, sonnet for synthesis
    anthropic_model_fast: str = "claude-haiku-4-5"
    anthropic_model_synthesis: str = "claude-sonnet-4-5"

    github_token: str = ""
    # Global fallback GitHub settings (used when no profile is injected).
    # Leave these empty by default so the app can be configured for a standalone
    # deployment without shipping internal repository defaults.
    github_repos: list[str] = []
    github_allowed_extensions: list[str] = [".py", ".md", ".tf"]
    github_extension_scores: dict[str, int] = {".py": 5, ".md": 2, ".tf": 2}

    # Profile-specific settings used by auto-detection in GitHubProvider.
    github_cf_repos: list[str] = []
    github_cf_allowed_extensions: list[str] = [".cfm", ".cfc"]
    github_cf_extension_scores: dict[str, int] = {".cfc": 5, ".cfm": 3}

    github_py_repos: list[str] = []
    github_py_allowed_extensions: list[str] = [".py", ".md", ".tf"]
    github_py_extension_scores: dict[str, int] = {".py": 5, ".md": 2, ".tf": 2}

    jira_token: str = ""
    jira_base_url: str = ""
    jira_email: str = ""

    confluence_base_url: str = ""

    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"

    # Optional comma-separated list of allowed Slack user IDs (e.g. "U123,U456")
    # If empty, all users are allowed
    slack_allowed_user_ids: str = ""

    # Set to true to bypass real Anthropic API calls (dev/test environments)
    llm_stub_mode: bool = False

    # Logging: JSON lines by default; "text" keeps a human-readable format.
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # Agent mode (off by default): Claude chooses which sources to search via
    # tools, for at most agent_max_tool_rounds rounds of tool calls. The upper
    # bound keeps cost predictable; an out-of-range value fails at startup.
    agent_mode: bool = False
    agent_max_tool_rounds: int = Field(default=4, ge=1, le=8)

    # Retrieval policy (data-driven; can be tuned via env)
    retrieval_source_caps: dict[str, int] = {
        "github": 6,
        "jira": 3,
        "confluence": 1,
    }
    retrieval_endpoint_intent_keywords: list[str] = [
        "endpoint",
        "route",
        "api",
    ]
    retrieval_architecture_intent_keywords: list[str] = [
        "flow",
        "process",
        "architecture",
        "handler",
        "processor",
        "worker",
        "lambda",
    ]
    retrieval_table_intent_keywords: list[str] = [
        "table",
        "tables",
        "database",
        "schema",
        "sql",
        "column",
        "columns",
        "db",
    ]
    retrieval_query_stopwords: list[str] = [
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "have",
        "show",
        "many",
        "what",
        "where",
        "currently",
        "please",
    ]
    retrieval_table_entity_exclude_keywords: list[str] = [
        "table",
        "tables",
        "database",
        "schema",
        "sql",
        "column",
        "columns",
        "query",
        "queries",
        "main",
        "involved",
    ]

    @field_validator(
        "github_repos",
        "github_cf_repos",
        "github_py_repos",
        "retrieval_endpoint_intent_keywords",
        "retrieval_architecture_intent_keywords",
        "retrieval_table_intent_keywords",
        "retrieval_query_stopwords",
        "retrieval_table_entity_exclude_keywords",
        mode="before",
    )
    @classmethod
    def _parse_string_lists(cls, value: object) -> object:
        """Normalize string-backed list settings from environment variables.

        Args:
            value: Raw configuration value. It may be a comma-separated string or a
                JSON array string.

        Returns:
            list[str]: Lower-cased values with blank entries removed, or [] when the
            input is empty.
        """
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                loaded = json.loads(raw)
                return [
                    str(item).strip().lower() for item in loaded if str(item).strip()
                ]
            return [part.strip().lower() for part in raw.split(",") if part.strip()]
        return value

    @field_validator(
        "github_allowed_extensions",
        "github_cf_allowed_extensions",
        "github_py_allowed_extensions",
        mode="before",
    )
    @classmethod
    def _parse_github_extensions(cls, value: object) -> object:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            extensions = [
                part.strip().lower() for part in raw.split(",") if part.strip()
            ]
            normalized = []
            for ext in extensions:
                normalized.append(ext if ext.startswith(".") else f".{ext}")
            return normalized
        return value

    @field_validator(
        "github_extension_scores",
        "github_cf_extension_scores",
        "github_py_extension_scores",
        "retrieval_source_caps",
        mode="before",
    )
    @classmethod
    def _parse_key_int_mappings(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return {}
            if raw.startswith("{"):
                return json.loads(raw)

            parsed: dict[str, int] = {}
            is_source_caps = info.field_name == "retrieval_source_caps"
            for pair in raw.split(","):
                if not pair.strip() or ":" not in pair:
                    continue
                key, score = pair.split(":", 1)
                normalized_key = key.strip().lower()
                # For extension score maps we normalize to dot-prefixed extensions.
                if (
                    not is_source_caps
                    and normalized_key
                    and normalized_key[0].isalnum()
                ):
                    normalized_key = (
                        normalized_key
                        if normalized_key.startswith(".")
                        else f".{normalized_key}"
                    )
                parsed[normalized_key] = int(score.strip())
            return parsed
        return value


settings = Settings()
