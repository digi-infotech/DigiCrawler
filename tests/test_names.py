"""Business name from the website itself (input sheets only have web addresses)."""
from __future__ import annotations

import pytest

from contact_scraper import extract
from contact_scraper.names import business_name, clean, name_from_address, site_label


def site(*pages_html: str):
    out = []
    for i, html in enumerate(pages_html):
        tree = extract.parse(html)
        out.append((f"https://x/{i}", tree, extract.visible_text(tree)))
    return out


def home(head: str = "", body: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def name(url: str, *pages_html: str) -> str:
    return business_name(site(*pages_html), url)


# ---------------------------------------------------------------- web address

@pytest.mark.parametrize("url, label, fallback", [
    ("https://www.joes-pizza.co.in/menu", "joes-pizza", "Joes Pizza"),
    ("https://fortnumandmason.com", "fortnumandmason", "Fortnumandmason"),
    ("https://shop.bettys.co.uk", "bettys", "Bettys"),
    ("https://acme.com.au", "acme", "Acme"),
    ("https://my_shop.io", "my_shop", "My Shop"),
    ("https://123.com", "123", ""),
    ("", "", ""),
])
def test_web_address(url, label, fallback):
    assert site_label(url) == label
    assert name_from_address(url) == fallback


def test_no_clues_at_all_uses_web_address():
    assert name("https://www.sharma-dental.in", home(body="<p>Call 98450 43210</p>")) == "Sharma Dental"


# ---------------------------------------------------------------- each clue

def test_schema_org_organization_wins():
    ld = '<script type="application/ld+json">{"@type":"Organization","name":"Acme Robotics Pvt. Ltd."}</script>'
    assert name("https://acmerobotics.in", home(f"<title>Home | Welcome</title>{ld}")) == "Acme Robotics Pvt. Ltd."


def test_schema_org_local_business_subtype_inside_graph():
    ld = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
          '{"@type":"WebPage","name":"Home - Best plumbers in Pune"},'
          '{"@type":"Plumber","name":"Kumar Plumbing Works","telephone":"+91 98450 43210"}]}</script>')
    assert name("https://kumarplumbing.in", home(ld)) == "Kumar Plumbing Works"


def test_schema_org_publisher_nested():
    ld = ('<script type="application/ld+json">{"@type":"WebSite","name":"BH","publisher":'
          '{"@type":"Organization","name":"Bright Homes"}}</script>')
    assert name("https://brighthomes.com", home(ld)) == "Bright Homes"


def test_broken_json_ld_is_ignored():
    head = '<script type="application/ld+json">{oops</script><title>Lotus Spa | Day spa in Goa</title>'
    assert name("https://lotusspa.in", home(head)) == "Lotus Spa"


def test_og_site_name():
    head = '<meta property="og:site_name" content="Zingerman\'s Deli"><title>Sandwiches & more</title>'
    assert name("https://zingermansdeli.com", home(head)) == "Zingerman's Deli"


def test_title_brand_at_end():
    assert name("https://greenleafcafe.com", home("<title>Organic coffee &amp; brunch in Austin | Green Leaf Cafe</title>")) \
        == "Green Leaf Cafe"


def test_title_brand_at_start():
    assert name("https://nimbus.io", home("<title>Nimbus - Cloud backups for small teams</title>")) == "Nimbus"


def test_title_with_welcome_and_home_words():
    assert name("https://rivertonhotel.com", home("<title>Welcome to Riverton Hotel - Home</title>")) == "Riverton Hotel"


def test_title_brand_matches_ampersand_in_address():
    assert name("https://fortnumandmason.com", home("<title>Luxury Food Hampers & Gifts | Fortnum & Mason</title>")) \
        == "Fortnum & Mason"


def test_acronym_address():
    head = "<title>Tata Consultancy Services | IT Services, Consulting</title>"
    assert name("https://tcs.com", home(head)) == "Tata Consultancy Services"


def test_logo_alt_text():
    body = '<header><a href="/"><img src="/img/logo.png" alt="Mehta &amp; Sons Logo"></a></header>'
    assert name("https://mehtasons.in", home("<title>Home</title>", body)) == "Mehta & Sons"


def test_copyright_footer():
    body = "<main><p>We fix things.</p></main><footer>© 2019 - 2024 Patel Electricals Pvt. Ltd. All rights reserved.</footer>"
    assert name("https://patelelectricals.in", home("<title>Home</title>", body)) == "Patel Electricals Pvt. Ltd."


def test_copyright_word_and_designed_by():
    body = "<footer>Copyright 2024 Blue Door Bakery | Designed by WebAgency</footer>"
    assert name("https://bluedoorbakery.com", home("", body)) == "Blue Door Bakery"


def test_clues_that_agree_beat_a_single_stronger_clue():
    head = ('<meta property="og:site_name" content="Shop">'
            '<title>Kiran Textiles | Sarees and Silks</title><meta name="application-name" content="Kiran Textiles">')
    assert name("https://kirantex.in", home(head)) == "Kiran Textiles"


def test_contact_page_title_is_not_used():
    pages = (home("<title>Home</title>"), home("<title>Contact Us | Some Other Words</title>"))
    assert name("https://ruby-salon.com", *pages) == "Ruby Salon"


def test_schema_org_on_contact_page_is_used():
    ld = '<script type="application/ld+json">{"@type":"LocalBusiness","name":"Ruby Hair Salon"}</script>'
    assert name("https://ruby-salon.com", home("<title>Home</title>"), home(ld)) == "Ruby Hair Salon"


# ---------------------------------------------------------------- junk that must not become a name

@pytest.mark.parametrize("junk", [
    "Home", "Homepage", "Welcome", "Untitled", "404 Not Found", "Access Denied", "Just another WordPress site",
    "Contact Us", "https://acme.com", "info@acme.com", "2024", "",
    "This is a very long tagline that nobody would ever call a business name at all",
])
def test_junk_is_rejected(junk):
    assert clean(junk) == ""


def test_lowercase_name_is_title_cased_and_symbols_trimmed():
    assert clean("  green leaf cafe™ ") == "Green Leaf Cafe"



def test_logo_abbreviation_that_does_not_fit_address_loses_to_address():
    body = '<header><a href="/"><img class="site-logo" alt="F&amp;M"></a></header>'
    assert name("https://www.fortnumandmason.com", home("", body)) == "Fortnumandmason"


def test_short_name_that_fits_address_is_kept():
    assert name("https://www.mtr.in", home("<title>MTR | Breakfast mixes and spices</title>")) == "MTR"


def test_only_a_tagline_title_uses_address():
    head = "<title>Best affordable wedding photography packages in Jaipur and Udaipur</title>"
    assert name("https://raj-studio.in", home(head)) == "Raj Studio"


def test_brand_dot_com_loses_its_ending():
    assert clean("Lenskart.com") == "Lenskart" and clean("flipkart.com") == "Flipkart"
    assert clean("expired.badssl.com") == "expired.badssl.com"   # not a "Brand.com" name, left alone


def test_results_page_shows_scraped_name_unless_sheet_has_one():
    from contact_scraper.api import summary
    from contact_scraper.models import SiteResult
    res = SiteResult("https://acme.in", business_name="Acme Pvt. Ltd.", status="ok")
    assert summary(res)["name"] == "Acme Pvt. Ltd."
    assert summary(res, "ACME (from my sheet)")["name"] == "ACME (from my sheet)"
