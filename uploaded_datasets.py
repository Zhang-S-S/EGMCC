"""Parsing and lifecycle support for user-uploaded edge-list datasets."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
from typing import Iterator
import uuid


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
UPLOAD_RETENTION = timedelta(hours=24)
LOGGER = logging.getLogger(__name__)


class DatasetFormatError(ValueError):
    """An uploaded edge list cannot be parsed safely."""

    def __init__(self, message: str, line_number: int | None = None):
        self.line_number = line_number
        prefix = f"Line {line_number}: " if line_number is not None else ""
        super().__init__(prefix + message)


class UploadValidationError(ValueError):
    """Upload metadata or file type is not accepted."""


class UploadTooLargeError(UploadValidationError):
    """Upload exceeds the configured file-size limit."""


@dataclass(frozen=True)
class DatasetInspection:
    node_count: int
    edge_count: int
    format: str


def _split_row(line: str, format_name: str, line_number: int) -> list[str]:
    if format_name == "csv":
        try:
            return next(csv.reader([line]))
        except csv.Error as error:
            raise DatasetFormatError(f"invalid CSV ({error})", line_number) from error
    return line.split()


def _detect_format(path: Path, format_hint: str) -> str:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                preferred = "csv" if format_hint == "csv" else "whitespace"
                fallback = "whitespace" if preferred == "csv" else "csv"
                if len(_split_row(line, preferred, line_number)) >= 2:
                    return preferred
                if len(_split_row(line, fallback, line_number)) >= 2:
                    return fallback
                return preferred
    except UnicodeDecodeError as error:
        raise DatasetFormatError("file must use UTF-8 encoding") from error
    return "csv" if format_hint == "csv" else "whitespace"


def _iter_edges(
    path: Path,
    format_name: str,
    header: bool,
    weighted: bool,
) -> Iterator[tuple[str, str, float | None]]:
    skipped_header = not header
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except UnicodeDecodeError as error:
        raise DatasetFormatError("file must use UTF-8 encoding") from error
    with stream:
        try:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if not skipped_header:
                    skipped_header = True
                    continue
                row = _split_row(line, format_name, line_number)
                if len(row) < 2:
                    raise DatasetFormatError("expected source and target columns", line_number)
                source, target = row[0].strip(), row[1].strip()
                if not source or not target:
                    raise DatasetFormatError("source and target must not be empty", line_number)
                weight = None
                if weighted:
                    if len(row) < 3 or not row[2].strip():
                        raise DatasetFormatError("expected a weight in column three", line_number)
                    try:
                        weight = float(row[2])
                    except ValueError as error:
                        raise DatasetFormatError("weight must be a number", line_number) from error
                    if not math.isfinite(weight):
                        raise DatasetFormatError("weight must be finite", line_number)
                yield source, target, weight
        except UnicodeDecodeError as error:
            raise DatasetFormatError("file must use UTF-8 encoding") from error


def inspect_edge_list(path: Path, format_hint: str, header: bool, weighted: bool) -> DatasetInspection:
    format_name = _detect_format(path, format_hint)
    nodes: set[str] = set()
    edge_count = 0
    for source, target, _ in _iter_edges(path, format_name, header, weighted):
        nodes.add(source)
        nodes.add(target)
        edge_count += 1
    if edge_count == 0:
        raise DatasetFormatError("dataset must contain at least one edge")
    return DatasetInspection(len(nodes), edge_count, format_name)


def read_edge_list(
    path: Path,
    format_name: str,
    header: bool,
    weighted: bool,
) -> tuple[list[str], list[tuple[str, str, float | None]]]:
    edges = list(_iter_edges(path, format_name, header, weighted))
    nodes = sorted({node for source, target, _ in edges for node in (source, target)})
    if not edges:
        raise DatasetFormatError("dataset must contain at least one edge")
    return nodes, edges


class UploadedDatasetStore:
    """Persist validated uploads with an expiring in-memory registry."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = MAX_UPLOAD_BYTES,
        retention: timedelta = UPLOAD_RETENTION,
        now=None,
    ):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.retention = retention
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.records: dict[str, dict] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._load()

    def _data_path(self, dataset_id: str, stored_filename: str) -> Path:
        if not re.fullmatch(r"upload-[0-9a-f]{16}", dataset_id):
            raise ValueError("Invalid uploaded dataset ID.")
        stored_path = Path(stored_filename)
        if (
            stored_path.name != stored_filename
            or stored_path.stem != dataset_id
            or stored_path.suffix.lower() not in {".csv", ".txt"}
        ):
            raise ValueError("Invalid uploaded dataset path.")
        return self.root / stored_filename

    def _load(self) -> None:
        for metadata_path in self.root.glob("upload-*.json"):
            try:
                record = json.loads(metadata_path.read_text(encoding="utf-8"))
                dataset_id = record["id"]
                data_path = self._data_path(dataset_id, record["stored_filename"])
                if metadata_path.name != f"{dataset_id}.json" or not data_path.is_file():
                    continue
                if datetime.fromisoformat(record["expires_at"]) <= self.now():
                    data_path.unlink(missing_ok=True)
                    metadata_path.unlink(missing_ok=True)
                    continue
                self.records[dataset_id] = record
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue

    @staticmethod
    def _public(record: dict) -> dict:
        return {
            "id": record["id"],
            "label": record["label"],
            "short_label": record["label"],
            "directed": record["directed"],
            "weighted": record["weighted"],
            "nodes": record["nodes"],
            "edges": record["edges"],
            "source": "uploaded",
            "expires_at": record["expires_at"],
        }

    def cleanup(self, protected_ids: set[str] | None = None) -> int:
        protected = protected_ids or set()
        removed = 0
        with self.lock:
            for dataset_id, record in list(self.records.items()):
                if dataset_id in protected:
                    continue
                if datetime.fromisoformat(record["expires_at"]) > self.now():
                    continue
                try:
                    self._data_path(dataset_id, record["stored_filename"]).unlink(missing_ok=True)
                    (self.root / f"{dataset_id}.json").unlink(missing_ok=True)
                except OSError as error:
                    LOGGER.warning("Unable to remove expired upload %s: %s", dataset_id, error)
                del self.records[dataset_id]
                removed += 1
        return removed

    def list_public(self, protected_ids: set[str] | None = None) -> list[dict]:
        self.cleanup(protected_ids)
        with self.lock:
            return [self._public(record) for record in self.records.values()]

    def resolve(self, dataset_id: str) -> dict | None:
        with self.lock:
            record = self.records.get(dataset_id)
            if record is None:
                return None
            data_path = self._data_path(dataset_id, record["stored_filename"])
            if not data_path.is_file():
                return None
            return {
                **record,
                "path": data_path,
                "uploaded": True,
                "node_type": "string",
            }

    def create(
        self,
        stream,
        *,
        filename: str,
        name: str,
        directed: bool,
        header: bool,
        weighted: bool,
    ) -> dict:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".csv", ".txt"}:
            raise UploadValidationError("Only CSV and TXT files are supported.")
        label = name.strip()
        if not label:
            raise UploadValidationError("Dataset name is required.")
        if len(label) > 100:
            raise UploadValidationError("Dataset name must be 100 characters or fewer.")

        dataset_id = f"upload-{uuid.uuid4().hex[:16]}"
        stored_filename = dataset_id + suffix
        final_path = self.root / stored_filename
        temporary_path = self.root / f".{dataset_id}.part"
        metadata_path = self.root / f"{dataset_id}.json"
        metadata_temporary_path = self.root / f".{dataset_id}.json.part"
        size = 0
        try:
            with temporary_path.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UploadTooLargeError(
                            f"File exceeds the {self.max_bytes // (1024 * 1024)} MB limit."
                        )
                    output.write(chunk)
            if size == 0:
                raise UploadValidationError("Uploaded file is empty.")

            inspection = inspect_edge_list(
                temporary_path,
                "csv" if suffix == ".csv" else "txt",
                header,
                weighted,
            )
            created_at = self.now()
            record = {
                "id": dataset_id,
                "label": label,
                "stored_filename": stored_filename,
                "original_filename": Path(filename).name,
                "created_at": created_at.isoformat(),
                "expires_at": (created_at + self.retention).isoformat(),
                "directed": bool(directed),
                "weighted": bool(weighted),
                "nodes": inspection.node_count,
                "edges": inspection.edge_count,
                "format": inspection.format,
                "header": bool(header),
            }
            metadata_temporary_path.write_text(
                json.dumps(record, separators=(",", ":"), ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary_path, final_path)
            os.replace(metadata_temporary_path, metadata_path)
            with self.lock:
                self.records[dataset_id] = record
            return self._public(record)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            metadata_temporary_path.unlink(missing_ok=True)
            if not metadata_path.exists():
                final_path.unlink(missing_ok=True)
            raise
