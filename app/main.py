from fastapi import FastAPI, HTTPException

from app import __version__
from app.ingestion.acquire import SourceAcquisitionError, acquire_source
from app.models import SourceBundle, SourceRequest

app = FastAPI(title="RecipeDrop", version=__version__)


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "RecipeDrop", "version": __version__, "docs": "/docs"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/extract-source", response_model=SourceBundle)
def extract_source(request: SourceRequest) -> SourceBundle:
    try:
        return acquire_source(request.url)
    except SourceAcquisitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
