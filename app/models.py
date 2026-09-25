from pathlib import Path

from pydantic import BaseModel, HttpUrl, TypeAdapter, ValidationError, field_validator


_http_url_adapter = TypeAdapter(HttpUrl)


class SourceRequest(BaseModel):
	url: str

	@field_validator("url")
	@classmethod
	def validate_url(cls, value: str) -> str:
		try:
			_http_url_adapter.validate_python(value)
		except ValidationError as exc:
			raise ValueError("URL must be a valid HTTP or HTTPS URL") from exc
		return value


class SourceBundle(BaseModel):
	source_url: str
	extractor: str | None = None
	title: str | None = None
	description: str | None = None
	uploader: str | None = None
	duration_seconds: float | None = None
	thumbnail_url: str | None = None


class EvidenceBundle(BaseModel):
	source: SourceBundle
	media_path: Path
	media_type: str
