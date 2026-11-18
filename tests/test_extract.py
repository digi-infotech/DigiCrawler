"""Unit tests for finding emails, phone numbers and contact pages in HTML."""
import pytest

from contact_scraper import extract

from .conftest import cf_encode

BASE = "https://www.acmeplumbing.com/"


def emails_in(html: str) -> list[str]:
    tree = extract.parse(html)
    return extract.extract_emails(tree, extract.visible_text(tree))


def phones_in(html: str, region: str = "IN") -> dict[str, bool]:
    tree = extract.parse(html)
    return {p.number: p.mobile for p in extract.extract_phones(tree, extract.visible_text(tree), region)}


# ============================================================== emails

def test_fixture_emails_from_all_sources(contact_html):
    emails = emails_in(contact_html)
    assert "sales@acmeplumbing.com" in emails      # mailto, lower-cased, ?subject dropped
    assert "support@acmeplumbing.com" in emails    # "[at] / [dot]" in a paragraph
    assert "billing@acmeplumbing.com" in emails    # Cloudflare protected
    assert "partner@othercompany.io" in emails     # plain paragraph text
    assert not any("example.com" in e or "sentry" in e or e.endswith(".png") for e in emails)


@pytest.mark.parametrize("html,expected", [
    # Plain paragraph text (no mailto link)
    ("<p>Write to info@acme.co.in for quotes.</p>", ["info@acme.co.in"]),
    ("<p>Email:info@acme.com</p>", ["info@acme.com"]),
    ("<p>EMAIL: INFO@ACME.COM</p>", ["info@acme.com"]),
    ("<p>first.last+tag@mail.acme.co.uk</p>", ["first.last+tag@mail.acme.co.uk"]),
    # Several emails in one paragraph, any separator
    ("<p>sales@acme.com, hr@acme.com / accounts@acme.com; ceo@acme.com</p>",
     ["sales@acme.com", "hr@acme.com", "accounts@acme.com", "ceo@acme.com"]),
    # Same email repeated (header + footer) -> once
    ("<header>info@acme.com</header><footer>Info@Acme.com</footer>", ["info@acme.com"]),
    # Split by inline formatting tags, the way a browser still shows it
    ("<p>info@<span>acme</span>.com</p>", ["info@acme.com"]),
    ("<p><b>support</b>@acme.com</p>", ["support@acme.com"]),
    # Obfuscated forms
    ("<p>sales [at] acme [dot] com</p>", ["sales@acme.com"]),
    ("<p>sales(at)acme(dot)co(dot)uk</p>", ["sales@acme.co.uk"]),
    ("<p>sales {at} acme.com</p>", ["sales@acme.com"]),
    ("<p>Mail hr at acme dot com</p>", ["hr@acme.com"]),
    # Punctuation around the address
    ("<p>(info@acme.com).</p>", ["info@acme.com"]),
    ("<p>&lt;info@acme.com&gt;</p>", ["info@acme.com"]),
    # Glued to the next link text
    ('<div><a href="#">info@acme.com</a><a href="#">Call us</a></div>', ["info@acme.com"]),
    ("<div><span>info@acme.com</span><span>Call</span></div>", ["info@acme.com"]),
    # mailto variants
    ('<a href="mailto:a@acme.com,b@acme.com">Email</a>', ["a@acme.com", "b@acme.com"]),
    ('<a href="mailto:info%40acme.com">Email</a>', ["info@acme.com"]),
    ('<a href="MAILTO:Info@Acme.com?subject=Hi&body=x">Email</a>', ["info@acme.com"]),
])
def test_email_found(html, expected):
    assert emails_in(html) == expected


@pytest.mark.parametrize("html", [
    '<script>var e = "hidden@acme.com";</script>',                 # inside code
    "<style>/* css@acme.com */</style>",
    "<!-- old: old@acme.com -->",                                  # HTML comment
    '<input placeholder="you@example.com">',                       # form placeholder
    "<p>you@example.com or your@email.com or name@domain.com</p>",  # template placeholders
    '<img src="logo@2x.png"><p>icon@3x.webp</p>',                   # retina image names
    "<p>a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6@sentry.io</p>",            # error-tracking ids
    "<p>Contact us at hello dot com</p>",                          # normal sentence, not an email
    "<p>Follow @acme on Twitter</p>",
    "<p>Price 10@5.00</p>",
])
def test_email_not_found(html):
    assert emails_in(html) == []


def test_cloudflare_protected_email_both_forms():
    html = (f'<a href="/cdn-cgi/l/email-protection#{cf_encode("a@acme.com")}">[email protected]</a>'
            f'<span class="__cf_email__" data-cfemail="{cf_encode("b@acme.com")}">[email protected]</span>')
    assert emails_in(html) == ["a@acme.com", "b@acme.com"]
    assert extract.decode_cfemail("zz") is None


def test_same_domain_emails_first():
    emails = ["partner@othercompany.io", "sales@acmeplumbing.com", "x@mail.acmeplumbing.com"]
    assert extract.sort_emails_for_site(emails, BASE) == [
        "sales@acmeplumbing.com", "x@mail.acmeplumbing.com", "partner@othercompany.io"]


# ============================================================== phones

INDIA_PAGE = """<div>
<p>Mobile: +91-98450-43210</p>
<p>Call 9845043211 or 98450 43212</p>
<p>Ph: 022-2345 6789, 022 2345 6790</p>
<p>Office: (080) 4123 4567</p>
<p>Toll free: 1800-266-1234</p>
<p>Fax: 022 2345 6799</p>
<p>Tel/Fax: 022 2345 6800</p>
<p>US office +1 (212) 736-5000</p>
</div>"""


def test_indian_page_all_numbers_and_types():
    assert phones_in(INDIA_PAGE, "IN") == {
        "+91 98450 43210": True,
        "+91 98450 43211": True,
        "+91 98450 43212": True,
        "+91 22 2345 6789": False,
        "+91 22 2345 6790": False,
        "+91 80 4123 4567": False,
        "+91 22 2345 6800": False,    # "Tel/Fax" is also a phone line, so it's kept
        "+1 212-736-5000": False,
    }                                 # "Fax: 022 2345 6799" and the 1800 toll-free number are left out


def test_us_page_uses_labels_for_mobiles():
    html = """<p>Phone: (212) 736-5000</p><p>Cell: 646-464-0123</p><p>Mobile (917) 464 0188</p>
              <p>Fax: 212-736-5001</p><p>Call 1-800-275-2273 toll free</p>"""
    assert phones_in(html, "US") == {  # fax and 1-800 left out
        "+1 212-736-5000": False, "+1 646-464-0123": True, "+1 917-464-0188": True}


@pytest.mark.parametrize("html,region", [
    ('<a href="tel:18002661234">Customer care</a>', "IN"),              # Indian toll-free link
    ("<p>Helpline: 1860 500 5000</p>", "IN"),                           # shared-cost
    ("<p>Freephone 0800 123 4567, enquiries 0845 123 4567</p>", "GB"),  # free / premium rate
    ('<script type="application/ld+json">{"telephone": "+1-888-275-2273"}</script>', "US"),
])
def test_toll_free_and_special_rate_numbers_left_out(html, region):
    tree = extract.parse(html)
    assert phones_in(html, region) == {} and extract.extract_jsonld(tree, region)[1] == []


def test_uk_page():
    assert phones_in("<p>Tel: 020 7946 0018</p><p>Mobile: 07911 123456</p>", "GB") == {
        "+44 20 7946 0018": False, "+44 7911 123456": True}


@pytest.mark.parametrize("html", [
    "<p>GSTIN: 27AAPFU0939F1ZV</p>",
    "<p>CIN: U72900MH2010PTC123456</p>",
    "<p>PIN 400001, Mumbai</p>",
    "<p>© 2010-2024 All rights reserved.</p>",
    "<p>Order #2023456789012 shipped</p>",
    "<p>Open 10:00 - 18:00, Mon-Sat</p>",
    "<p>Price Rs. 1,23,456 only</p>",
    "<p>Dates 12/05/2023 and 2023-05-12</p>",
    "<p>Call 555 today</p>",
    "<script>var phone = '9845043210';</script>",
    "<p>Reference #18.394c3917.1794386023.9f7af13e</p>",   # error/reference codes
    "<p>https://cdn.site.in/img/9845043210/logo.png</p>",   # inside an address
    "<p>?id=9845043210</p>",
    "<!-- 9845043210 -->",
])
def test_not_a_phone_number(html):
    assert phones_in(html, "IN") == {}


def test_same_number_different_formats_counted_once():
    html = "<p>+91 98450 43210</p><p>098450-43210</p><p>9845043210</p><a href='tel:+919845043210'>Call</a>"
    assert phones_in(html) == {"+91 98450 43210": True}


def test_number_split_by_inline_tags():
    assert phones_in("<p><b>+91</b> 98450 <span>43210</span></p>") == {"+91 98450 43210": True}


def test_numbers_in_table_cells_are_separate():
    html = "<table><tr><td>9845043210</td><td>9845043211</td></tr></table>"
    assert phones_in(html) == {"+91 98450 43210": True, "+91 98450 43211": True}


def test_tel_and_whatsapp_links():
    html = """<a href="tel:+91%2098450%2043210">Call</a>
              <a href="https://wa.me/919845043299">Chat</a>
              <a href="https://api.whatsapp.com/send?phone=919845043298&text=hi">Chat</a>
              <a href="tel:02223456789">Office</a>"""
    assert phones_in(html) == {
        "+91 98450 43210": True, "+91 98450 43299": True, "+91 98450 43298": True, "+91 22 2345 6789": False}


def test_us_whatsapp_link_counts_as_mobile():
    assert phones_in('<a href="https://wa.me/16464640123">WhatsApp</a>', "US") == {"+1 646-464-0123": True}


def test_fixture_phones(contact_html):
    assert "+1 415-464-0100" in phones_in(contact_html, "US")
    assert "+44 20 7946 0018" in phones_in(contact_html, "GB")


@pytest.mark.parametrize("html", [
    "<p>Mobile: 98765 43210</p>",                 # the classic Indian dummy number
    "<p>Call 1234567890</p>",
    "<p>Phone 99999 99999</p>",
    "<p>(212) 555-0123</p>",                      # US 555-01xx film/example numbers
    '<a href="tel:+919876543210">Call</a>',
    '<a href="tel:+91 67774560">Call</a>',        # invalid number inside a tel: link
    '<a href="tel:12345">Call</a>',
    '<a href="https://wa.me/919999999999">WhatsApp</a>',
])
def test_dummy_and_invalid_numbers_are_dropped(html):
    assert phones_in(html, "US" if "212" in html else "IN") == {}


# ============================================================== JSON-LD

def test_jsonld(contact_html):
    emails, phones = extract.extract_jsonld(extract.parse(contact_html), "US")
    assert emails == ["hello@acmeplumbing.com"]
    assert {p.number for p in phones} == {"+1 415-464-0132", "+1 212-464-0199"}


def test_jsonld_ignores_fax_placeholders_and_bad_json():
    html = """<script type="application/ld+json">{"telephone":"+91 98450 43210","faxNumber":"+91 22 2345 6799"}</script>
              <script type="application/ld+json">{"telephone":"+91-9876543210"}</script>
              <script type="application/ld+json">{not json</script>"""
    _, phones = extract.extract_jsonld(extract.parse(html), "IN")
    assert phones == [extract.Phone("+91 98450 43210", True)]


# ============================================================== which country a site is in

def region_of(url, html, default):
    tree = extract.parse(html)
    return extract.detect_region(url, [(tree, extract.visible_text(tree))], default)


@pytest.mark.parametrize("url,html,default,expected", [
    # domain ending wins
    ("https://shop.in", "<p>New York, NY 10002</p>", "US", "IN"),
    ("https://shop.co.uk", "", "IN", "GB"),
    # .com sites: clues on the page
    ("https://katz.com", "<p>205 E Houston St, New York, NY 10002</p><p>(212) 254-2246</p>", "IN", "US"),
    ("https://katz.com", "<p>205 East Houston Street</p><p>New York City, 10002</p>"
                         "<p>1-800-4HOTDOG OR (212) 254-2246</p>", "IN", "US"),
    ("https://shop.com", "<p>Call the shop (212-475-4880). NYC delivery</p>", "IN", "US"),
    ("https://shop.com", "<p>Andheri East, Mumbai - 400069, India</p>", "US", "IN"),
    ("https://shop.com", "<p>Store in Bengaluru. Prices from ₹499</p>", "US", "IN"),
    ("https://shop.com", "<p>London W1A 1AA, United Kingdom</p>", "IN", "GB"),
    ("https://shop.com", '<a href="tel:+16464640123">Call</a><p>(646) 464-0124</p>', "IN", "US"),
    ("https://shop.com", '<script type="application/ld+json">{"address":{"addressCountry":"IN"}}</script>', "US", "IN"),
    ("https://shop.com", '<script type="application/ld+json">{"address":{"addressCountry":{"name":"India"}}}</script>', "US", "IN"),
    # things that must NOT be read as US clues
    ("https://shop.com", "<p>Call 987-654-3201 for orders</p>", "IN", "IN"),     # not a valid US number
    ("https://shop.com", "<p>Toll free 1800-266-1234</p>", "IN", "IN"),          # Indian toll-free, not US 1-800
    # no clues: the user's choice
    ("https://shop.com", "<p>Welcome</p>", "IN", "IN"),
    ("https://shop.io", "<p>Welcome</p>", "GB", "GB"),
])
def test_detect_region(url, html, default, expected):
    assert region_of(url, html, default) == expected


def test_us_numbers_on_us_site_even_when_india_chosen():
    html = "<p>205 E Houston St, New York, NY 10002</p><p>Call (212) 254-2246</p>"
    region = region_of("https://katz.com", html, "IN")
    assert phones_in(html, region) == {"+1 212-254-2246": False}


@pytest.mark.parametrize("url,expected", [
    ("https://acme.in", "IN"), ("https://www.acme.co.in/x", "IN"), ("https://acme.co.uk", "GB"),
    ("https://acme.com.au", "AU"), ("https://acme.de", "DE"), ("https://acme.com", "US"),
    ("https://acme.io", "US"), ("https://acme.co", "US"), ("https://acme.ai", "US"),
])
def test_site_region(url, expected):
    assert extract.site_region(url, "US") == expected


# ============================================================== page text

def test_visible_text_lines_and_skips_code(contact_html):
    text = extract.visible_text(extract.parse(contact_html))
    assert "sentry" not in text and "Call us" in text
    assert extract.visible_text(extract.parse("<p>a</p><p>b</p><span>c</span><span>d</span>")) == "a\nb\ncd"


def test_looks_js_rendered_and_not_found():
    assert extract.looks_js_rendered("")
    assert not extract.looks_js_rendered("word " * 200)
    assert extract.looks_not_found(extract.parse("<title>Page not found – Acme</title>"))
    assert extract.looks_not_found(extract.parse("<h1>404</h1>"))
    assert not extract.looks_not_found(extract.parse("<title>Contact us</title><h1>Contact</h1>"))


# ============================================================== which pages to open

def test_contact_links(contact_html):
    links = extract.find_contact_links(extract.parse(contact_html), BASE)
    assert links[0] == "https://www.acmeplumbing.com/contact"   # best match first, #fragment dropped
    assert "https://www.acmeplumbing.com/about-us" in links
    assert "https://www.acmeplumbing.com/support/" in links
    assert not any("facebook" in link for link in links)        # other website
    assert not any(link.endswith(".pdf") for link in links)     # files
    assert not any("blog" in link for link in links)            # irrelevant pages


@pytest.mark.parametrize("path,text,group", [
    ("/contact-us", "", "contact"), ("/contactus.html", "", "contact"), ("/en/contact/", "", "contact"),
    ("/x", "Get in Touch", "contact"), ("/enquiry", "", "contact"), ("/our-branches", "", "contact"),
    ("/about-us", "", "about"), ("/aboutus", "", "about"), ("/x", "Who we are", "about"),
    ("/services", "", "services"), ("/our-services", "", "services"), ("/x", "What we do", "services"),
    ("/pages/get-in-touch", "", "contact"), ("/contact_us.php", "", "contact"), ("/x", "Store Locator", "contact"),
    ("/x", "Customer Care", "contact"), ("/about/our-team", "", "about"), ("/x", "Our Locations", "contact"),
    ("/support", "", "other"),
    # Not the page we want, even though the words appear somewhere
    ("/blog/post", "Read more", None), ("/steam-cleaning", "", None), ("/products", "", None),
    ("/our-services/plumbing", "", None),                                    # one service, not the overview
    ("/investor/shareholder-services", "", None),                            # "services" not at the start
    ("/news/latest-news-about-apollo", "", None),                            # "about" inside a title
    ("/library/how-to-build", "How to build the future, with help from our team and friends", None),  # long text
    ("/contact-us/thank-you", "", None),
])
def test_link_group(path, text, group):
    assert extract.link_group(path, text) == group


def test_plan_pages_round_robin_and_probes():
    home = extract.parse("""
        <a href="/contact-us/">Contact</a><a href="/contact-us">Contact again</a>
        <a href="/locations">Locations</a><a href="/branches">Branches</a>
        <a href="/support">Support</a>""")
    plan = extract.plan_pages(home, "https://acme.com/", 5)
    assert plan == [
        ("https://acme.com/contact-us/", False),   # contact (discovered)
        ("https://acme.com/about", True),          # about (guessed: no link on the homepage)
        ("https://acme.com/services", True),       # services (guessed)
        ("https://acme.com/support", False),       # other
        ("https://acme.com/branches", False),      # 2nd contact-type page (shorter path than /locations)
    ]


def test_plan_pages_guesses_everything_for_empty_homepage():
    plan = extract.plan_pages(extract.parse("<p>Welcome</p>"), "https://acme.com/", 10)
    assert [u for u, _ in plan] == [
        "https://acme.com/contact", "https://acme.com/about", "https://acme.com/services",
        "https://acme.com/contact-us", "https://acme.com/about-us"]
    assert all(guessed for _, guessed in plan)
    assert extract.plan_pages(extract.parse(""), "https://acme.com/", 0) == []


def test_numbers_at_sentence_end_still_found():
    assert phones_in("<p>Call 98450 43210.</p><p>Tel.9845043211</p>") == {"+91 98450 43210": True, "+91 98450 43211": True}
