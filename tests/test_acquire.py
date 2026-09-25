import pytest
from fastapi.testclient import TestClient
from yt_dlp.utils import DownloadError

from app.ingestion import acquire
from app.main import app
from app.models import SourceBundle

client = TestClient(app)


class FakeDownloader:
    def __init__(self, info: dict | None = None, error: Exception | None = None) -> None:
        self.info = info
        self.error = error
        self.options = None
        self.extract_args = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def extract_info(self, url: str, download: bool):
        self.extract_args = (url, download)
        if self.error is not None:
            raise self.error
        return self.info


def install_fake_downloader(monkeypatch, downloader: FakeDownloader) -> None:
    def make_downloader(options):
        downloader.options = options
        return downloader

    monkeypatch.setattr(acquire.yt_dlp, "YoutubeDL", make_downloader)


def test_acquire_source_normalizes_metadata_and_skips_download(monkeypatch) -> None:
    url = "https://www.youtube.com/watch?v=example"
    downloader = FakeDownloader(
        {
            "extractor_key": "Youtube",
            "title": " Tomato Pasta ",
            "description": "A quick recipe",
            "uploader": "Kitchen Channel",
            "duration": 42,
            "thumbnails": [
                {"url": "https://img.example/small.jpg", "width": 320, "height": 180},
                {"url": "https://img.example/large.jpg", "width": 1280, "height": 720},
            ],
        }
    )
    install_fake_downloader(monkeypatch, downloader)

    bundle = acquire.acquire_source(url)

    assert bundle == SourceBundle(
        source_url=url,
        extractor="Youtube",
        title="Tomato Pasta",
        description="A quick recipe",
        uploader="Kitchen Channel",
        duration_seconds=42.0,
        thumbnail_url="https://img.example/large.jpg",
    )
    assert downloader.extract_args == (url, False)
    assert downloader.options == {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
    }


def test_acquire_source_allows_missing_optional_metadata(monkeypatch) -> None:
    url = "https://example.com/recipe"
    downloader = FakeDownloader({"extractor": "Generic", "url": url})
    install_fake_downloader(monkeypatch, downloader)

    bundle = acquire.acquire_source(url)

    assert bundle == SourceBundle(source_url=url, extractor="Generic")


def test_extract_source_rejects_malformed_url() -> None:
    response = client.post("/extract-source", json={"url": "not-a-url"})

    assert response.status_code == 422
    assert "valid HTTP or HTTPS URL" in response.json()["detail"][0]["msg"]


def test_download_error_returns_sanitized_422(monkeypatch) -> None:
    downloader = FakeDownloader(error=DownloadError("internal yt-dlp details"))
    install_fake_downloader(monkeypatch, downloader)

    response = client.post(
        "/extract-source", json={"url": "https://example.com/recipe"}
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Unable to extract source metadata from this URL."
    }
    assert "internal yt-dlp details" not in response.text


@pytest.mark.parametrize(
    "info",
    [
        {"_type": "playlist", "entries": [{"title": "First item"}]},
        {"entries": [{"title": "First item"}]},
    ],
)
def test_playlist_result_returns_clear_422(monkeypatch, info: dict) -> None:
    install_fake_downloader(monkeypatch, FakeDownloader(info))

    response = client.post(
        "/extract-source", json={"url": "https://example.com/recipe"}
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "RecipeDrop only accepts individual media URLs."
    }


def test_unexpected_exception_is_not_converted(monkeypatch) -> None:
    downloader = FakeDownloader(error=RuntimeError("programming error"))
    install_fake_downloader(monkeypatch, downloader)

    with pytest.raises(RuntimeError, match="programming error"):
        acquire.acquire_source("https://example.com/recipe")
