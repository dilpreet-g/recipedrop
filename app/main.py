from fastapi import FastAPI

from app import __version__

app = FastAPI(title="RecipeDrop", version=__version__)


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "RecipeDrop", "version": __version__, "docs": "/docs"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
