import mimetypes
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError

from app.config import settings
from app.ingestion.acquire import SourceAcquisitionError, acquire_source
from app.models import EvidenceBundle, SourceBundle


class EvidenceAcquisitionError(Exception):
    """Raised when bounded temporary media evidence cannot be acquired."""


class _MediaByteLimitExceeded(Exception):
    """Internal signal to stop yt-dlp after the shared media budget is exceeded."""


@contextmanager
def acquire_evidence(url: str) -> Iterator[EvidenceBundle]:
    """Yield evidence while its temporary media file exists; remove it on exit."""
    try:
        source = acquire_source(url)
    except SourceAcquisitionError as exc:
        raise EvidenceAcquisitionError(str(exc)) from exc

    if (
        source.duration_seconds is not None
        and source.duration_seconds > settings.media_max_duration_seconds
    ):
        raise EvidenceAcquisitionError("Media exceeds the configured duration limit.")

    with TemporaryDirectory(prefix="recipedrop-media-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        progress_hook = _make_progress_hook(settings.media_max_bytes)
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_downloads": 1,
            "max_filesize": settings.media_max_bytes,
            "cachedir": False,
            "format": (
                "best[height<=720][ext=mp4]/"
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                "best[height<=720]/bestvideo[height<=720]+bestaudio"
            ),
            "outtmpl": str(temporary_path / "media.%(ext)s"),
            "skip_download": False,
            "writeinfojson": False,
            "writethumbnail": False,
            "write_all_thumbnails": False,
            "writesubtitles": False,
            "writeautomaticsub": False,
            "getcomments": False,
            "writedescription": False,
            "writeannotations": False,
            "writeplaylistmetafiles": False,
        }

        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.add_progress_hook(progress_hook)
                info = downloader.extract_info(url, download=True)
        except _MediaByteLimitExceeded as exc:
            raise EvidenceAcquisitionError(
                "Downloaded media exceeds the configured size limit."
            ) from exc
        except DownloadError as exc:
            raise EvidenceAcquisitionError(
                "Unable to acquire media from this URL."
            ) from exc

        if not isinstance(info, Mapping):
            raise EvidenceAcquisitionError("The source did not provide downloadable media.")
        if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
            raise EvidenceAcquisitionError(
                "RecipeDrop only accepts individual media URLs."
            )
        duration = info.get("duration")
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and duration > settings.media_max_duration_seconds
        ):
            raise EvidenceAcquisitionError(
                "Media exceeds the configured duration limit."
            )

        media_path = _select_media_file(info, temporary_path)
        if media_path.stat().st_size > settings.media_max_bytes:
            raise EvidenceAcquisitionError("Downloaded media exceeds the configured size limit.")

        media_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
        yield EvidenceBundle(
            source=source,
            media_path=media_path,
            media_type=media_type,
        )


def _make_progress_hook(max_bytes: int):
    high_water_marks: dict[tuple[str, str, str], int] = {}
    total_downloaded = 0

    def progress_hook(status: dict[str, Any]) -> None:
        nonlocal total_downloaded
        downloaded_bytes = status.get("downloaded_bytes")
        if not isinstance(downloaded_bytes, int) or isinstance(downloaded_bytes, bool):
            return

        info = status.get("info_dict")
        if not isinstance(info, Mapping):
            info = {}
        stream_key = (
            str(info.get("ctx_id") or status.get("ctx_id") or ""),
            str(info.get("format_id") or ""),
            str(info.get("url") or ""),
        )
        previous_high_water = high_water_marks.get(stream_key, 0)
        if downloaded_bytes <= previous_high_water:
            return

        total_downloaded += downloaded_bytes - previous_high_water
        high_water_marks[stream_key] = downloaded_bytes
        # yt-dlp reports at downloader/chunk granularity, so a small overrun can occur.
        if total_downloaded > max_bytes:
            raise _MediaByteLimitExceeded

    return progress_hook


def _select_media_file(info: Mapping[str, Any], temporary_path: Path) -> Path:
    reported_path = info.get("filepath")
    if reported_path is not None:
        if not isinstance(reported_path, (str, Path)):
            raise EvidenceAcquisitionError("yt-dlp returned an invalid media path.")
        return _validate_media_file(reported_path, temporary_path)

    candidates = []
    for candidate in temporary_path.iterdir():
        if not candidate.is_file() or _is_temporary_file(candidate):
            continue
        if not _is_video_file(candidate):
            continue
        try:
            candidates.append(_validate_media_file(candidate, temporary_path))
        except EvidenceAcquisitionError:
            continue

    if len(candidates) != 1:
        raise EvidenceAcquisitionError("Unable to identify one finalized video media file.")
    return candidates[0]


def _validate_media_file(path: str | Path, temporary_path: Path) -> Path:
    try:
        resolved_path = Path(path).resolve(strict=True)
        resolved_root = temporary_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceAcquisitionError("The finalized media file is unavailable.") from exc

    if not resolved_path.is_relative_to(resolved_root):
        raise EvidenceAcquisitionError("The finalized media file is outside the temporary workspace.")
    if not resolved_path.is_file() or _is_temporary_file(resolved_path):
        raise EvidenceAcquisitionError("The finalized media file is incomplete.")
    if not _is_video_file(resolved_path):
        raise EvidenceAcquisitionError("The finalized media file is not video media.")
    return resolved_path


def _is_temporary_file(path: Path) -> bool:
    name = path.name.lower()
    return bool(
        re.search(
            r"(?:\.part(?:\.|$)|\.ytdl(?:\.|$)|\.tmp(?:\.|$)|\.temp(?:\.|$)|\.f\d+\.|\.frag(?:ment)?\d*)",
            name,
        )
    )


def _is_video_file(path: Path) -> bool:
    media_type = mimetypes.guess_type(path.name)[0]
    return media_type is not None and media_type.startswith("video/")
