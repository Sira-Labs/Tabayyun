"""File-drop sources (spec 026): CSV and Parquet files dropped into a folder on the server."""

from tabayyun.connectors.csv_dir.config import CsvDirConfig
from tabayyun.connectors.csv_dir.connector import CsvDirConnector
from tabayyun.connectors.csv_dir.files import NOT_ENABLED, FoundFile, Listing
from tabayyun.connectors.csv_dir.reader import FileReadError, FileRows, TooManySeriesError, read_file

__all__ = [
    "NOT_ENABLED",
    "CsvDirConfig",
    "CsvDirConnector",
    "FileReadError",
    "FileRows",
    "FoundFile",
    "Listing",
    "TooManySeriesError",
    "read_file",
]
