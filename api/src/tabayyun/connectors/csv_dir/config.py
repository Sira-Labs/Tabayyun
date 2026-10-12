"""The `csv_dir` source's config (spec 026)."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from tabayyun.connectors.base import ConnectorConfig

MB = 1024 * 1024


class CsvDirConfig(ConnectorConfig):
    """Where the files are and how to read them."""

    settle_s: int = Field(default=60, ge=0, le=86_400)
    path: str = Field(min_length=1, max_length=500)
    recursive: bool = False
    pattern: str = Field(default="*", min_length=1, max_length=200)
    layout: Literal["long", "wide"] = "long"
    ts_column: str = Field(default="ts", min_length=1, max_length=200)
    ts_unit: Literal["auto", "s", "ms", "us", "ns"] = "auto"
    series_column: str | None = Field(default="series", min_length=1, max_length=200)
    value_column: str | None = Field(default="value", min_length=1, max_length=200)
    quality_column: str | None = Field(default=None, min_length=1, max_length=200)
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    max_file_mb: int = Field(default=256, ge=1, le=4096)
    max_files_per_scan: int = Field(default=100, ge=1, le=1000)
    max_series_per_file: int = Field(default=1000, ge=1, le=10_000)

    @field_validator("path")
    @classmethod
    def _relative(cls, path: str) -> str:
        """A `/`-separated path inside the root: no absolute path, `..`, backslash or control character."""
        if any(ord(c) < 32 or c == "\x7f" for c in path) or "\\" in path:
            raise ValueError("must not contain backslashes or control characters")
        pure = PurePosixPath(path)
        if pure.is_absolute():
            raise ValueError("must be relative to the server's file sources folder")
        if ".." in pure.parts:
            raise ValueError("must not contain '..'")
        return str(pure)

    @field_validator("delimiter")
    @classmethod
    def _delimiter(cls, value: str) -> str:
        if value in ('"', "\n", "\r"):
            raise ValueError("must not be a quote or a line break")
        return value

    @model_validator(mode="after")
    def _columns(self) -> Self:
        """A long layout names its series and value columns, all distinct from the timestamp."""
        if self.layout == "long":
            if self.series_column is None or self.value_column is None:
                raise ValueError("a long layout needs series_column and value_column")
            named = [self.ts_column, self.series_column, self.value_column]
            if self.quality_column is not None:
                named.append(self.quality_column)
            if len(set(named)) != len(named):
                raise ValueError("ts_column, series_column, value_column and quality_column must differ")
        return self

    @property
    def max_file_bytes(self) -> int:
        """`max_file_mb` in bytes."""
        return self.max_file_mb * MB
