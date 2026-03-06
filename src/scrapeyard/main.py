"""FastAPI application entry point."""

from fastapi import FastAPI

app = FastAPI(
    title="Scrapeyard",
    description="Config-driven web scraping microservice",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict:
    """Service health check endpoint."""
    return {"status": "ok"}
