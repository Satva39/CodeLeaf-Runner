from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,119}$")
OWNER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class CodeFile(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=262144)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if (
            not FILENAME_RE.fullmatch(value)
            or value in {".", ".."}
            or ".." in value
            or "\x00" in value
        ):
            raise ValueError("File names must be simple root-level names (no paths or '..').")
        path = PurePosixPath(value)
        if path.name != value or "/" in value or "\\" in value:
            raise ValueError("File paths are not accepted; provide flat filenames only.")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Source files cannot contain NUL bytes.")
        return value


class CreateJobRequest(StrictModel):
    # Kept for the current CodeLeaf backend's POST /v1/jobs contract.
    # New callers should send X-CodeLeaf-User instead.
    owner_id: str | None = Field(default=None, min_length=1, max_length=80)
    language: str = Field(min_length=1, max_length=40)
    version: str = Field(default="*", min_length=1, max_length=40)
    source: str | None = Field(default=None, min_length=1, max_length=262144)
    files: list[CodeFile] = Field(default_factory=list, max_length=25)
    stdin: str = Field(default="", max_length=16384)
    args: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("owner_id")
    @classmethod
    def validate_owner_id(cls, value: str | None) -> str | None:
        if value is not None and not OWNER_ID_RE.fullmatch(value):
            raise ValueError("owner_id must contain only letters, numbers, underscores, or hyphens.")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not re.fullmatch(r"(?:\*|[0-9A-Za-z.+*^~<>= -]{1,39})", value):
            raise ValueError("Invalid runtime version selector.")
        return value.strip()

    @field_validator("source", "stdin")
    @classmethod
    def validate_text_payload(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("Source and stdin cannot contain NUL bytes.")
        return value

    @field_validator("args")
    @classmethod
    def validate_args(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or len(item) > 256 or "\x00" in item for item in value):
            raise ValueError("Each argument must be text of at most 256 characters.")
        return value

    @model_validator(mode="after")
    def validate_files_and_source(self) -> "CreateJobRequest":
        has_source = self.source is not None
        has_files = bool(self.files)
        if has_source == has_files:
            raise ValueError("Provide exactly one of 'source' or 'files'.")
        names = [item.name.lower() for item in self.files]
        if len(names) != len(set(names)):
            raise ValueError("File names must be unique (case-insensitive).")
        return self


class JobPublic(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    result: dict[str, Any] = Field(default_factory=dict)
