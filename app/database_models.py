from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DatabaseEngine(StrEnum):
    SQLITE = "sqlite"
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"


class StrictDatabaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class CreateDatabaseJobRequest(StrictDatabaseModel):
    engine: DatabaseEngine
    query: str = Field(min_length=1, max_length=65536)
    setup_sql: str = Field(default="", max_length=131072)

    @field_validator("query", "setup_sql")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("SQL must not contain NUL characters.")
        return value

    @model_validator(mode="after")
    def query_not_blank(self) -> "CreateDatabaseJobRequest":
        if not self.query.strip():
            raise ValueError("A SQL query is required.")
        return self


class DatabaseJobResult(BaseModel):
    engine: DatabaseEngine
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    affected_rows: int | None = None
    execution_ms: int = 0
    truncated: bool = False
    message: str | None = None
    error: str | None = None
