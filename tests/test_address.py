"""State and country, read only from the business's postal address."""
import pytest

from contact_scraper import address, extract


def location(html: str) -> tuple[str, str]:
    tree = extract.parse(html)
    return address.detect_location([(tree, extract.visible_text(tree))])


@pytest.mark.parametrize("html,expected", [
    # India: PIN code with the state close by, or "<State>, India"
    ("<footer><p>12 MG Road,<br>Pune, Maharashtra 411001<br>India</p></footer>", ("Maharashtra", "India")),
    ("<p>Residency Road, Srinagar, Jammu &amp; Kashmir 190001</p>", ("Jammu and Kashmir", "India")),
    ("<p>Connaught Place, New Delhi 110001</p>", ("Delhi", "India")),
    ("<p>Anna Salai, Chennai - 600 002, Tamilnadu</p>", ("Tamil Nadu", "India")),
    ("<p>Panaji, Goa, India</p>", ("Goa", "India")),
    ("<p>Shop 4, Linking Road, Mumbai - 400050, India</p>", ("", "India")),      # no state written
    # United States
    ("<address>350 5th Ave<br>New York, NY 10118</address>", ("New York", "United States")),
    ("<p>100 Congress Ave, Austin, Texas 78701-4042</p>", ("Texas", "United States")),
    ("<p>1600 Pennsylvania Ave NW, Washington, DC 20500</p>", ("District of Columbia", "United States")),
    # Canada, Australia, UK
    ("<p>290 Bremner Blvd, Toronto, ON M5V 3L9</p>", ("Ontario", "Canada")),
    ("<p>1 Macquarie St, Sydney NSW 2000</p>", ("New South Wales", "Australia")),
    ("<p>221B Baker Street, London NW1 6XE</p>", ("England", "United Kingdom")),
    ("<p>10 Princes St, Edinburgh EH2 2AN</p>", ("Scotland", "United Kingdom")),
    ("<p>Queen St, Cardiff CF10 2BH</p>", ("Wales", "United Kingdom")),
])
def test_written_addresses(html, expected):
    assert location(html) == expected


@pytest.mark.parametrize("html,expected", [
    ('<script type="application/ld+json">{"@type": "LocalBusiness", "address": {"@type": "PostalAddress",'
     ' "addressRegion": "KA", "addressCountry": "IN"}}</script>', ("Karnataka", "India")),
    ('<script type="application/ld+json">{"address": {"addressRegion": "CA", "addressCountry": "US"}}</script>',
     ("California", "United States")),
    ('<script type="application/ld+json">{"address": {"addressRegion": "Bayern", "addressCountry": '
     '{"@type": "Country", "name": "Germany"}}}</script>', ("Bayern", "Germany")),
    ('<script type="application/ld+json">{"address": "350 5th Ave, New York, NY 10118"}</script>',
     ("New York", "United States")),
    ('<div itemscope itemtype="https://schema.org/PostalAddress"><span itemprop="addressRegion">Kerala</span>'
     '<span itemprop="addressCountry">India</span></div>', ("Kerala", "India")),
])
def test_schema_org_address(html, expected):
    assert location(html) == expected


@pytest.mark.parametrize("html", [
    "<p>We deliver across India and the USA. Call 98450 43210.</p>",   # mentions, not an address
    "<p>Call (212) 254-2246 or 212-475-4880</p>",                     # phone numbers are not an address
    "<p>Order #411001 shipped. Model QX12 3CD.</p>",                   # codes that look like postcodes
    "<p>Open 9am-6pm, Monday to Saturday.</p>",
])
def test_no_address_means_blank(html):
    assert location(html) == ("", "")


def test_most_mentioned_address_wins():
    head_office = "<p>HQ: Bandra, Mumbai, Maharashtra 400050</p>"
    branch = "<p>Branch: Koramangala, Bengaluru, Karnataka 560034</p>"
    pages = [extract.parse(h) for h in (head_office + branch, head_office)]
    assert address.detect_location([(t, extract.visible_text(t)) for t in pages]) == ("Maharashtra", "India")


@pytest.mark.parametrize("html,expected", [
    # a state written after a town, with no postcode or country: the country comes from the state
    ("<footer><p>2nd Floor, FC Road, Pune, Maharashtra</p></footer>", ("Maharashtra", "India")),
    ("<footer><p>Jaipur, Rajasthan</p></footer>", ("Rajasthan", "India")),
    ("<p>Visit our store in Austin, Texas.</p>", ("Texas", "United States")),
    ("<p>We serve Texas, Ohio and California.</p>", ("", "")),          # a list of states, not a town
    ('<script type="application/ld+json">{"address": {"addressRegion": "Kerala"}}</script>', ("Kerala", "India")),
])
def test_state_without_postcode_or_country(html, expected):
    assert location(html) == expected


@pytest.mark.parametrize("html,expected", [
    ("<footer><p>12 MG Road,<br>Pune, Maharashtra 411001<br>India</p></footer>", ("Pune", "411001")),
    ("<p>Connaught Place, New Delhi 110001</p>", ("New Delhi", "110001")),
    ("<p>Anna Salai, Chennai - 600 002, Tamilnadu</p>", ("Chennai", "600002")),
    ("<p>Shop 4, Linking Road, Mumbai-400050, Maharashtra</p>", ("Mumbai", "400050")),
    ("<p>HQ: Bandra, Mumbai, Maharashtra 400050</p>", ("Mumbai", "400050")),
    ("<p>Panaji, Goa, India</p>", ("Panaji", "")),
    ("<footer><p>Jaipur, Rajasthan</p></footer>", ("Jaipur", "")),
    ("<address>350 5th Ave<br>New York, NY 10118</address>", ("New York", "10118")),
    ("<p>100 Congress Ave, Austin, Texas 78701-4042</p>", ("Austin", "78701-4042")),
    ("<p>290 Bremner Blvd, Toronto, ON M5V3L9</p>", ("Toronto", "M5V 3L9")),
    ("<p>1 Macquarie St, Sydney NSW 2000</p>", ("Sydney", "2000")),
    ("<p>221B Baker Street, London NW1 6XE</p>", ("London", "NW1 6XE")),
    ('<script type="application/ld+json">{"address": {"addressLocality": "Kochi", "addressRegion": "Kerala", '
     '"postalCode": "682001"}}</script>', ("Kochi", "682001")),
    ("<p>Call 022-400050 now</p>", ("", "")),
])
def test_city_and_pin_code(html, expected):
    tree = extract.parse(html)
    place = address.detect_place([(tree, extract.visible_text(tree))])
    assert (place.city, place.postcode) == expected


def test_schema_country_beats_a_stray_written_postcode():
    html = ('<script type="application/ld+json">{"address": {"addressRegion": "Gujarat", "addressCountry": "India"}}'
            '</script><p>Our London partner: 1 Fleet St, London EC4Y 1AA</p>')
    assert location(html) == ("Gujarat", "India")


# ------------------------------------------------------------------ the address itself

def full_address(*pages_html: str) -> str:
    pages = [(t, extract.visible_text(t)) for t in map(extract.parse, pages_html)]
    return address.detect_address(pages, address.detect_location(pages)[1])


@pytest.mark.parametrize("html,expected", [
    # street lines above the postcode line, country on the line below, phone line left out
    ("<footer><p>12 MG Road,<br>Pune, Maharashtra 411001<br>India</p><p>Phone: +91 98450 43210</p></footer>",
     "12 MG Road, Pune, Maharashtra 411001, India"),
    ("<p>Welcome to our shop. Visit us at 100 Congress Ave, Austin, Texas 78701-4042. Open daily.</p>",
     "100 Congress Ave, Austin, Texas 78701-4042"),
    ("<p>&copy; 2024 Acme | 221B Baker Street, London NW1 6XE | Call 020 7946 0958</p>",
     "221B Baker Street, London NW1 6XE"),
    ("<div>Home</div><div>About</div><p>HQ: Bandra, Mumbai, Maharashtra 400050</p>",
     "Bandra, Mumbai, Maharashtra 400050"),
    ("<address>Acme Corp<br>350 5th Ave<br>New York, NY 10118<br>Tel: (212) 555-1234<br>info@acme.com</address>",
     "Acme Corp, 350 5th Ave, New York, NY 10118"),
    # countries without a postcode pattern: found from an "Address" label
    ("<p>Address: Office 1204, Bay Square, Business Bay, Dubai, UAE</p>",
     "Office 1204, Bay Square, Business Bay, Dubai, UAE"),
    ("<h4>Our Address</h4><p>Office 1204, Bay Square,</p><p>Business Bay, Dubai</p><p>United Arab Emirates</p>"
     "<p>Email: hello@example.ae</p>", "Office 1204, Bay Square, Business Bay, Dubai, United Arab Emirates"),
    # schema.org parts are joined, codes spelled out
    ('<script type="application/ld+json">{"address": {"streetAddress": "12 MG Road", "addressLocality": "Pune",'
     ' "addressRegion": "MH", "postalCode": "411001", "addressCountry": "IN"}}</script>',
     "12 MG Road, Pune, Maharashtra 411001, India"),
    ('<div itemscope itemtype="https://schema.org/PostalAddress"><span itemprop="streetAddress">1 Macquarie St'
     '</span> <span itemprop="addressLocality">Sydney</span> <span itemprop="postalCode">2000</span></div>',
     "1 Macquarie St, Sydney, 2000"),
])
def test_full_address(html, expected):
    assert full_address(html) == expected


@pytest.mark.parametrize("html", [
    "<p>We deliver across India and the USA. Call 98450 43210.</p>",
    "<p>Office: 9am-6pm, Monday to Saturday</p>",
    "<address>Email: info@acme.com<br>Phone: (212) 555-1234</address>",
])
def test_no_full_address(html):
    assert full_address(html) == ""


def test_address_prefers_schema_and_the_sites_own_country():
    schema = ('<script type="application/ld+json">{"address": {"streetAddress": "5 Ring Road", '
              '"addressLocality": "Ahmedabad", "addressRegion": "Gujarat", "addressCountry": "India"}}</script>')
    written = "<p>Showroom: 9 CG Road, Ahmedabad, Gujarat 380009</p>"
    assert full_address(written + schema) == "5 Ring Road, Ahmedabad, Gujarat, India"
    # a partner office abroad is not taken as the business's address
    html = ('<script type="application/ld+json">{"address": {"addressRegion": "Gujarat", "addressCountry": "India"}}'
            '</script><p>Our London partner: 1 Fleet St, London EC4Y 1AA</p>')
    assert full_address(html) == ""
