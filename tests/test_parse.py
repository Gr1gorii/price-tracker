from decimal import Decimal as D

from bfp.config import Selectors, Shop
from bfp.parse import parse_page
from bfp.parse.display import low30_from_text

from conftest import fixture_html

URL = "https://www.shop.test/p/smartphone-x-128"


def test_jsonld_graph_with_strikethrough_and_related_products(shop):
    r = parse_page(fixture_html("jsonld_graph.html"), URL, shop)
    assert r.status == "ok"
    assert r.parse_method == "jsonld"
    assert r.price == D("649.90") and r.currency == "EUR"
    assert r.availability == "InStock"
    assert r.page_gtin == "8806094924541" and r.sku == "SX128"
    assert r.title == "Smartphone X 128GB"
    # related product (19.90) must not make it ambiguous; strikethrough from priceSpecification
    assert r.displayed_prev_price == D("799.00")
    # 30-day low from the generic Italian text fallback
    assert r.displayed_low30_price == D("699.00")
    assert r.display_method == "jsonld+text"


def test_css_selectors_override_for_displayed_fields():
    shop = Shop(name="s", domain="www.shop.test", selectors=Selectors(prev_price="del.old", discount_pct=".badge"))
    r = parse_page(fixture_html("jsonld_graph.html"), URL, shop)
    assert r.displayed_prev_price == D("799.00")
    assert r.displayed_discount_pct == D("19")
    assert r.display_method == "css+text"


def test_list_price_is_rrp_not_previous_price(shop):
    html = (fixture_html("jsonld_graph.html")
            .replace("https://schema.org/StrikethroughPrice", "https://schema.org/ListPrice"))
    r = parse_page(html, URL, shop)
    assert r.displayed_prev_price is None and r.displayed_rrp_price == D("799.00")


def test_rrp_selector():
    shop = Shop(name="s", domain="www.shop.test", selectors=Selectors(rrp_price="p.rrp span"))
    html = fixture_html("jsonld_graph.html").replace("<h1>", '<p class="rrp"><span>€ 1.199,00</span> prezzo consigliato</p><h1>')
    r = parse_page(html, URL, shop)
    assert r.displayed_rrp_price == D("1199.00") and r.displayed_prev_price == D("799.00")


def test_variants_selected_by_ean(shop):
    html = fixture_html("jsonld_variants.html")
    r = parse_page(html, "https://www.shop.test/profumo-y", shop, ean="3614273069557")
    assert r.price == D("119.90") and r.availability == "OutOfStock"


def test_variants_selected_by_url(shop):
    html = fixture_html("jsonld_variants.html")
    r = parse_page(html, "https://www.shop.test/profumo-y?size=50", shop)
    assert r.price == D("79.90") and r.availability == "InStock" and r.page_gtin == "3614273069540"


def test_variants_ambiguous_without_hint(shop):
    r = parse_page(fixture_html("jsonld_variants.html"), "https://www.shop.test/profumo-y", shop)
    assert r.status == "parse_failed"
    assert "ambiguous" in r.error


def test_aggregate_offer_with_range_is_not_a_price(shop):
    r = parse_page(fixture_html("jsonld_aggregate.html"), "https://www.shop.test/lego-z", shop)
    assert r.price is None and r.status in ("parse_failed", "needs_js")


def test_broken_jsonld_lenient_fallback(shop):
    r = parse_page(fixture_html("jsonld_broken.html"), "https://www.shop.test/friggitrice", shop)
    assert r.parse_method == "jsonld" and r.price == D("89.99") and r.availability == "InStock"


def test_microdata(shop):
    r = parse_page(fixture_html("microdata.html"), "https://www.shop.test/v8", shop)
    assert r.parse_method == "microdata"
    assert r.price == D("1299.00") and r.currency == "EUR" and r.page_gtin == "5025155040349"


def test_css_fallback_and_italian_texts():
    shop = Shop(
        name="toys", domain="www.shop.test",
        selectors=Selectors(price=".pdp-price__current", prev_price=".pdp-price__old",
                            discount_pct=".pdp-price__discount", availability=".pdp-stock", title="h1.pdp-title"),
    )
    r = parse_page(fixture_html("css_only.html"), "https://www.shop.test/bambola", shop)
    assert r.parse_method == "css" and r.price == D("24.99") and r.currency == "EUR"
    assert r.displayed_prev_price == D("34.99") and r.displayed_discount_pct == D("29")
    assert r.displayed_low30_price == D("27.50")
    assert r.availability == "InStock" and r.title == "Bambola Fashion con accessori"


def test_css_price_missing_reports_selector(shop):
    s = Shop(name="x", domain="www.shop.test", selectors=Selectors(price=".nope"))
    r = parse_page(fixture_html("css_only.html"), "https://www.shop.test/bambola", s)
    assert r.status == "parse_failed" and "selector matched nothing" in r.error


def test_spa_shell_flagged_needs_js(shop):
    r = parse_page(fixture_html("spa_shell.html"), "https://www.shop.test/x", shop)
    assert r.status == "needs_js"


def test_attr_selector():
    s = Shop(name="x", domain="www.shop.test", selectors=Selectors(price='span[itemprop="price"]@content'))
    html = '<html><body><span itemprop="price" content="12.50">dodici</span>' + "<p>testo</p>" * 400 + "</body></html>"
    assert parse_page(html, "https://www.shop.test/a", s).price == D("12.50")


def test_discount_from_text_consistency():
    from bfp.parse.display import discount_from_text
    t = "Offerte online 29,90 € -33% 19,90 € Il prezzo più basso degli ultimi 30 giorni: 29,90 €"
    assert discount_from_text(t, D("19.90")) == (D("29.90"), D("33"))
    assert discount_from_text("Prima 50,00 € -33% ora 19,90 €", D("19.90")) is None  # inconsistent
    assert discount_from_text("1.299,00 € -23% 999,00 €", D("999.00")) == (D("1299.00"), D("23"))
    assert discount_from_text("29,90 € -33% 19,90 €", D("19.90"), exclude={D("29.90")}) is None


def test_text_discount_fallback_is_opt_in():
    html = ("<html><body><h1>Stiratore</h1><p>Offerte online 29,90 € -33% 19,90 €</p>"
            '<span itemprop="price" content="19.90"></span>' + "<p>testo</p>" * 300 + "</body></html>")
    off = Shop(name="a", domain="www.shop.test", selectors=Selectors(price='span[itemprop="price"]@content'))
    on = Shop(name="b", domain="www.shop.test", text_discount_fallback=True,
              selectors=Selectors(price='span[itemprop="price"]@content'))
    assert parse_page(html, "https://www.shop.test/x", off).displayed_prev_price is None
    r = parse_page(html, "https://www.shop.test/x", on)
    assert r.displayed_prev_price == D("29.90") and r.displayed_discount_pct == D("33") and r.display_method == "text"


def test_low30_phrases():
    cases = {
        "Prezzo più basso degli ultimi 30 giorni: 1.199,00 €": D("1199.00"),
        "Prezzo più basso negli ultimi 30 gg 89,90€": D("89.90"),
        "Il prezzo più basso nei 30 giorni precedenti è € 45": D("45.00"),
        "Prezzo minimo degli ultimi 30 giorni (prezzo di riferimento): 12,00 €": D("12.00"),
        "Prezzo più basso (ultimi 30 giorni) € 299": D("299.00"),
        "Prezzo attuale € 16,00 -10% Ultimo prezzo più basso € 15,00 -4%": D("15.00"),  # Notino
        "Prezzo più basso garantito! 49,90 €": None,  # no "30 giorni"
        "Spedizione gratuita sopra 30 €": None,
    }
    for text, exp in cases.items():
        assert low30_from_text(text) == exp, text
