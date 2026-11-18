"""Pure functions that pull emails, phone numbers and contact-page links out of HTML.

Only what a visitor can actually read on the page is used (plus mailto:/tel:/WhatsApp
links and schema.org data). Text inside <script>, <style> etc. is ignored, which keeps
out junk like tracking ids and image file names.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse

import phonenumbers
from bs4 import BeautifulSoup, NavigableString
from phonenumbers import PhoneNumberType

# Pure-Python parser: no compiled code, so it works on locked-down Windows machines too.
HTMLParser = BeautifulSoup

# ---------------------------------------------------------------- patterns

EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24}")

# "info [at] acme [dot] com", "info(at)acme(dot)co(dot)uk", "info {at} acme.com"
BRACKET_EMAIL_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)[ \t]*[\[\(\{<][ \t]*at[ \t]*[\]\)\}>][ \t]*"
    r"([A-Za-z0-9-]+(?:(?:[ \t]*[\[\(\{<][ \t]*dot[ \t]*[\]\)\}>][ \t]*|\.)[A-Za-z0-9-]+)+)",
    re.IGNORECASE,
)
BRACKET_DOT_RE = re.compile(r"[ \t]*[\[\(\{<][ \t]*dot[ \t]*[\]\)\}>][ \t]*", re.IGNORECASE)
# "info at acme dot com" (needs at least one "dot" and a common ending, to avoid normal sentences)
PLAIN_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)[ \t]+at[ \t]+([A-Za-z0-9-]+(?:[ \t]+dot[ \t]+[A-Za-z0-9-]+)*"
    r"[ \t]+dot[ \t]+(?:com|net|org|in|co|io|biz|info|us|uk|au|ca|de|ae|sg|nz|ie|za))\b",
    re.IGNORECASE,
)
CAMEL_TLD_RE = re.compile(r"(\.[a-z]{2,})[A-Z][A-Za-z]*$")  # "info@acme.comCall" -> "info@acme.com"
PLAIN_DOT_RE = re.compile(r"[ \t]+dot[ \t]+", re.IGNORECASE)
PRONOUNS = {"us", "me", "you", "them", "him", "her", "it", "we", "our"}

FILE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico", ".bmp",
    ".css", ".js", ".mjs", ".json", ".map", ".woff", ".woff2", ".ttf", ".mp4", ".pdf",
)
JUNK_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "yourcompany.com",
    "email.com", "mysite.com", "yoursite.com", "website.com", "company.com", "sentry.io",
    "wixpress.com", "sentry.wixpress.com", "sentry-next.wixpress.com", "test.com", "localhost.com",
}
JUNK_LOCAL_PARTS = {"you", "your", "name", "yourname", "youremail", "email", "user", "username",
                    "someone", "john.doe", "johndoe", "jane.doe", "firstname.lastname"}
HEX_ID_RE = re.compile(r"^[0-9a-f]{24,}$")
JSON_ESCAPE_PREFIX_RE = re.compile(r"^(?:u00[0-9a-f]{2})+", re.IGNORECASE)

# Words just before a number that tell us what kind of number it is.
FAX_HINT_RE = re.compile(r"fax", re.IGNORECASE)
PHONE_HINT_RE = re.compile(r"\b(tel|telephone|phone|ph|call|mobile|mob|cell|whatsapp|contact)\b", re.IGNORECASE)
MOBILE_HINT_RE = re.compile(r"(mobile|\bmob\b|\bmob\.|\bcell|whats\s?app|\bsms\b|\bm\s*[:.]\s*$)", re.IGNORECASE)

NON_CONTENT_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head", "title", "select"}
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
    "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "option", "label",
}
# Inline elements that still visually separate words (menus, icon buttons...).
SPACED_TAGS = {"a", "button", "img", "input", "textarea"}

# Which links are worth opening, by the page's own name (last part of the address) or a
# short menu label. Order = priority. Matching from the start avoids pages like
# "latest-news-about-us" or "shareholder-services".
LINK_GROUPS: list[tuple[str, re.Pattern[str]]] = [
    ("contact", re.compile(
        r"(contact|kontakt|contacto|get ?in ?touch|reach ?us|reach out|enquir|inquir|connect with us|"
        r"find us|locate us|write to us|talk to us|call us|customer (care|service|support)|"
        r"(our |store |office )?(branch|office|location|store locator)(es|s)?( us)?$)")),
    ("about", re.compile(r"(about|who we are|our story|our company|company profile|corporate profile|"
                         r"our team$|team$|impressum|imprint)")),
    ("services", re.compile(r"(services?($| )|our services|what we do|solutions?$|our solutions)")),
    ("other", re.compile(r"(support$|help ?desk|help$|help cent(er|re))")),
]
MAX_LABEL_WORDS = 4
GROUP_ORDER = [g for g, _ in LINK_GROUPS]
MAIN_WORD = {"contact": "contact", "about": "about", "services": "service"}
# Tried directly even if the homepage menu doesn't link to them.
PROBE_PATHS = {"contact": ["/contact", "/contact-us"], "about": ["/about", "/about-us"], "services": ["/services"]}
SKIP_LINK_SUFFIXES = FILE_SUFFIXES + (".zip", ".doc", ".docx", ".xls", ".xlsx", ".mp3", ".xml", ".rss")
NOT_FOUND_RE = re.compile(
    r"\b404\b|not found|page (?:does not|doesn'?t) exist|page cannot be found|no longer available|page not available",
    re.IGNORECASE,
)

# ccTLDs that are mostly used as generic domains, so they don't tell us the country.
GENERIC_CCTLDS = {"io", "co", "ai", "me", "tv", "ly", "fm", "gg", "to", "ws", "cc", "am", "is", "it", "so",
                  "sh", "ac", "la", "vc", "gl", "im", "nu", "tk", "ms", "md", "mu", "fi", "st", "app"}


SPECIAL_RATE_TYPES = {PhoneNumberType.TOLL_FREE, PhoneNumberType.SHARED_COST, PhoneNumberType.PREMIUM_RATE}

# Dummy numbers used in examples and form hints, never real contacts.
PLACEHOLDER_NUMBERS = {"9876543210", "1234567890", "0123456789", "0987654321", "1231231234", "9898989898"}
SEQUENCE_RE = re.compile(r"123456789|234567890|987654321|876543210")

# Clues on a page that tell which country its local phone numbers belong to.
US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
             "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY")
US_ADDRESS_RE = re.compile(r"\b(?:%s),?\s+\d{5}(?:-\d{4})?\b" % "|".join(US_STATES.split()))
UK_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s\d[A-Z]{2}\b")
INDIA_ADDRESS_RE = re.compile(r"\b\d{3}\s?\d{3}\b[^\n]{0,25}\bIndia\b|\bIndia\b[^\n]{0,5}\b\d{3}\s?\d{3}\b", re.IGNORECASE)
US_CITIES = (r"New York|NYC|Brooklyn|Manhattan|Queens|Los Angeles|San Francisco|San Diego|Chicago|Seattle|"
             r"Boston|Miami|Austin|Dallas|Atlanta|Philadelphia|Washington,? D\.?C|Denver|Portland|Las Vegas")
US_CITY_ZIP_RE = re.compile(r"\b(?:%s)(?: City)?,?\s+\d{5}\b" % US_CITIES)
US_TOLL_FREE_RE = re.compile(r"\b1[-.\s]8(?:00|33|44|55|66|77|88)[-.\s][A-Z0-9]{3}[-.\s]?[A-Z0-9]{4}\b")
US_STYLE_NUMBER_RE = re.compile(r"\(([2-9]\d{2})\)\s?([2-9]\d{2})-(\d{4})|\b([2-9]\d{2})[-.]([2-9]\d{2})[-.](\d{4})\b")
# Weak clues: one vote per mention.
COUNTRY_WORDS = {
    "IN": re.compile(r"\bIndia\b|₹|\b(Mumbai|New Delhi|Delhi|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|"
                     r"Ahmedabad|Gurugram|Gurgaon|Noida|Jaipur|Lucknow|Chandigarh|Kochi|Indore)\b"),
    "US": re.compile(r"\b(USA|U\.S\.A|United States)\b|\b(%s)\b" % US_CITIES),
    "GB": re.compile(r"\b(United Kingdom|England|Scotland|Wales)\b|£"),
}


@dataclass(frozen=True)
class Phone:
    number: str    # international format, e.g. "+91 98765 43210"
    mobile: bool


# ---------------------------------------------------------------- helpers

# Big blocks that never hold visible contact details. Removing them before parsing makes
# heavy pages (Wix, Next.js, page builders) many times faster. schema.org JSON-LD is kept.
_HEAVY_BLOCKS_RE = re.compile(
    r"<script\b(?![^>]*application/ld\+json)[^>]*>.*?</script\s*>"
    r"|<style\b[^>]*>.*?</style\s*>|<svg\b[^>]*>.*?</svg\s*>|<!--.*?-->",
    re.IGNORECASE | re.DOTALL,
)


def slim_html(html: str) -> str:
    return _HEAVY_BLOCKS_RE.sub(" ", html or "")


# Pages shown by bot-protection services instead of the real website.
BLOCKED_RE = re.compile(
    r"access denied|you don'?t have permission to access|attention required!? \| cloudflare|"
    r"just a moment\.\.\.|checking your browser|verify you are (a )?human|are you a robot|"
    r"request unsuccessful\. incapsula|pardon our interruption|enable javascript and cookies to continue|"
    r"ddos protection by|access to this page has been denied",
    re.IGNORECASE,
)


def looks_blocked(html: str) -> bool:
    """A small 'Access denied' / 'Checking your browser' page instead of the website."""
    slim = slim_html(html)
    return len(slim) < 30_000 and bool(BLOCKED_RE.search(slim))


def parse(html: str) -> HTMLParser:
    return BeautifulSoup(slim_html(html), "html.parser")


def bare_host(url_or_host: str) -> str:
    host = urlparse(url_or_host).hostname if "//" in url_or_host else url_or_host
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def site_region(url: str, default: str) -> str:
    """Country implied by the domain ending (.in -> IN, .co.uk -> GB), else the default."""
    tld = bare_host(url).rsplit(".", 1)[-1]
    if tld == "uk":
        return "GB"
    if len(tld) == 2 and tld not in GENERIC_CCTLDS and tld.upper() in phonenumbers.SUPPORTED_REGIONS:
        return tld.upper()
    return default


COUNTRY_NAMES = {"india": "IN", "united states": "US", "usa": "US", "united states of america": "US",
                 "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB", "canada": "CA",
                 "australia": "AU", "united arab emirates": "AE", "uae": "AE", "singapore": "SG"}


def _jsonld_countries(tree: HTMLParser) -> list[str]:
    found: list[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() == "addresscountry":
                    name = value.get("name", "") if isinstance(value, dict) else str(value)
                    code = name.strip().upper() if len(name.strip()) == 2 else COUNTRY_NAMES.get(name.strip().lower())
                    if code == "UK":
                        code = "GB"
                    if code in phonenumbers.SUPPORTED_REGIONS:
                        found.append(code)
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for node in tree.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk(json.loads(node.string or node.get_text() or ""))
        except (json.JSONDecodeError, ValueError):
            continue
    return found


def detect_region(url: str, pages: list[tuple[HTMLParser, str]], default: str) -> str:
    """Which country a site's local numbers (written without +code) belong to.

    Domain ending first (.in, .co.uk...). For .com/.org etc. the pages are checked for
    clues: schema.org address country, numbers written with a +country code, US
    "State ZIP", UK postcodes, Indian "PIN ... India". The user's choice is the fallback.
    """
    by_domain = site_region(url, "")
    if by_domain:
        return by_domain
    votes: Counter[str] = Counter()
    for tree, text in pages:
        for code in _jsonld_countries(tree):
            votes[code] += 5
        for node in tree.find_all("a", href=True):
            href = (node.get("href") or "").strip()
            if href.lower().startswith("tel:") and "+" in href:
                number = parse_number(href.split(":", 1)[1], "ZZ")
                if number:
                    votes[phonenumbers.region_code_for_number(number)] += 2
        for match in phonenumbers.PhoneNumberMatcher(text[:300_000], "ZZ"):  # only "+code" numbers
            votes[phonenumbers.region_code_for_number(match.number)] += 2
        votes["US"] += 3 * (len(US_ADDRESS_RE.findall(text)) + len(US_CITY_ZIP_RE.findall(text)))
        votes["US"] += 2 * len(US_TOLL_FREE_RE.findall(text))
        for groups in US_STYLE_NUMBER_RE.findall(text):  # "(212) 254-2246" / "212-475-4880"
            if parse_number("".join(groups), "US"):     # only if it really is a valid US number
                votes["US"] += 1
        votes["GB"] += 3 * len(UK_POSTCODE_RE.findall(text))
        votes["IN"] += 3 * len(INDIA_ADDRESS_RE.findall(text))
        for code, pattern in COUNTRY_WORDS.items():
            votes[code] += len(pattern.findall(text))
    votes = Counter({c: n for c, n in votes.items() if c in phonenumbers.SUPPORTED_REGIONS and n > 0})
    if not votes:
        return default
    best, best_votes = votes.most_common(1)[0]
    if votes.get(default) == best_votes:
        return default  # a tie: trust the user's choice
    return best


_BLOCK_END = object()
_SPACE = object()


def visible_text(tree: HTMLParser) -> str:
    """Text a visitor would read, one line per block (paragraph, list item, cell...).

    Inline tags (<span>, <b>, <a>) are joined without spaces, exactly like the browser
    shows them, so "info@<span>acme.com</span>" still reads as one email.
    """
    out: list[str] = []
    stack: list[object] = [tree]
    while stack:
        node = stack.pop()
        if node is _BLOCK_END:
            out.append("\n")
            continue
        if node is _SPACE:
            out.append(" ")
            continue
        if isinstance(node, NavigableString):
            if type(node) is NavigableString:  # skip comments, doctype, CDATA...
                out.append(str(node))
            continue
        name = getattr(node, "name", None)
        if name in NON_CONTENT_TAGS:
            continue
        if name in BLOCK_TAGS:
            out.append("\n")
            stack.append(_BLOCK_END)
        elif name in SPACED_TAGS:
            out.append(" ")
            stack.append(_SPACE)
        stack.extend(reversed(node.contents))
    text = "".join(out)
    text = re.sub(r"[ \t\r\f\v ​]+", " ", text)
    text = re.sub(r" *\n\s*", "\n", text)
    return text.strip()


def looks_js_rendered(text: str) -> bool:
    """Very little visible text usually means the page is built by JavaScript."""
    return len(text.strip()) < 500


def looks_not_found(tree: HTMLParser) -> bool:
    """A 'page not found' page served with status 200 (common on WordPress/Wix)."""
    title = tree.title.get_text(" ", strip=True) if tree.title else ""
    h1 = tree.find("h1")
    return bool(NOT_FOUND_RE.search(f"{title} {h1.get_text(' ', strip=True) if h1 else ''}"))


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------- emails

def decode_cfemail(encoded: str) -> str | None:
    """Decode a Cloudflare 'email protection' hex string."""
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except (ValueError, IndexError):
        return None


def clean_email(raw: str) -> str | None:
    email = unquote(raw).strip().strip(".,;:'\"<>()[]").lower()
    email = email.split("?", 1)[0]
    email = JSON_ESCAPE_PREFIX_RE.sub("", email)
    if email.count("@") != 1:
        return None
    local, domain = email.split("@")
    if not local or not EMAIL_RE.fullmatch(email):
        return None
    if domain.endswith(FILE_SUFFIXES) or email.endswith(FILE_SUFFIXES):
        return None
    if domain in JUNK_EMAIL_DOMAINS or local in JUNK_LOCAL_PARTS:
        return None
    if HEX_ID_RE.match(local) or re.search(r"@\d+x\.", email):
        return None
    return email


def extract_emails(tree: HTMLParser, text: str) -> list[str]:
    """Emails from mailto: links, Cloudflare-protected links and the readable text."""
    found: list[str] = []

    for node in tree.find_all("a", href=True):
        href = node.get("href") or ""
        if href.lower().startswith("mailto:"):
            found.extend(href[7:].split(","))
        if "/cdn-cgi/l/email-protection#" in href:
            decoded = decode_cfemail(href.rsplit("#", 1)[1])
            if decoded:
                found.append(decoded)
    for node in tree.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cfemail(node.get("data-cfemail") or "")
        if decoded:
            found.append(decoded)

    found.extend(CAMEL_TLD_RE.sub(r"\1", m) for m in EMAIL_RE.findall(text))
    for local, domain in BRACKET_EMAIL_RE.findall(text):
        found.append(f"{local}@{BRACKET_DOT_RE.sub('.', domain)}")
    for local, domain in PLAIN_EMAIL_RE.findall(text):
        if local.lower() not in PRONOUNS:
            found.append(f"{local}@{PLAIN_DOT_RE.sub('.', domain)}")

    return _dedupe([e for e in (clean_email(x) for x in found) if e])


def sort_emails_for_site(emails: list[str], site_url: str) -> list[str]:
    """Emails on the business's own domain first, then the rest."""
    host = bare_host(site_url)

    def same_domain(email: str) -> bool:
        domain = email.split("@", 1)[1]
        return bool(host) and (domain == host or domain.endswith("." + host) or host.endswith("." + domain))

    return sorted(emails, key=lambda e: 0 if same_domain(e) else 1)


# ---------------------------------------------------------------- phones

def format_phone(number: phonenumbers.PhoneNumber) -> str:
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)


def parse_number(raw: str, region: str, strict: bool = True) -> phonenumbers.PhoneNumber | None:
    raw = unquote(raw).strip()
    if not raw:
        return None
    try:
        number = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    ok = phonenumbers.is_valid_number(number) if strict else phonenumbers.is_possible_number(number)
    return number if ok else None


def parse_phone(raw: str, region: str, strict: bool = True) -> str | None:
    number = parse_number(raw, region, strict)
    return format_phone(number) if number else None


def is_placeholder(number: phonenumbers.PhoneNumber) -> bool:
    """Dummy numbers such as 98765 43210, 99999 99999 or the US 555-01xx film numbers."""
    nsn = phonenumbers.national_significant_number(number)
    if nsn in PLACEHOLDER_NUMBERS or SEQUENCE_RE.search(nsn):
        return True
    if len(nsn) >= 8 and len(set(nsn)) <= 2:
        return True
    return number.country_code == 1 and nsn[3:6] == "555" and nsn[6:8] == "01"


def is_special_rate(number: phonenumbers.PhoneNumber) -> bool:
    """Toll-free, shared-cost and premium-rate lines (1800 / 1860 / 0800 / 084x / 09x...): call-centre
    numbers, not the business's own mobile or telephone, so they are left out."""
    return phonenumbers.number_type(number) in SPECIAL_RATE_TYPES


def is_mobile(number: phonenumbers.PhoneNumber, context: str = "") -> bool:
    """Mobile per the official numbering plan. Where the plan can't tell (US/Canada),
    we trust a nearby label such as 'Mobile:', 'Cell' or 'WhatsApp'."""
    kind = phonenumbers.number_type(number)
    if kind == PhoneNumberType.MOBILE:
        return True
    if kind == PhoneNumberType.FIXED_LINE_OR_MOBILE:
        return bool(MOBILE_HINT_RE.search(context))
    return False


def _whatsapp_digits(href: str) -> str | None:
    parsed = urlparse(href)
    host = (parsed.hostname or "").lower()
    if host in {"wa.me", "www.wa.me"}:
        digits = re.sub(r"\D", "", parsed.path)
    elif "whatsapp.com" in host or href.lower().startswith("whatsapp:"):
        digits = re.sub(r"\D", "", (parse_qs(parsed.query).get("phone") or [""])[0])
    else:
        return None
    return digits or None


def _inside_code(text: str, start: int, end: int) -> bool:
    """Digits that are part of a longer code, e.g. 'Ref #18.394c3917.1794386023.9f7a' or a URL."""
    before = text[max(0, start - 2):start]
    after = text[end:end + 2]
    if before[-1:] in {"#", "/", "=", "_"} or after[:1] in {"/", "=", "_"}:
        return True
    if len(before) == 2 and before[1] == "." and before[0].isdigit():
        return True
    return len(after) == 2 and after[0] == "." and after[1].isalnum()


def extract_phones(tree: HTMLParser, text: str, region: str) -> list[Phone]:
    """Mobile, WhatsApp and telephone numbers from tel:/WhatsApp links and from the readable text.
    Fax, toll-free and other special-rate numbers are skipped."""
    found: dict[str, bool] = {}

    def add(number: phonenumbers.PhoneNumber, mobile: bool) -> None:
        if is_placeholder(number) or is_special_rate(number):
            return
        key = format_phone(number)
        found[key] = found.get(key, False) or mobile

    for node in tree.find_all("a", href=True):
        href = (node.get("href") or "").strip()
        low = href.lower()
        context = node.parent.get_text(" ", strip=True)[:80] if node.parent else ""
        if low.startswith(("tel:", "callto:")):
            number = parse_number(href.split(":", 1)[1], region)
            if number:
                add(number, is_mobile(number, context))
        else:
            digits = _whatsapp_digits(href)
            number = parse_number("+" + digits, region) if digits else None
            if number:
                add(number, True)  # WhatsApp only works with mobile numbers

    prev_end = 0
    for match in phonenumbers.PhoneNumberMatcher(text[:300_000], region, leniency=phonenumbers.Leniency.VALID):
        if _inside_code(text, match.start, match.end):
            continue
        line_start = text.rfind("\n", 0, match.start) + 1
        context = text[max(prev_end, line_start, match.start - 30):match.start]
        prev_end = match.end
        if FAX_HINT_RE.search(context) and not PHONE_HINT_RE.search(context):
            continue
        add(match.number, is_mobile(match.number, context))

    return [Phone(n, m) for n, m in found.items()]


# ---------------------------------------------------------------- JSON-LD (schema.org)

def extract_jsonld(tree: HTMLParser, region: str) -> tuple[list[str], list[Phone]]:
    emails: list[str] = []
    phones: dict[str, bool] = {}

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                k = str(key).lower()
                if k == "email" and isinstance(value, str):
                    email = clean_email(value.replace("mailto:", ""))
                    if email:
                        emails.append(email)
                elif k in {"telephone", "phone"} and isinstance(value, str):
                    number = parse_number(value, region)
                    if number and not is_placeholder(number) and not is_special_rate(number):
                        phones[format_phone(number)] = is_mobile(number)
                elif k != "faxnumber":
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for node in tree.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk(json.loads(node.string or node.get_text() or ""))
        except (json.JSONDecodeError, ValueError):
            continue
    return _dedupe(emails), [Phone(n, m) for n, m in phones.items()]


# ---------------------------------------------------------------- links

def _label(value: str) -> str:
    value = re.sub(r"\.(html?|php|aspx?|jsp)$", "", value.lower())
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def link_group(path: str, text: str = "") -> str | None:
    """'contact' / 'about' / 'services' / 'other', or None if the link isn't useful."""
    segments = [s for s in unquote(path).split("/") if s]
    candidates = [_label(segments[-1])] if segments else []
    label = _label(text)
    if label and len(label.split()) <= MAX_LABEL_WORDS:
        candidates.append(label)
    for group, pattern in LINK_GROUPS:
        if any(pattern.match(c) for c in candidates):
            return group
    return None


def discover_links(tree: HTMLParser, base_url: str) -> list[tuple[str, str]]:
    """Same-site links that probably carry contact details, as (url, group), best first."""
    site = bare_host(base_url)
    home_key = urldefrag(base_url)[0].rstrip("/")
    best: dict[str, tuple[str, str]] = {}

    for node in tree.find_all("a", href=True):
        href = (node.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "whatsapp:")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or bare_host(url) != site:
            continue
        key = url.rstrip("/")
        if parsed.path.lower().endswith(SKIP_LINK_SUFFIXES) or key == home_key:
            continue
        group = link_group(parsed.path, node.get_text(" ", strip=True))
        if group and (key not in best or GROUP_ORDER.index(group) < GROUP_ORDER.index(best[key][1])):
            best[key] = (url, group)

    def rank(item: tuple[str, str]) -> tuple:
        url, group = item
        path = urlparse(url).path.lower()
        named = MAIN_WORD.get(group, "~") in path  # e.g. /contact-us beats /branches
        return GROUP_ORDER.index(group), not named, len(path), url

    return sorted(best.values(), key=rank)


def find_contact_links(tree: HTMLParser, base_url: str) -> list[str]:
    return [url for url, _ in discover_links(tree, base_url)]


def plan_pages(tree: HTMLParser, base_url: str, limit: int) -> list[tuple[str, bool]]:
    """Pick up to `limit` pages to open: contact, about and services first (one of each,
    then the next of each...). Well-known paths like /contact-us are tried directly when
    the homepage has no matching link. Returns (url, was_guessed)."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    buckets: dict[str, list[tuple[str, bool]]] = {g: [] for g in GROUP_ORDER}
    for url, group in discover_links(tree, base_url):
        buckets[group].append((url, False))
    for group, paths in PROBE_PATHS.items():
        if not buckets[group]:
            buckets[group] = [(origin + p, True) for p in paths]

    chosen: list[tuple[str, bool]] = []
    seen = {urldefrag(base_url)[0].rstrip("/")}
    while len(chosen) < limit and any(buckets.values()):
        for group in GROUP_ORDER:
            if buckets[group] and len(chosen) < limit:
                url, guessed = buckets[group].pop(0)
                if url.rstrip("/") not in seen:
                    seen.add(url.rstrip("/"))
                    chosen.append((url, guessed))
    return chosen
