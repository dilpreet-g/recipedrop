from collections.abc import Mapping
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError

from app.models import SourceBundle


class SourceAcquisitionError(Exception):
    """Raised when yt-dlp cannot retrieve usable source metadata."""


def acquire_source(url: str) -> SourceBundle:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
    }

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
            if not isinstance(info, Mapping):
                raise SourceAcquisitionError("The source did not provide usable metadata.")
            if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
                raise SourceAcquisitionError(
                    "RecipeDrop only accepts individual media URLs."
                )

            return SourceBundle(
                source_url=url,
                extractor=_first_text(info, "extractor_key", "extractor"),
                title=_first_text(info, "title"),
                description=_first_text(info, "description", "caption"),
                uploader=_first_text(info, "uploader", "channel", "creator"),
                duration_seconds=_duration(info.get("duration")),
                thumbnail_url=_thumbnail(info),
            )
    except SourceAcquisitionError:
        raise
    except DownloadError as exc:
        raise SourceAcquisitionError(
            "Unable to extract source metadata from this URL."
        ) from exc


def _first_text(info: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _duration(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _thumbnail(info: Mapping[str, Any]) -> str | None:
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        candidates = [
            item
            for item in thumbnails
            if isinstance(item, Mapping) and isinstance(item.get("url"), str)
        ]
        if candidates:
            best = max(
                candidates,
                key=lambda item: (item.get("width") or 0) * (item.get("height") or 0),
            )
            return best["url"]
    return _first_text(info, "thumbnail")
