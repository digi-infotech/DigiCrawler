"""Reading the input spreadsheet and writing the results spreadsheet."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import SiteResult

URL_HEADER_NAMES = {
    "url", "urls", "website", "websites", "site", "domain", "web", "homepage",
    "websiteurl", "weburl", "siteurl", "webaddress", "websiteaddress", "link", "companywebsite",
}
DOMAIN_LIKE_RE = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,24}(:\d+)?(/\S*)?$", re.IGNORECASE)
# Result columns: business_name, website_final, address, city, state, pin_code, country, email_1..N, mobile_1..N, phone_1..N,
# source_pages, status. N is the most any row has (at least 1), so a third email anywhere adds email_3.
COLUMNS_BEFORE = ["business_name", "website_final", "address", "city", "state", "pin_code", "country"]
COLUMNS_AFTER = ["source_pages", "status"]
SPLIT_KINDS = ("email", "mobile", "phone")
# Analytics mode off: only what a salesperson needs, and only rows with an email or a mobile.
SIMPLE_BEFORE = ["business_name", "address", "city", "state", "pin_code", "country"]
SIMPLE_KINDS = ("email", "mobile")
MAX_PER_KIND = 50  # a site listing more is a directory, not one business; extra values are dropped
EXCEL_CELL_LIMIT = 32_000


class InputError(Exception):
    """Problem with the uploaded file, worded for a non-technical user."""


@dataclass
class Sheet:
    headers: list[str]
    rows: list[list[object]]
    url_column: int

    @property
    def urls(self) -> list[str]:
        return [normalize_url(row[self.url_column] if self.url_column < len(row) else None) for row in self.rows]


def _norm_header(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def normalize_url(value: object) -> str:
    """'Acme.com ' -> 'https://acme.com'. Returns '' when it isn't a usable web address."""
    if value is None:
        return ""
    text = str(value).strip().strip("<>\"'")
    if not text or " " in text:
        return ""
    if not re.match(r"^https?://", text, re.IGNORECASE):
        if "://" in text:
            return ""
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if "." not in host:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{host}{port}{parsed.path or ''}" + (f"?{parsed.query}" if parsed.query else "")


def detect_url_column(headers: list[str], rows: list[list[object]], preferred: str | None = None) -> int:
    if preferred:
        for i, h in enumerate(headers):
            if _norm_header(h) == _norm_header(preferred):
                return i
        raise InputError(f"Couldn't find a column called '{preferred}'. Columns in your file: {', '.join(map(str, headers))}.")

    normed = [_norm_header(h) for h in headers]
    for i, h in enumerate(normed):
        if h in URL_HEADER_NAMES:
            return i
    for i, h in enumerate(normed):
        if "website" in h or "url" in h or "domain" in h:
            return i

    sample = rows[:50]
    for i in range(len(headers)):
        values = [str(r[i]).strip() for r in sample if i < len(r) and r[i] not in (None, "")]
        if values and sum(bool(DOMAIN_LIKE_RE.match(v)) for v in values) / len(values) >= 0.5:
            return i

    raise InputError("Couldn't find a column with website addresses. Please name that column 'Website' and try again.")


def read_sheet(path: str | Path, url_column: str | None = None, max_rows: int | None = None) -> Sheet:
    headers, rows = load_rows(path, max_rows)
    col = detect_url_column(headers, rows, url_column)
    return Sheet(headers=headers, rows=rows, url_column=col)


def describe_columns(headers: list[str], rows: list[list[object]]) -> list[dict]:
    """For the upload page: how many usable website addresses each column holds, with examples."""
    out = []
    for i, header in enumerate(headers):
        urls = [normalize_url(r[i]) for r in rows if i < len(r)]
        valid = [u for u in urls if u]
        out.append({"name": header, "valid": len(valid), "samples": valid[:4]})
    return out


def label_column(headers: list[str], rows: list[list[object]], url_column: int) -> int | None:
    """A column that names each row (e.g. 'Company'), to show next to the website."""
    for i in range(len(headers)):
        if i == url_column:
            continue
        values = [r[i] for r in rows[:50] if r[i] not in (None, "")]
        if values and sum(isinstance(v, str) and not v.strip().isdigit() for v in values) / len(values) > 0.8:
            return i
    return None


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _blank_row(row: list[object]) -> bool:
    return all(_blank(v) for v in row)


def load_rows(path: str | Path, max_rows: int | None = None) -> tuple[list[str], list[list[object]]]:
    path = Path(path)
    if path.suffix.lower() == ".xls":
        raise InputError("This is an old Excel (.xls) file. Please open it in Excel and 'Save As' .xlsx, then try again.")
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise InputError("Couldn't open this file. Please upload an Excel file (.xlsx).") from exc
    try:
        ws = wb.active
        all_rows = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()

    # Clean up: Excel often reports columns and rows that only ever held formatting. Drop rows that are
    # completely blank, and columns with neither a name nor any value, so they don't reach the output file.
    all_rows = [r for r in all_rows if not _blank_row(r)]
    if len(all_rows) < 2:
        raise InputError("The file looks empty. Put column names in the first row and one website per row below it.")
    width = max(len(r) for r in all_rows)
    all_rows = [r + [None] * (width - len(r)) for r in all_rows]
    keep = [i for i in range(width) if any(not _blank(r[i]) for r in all_rows)]
    all_rows = [[r[i] for i in keep] for r in all_rows]

    width = len(keep)
    headers = [str(h).strip() if h not in (None, "") else f"Column {i + 1}" for i, h in enumerate(all_rows[0] + [None] * (width - len(all_rows[0])))]
    rows = [r + [None] * (width - len(r)) for r in all_rows[1:]]

    if max_rows and len(rows) > max_rows:
        raise InputError(f"The file has {len(rows):,} rows. The limit is {max_rows:,} per file — please split it into smaller files.")
    return headers, rows


def _safe(value: object) -> object:
    """Stop text that starts with '=' being treated as an Excel formula."""
    if isinstance(value, str):
        if value.startswith("="):
            value = "'" + value
        return value[:EXCEL_CELL_LIMIT]
    return value


def _split_values(res: SiteResult) -> dict[str, list[str]]:
    """The values that get one column each: emails, mobiles and the other (non-mobile) phones."""
    return {"email": res.emails, "mobile": res.mobiles, "phone": res.landlines}


def has_email_or_mobile(res: SiteResult) -> bool:
    """Rows kept in the simple (non-analytics) file."""
    return bool(res.emails or res.mobiles)


def output_columns(results: list[SiteResult], analytics: bool = True) -> tuple[list[str], dict[str, int]]:
    """The result column names, and how many numbered columns each kind gets."""
    kinds = SPLIT_KINDS if analytics else SIMPLE_KINDS
    counts = {kind: min(MAX_PER_KIND, max([1] + [len(_split_values(r)[kind]) for r in results])) for kind in kinds}
    numbered = [f"{kind}_{i}" for kind in kinds for i in range(1, counts[kind] + 1)]
    before, after = (COLUMNS_BEFORE, COLUMNS_AFTER) if analytics else (SIMPLE_BEFORE, [])
    return before + numbered + after, counts


def write_results(path: str | Path, sheet: Sheet, results: list[SiteResult], analytics: bool = True) -> None:
    """Analytics on: every row and every column. Off: only rows with an email or a mobile, and only
    the business name, city, state, PIN code, country, emails and mobiles next to the original columns."""
    pairs = list(zip(sheet.rows, results))
    if not analytics:
        pairs = [(row, res) for row, res in pairs if has_email_or_mobile(res)]
    columns, counts = output_columns([res for _, res in pairs], analytics)
    wb = Workbook()
    ws = wb.active
    ws.title = "Contacts"
    ws.append(sheet.headers + columns)

    for row, res in pairs:
        values: dict[str, object] = {
            "business_name": res.business_name, "website_final": res.final_url, "address": res.address,
            "city": res.city, "state": res.state, "pin_code": res.pin_code, "country": res.country, "source_pages": ", ".join(res.pages), "status": res.status,
        }
        for kind, found in _split_values(res).items():
            values.update({f"{kind}_{i}": v for i, v in enumerate(found[:counts.get(kind, 0)], start=1)})
        ws.append([_safe(v) for v in row] + [_safe(values.get(c) or None) for c in columns])

    header_font = Font(bold=True)
    new_fill = PatternFill("solid", fgColor="E8F0FE")
    first_new = len(sheet.headers) + 1
    for cell in ws[1]:
        cell.font = header_font
        if cell.column >= first_new:
            cell.fill = new_fill
    ws.freeze_panes = "A2"
    widths = {"business_name": 32, "website_final": 32, "address": 50, "city": 18, "state": 18, "pin_code": 12, "country": 16, "source_pages": 45,
              "status": 24}
    for i, header in enumerate(sheet.headers + columns, start=1):
        kind = str(header).rsplit("_", 1)[0] if i >= first_new else ""
        width = widths.get(header) if i >= first_new else None
        width = width or (30 if kind == "email" else 20 if kind in ("mobile", "phone") else max(12, min(40, len(str(header)) + 4)))
        ws.column_dimensions[get_column_letter(i)].width = width

    wb.save(path)
