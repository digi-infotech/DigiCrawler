import pytest
from openpyxl import Workbook, load_workbook

from contact_scraper.excel_io import InputError, detect_url_column, normalize_url, read_sheet, write_results
from contact_scraper.models import SiteResult


def make_xlsx(path, rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(path)
    return path


@pytest.mark.parametrize("raw,expected", [
    ("acme.com", "https://acme.com"),
    ("  WWW.Acme.com/contact ", "https://www.acme.com/contact"),
    ("http://acme.co.uk", "http://acme.co.uk"),
    ("https://acme.com/?a=1", "https://acme.com/?a=1"),
    ("", ""), (None, ""), ("not a url", ""), ("localhost", ""), ("ftp://acme.com", ""),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_detect_by_header_name():
    assert detect_url_column(["Name", "Company Website", "City"], [["a", "b", "c"]]) == 1
    assert detect_url_column(["Name", "URL"], [["a", "b"]]) == 1


def test_detect_by_values():
    rows = [["Acme", "acme.com"], ["Beta", "https://beta.io"], ["Gamma", None]]
    assert detect_url_column(["Name", "Link to"], rows) == 1


def test_detect_preferred_and_missing():
    assert detect_url_column(["Name", "Site Address"], [], "site address") == 1
    with pytest.raises(InputError):
        detect_url_column(["Name", "City"], [["Acme", "Paris"]])


def test_read_and_write_roundtrip(tmp_path):
    src = make_xlsx(tmp_path / "in.xlsx", [
        ["Company", "Website"],
        ["Acme", "acme.com"],
        ["Blank", None],
        ["Beta", "https://beta.io"],
        [None, None],
    ])
    sheet = read_sheet(src)
    assert sheet.urls == ["https://acme.com", "", "https://beta.io"]

    results = [
        SiteResult("https://acme.com", "https://www.acme.com/", address="12 MG Road, Pune, Maharashtra 411001",
                   city="Pune", state="Maharashtra", pin_code="411001", country="India",
                   emails=["a@acme.com", "b@acme.com"],
                   mobiles=["+91 98450 43210", "+91 98450 43211"],
                   phones=["+91 98450 43210", "+91 98450 43211", "+91 22 2345 6789"],
                   pages=["https://www.acme.com/"], status="ok"),
        SiteResult("", status="skipped: no valid website address"),
        SiteResult("https://beta.io", emails=["x@beta.io", "y@beta.io", "z@beta.io"], status="ok"),
    ]
    out = tmp_path / "out.xlsx"
    write_results(out, sheet, results)

    rows = list(load_workbook(out).active.iter_rows(values_only=True))
    # The most emails in any row is 3, so there are 3 email columns; mobiles 2; other phones 1.
    assert rows[0] == ("Company", "Website", "business_name", "website_final", "address", "city", "state",
                       "pin_code", "country", "email_1", "email_2", "email_3", "mobile_1", "mobile_2", "phone_1", "source_pages", "status")
    assert rows[1][4:9] == ("12 MG Road, Pune, Maharashtra 411001", "Pune", "Maharashtra", "411001", "India")
    assert rows[1][9:12] == ("a@acme.com", "b@acme.com", None)                     # one email per cell
    assert rows[1][12:14] == ("+91 98450 43210", "+91 98450 43211")
    assert rows[1][14] == "+91 22 2345 6789"                                       # mobiles not repeated
    assert rows[2][4] is None and rows[2][9] is None and rows[2][16] == "skipped: no valid website address"
    assert rows[3][9:12] == ("x@beta.io", "y@beta.io", "z@beta.io")
    assert [r[0] for r in rows[1:]] == ["Acme", "Blank", "Beta"]  # row order kept


def test_at_least_one_column_per_kind(tmp_path):
    src = make_xlsx(tmp_path / "in.xlsx", [["Website"], ["acme.com"]])
    write_results(tmp_path / "out.xlsx", read_sheet(src), [SiteResult("https://acme.com", status="no_contacts")])
    header = next(load_workbook(tmp_path / "out.xlsx").active.iter_rows(values_only=True))
    assert header == ("Website", "business_name", "website_final", "address", "city", "state", "pin_code", "country",
                      "email_1", "mobile_1", "phone_1", "source_pages", "status")


def test_simple_mode_keeps_businesses_with_email_or_mobile(tmp_path):
    src = make_xlsx(tmp_path / "in.xlsx", [["Business", "Website", "Type"], ["A", "a.in", "Cafe"],
                                           ["B", "b.in", "Shop"], ["C", "c.in", "Hotel"], ["D", "d.in", "Gym"]])
    results = [
        SiteResult("https://a.in", "https://a.in/", business_name="A Cafe", address="1 MG Road", city="Panaji", state="Goa",
                   pin_code="403001", country="India", emails=["a@a.in"], phones=["+91 832 222 3333"], status="ok"),
        SiteResult("https://b.in", phones=["+91 22 2345 6789"], status="ok"),        # landline only: dropped
        SiteResult("https://c.in", status="error: website address not found"),       # failed: dropped
        SiteResult("https://d.in", business_name="D Gym", mobiles=["+91 98450 43210", "+91 98450 43211"],
                   phones=["+91 98450 43210", "+91 98450 43211"], status="ok"),
    ]
    write_results(tmp_path / "out.xlsx", read_sheet(src), results, analytics=False)
    rows = list(load_workbook(tmp_path / "out.xlsx").active.iter_rows(values_only=True))
    assert rows == [
        ("Business", "Website", "Type", "business_name", "city", "state", "pin_code", "country", "email_1", "mobile_1",
         "mobile_2"),
        ("A", "a.in", "Cafe", "A Cafe", "Panaji", "Goa", "403001", "India", "a@a.in", None, None),
        ("D", "d.in", "Gym", "D Gym", None, None, None, None, None, "+91 98450 43210", "+91 98450 43211"),
    ]


def test_row_limit_and_empty(tmp_path):
    big = make_xlsx(tmp_path / "big.xlsx", [["Website"]] + [[f"s{i}.com"] for i in range(11)])
    with pytest.raises(InputError, match="limit"):
        read_sheet(big, max_rows=10)
    empty = make_xlsx(tmp_path / "empty.xlsx", [["Website"]])
    with pytest.raises(InputError, match="empty"):
        read_sheet(empty)


def test_xls_rejected_with_friendly_message(tmp_path):
    p = tmp_path / "old.xls"
    p.write_bytes(b"junk")
    with pytest.raises(InputError, match="Save As"):
        read_sheet(p)


def test_formula_like_text_is_not_a_formula(tmp_path):
    src = make_xlsx(tmp_path / "in.xlsx", [["Website", "Note"], ["acme.com", "x"]])
    sheet = read_sheet(src)
    out = tmp_path / "out.xlsx"
    write_results(out, sheet, [SiteResult("https://acme.com", status="=HYPERLINK(\"http://evil\")")])
    ws = load_workbook(out).active
    cell = ws.cell(2, ws.max_column)  # status
    assert cell.data_type == "s" and not str(cell.value).startswith("=")


def test_email_columns_are_capped(tmp_path):
    src = make_xlsx(tmp_path / "in.xlsx", [["Website"], ["acme.com"]])
    emails = [f"person{i}@acme.com" for i in range(3000)]  # a directory page, not one business
    write_results(tmp_path / "out.xlsx", read_sheet(src), [SiteResult("https://acme.com", emails=emails, status="ok")])
    header = next(load_workbook(tmp_path / "out.xlsx").active.iter_rows(values_only=True))
    assert sum(str(h).startswith("email_") for h in header) == 50


def test_detects_website_column_among_many(tmp_path):
    src = make_xlsx(tmp_path / "in.xlsx", [
        ["S.No", "Business Name", "City", "Web Address", "Owner"],
        [1, "Acme", "Pune", "acme.in", "Ravi"],
    ])
    sheet = read_sheet(src)
    assert sheet.headers[sheet.url_column] == "Web Address"


def test_empty_columns_and_rows_are_cleaned_out(tmp_path):
    """Columns with no name and no values (often left by Excel formatting) and blank rows are dropped;
    a named column stays even when it's empty."""
    src = make_xlsx(tmp_path / "in.xlsx", [
        ["website", None, "Notes", None, "  "],
        ["acme.com", None, None, None, None],
        [None, None, None, None, " "],
        ["beta.io", None, None, None, None],
    ])
    sheet = read_sheet(src)
    assert sheet.headers == ["website", "Notes"]
    assert sheet.rows == [["acme.com", None], ["beta.io", None]]
    assert sheet.urls == ["https://acme.com", "https://beta.io"]
