from pathlib import Path

import pytest
from pydantic import ValidationError
from yt_dlp.utils import DownloadError

from app.config import Settings
from app.ingestion import evidence
from app.models import SourceBundle


class FakeDownloader:
    def __init__(
        self,
        info: dict | None = None,
        payload: bytes = b"recipe video",
        error: Exception | None = None,
        progress_updates: list[dict] | None = None,
        extra_files: dict[str, bytes] | None = None,
        output_name: str = "media.mp4",
        result_filepath: str | None = "default",
    ) -> None:
        self.info = info if info is not None else {"id": "sample", "title": "Recipe video"}
        self.payload = payload
        self.error = error
        self.progress_updates = progress_updates or []
        self.extra_files = extra_files or {}
        self.output_name = output_name
        self.result_filepath = result_filepath
        self.options = None
        self.extract_args = None
        self.output_path: Path | None = None
        self.progress_hooks = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def add_progress_hook(self, hook) -> None:
        self.progress_hooks.append(hook)

    def extract_info(self, url: str, download: bool):
        self.extract_args = (url, download)
        if self.error is not None:
            raise self.error
        result = dict(self.info)
        if download:
            output_directory = Path(self.options["outtmpl"]).parent
            self.output_path = output_directory / self.output_name
            self.output_path.write_bytes(self.payload)
            for name, contents in self.extra_files.items():
                (output_directory / name).write_bytes(contents)
            if self.result_filepath == "default":
                result["filepath"] = str(self.output_path)
            elif self.result_filepath is not None:
                result["filepath"] = self.result_filepath
            for update in self.progress_updates:
                for hook in self.progress_hooks:
                    hook(update)
        return result


def configure_fakes(
    monkeypatch,
    downloader: FakeDownloader | None = None,
    source: SourceBundle | None = None,
    max_bytes: int = 1024,
    max_duration_seconds: int = 300,
) -> FakeDownloader | None:
    monkeypatch.setattr(
        evidence,
        "settings",
        Settings(
            _env_file=None,
            media_max_bytes=max_bytes,
            media_max_duration_seconds=max_duration_seconds,
        ),
    )
    if source is not None:
        monkeypatch.setattr(evidence, "acquire_source", lambda _url: source)
    if downloader is not None:
        def make_downloader(options):
            downloader.options = options
            return downloader

        monkeypatch.setattr(evidence.yt_dlp, "YoutubeDL", make_downloader)
    return downloader


def test_settings_default_to_positive_media_limits() -> None:
    settings = Settings(_env_file=None)

    assert settings.media_max_bytes == 52_428_800
    assert settings.media_max_duration_seconds == 300


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("MEDIA_MAX_BYTES", "0"),
        ("MEDIA_MAX_BYTES", "-1"),
        ("MEDIA_MAX_DURATION_SECONDS", "0"),
        ("MEDIA_MAX_DURATION_SECONDS", "-1"),
        ("MEDIA_MAX_BYTES", "not-an-integer"),
        ("MEDIA_MAX_DURATION_SECONDS", "not-an-integer"),
    ],
)
def test_invalid_media_limit_environment_values_fail_validation(
    monkeypatch, environment_name: str, value: str
) -> None:
    monkeypatch.setenv(environment_name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_acquire_evidence_yields_media_and_cleans_temporary_workspace(monkeypatch) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url, title="Recipe video", duration_seconds=42)
    downloader = FakeDownloader(payload=b"small media payload")
    configure_fakes(monkeypatch, downloader, source)

    with evidence.acquire_evidence(url) as bundle:
        media_path = bundle.media_path
        media_directory = media_path.parent
        assert bundle.source == source
        assert bundle.media_type == "video/mp4"
        assert media_path.is_file()
        assert media_path.read_bytes() == b"small media payload"
        assert downloader.extract_args == (url, True)
        assert len(downloader.progress_hooks) == 1
        assert downloader.options["max_filesize"] == 1024
        assert downloader.options["cachedir"] is False
        assert downloader.options["noplaylist"] is True
        assert downloader.options["max_downloads"] == 1
        assert "height<=720" in downloader.options["format"]
        assert downloader.options["writesubtitles"] is False
        assert downloader.options["writeautomaticsub"] is False

    assert not media_path.exists()
    assert not media_directory.exists()


def test_progress_budget_counts_cumulative_bytes_once_and_allows_under_limit(
    monkeypatch,
) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    updates = [
        {"downloaded_bytes": 31, "info_dict": {"format_id": "video"}},
        {"downloaded_bytes": 31, "info_dict": {"format_id": "video"}},
        {"downloaded_bytes": 31, "info_dict": {"format_id": "audio"}},
        {"downloaded_bytes": 43, "info_dict": {"format_id": "video"}},
    ]
    downloader = FakeDownloader(progress_updates=updates)
    configure_fakes(monkeypatch, downloader, source, max_bytes=100)

    with evidence.acquire_evidence(url) as bundle:
        assert bundle.media_path.is_file()


def test_video_and_audio_progress_share_one_byte_budget(monkeypatch) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    updates = [
        {"downloaded_bytes": 70, "info_dict": {"format_id": "video"}},
        {"downloaded_bytes": 70, "info_dict": {"format_id": "video"}},
        {"downloaded_bytes": 31, "info_dict": {"format_id": "audio"}},
    ]
    downloader = FakeDownloader(progress_updates=updates)
    configure_fakes(monkeypatch, downloader, source, max_bytes=100)

    with pytest.raises(evidence.EvidenceAcquisitionError, match="size limit"):
        with evidence.acquire_evidence(url):
            pytest.fail("Over-budget media must not be yielded")

    assert downloader.output_path is not None
    assert not downloader.output_path.parent.exists()


def test_playlist_result_is_rejected_and_workspace_is_cleaned(monkeypatch) -> None:
    url = "https://example.com/playlist"
    source = SourceBundle(source_url=url)
    downloader = FakeDownloader(info={"_type": "playlist", "entries": [{"id": "one"}]})
    configure_fakes(monkeypatch, downloader, source)

    with pytest.raises(
        evidence.EvidenceAcquisitionError,
        match="RecipeDrop only accepts individual media URLs",
    ):
        with evidence.acquire_evidence(url):
            pytest.fail("Playlist evidence must not be yielded")

    assert downloader.output_path is not None
    assert not downloader.output_path.parent.exists()


def test_known_over_duration_metadata_is_rejected_before_downloader_construction(
    monkeypatch,
) -> None:
    url = "https://example.com/long-video"
    source = SourceBundle(source_url=url, duration_seconds=301)
    configure_fakes(monkeypatch, source=source, max_duration_seconds=300)

    def unexpected_downloader(_options):
        raise AssertionError("Downloader must not be constructed for overlong media")

    monkeypatch.setattr(evidence.yt_dlp, "YoutubeDL", unexpected_downloader)

    with pytest.raises(evidence.EvidenceAcquisitionError, match="duration limit"):
        with evidence.acquire_evidence(url):
            pytest.fail("Overlong media must not be yielded")


def test_second_pass_over_duration_is_rejected_and_cleaned(monkeypatch) -> None:
    url = "https://example.com/long-video"
    source = SourceBundle(source_url=url, duration_seconds=None)
    downloader = FakeDownloader(info={"duration": 301})
    configure_fakes(monkeypatch, downloader, source, max_duration_seconds=300)

    with pytest.raises(evidence.EvidenceAcquisitionError, match="duration limit"):
        with evidence.acquire_evidence(url):
            pytest.fail("Overlong media must not be yielded")

    assert downloader.output_path is not None
    assert not downloader.output_path.parent.exists()


def test_download_error_is_sanitized_and_workspace_is_cleaned(monkeypatch) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    downloader = FakeDownloader(error=DownloadError("internal yt-dlp details"))
    configure_fakes(monkeypatch, downloader, source)

    with pytest.raises(
        evidence.EvidenceAcquisitionError,
        match="Unable to acquire media from this URL",
    ) as error:
        with evidence.acquire_evidence(url):
            pytest.fail("Failed media acquisition must not be yielded")

    assert "internal yt-dlp details" not in str(error.value)
    assert downloader.options is not None
    assert not Path(downloader.options["outtmpl"].replace("media.%(ext)s", "")).exists()


def test_actual_media_size_over_limit_is_rejected_and_cleaned(monkeypatch) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    downloader = FakeDownloader(payload=b"x" * 11)
    configure_fakes(monkeypatch, downloader, source, max_bytes=10)

    with pytest.raises(evidence.EvidenceAcquisitionError, match="size limit"):
        with evidence.acquire_evidence(url):
            pytest.fail("Oversized media must not be yielded")

    assert downloader.output_path is not None
    assert not downloader.output_path.parent.exists()


def test_part_file_is_not_accepted(monkeypatch) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    downloader = FakeDownloader(output_name="media.mp4.part")
    configure_fakes(monkeypatch, downloader, source)

    with pytest.raises(evidence.EvidenceAcquisitionError, match="incomplete"):
        with evidence.acquire_evidence(url):
            pytest.fail("A partial media file must not be yielded")

    assert downloader.output_path is not None
    assert not downloader.output_path.parent.exists()


def test_multiple_final_video_candidates_are_rejected(monkeypatch) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    downloader = FakeDownloader(
        extra_files={"second.webm": b"another video"},
        result_filepath=None,
    )
    configure_fakes(monkeypatch, downloader, source)

    with pytest.raises(evidence.EvidenceAcquisitionError, match="one finalized video"):
        with evidence.acquire_evidence(url):
            pytest.fail("Ambiguous video files must not be yielded")

    assert downloader.output_path is not None
    assert not downloader.output_path.parent.exists()


def test_reported_media_path_must_be_inside_workspace(monkeypatch, tmp_path) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    external_path = tmp_path / "outside.mp4"
    external_path.write_bytes(b"outside media")
    downloader = FakeDownloader(result_filepath=str(external_path))
    configure_fakes(monkeypatch, downloader, source)

    with pytest.raises(evidence.EvidenceAcquisitionError, match="outside the temporary"):
        with evidence.acquire_evidence(url):
            pytest.fail("An external media path must not be yielded")

    assert downloader.output_path is not None
    assert not downloader.output_path.parent.exists()
    assert external_path.exists()


def test_unexpected_exception_propagates_and_workspace_is_cleaned(monkeypatch) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    downloader = FakeDownloader(error=RuntimeError("programming error"))
    configure_fakes(monkeypatch, downloader, source)

    with pytest.raises(RuntimeError, match="programming error"):
        with evidence.acquire_evidence(url):
            pytest.fail("Unexpected failure must propagate")

    assert downloader.options is not None
    assert not Path(downloader.options["outtmpl"].replace("media.%(ext)s", "")).exists()


def test_caller_exception_cleans_temporary_workspace(monkeypatch) -> None:
    url = "https://example.com/recipe"
    source = SourceBundle(source_url=url)
    downloader = FakeDownloader()
    configure_fakes(monkeypatch, downloader, source)

    with pytest.raises(RuntimeError, match="caller failed"):
        with evidence.acquire_evidence(url) as bundle:
            media_path = bundle.media_path
            media_directory = media_path.parent
            raise RuntimeError("caller failed")

    assert not media_path.exists()
    assert not media_directory.exists()
