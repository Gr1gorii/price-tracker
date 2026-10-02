from decimal import Decimal as D

from bfp.parse import parse_page
from bfp.sanitize import sanitize_html

from conftest import fixture_html


def test_reviews_and_personal_data_removed_product_kept(shop):
    html = fixture_html("jsonld_graph.html")
    clean = sanitize_html(html)
    assert "Mario Rossi" not in clean
    assert "mario.rossi@example.com" not in clean
    assert "aggregateRating" not in clean and '"review"' not in clean
    assert "tel:+39" not in clean
    r = parse_page(clean, "https://www.shop.test/p/smartphone-x-128", shop)
    assert r.price == D("649.90") and r.displayed_prev_price == D("799.00") and r.displayed_low30_price == D("699.00")


def test_microdata_review_removed(shop):
    clean = sanitize_html(fixture_html("microdata.html"))
    assert "Giulia" not in clean
    r = parse_page(clean, "https://www.shop.test/v8", shop)
    assert r.price == D("1299.00")


def test_scripts_and_forms_removed():
    clean = sanitize_html('<html><body><form><input name="email" value="a@b.it"></form>'
                          '<script>var user={"email":"x@y.it"}</script><p onclick="x()">ok</p></body></html>')
    assert "<form" not in clean and "<input" not in clean and "x@y.it" not in clean and "a@b.it" not in clean
    assert "onclick" not in clean and ">ok<" in clean


def test_price_inside_form_is_kept_and_attr_emails_masked():
    html = ('<html><body><div id="app" data-user-agent="Bot (+mailto:me@example.com)"></div>'
            '<form class="cart"><p data-discount>-10%</p><button>Aggiungi</button></form></body></html>')
    clean = sanitize_html(html)
    assert "data-discount" in clean and "-10%" in clean and "Aggiungi" not in clean
    assert "me@example.com" not in clean and "[email]" in clean
