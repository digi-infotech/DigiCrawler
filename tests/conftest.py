from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def cf_encode(email: str, key: int = 0x42) -> str:
    """Encode an email the way Cloudflare's email protection does."""
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


@pytest.fixture
def contact_html() -> str:
    html = (FIXTURES / "contact.html").read_text(encoding="utf-8")
    return html.replace("{CF}", cf_encode("billing@acmeplumbing.com"))
