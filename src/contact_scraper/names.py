"""Works out a business's name from its website (the input sheet only has web addresses).

Several clues are collected and scored; clues that agree with each other or with the web
address win:
  * schema.org data (Organization / LocalBusiness "name")    strongest
  * <meta property="og:site_name">, the site's app name
  * the homepage <title> (split on "|", "-", ...)
  * the logo's alt text, the "(c) 2024 Acme Pvt Ltd" footer line
  * the web address itself ("joes-pizza.co.in" -> "Joes Pizza")   last resort
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import urlparse

from bs4 import BeautifulSoup

ORG_TYPE_RE = re.compile(
    r"organi[sz]ation|business|corporation|company|store|shop|restaurant|hotel|cafe|bakery|bar|"
    r"clinic|hospital|dentist|physician|doctor|attorney|legal|lawyer|agency|school|college|university|"
    r"brand|bank|airline|resort|lodging|contractor|plumber|electrician|salon|spa|gym|dealer|service",
    re.IGNORECASE,
)
NOT_ORG_TYPES = {"website", "webpage", "breadcrumblist", "imageobject", "person", "product", "offer", "searchaction",
                 "listitem", "sitenavigationelement", "faqpage", "question", "answer", "article", "blogposting",
                 "newsarticle", "review", "aggregaterating", "postaladdress", "geocoordinates", "contactpoint",
                 "openinghoursspecification", "collectionpage", "itemlist", "videoobject", "event", "readaction",
                 "aboutpage", "contactpage", "entrypoint", "propertyvalue", "howto", "recipe", "service"}

TITLE_SPLIT_RE = re.compile(r"\s+[|\-–—:·•»~/]\s+|\s*[|·•»]\s*|\s+::\s+")
GENERIC_RE = re.compile(
    r"^(home|home ?page|index|welcome|main|untitled|logo|menu|default|site|website|web ?site|official (web)?site|"
    r"just another wordpress site|coming soon|under construction|maintenance|page not found|not found|"
    r"40[34]( forbidden| not found)?|error|access denied|loading\.*|search|contact( us)?|about( us)?|"
    r"services|shop|online shop|store|blog|news|login|sign in|my account|cart)$",
    re.IGNORECASE,
)
STRIP_PREFIX_RE = re.compile(r"^(welcome to( the)?|home( page)?\s*[-|:]|official (web)?site of( the)?)\s+", re.IGNORECASE)
STRIP_SUFFIX_RE = re.compile(
    r"\s*([-|:]\s*)?(home( ?page)?|official (web)?site|official website|logo|website|online)$", re.IGNORECASE)
COPYRIGHT_RE = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:©|\(c\))?\s*(?:\d{4}\s*(?:[-–]\s*\d{2,4})?)?\s*[,.]?\s*(?:by\s+)?"
    r"(?P<name>[^\n|•·]{2,80}?)"
    r"\s*(?=[.,]?\s*(?:all\s+rights|rights\s+reserved|designed|developed|powered|made\s|website\s+by|site\s+by|"
    r"privacy|terms|[|•·]|\s[-–]\s|\n|$))",
    re.IGNORECASE,
)
ABBREVIATION_END_RE = re.compile(r"\b(ltd|pvt|inc|co|corp|llc|llp|plc|bros|st|jr|sr|dr|no)\.$", re.IGNORECASE)
BRAND_DOT_COM_RE = re.compile(r"^([^\W\d_][\w'&-]*)\.(?:com|in|net|org|co|io|co\.uk|co\.in|com\.au)$", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "nic", "res", "gen", "firm", "ind"}
MAX_WORDS = 8
MAX_CHARS = 70

# (base score) of each clue
SCORES = {"jsonld_org": 50, "og_site_name": 45, "jsonld_site": 40, "app_name": 35,
          "logo": 30, "copyright": 30, "title": 25, "og_title": 20}
ADDRESS_SCORE = 22  # the web address itself, when no clue does better


def compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.lower().replace("&", "and"))


def site_label(url: str) -> str:
    """'https://www.joes-pizza.co.in/x' -> 'joes-pizza'."""
    host = (urlparse(url).hostname if "//" in url else url) or ""
    parts = [p for p in host.lower().strip(".").split(".") if p]
    if parts and parts[0] == "www":
        parts = parts[1:]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SECOND_LEVEL:
        parts = parts[:-2]
    elif len(parts) >= 2:
        parts = parts[:-1]
    return parts[-1] if parts else ""


def name_from_address(url: str) -> str:
    """Last resort: the web address itself. 'joes-pizza.co.in' -> 'Joes Pizza'."""
    label = site_label(url)
    if not label or label.replace("-", "").isdigit():
        return ""
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", label) if w)


def clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(" \"'“”‘’«»|-–—:·•,").strip()
    if value.endswith(".") and not ABBREVIATION_END_RE.search(value):
        value = value.rstrip(".").strip()
    value = value.lstrip(".").strip()
    value = re.sub(r"[™®]+$", "", value).strip()
    for _ in range(2):
        value = STRIP_PREFIX_RE.sub("", value).strip()
        value = STRIP_SUFFIX_RE.sub("", value).strip(" -|:,").strip()
    brand_dot_com = BRAND_DOT_COM_RE.match(value)
    if brand_dot_com:  # "Lenskart.com" -> "Lenskart"
        value = brand_dot_com.group(1)
        value = value[0].upper() + value[1:]
    if not value or len(value) > MAX_CHARS or len(value.split()) > MAX_WORDS:
        return ""
    if GENERIC_RE.match(value) or "://" in value or "@" in value:
        return ""
    if not re.search(r"[^\W\d_]", value):  # needs at least one letter
        return ""
    if value.islower() and " " in value:
        value = value.title()
    return value


# ---------------------------------------------------------------- clues

def _jsonld_names(tree: BeautifulSoup) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def types_of(obj: dict) -> list[str]:
        t = obj.get("@type", [])
        return [str(x) for x in (t if isinstance(t, list) else [t])]

    def walk(obj: object, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)
            return
        if not isinstance(obj, dict):
            return
        types = [t.lower() for t in types_of(obj)]
        name = obj.get("name") or obj.get("legalName")
        if isinstance(name, str) and types:
            if "website" in types:
                found.append(("jsonld_site", name))
            elif any(ORG_TYPE_RE.search(t) for t in types) and not all(t in NOT_ORG_TYPES for t in types):
                found.append(("jsonld_org", name))
            elif not all(t in NOT_ORG_TYPES for t in types) and any(k in obj for k in ("address", "telephone", "logo")):
                found.append(("jsonld_org", name))  # e.g. "Plumber", "Florist": local business subtypes
        for key, value in obj.items():
            if key in ("@graph", "publisher", "provider", "brand", "author", "parentOrganization", "mainEntity",
                       "isPartOf", "about", "seller", "organizer"):
                walk(value, depth + 1)

    for node in tree.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk(json.loads(node.string or node.get_text() or ""))
        except (json.JSONDecodeError, ValueError, RecursionError):
            continue
    return found


def _meta(tree: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        node = tree.find("meta", attrs={"property": key}) or tree.find("meta", attrs={"name": key})
        if node and node.get("content"):
            return str(node["content"])
    return ""


def _logo_alt(tree: BeautifulSoup) -> str:
    for img in tree.find_all("img", limit=60):
        alt = str(img.get("alt") or "").strip()
        hints = " ".join(str(img.get(a) or "") for a in ("class", "id", "src")) + " " + alt
        parent = img.find_parent("a")
        home_link = parent is not None and str(parent.get("href") or "").strip() in ("/", "./", "#", "")
        if alt and ("logo" in hints.lower() or (home_link and img.find_parent(["header", "nav"]))):
            return re.sub(r"\blogo\b", "", alt, flags=re.IGNORECASE)
    return ""


def _copyright(text: str) -> str:
    # The footer is at the end; the last notice is the right one.
    footer = text[-4000:]
    for match in reversed(list(COPYRIGHT_RE.finditer(footer))):
        name = match.group("name") + footer[match.end("name"):match.end("name") + 1]  # keep "Ltd." (clean() drops other dots)
        name = YEAR_RE.sub("", name)
        name = re.sub(r"^\s*(?:[-–,.]|by)\s*", "", name).strip()
        if clean(name):
            return name
    return ""


def clues(pages: list[tuple[str, BeautifulSoup, str]]) -> list[tuple[str, str]]:
    """(kind, raw value) for every clue on the site. The first page is the homepage."""
    found: list[tuple[str, str]] = []
    for i, (_, tree, text) in enumerate(pages):
        found += _jsonld_names(tree)
        if i == 0:
            found.append(("og_site_name", _meta(tree, "og:site_name")))
            found.append(("app_name", _meta(tree, "application-name", "apple-mobile-web-app-title")))
            title = tree.title.get_text(" ", strip=True) if tree.title else ""
            found += [("title", part) for part in TITLE_SPLIT_RE.split(title)]
            found += [("og_title", part) for part in TITLE_SPLIT_RE.split(_meta(tree, "og:title"))]
            found.append(("logo", _logo_alt(tree)))
            found.append(("copyright", _copyright(text)))
    return [(kind, value) for kind, value in found if value]


# ---------------------------------------------------------------- scoring

def _address_match(name: str, label: str) -> int:
    """How well a name fits the web address ('Fortnum & Mason' vs 'fortnumandmason')."""
    c, lab = compact(name), compact(label)
    if not c or not lab:
        return 0
    if c == lab:
        return 40
    if len(lab) >= 3 and lab in c:
        return 30
    if len(c) >= 3 and c in lab:
        return 25
    words = [compact(w) for w in re.split(r"[\s&+\-]+", name) if compact(w)]
    if len(words) >= 2 and "".join(w[0] for w in words) == lab:
        return 25  # "Tata Consultancy Services" -> tcs.com
    if any(len(w) >= 4 and w in lab for w in words):
        return 12
    return 0


def business_name(pages: list[tuple[str, BeautifulSoup, str]], site_url: str) -> str:
    """Best guess at the business's name; the web address is the fallback."""
    label = site_label(site_url)
    best: dict[str, tuple[str, int]] = {}  # compact key -> (display name, best base score)
    votes: dict[str, set[str]] = defaultdict(set)
    for kind, raw in clues(pages):
        name = clean(raw)
        if not name:
            continue
        key = compact(name)
        base = SCORES[kind] - 4 * max(0, len(name.split()) - 4)  # long phrases are usually taglines
        votes[key].add(kind.split("_")[0] if kind.startswith("jsonld") else kind)
        if key not in best or base > best[key][1]:
            best[key] = (name, base)
    if not best:
        return name_from_address(site_url)

    def score(key: str) -> tuple[int, int]:
        name, base = best[key]
        fit = _address_match(name, label)
        if not fit and len(re.sub(r"[\W_]+", "", name)) <= 3:
            base -= 12  # "F&M" on fortnumandmason.com: a logo abbreviation, not the name
        return base + fit + 8 * (len(votes[key]) - 1), -len(name)

    winner = max(best, key=score)
    if score(winner)[0] < ADDRESS_SCORE:
        return name_from_address(site_url) or best[winner][0]  # only weak clues: the web address is safer
    return best[winner][0]
