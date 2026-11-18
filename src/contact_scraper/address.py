"""State and country of a business, read from its postal address.

Only real address clues count:
  * schema.org data (JSON-LD `PostalAddress`, or microdata `itemprop="addressRegion"`),
  * an address written on the page: "Austin, TX 78701", "Toronto, ON M5V 2T6", "Sydney NSW 2000",
    "Pune, Maharashtra 411001, India", "London SW1A 1AA".
The domain ending or phone numbers are NOT used: when no address is found, both stay blank.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable

import phonenumbers
from bs4 import BeautifulSoup

try:  # English country names ship with phonenumbers, so no extra dependency is needed
    from phonenumbers.geocoder import _region_display_name
except ImportError:  # pragma: no cover
    def _region_display_name(code: str, lang: str) -> str:
        return ""

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
}
AU_STATES = {
    "ACT": "Australian Capital Territory", "NSW": "New South Wales", "NT": "Northern Territory",
    "QLD": "Queensland", "SA": "South Australia", "TAS": "Tasmania", "VIC": "Victoria", "WA": "Western Australia",
}
IN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    # Union territories
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]
IN_ALIASES = {
    "New Delhi": "Delhi", "NCT of Delhi": "Delhi", "Orissa": "Odisha", "Pondicherry": "Puducherry",
    "Uttaranchal": "Uttarakhand", "Tamilnadu": "Tamil Nadu", "Chhatisgarh": "Chhattisgarh",
    "Andaman and Nicobar": "Andaman and Nicobar Islands",
}
# Indian state codes (vehicle / ISO 3166-2 style), trusted only in schema.org data that says India.
IN_CODES = {
    "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam", "BR": "Bihar", "CG": "Chhattisgarh",
    "CT": "Chhattisgarh", "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana", "HP": "Himachal Pradesh",
    "JH": "Jharkhand", "KA": "Karnataka", "KL": "Kerala", "MP": "Madhya Pradesh", "MH": "Maharashtra",
    "MN": "Manipur", "ML": "Meghalaya", "MZ": "Mizoram", "NL": "Nagaland", "OD": "Odisha", "OR": "Odisha",
    "PB": "Punjab", "RJ": "Rajasthan", "SK": "Sikkim", "TN": "Tamil Nadu", "TG": "Telangana", "TS": "Telangana",
    "TR": "Tripura", "UP": "Uttar Pradesh", "UK": "Uttarakhand", "UT": "Uttarakhand", "WB": "West Bengal",
    "AN": "Andaman and Nicobar Islands", "CH": "Chandigarh", "DL": "Delhi", "JK": "Jammu and Kashmir",
    "LA": "Ladakh", "LD": "Lakshadweep", "PY": "Puducherry",
}
REGIONS_BY_COUNTRY = {"US": US_STATES, "CA": CA_PROVINCES, "AU": AU_STATES}

# UK: the postcode area tells the nation.
UK_AREAS = set((
    "AB AL B BA BB BD BH BL BN BR BS BT CA CB CF CH CM CO CR CT CV CW DA DD DE DG DH DL DN DT DY E EC EH EN EX "
    "FK FY G GL GU HA HD HG HP HR HS HU HX IG IP IV KA KT KW KY L LA LD LE LL LN LS LU M ME MK ML N NE NG NN NP "
    "NR NW OL OX PA PE PH PL PO PR RG RH RM S SA SE SG SK SL SM SN SO SP SR SS ST SW SY TA TD TF TN TQ TR TS TW "
    "UB W WA WC WD WF WN WR WS WV YO ZE").split())
SCOTLAND_AREAS = set("AB DD DG EH FK G HS IV KA KW KY ML PA PH TD ZE".split())
WALES_AREAS = set("CF LD LL NP SA".split())

STRUCTURED = 5   # schema.org data: the site states it explicitly
WRITTEN = 3      # an address pattern in the visible text


def _key(name: str) -> str:
    """'Jammu & Kashmir', 'jammu and  kashmir' and 'TamilNadu' compare equal to their proper names."""
    return re.sub(r"\s+", "", name.lower().replace("&", "and"))


def _names_re(names: Iterable[str]) -> str:
    """Alternation of names, longest first, tolerant of spacing and 'and' vs '&'."""
    parts = []
    for name in sorted(names, key=len, reverse=True):
        words = ["(?:and|&)" if w.lower() == "and" else re.escape(w) for w in name.split()]
        parts.append(r"\s*".join(words))
    return "|".join(parts)


_US_ABBR = "|".join(US_STATES)
_US_NAMES = _names_re(US_STATES.values())
_ZIP = r"\d{5}(?:-\d{4})?(?![\d-])"
US_ABBR_ZIP_RE = re.compile(r"[A-Za-z.]{2,},?\s+(%s)\.?,?\s+%s" % (_US_ABBR, _ZIP))           # "Austin, TX 78701"
US_NAME_ZIP_RE = re.compile(r"\b(%s),?\s+%s" % (_US_NAMES, _ZIP))                             # "Austin, Texas 78701"
_CA_POSTAL = r"[A-Z]\d[A-Z]\s?\d[A-Z]\d\b"
CA_ABBR_RE = re.compile(r"[A-Za-z.]{2,},?\s+(%s)\.?,?\s+%s" % ("|".join(CA_PROVINCES), _CA_POSTAL))
CA_NAME_RE = re.compile(r"\b(%s),?\s+%s" % (_names_re(CA_PROVINCES.values()), _CA_POSTAL))
AU_ABBR_RE = re.compile(r"[A-Za-z.]{2,},?\s+(%s),?\s+\d{4}(?!\d)" % "|".join(AU_STATES))       # "Sydney NSW 2000"
UK_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2})\d[A-Z\d]?\s\d[A-Z]{2}\b")
IN_PIN_RE = re.compile(r"(?<![\d-])[1-9]\d{2}\s?\d{3}(?![\d-])")
_IN_ALL = list(IN_STATES) + list(IN_ALIASES)
IN_STATE_RE = re.compile(r"\b(%s)\b" % _names_re(_IN_ALL), re.IGNORECASE)
IN_STATE_THEN_INDIA_RE = re.compile(r"\b(%s)\b[\s,.-]*(?:[1-9]\d{2}\s?\d{3}[\s,.-]*)?India\b" % _names_re(_IN_ALL),
                                    re.IGNORECASE)
INDIA_RE = re.compile(r"\bIndia\b", re.IGNORECASE)

# Country names (and a few common short forms) -> ISO code, for schema.org "addressCountry".
COUNTRY_ALIASES = {"usa": "US", "u.s.a.": "US", "u.s.": "US", "united states of america": "US", "america": "US",
                   "uk": "GB", "u.k.": "GB", "great britain": "GB", "britain": "GB", "england": "GB",
                   "scotland": "GB", "wales": "GB", "northern ireland": "GB", "uae": "AE", "bharat": "IN"}


def country_name(code: str) -> str:
    return _region_display_name(code, "en") if code else ""


_NAME_TO_CODE: dict[str, str] = {}


def country_code(value: str) -> str:
    """'IN', 'India', 'USA', 'United Kingdom' -> ISO code; '' when unknown."""
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip(".,")
    if not text:
        return ""
    if len(text) == 2 and text.isalpha():
        code = text.upper()
        code = "GB" if code == "UK" else code
        return code if code in phonenumbers.SUPPORTED_REGIONS else ""
    if not _NAME_TO_CODE:
        for code in phonenumbers.SUPPORTED_REGIONS:
            name = country_name(code)
            if name:
                _NAME_TO_CODE[name.lower()] = code
        _NAME_TO_CODE.update(COUNTRY_ALIASES)
    return _NAME_TO_CODE.get(text.lower(), "")


def _india_state(name: str) -> str:
    key = _key(name)
    for state in IN_STATES:
        if _key(state) == key:
            return state
    for alias, state in IN_ALIASES.items():
        if _key(alias) == key:
            return state
    return ""


def _full_state(value: str, country: str) -> tuple[str, str]:
    """Normalise a region name or code. Returns (state, country code it implies, if any)."""
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip(".,")
    if not text or len(text) > 60:
        return "", country
    regions = REGIONS_BY_COUNTRY.get(country)
    if regions:
        if text.upper() in regions:
            return regions[text.upper()], country
        for name in regions.values():
            if _key(name) == _key(text):
                return name, country
    if country == "IN" and text.upper() in IN_CODES:
        return IN_CODES[text.upper()], country
    if country in ("", "IN"):
        state = _india_state(text)
        if state:
            return state, "IN"
    if not country:
        for code, regions in REGIONS_BY_COUNTRY.items():
            for name in regions.values():
                if _key(name) == _key(text):
                    return name, code
    return text, country


def _uk_nation(area: str) -> str:
    if area == "BT":
        return "Northern Ireland"
    if area in SCOTLAND_AREAS:
        return "Scotland"
    if area in WALES_AREAS:
        return "Wales"
    return "England"


Clue = tuple[str, str, int]  # (state, country code, weight); either part may be ""


Match = tuple[str, str, int, int]  # (state, country code, start, end) of an address pattern in the text


def _written(text: str) -> list[Match]:
    """Address patterns written on the page, with where they are."""
    found: list[Match] = []
    for m in US_ABBR_ZIP_RE.finditer(text):
        found.append((US_STATES[m.group(1)], "US", m.start(), m.end()))
    for m in US_NAME_ZIP_RE.finditer(text):
        found.append((_full_state(m.group(1), "US")[0], "US", m.start(), m.end()))
    for m in CA_ABBR_RE.finditer(text):
        found.append((CA_PROVINCES[m.group(1)], "CA", m.start(), m.end()))
    for m in CA_NAME_RE.finditer(text):
        found.append((_full_state(m.group(1), "CA")[0], "CA", m.start(), m.end()))
    for m in AU_ABBR_RE.finditer(text):
        found.append((AU_STATES[m.group(1)], "AU", m.start(), m.end()))
    for m in UK_POSTCODE_RE.finditer(text):
        if m.group(1) in UK_AREAS:
            found.append((_uk_nation(m.group(1)), "GB", m.start(), m.end()))

    # India: a 6-digit PIN with a state name close by (the nearest one wins), or "<State>, India".
    india_spans: list[tuple[int, int]] = []
    for m in IN_PIN_RE.finditer(text):
        start, end = max(0, m.start() - 100), min(len(text), m.end() + 40)
        window = text[start:end]
        states = [(abs(start + s.start() - m.start()), _india_state(s.group(1))) for s in IN_STATE_RE.finditer(window)]
        if states:
            found.append((min(states)[1], "IN", m.start(), m.end()))
            india_spans.append((start, end))
        elif INDIA_RE.search(window):
            found.append(("", "IN", m.start(), m.end()))
            india_spans.append((start, end))
    for m in IN_STATE_THEN_INDIA_RE.finditer(text):
        if not any(s <= m.start() < e for s, e in india_spans):  # not already counted with its PIN
            found.append((_india_state(m.group(1)), "IN", m.start(), m.end()))
    return found


def clues_from_text(text: str) -> list[Clue]:
    """Address patterns written on the page."""
    return [(state, code, WRITTEN) for state, code, _, _ in _written(text)]


def clues_from_schema(tree: BeautifulSoup) -> list[Clue]:
    """schema.org PostalAddress in JSON-LD, or microdata on the page."""
    found: list[Clue] = []

    def add(region: object, country: object) -> None:
        if isinstance(country, dict):
            country = country.get("name") or country.get("@id") or ""
        if isinstance(region, dict):
            region = region.get("name") or ""
        code = country_code(str(country or ""))
        state, code = _full_state(str(region or ""), code)
        if state or code:
            found.append((state, code, STRUCTURED))

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            keys = {str(k).lower(): v for k, v in obj.items()}
            if "addressregion" in keys or "addresscountry" in keys:
                add(keys.get("addressregion"), keys.get("addresscountry"))
            if isinstance(keys.get("address"), str):  # "address": "350 5th Ave, New York, NY 10118"
                found.extend((s, c, STRUCTURED) for s, c, _ in clues_from_text(keys["address"]))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for node in tree.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk(json.loads(node.string or node.get_text() or ""))
        except (json.JSONDecodeError, ValueError):
            continue

    def prop(node, name: str) -> str:
        el = node.find(attrs={"itemprop": name})
        return (el.get("content") or el.get_text(" ", strip=True)) if el else ""

    for node in tree.find_all(attrs={"itemtype": re.compile(r"PostalAddress", re.IGNORECASE)}):
        add(prop(node, "addressRegion"), prop(node, "addressCountry"))
    return found


def detect_location(pages: Iterable[tuple[BeautifulSoup, str]]) -> tuple[str, str]:
    """(state, country name) from the address on a site's pages; blanks when there is none."""
    clues: list[Clue] = []
    for tree, text in pages:
        clues += clues_from_schema(tree)
        clues += clues_from_text(text[:300_000])

    countries: Counter[str] = Counter()
    for _, code, weight in clues:
        if code:
            countries[code] += weight
    country = countries.most_common(1)[0][0] if countries else ""

    states: Counter[str] = Counter()
    for state, code, weight in clues:
        if state and code in (country, ""):
            states[state] += weight
    state = states.most_common(1)[0][0] if states else ""
    return state, country_name(country)


# ------------------------------------------------------------------ full address text

ADDRESS_MAX = 300
_STREET_WORD_RE = re.compile(
    r"\b(?:road|rd|street|st|avenue|ave|lane|ln|boulevard|blvd|drive|highway|hwy|place|court|square|floor|flr|"
    r"suite|ste|unit|plot|building|bldg|tower|complex|plaza|centre|center|nagar|marg|sector|phase|block|colony|"
    r"chowk|near|opp|opposite|behind|apartment|apt|industrial|estate|cross|po\s?box|p\.o\.)\b|\bno\.\s*\d",
    re.IGNORECASE)
_LABEL = (r"(?:(?:our|office|postal|mailing|registered|regd\.?|head|corporate|business|store|shop|factory|"
          r"studio|branch|main|full|street)\s+)*(?:address|office|hq|headquarters|location|visit\s+us|find\s+us)")
LABEL_RE = re.compile(r"\b%s\s*(?::|–|—|-\s|(?<=us)\s+at\s)\s*" % _LABEL, re.IGNORECASE)  # "Regd. Office: ..."
LABEL_LINE_RE = re.compile(r"^\s*%s\s*:?\s*$" % _LABEL, re.IGNORECASE)          # a heading line: "Our Address"
# Where an address stops: contact labels, separators, or the start of the next sentence.
_TAIL_LABEL_RE = re.compile(
    r"[|•·]|\b(?:phone|tel|telephone|mobile|mob|cell|call|email|e-mail|fax|ph|whatsapp|gst|gstin|cin|"
    r"open|hours|timings?)\b", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(?<=[a-z]{4})[.!?]\s+(?=[A-Z])")
_PHONE_RE = re.compile(r"\+?\(?\d[\d\s().-]{6,}\d")
_SKIP_LINE_RE = re.compile(r"©|copyright|all rights|https?:|www\.|@", re.IGNORECASE)
_HOURS_RE = re.compile(r"\b\d{1,2}(?:[:.]\d\d)?\s*(?:am|pm)\b|\b(?:mon|tue|wed|thu|fri|sat|sun)"
                       r"(?:day|sday|nesday|rsday|urday)?\b", re.IGNORECASE)

Candidate = tuple[int, int, str, str]  # (rank: lower is more trusted, order found, address, country code)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or ""))
    text = re.sub(r"\s+([,.])", r"\1", text)
    text = re.sub(r",(\s*,)+", ",", text)
    text = text.strip(" ,;:-–—|•·.")
    if len(text) > ADDRESS_MAX:
        text = text[:ADDRESS_MAX].rsplit(",", 1)[0]
    return text


def _phone_like(line: str) -> bool:
    return any(len(re.sub(r"\D", "", m.group())) >= 8 for m in _PHONE_RE.finditer(line))


def _is_address_line(line: str) -> bool:
    """A short line next to an address that is part of it ("12 MG Road," / "350 5th Ave")."""
    if not line or len(line) > 80 or ":" in line or _SKIP_LINE_RE.search(line) or _phone_like(line):
        return False
    if _TAIL_LABEL_RE.search(line) or LABEL_LINE_RE.match(line):
        return False
    return line.endswith((",", "-")) or bool(_STREET_WORD_RE.search(line)) or (
        "," in line and any(c.isdigit() for c in line))


def _is_country(line: str) -> bool:
    return len(line) <= 40 and bool(country_code(line.strip(" .,")))


def _join_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        out = line if not out else (out + " " + line if out.endswith((",", "-")) else out + ", " + line)
    return out


def _cut_before(segment: str) -> str:
    """Keep only the part of a line that leads into the address."""
    for regex in (LABEL_RE, _TAIL_LABEL_RE, _SENTENCE_END_RE):
        ends = [m.end() for m in regex.finditer(segment)]
        if ends:
            segment = segment[max(ends):]
    if len(segment) > 120:
        tail = segment[-120:]
        segment = tail[tail.find(",") + 1:] if "," in tail else tail
    return segment


def _cut_after(segment: str) -> str:
    """Keep only the end of the address (", India") and drop what follows it."""
    cuts = [m.start() for m in (_TAIL_LABEL_RE.search(segment), _SENTENCE_END_RE.search(segment)) if m]
    segment = segment[:min(cuts)] if cuts else segment
    return segment[:80]


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def _line_end(text: str, pos: int) -> int:
    end = text.find("\n", pos)
    return len(text) if end < 0 else end


def _from_text(text: str) -> list[tuple[str, str]]:
    """(address, country code) for each address pattern written on a page, with its street lines."""
    found: list[tuple[str, str]] = []
    for _, code, m_start, m_end in sorted(_written(text), key=lambda m: m[2]):
        start, end = _line_start(text, m_start), _line_end(text, m_end)
        lead = text[start:m_start]
        head = _cut_before(lead)
        body = head + text[m_start:m_end] + _cut_after(text[m_end:end])
        parts = [body.replace("\n", ", ")]

        # Street lines just above ("12 MG Road," then "Pune, Maharashtra 411001"), stopping at a label.
        if head == lead:  # the address starts at the beginning of its line
            pos = start - 1
            for _ in range(3):
                if pos <= 0:
                    break
                p_start = _line_start(text, pos)
                prev = text[p_start:pos].strip()
                label = LABEL_RE.match(prev)
                if label:
                    if prev[label.end():].strip():
                        parts.insert(0, prev[label.end():])
                    break
                if not _is_address_line(prev):
                    break
                parts.insert(0, prev)
                pos = p_start - 1
        # A country on the line below ("India").
        if end < len(text):
            following = text[end + 1:_line_end(text, end + 1)].strip()
            if _is_country(following) and following.lower() not in body.lower():
                parts.append(following)
        address = _clean(_join_lines(parts))
        if len(address) >= 8:
            found.append((address, code))
    return found


def _from_labels(text: str) -> list[str]:
    """Addresses introduced by a label, for countries without a known postcode pattern
    ("Address: Office 1204, Bay Square, Dubai, UAE", or a heading "Our Address" over a few lines)."""
    found: list[str] = []
    lines = [line.strip() for line in text.split("\n")]
    for i, line in enumerate(lines):
        label = LABEL_RE.match(line)
        if not label and not LABEL_LINE_RE.match(line):
            continue
        rest = line[label.end():] if label else ""
        parts = [_cut_after(rest)] if rest.strip() else []
        if not parts:
            for nxt in lines[i + 1:i + 5]:
                plain = (nxt and len(nxt) <= 120 and ":" not in nxt and not _phone_like(nxt)
                         and not _SKIP_LINE_RE.search(nxt) and not _TAIL_LABEL_RE.search(nxt))
                if _is_address_line(nxt) or _is_country(nxt) or (
                        plain and ("," in nxt or not parts or parts[-1].endswith((",", "-")))):
                    parts.append(nxt)
                else:
                    break
        address = _clean(_join_lines(parts))
        if (len(address) >= 10 and ("," in address or _STREET_WORD_RE.search(address))
                and any(c.isdigit() for c in address) and not _HOURS_RE.search(address)):
            found.append(address)
    return found


def _compose(fields: dict[str, object]) -> str:
    """streetAddress, addressLocality, addressRegion postalCode, addressCountry."""
    def text(key: str) -> str:
        value = fields.get(key)
        if isinstance(value, dict):
            value = value.get("name") or ""
        if isinstance(value, list):
            value = " ".join(str(v) for v in value if isinstance(v, str))
        return re.sub(r"\s+", " ", str(value or "")).strip(" ,")

    country = text("addresscountry")
    code = country_code(country)
    if len(country) == 2 and code:
        country = country_name(code)
    region = _full_state(text("addressregion"), code)[0] if text("addressregion") else ""  # "MH" -> "Maharashtra"
    region_zip = " ".join(p for p in (region, text("postalcode")) if p)
    parts: list[str] = []
    for part in (text("streetaddress"), text("addresslocality"), region_zip, country):
        if part and part.lower() not in ", ".join(parts).lower():
            parts.append(part)
    return _clean(", ".join(parts))


def _code_of(address: str, country: object = "") -> str:
    if isinstance(country, dict):
        country = country.get("name") or ""
    return country_code(str(country or "")) or next((c for _, c, _, _ in _written(address) if c), "")


def _from_schema(tree: BeautifulSoup) -> list[tuple[str, str]]:
    """(address, country code) from schema.org data (JSON-LD or microdata)."""
    found: list[tuple[str, str]] = []

    def add(address: str, country: object = "") -> None:
        if len(address) >= 8:
            found.append((address, _code_of(address, country)))

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            keys = {str(k).lower(): v for k, v in obj.items()}
            if "streetaddress" in keys or "postalcode" in keys:
                add(_compose(keys), keys.get("addresscountry"))
            if isinstance(keys.get("address"), str):  # "address": "350 5th Ave, New York, NY 10118"
                add(_clean(keys["address"]))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for node in tree.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk(json.loads(node.string or node.get_text() or ""))
        except (json.JSONDecodeError, ValueError):
            continue

    for node in tree.find_all(attrs={"itemtype": re.compile(r"PostalAddress", re.IGNORECASE)}):
        fields: dict[str, object] = {}
        for name in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
            el = node.find(attrs={"itemprop": name})
            if el:
                fields[name.lower()] = el.get("content") or el.get_text(" ", strip=True)
        if "streetaddress" in fields or "postalcode" in fields:
            add(_compose(fields), fields.get("addresscountry"))
    return found


def _from_address_tags(tree: BeautifulSoup) -> list[tuple[str, str]]:
    """The HTML <address> element, minus phone / email lines."""
    found: list[tuple[str, str]] = []
    for node in tree.find_all("address"):
        lines = []
        for line in node.get_text("\n").split("\n"):
            line = line.strip()
            label = LABEL_RE.match(line)
            line = line[label.end():] if label else line
            if not line or _SKIP_LINE_RE.search(line) or _phone_like(line) or _TAIL_LABEL_RE.search(line):
                continue
            lines.append(line)
        address = _clean(_join_lines(lines[:6]))
        if len(address) >= 10 and any(c.isdigit() for c in address):
            found.append((address, _code_of(address)))
    return found


def detect_address(pages: Iterable[tuple[BeautifulSoup, str]], country: str = "") -> str:
    """The business's postal address as one line of text; '' when the site shows none.

    schema.org data wins, then an <address> element, then an address written on the page
    (found from its postcode), then a line labelled "Address:". With several offices, the
    first one in `country` (the answer of `detect_location`) is used.
    """
    candidates: list[Candidate] = []
    for tree, text in pages:
        text = text[:300_000]
        for rank, found in enumerate((_from_schema(tree), _from_address_tags(tree), _from_text(text),
                                      [(a, "") for a in _from_labels(text)])):
            candidates += [(rank, len(candidates) + i, a, c) for i, (a, c) in enumerate(found)]

    code = country_code(country)
    pool = [c for c in candidates if not (code and c[3] and c[3] != code)]  # not a partner office abroad
    if not pool:
        return ""
    return min(pool, key=lambda c: (c[0], bool(code) and c[3] != code, c[1]))[2]
