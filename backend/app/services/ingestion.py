"""Turn pasted text, CSV and XLSX uploads into a validated, de-duplicated brand list.

Original text is preserved; normalisation only decides identity (duplicates) and the
search term. Every row that is not accepted is accounted for in the report.
"""

import csv
import io
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from openpyxl import load_workbook

from app.domain.brand import clean_whitespace, dedupe_key, match_key
from app.schemas import DuplicateRow, IngestionReport, InvalidRow

HEADER_NAMES = {"brand", "brands", "brand name", "brand names", "brandname", "brand_name"}
MAX_XLSX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_SCAN_ROWS = 100_000
MAX_SCAN_COLUMNS = 50


class IngestionError(ValueError):
    """The input as a whole cannot be used; the message is safe to show to the client."""


@dataclass(frozen=True)
class RawRow:
    row: int  # 1-based, matching the line or spreadsheet row the client sees
    text: str


@dataclass(frozen=True)
class AcceptedBrand:
    position: int
    row: int
    original_name: str
    display_name: str
    dedupe_key: str


@dataclass
class IngestionResult:
    accepted: list[AcceptedBrand] = field(default_factory=list)
    report: IngestionReport = field(default_factory=IngestionReport)


def rows_from_list(entries: Iterable[str]) -> list[RawRow]:
    """Pasted brands, one per line. Entries that contain newlines are split into lines."""
    rows: list[RawRow] = []
    for entry in entries:
        for line in entry.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            rows.append(RawRow(len(rows) + 1, line))
    return rows


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _rows_from_grid(grid: Sequence[Sequence[Any]]) -> list[RawRow]:
    """Pick the brand column (by header, else the first column) and skip the header row."""
    column, start = 0, 0
    if grid:
        for index, cell in enumerate(grid[0]):
            if clean_whitespace(_cell_text(cell)).casefold() in HEADER_NAMES:
                column, start = index, 1
                break
    rows = []
    for offset, cells in enumerate(grid[start:], start=start + 1):
        value = cells[column] if column < len(cells) else None
        rows.append(RawRow(offset, _cell_text(value)))
    return rows


def _decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):  # Excel on Windows saves "CSV" as cp1252
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestionError("Could not read the CSV file; save it as UTF-8 and try again.")


def rows_from_csv(data: bytes) -> list[RawRow]:
    text = _decode_csv(data)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    grid = []
    for line_number, record in enumerate(csv.reader(io.StringIO(text), dialect), start=1):
        if line_number > MAX_SCAN_ROWS:
            raise IngestionError(f"The file has more than {MAX_SCAN_ROWS} rows.")
        grid.append(record[:MAX_SCAN_COLUMNS])
    return _rows_from_grid(grid)


def rows_from_xlsx(data: bytes) -> list[RawRow]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise IngestionError("The workbook is too large once decompressed.")
    except zipfile.BadZipFile as exc:
        raise IngestionError("The file is not a valid .xlsx workbook.") from exc

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise IngestionError("The workbook could not be read.") from exc
    try:
        if not workbook.worksheets:
            raise IngestionError("The workbook has no sheets.")
        sheet = workbook.worksheets[0]
        grid = []
        for row_number, cells in enumerate(
            sheet.iter_rows(max_col=MAX_SCAN_COLUMNS, values_only=True), start=1
        ):
            if row_number > MAX_SCAN_ROWS:
                raise IngestionError(f"The sheet has more than {MAX_SCAN_ROWS} rows.")
            grid.append(cells)
        return _rows_from_grid(grid)
    finally:
        workbook.close()


def rows_from_upload(filename: str | None, data: bytes, *, max_bytes: int) -> list[RawRow]:
    if len(data) > max_bytes:
        raise IngestionError(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    if not data.strip():
        raise IngestionError("The file is empty.")
    name = (filename or "").lower()
    if name.endswith(".xlsx") or data[:2] == b"PK":
        return rows_from_xlsx(data)
    if name.endswith(".xls"):
        raise IngestionError("Legacy .xls files are not supported; save as .xlsx or .csv.")
    return rows_from_csv(data)


def ingest(rows: Iterable[RawRow], *, max_brands: int, max_length: int) -> IngestionResult:
    result = IngestionResult()
    first_row_by_key: dict[str, int] = {}

    for raw in rows:
        result.report.total_rows += 1
        display = clean_whitespace(raw.text)
        if not display:
            result.report.empty_rows.append(raw.row)
            continue
        if len(display) > max_length:
            result.report.invalid.append(
                InvalidRow(row=raw.row, original_name=raw.text[:max_length], reason="TOO_LONG")
            )
            continue
        if not match_key(display):
            result.report.invalid.append(
                InvalidRow(row=raw.row, original_name=raw.text, reason="NO_LETTERS_OR_DIGITS")
            )
            continue
        key = dedupe_key(display)
        if key in first_row_by_key:
            result.report.duplicates.append(
                DuplicateRow(
                    row=raw.row, original_name=raw.text, duplicate_of_row=first_row_by_key[key]
                )
            )
            continue
        if len(result.accepted) >= max_brands:
            raise IngestionError(f"Too many brands: the limit is {max_brands} per job.")
        first_row_by_key[key] = raw.row
        result.accepted.append(
            AcceptedBrand(
                position=len(result.accepted),
                row=raw.row,
                original_name=raw.text,
                display_name=display,
                dedupe_key=key,
            )
        )

    result.report.accepted = len(result.accepted)
    if not result.accepted:
        raise IngestionError("No usable brand names were found.")
    return result
