from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    region: str = "US"
    concurrency: int = 20
    max_pages: int = 6
    timeout: float = 15.0
    respect_robots: bool = True
    use_browser: bool = False
    # False = quick mode ("Analytics mode" off): open extra pages / the browser only while no email or
    # mobile has been found, and skip the address. True = check every planned page for every detail.
    analytics: bool = True
    # Pause when the internet connection drops instead of saving every website as failed
    # (see netguard). Off by default here so tests never touch the network; on by default in .env.
    online_check: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            region=os.getenv("REGION", "US").upper(),
            concurrency=int(os.getenv("CONCURRENCY", "20")),
            max_pages=int(os.getenv("MAX_PAGES", "6")),
            timeout=float(os.getenv("TIMEOUT", "15")),
            respect_robots=_env_bool("RESPECT_ROBOTS", True),
            use_browser=_env_bool("WITH_BROWSER", True),
            online_check=_env_bool("ONLINE_CHECK", True),
        )


@dataclass
class SiteResult:
    url: str
    final_url: str = ""
    business_name: str = ""  # from the website itself (schema.org, title, logo, footer...)
    address: str = ""  # the site's postal address as one line of text; blank when none is found
    state: str = ""    # from the site's postal address only; blank when no address is found
    country: str = ""  # English country name, e.g. "India"
    emails: list[str] = field(default_factory=list)
    mobiles: list[str] = field(default_factory=list)  # subset of phones
    phones: list[str] = field(default_factory=list)   # every mobile and telephone number (no fax / toll-free)

    @property
    def landlines(self) -> list[str]:
        """Telephone numbers that are not mobiles."""
        return [p for p in self.phones if p not in self.mobiles]
    pages: list[str] = field(default_factory=list)
    status: str = "pending"
    region: str = ""  # country used to read local numbers on this site
    method: str = "http"  # "http", "browser" (opened in headless browser) or "http+browser"

    def finish(self) -> "SiteResult":
        if self.status == "pending":
            self.status = "ok" if (self.emails or self.phones) else "no_contacts"
        return self
