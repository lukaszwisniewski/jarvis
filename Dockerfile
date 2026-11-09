FROM python:3.12-slim

WORKDIR /app

# Install Poetry
RUN pip install poetry==2.2.1

# Copy dependency files first — Docker layer cache means this only
# re-runs when dependencies change, not on every code change
COPY pyproject.toml poetry.lock ./

# Install runtime deps only (no dev tools in the container)
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-root --no-interaction --no-ansi

COPY . .
