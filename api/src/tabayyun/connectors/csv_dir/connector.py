"""The `csv_dir` connector (spec 026): a folder on the server that files are dropped into.

It reads no windows (`reads_windows = False`): the ingest job (`tabayyun.services.ingest`) lists
the folder, reads new and changed files with `reader.read_file` and records them. The connector
validates the config and checks the folder.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from tabayyun.connectors.base import Connector, FetchedBatch, PointFailure, PointRef
from tabayyun.connectors.csv_dir import files
from tabayyun.connectors.csv_dir.config import CsvDirConfig
from tabayyun.connectors.errors import ConnectorError, NotSupportedError


class CsvDirConnector(Connector):
    """Files dropped into `path` under the server's file sources folder."""

    type = "csv_dir"
    config_model = CsvDirConfig
    reads_windows = False

    @property
    def settings(self) -> CsvDirConfig:
        """The config as its own model."""
        assert isinstance(self.config, CsvDirConfig)
        return self.config

    def folder(self) -> Path:
        """The source's folder, resolved inside the root; blocking.

        Raises:
            ConnectorError: see `files.folder`.
        """
        return files.folder(self.files_root, self.settings.path)

    def list_files(self, now_ns: int) -> files.Listing:
        """The folder's files a scan may read, and what it skipped; blocking."""
        base = self.folder()
        assert self.files_root is not None  # `folder` refuses an unset root
        try:
            return files.list_files(self.files_root, base, self.settings, now_ns)
        except PermissionError:
            raise ConnectorError(f"folder {self.settings.path} cannot be read", retryable=False) from None

    async def check(self) -> dict[str, Any]:
        """The folder exists inside the root and can be read: how many matching files, how many bytes."""
        listing = await asyncio.to_thread(self.list_files, time.time_ns())
        return {"files": listing.matched, "bytes": listing.bytes}

    async def fetch(
        self, points: Sequence[PointRef], start_ns: int, end_ns: int
    ) -> AsyncIterator[FetchedBatch | PointFailure]:
        """Never: files are ingested as they come, not fetched by window."""
        raise NotSupportedError("csv_dir sources ingest their files; they cannot be fetched by window")
        yield  # pragma: no cover  (makes this an async generator, as the interface wants)
