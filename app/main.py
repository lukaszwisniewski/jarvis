from fastapi import FastAPI

from app.api.webhooks.slack import router as slack_router

app = FastAPI(title="Jarvis", version="0.1.0")

app.include_router(slack_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
