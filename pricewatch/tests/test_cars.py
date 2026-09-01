"""Offline tests for the used-car swarm.

No network: source adapters are exercised against fixtures shaped exactly like
the payloads the real sites serve (schema.org JSON-LD for AutoTrader and
Carpages, an Apollo cache for Kijiji, the two-letter lot JSON for Copart, an
inline GraphQL blob for Marketplace).
"""
from __future__ import annotations

import json
from decimal import Decimal

from pricewatch.cars import geo, report, spec, swarm, taxonomy, valuation
from pricewatch.cars.listing import AUCTION, PRIVATE, RETAIL, SALVAGE, CarListing
from pricewatch.cars.llm import parse_json
from pricewatch.cars.sources import autotrader, carpages, copart, facebook, kijiji


def q(**kwargs):
    base = dict(make="audi", model="A3", location="Laval, Quebec, Canada",
                year=2022, radius_km=150)
    base.update(kwargs)
    return spec.build_offline(**base)


# ── taxonomy ────────────────────────────────────────────────────────────
def test_mileage_handles_french_and_imperial():
    assert taxonomy.parse_mileage_km("61,351 km") == 61351
    assert taxonomy.parse_mileage_km("61 351 kilomètres") == 61351
    assert taxonomy.parse_mileage_km("38,000 miles") == 61155


def test_french_listing_vocabulary():
    assert taxonomy.parse_transmission("Automatique, cuir") == "automatic"
    assert taxonomy.parse_drivetrain("traction intégrale") == "awd"
    assert taxonomy.parse_fuel("hybride rechargeable") == "plug-in hybrid"
    assert taxonomy.looks_salvage("véhicule reconstruit")
    assert not taxonomy.looks_salvage("2022 Audi A3 Komfort")


def test_hybrid_beats_gas_when_both_words_appear():
    assert taxonomy.parse_fuel("Gas/Electric Hybrid") == "hybrid"


def test_model_matching_keeps_trims_and_rejects_siblings():
    assert taxonomy.model_matches("A3", "A3 Sportback")
    assert taxonomy.model_matches("A3", "a3")
    assert not taxonomy.model_matches("A3", "A4")
    assert not taxonomy.model_matches("A3", "Q3")


# ── geography ───────────────────────────────────────────────────────────
def test_place_resolution_and_slug():
    place = geo.resolve_offline("Laval, Quebec, Canada")
    assert (place.region, place.postal) == ("QC", "H7T")
    assert place.slug == "laval"


def test_embedded_postal_code_does_not_break_the_city():
    place = geo.resolve_offline("Montreal H2X 1Y4, QC")
    assert place.city == "Montreal" and place.latitude is not None


def test_distance_is_real():
    place = geo.resolve_offline("Laval, Quebec, Canada")
    assert 140 < geo.city_distance(place, "ottawa", "ON") < 175
    assert geo.city_distance(place, "montreal", "QC") < 25


# ── query parsing and filtering ─────────────────────────────────────────
def test_parses_the_shoppers_sentence():
    parsed = spec.parse_free_text("I want to know all the 2022 audi a3's in laval, quebec, canada")
    assert parsed["make"] == "audi"
    assert parsed["model"] == "a3"
    assert parsed["year_min"] == parsed["year_max"] == 2022
    assert parsed["location"] == "laval, quebec, canada"


def test_location_clause_stops_at_the_next_filter():
    parsed = spec.parse_free_text("2019-2022 Honda Civic near Toronto under $25,000 within 50 km")
    assert parsed["location"] == "Toronto"
    assert (parsed["year_min"], parsed["year_max"]) == (2019, 2022)
    assert parsed["price_max"] == 25000.0
    assert parsed["radius_km"] == 50.0


def test_bare_article_is_not_a_location():
    # "for a cheap Golf" must not make "cheap Golf ..." the place name
    parsed = spec.parse_free_text("looking for a cheap Golf GTI around Montreal")
    assert parsed["location"] == "Montreal"


def test_query_rejects_what_the_sites_wrongly_returned():
    query = q()
    def keep(**kw):
        return query.matches(CarListing(source="t", url="u", title="t", **kw))[0]

    assert keep(year=2022, model="A3", price=Decimal("25000"), distance_km=20)
    assert not keep(year=2020, model="A3", price=Decimal("25000"))      # wrong year
    assert not keep(year=2022, model="Q3", price=Decimal("25000"))      # wrong model
    assert not keep(year=2022, model="A3", price=Decimal("25000"), distance_km=400)
    assert not keep(year=2022, model="A3", price=Decimal("9000"), condition=SALVAGE)


def test_year_filter_rejects_a_listing_with_no_year():
    assert not q().matches(CarListing(source="t", url="u", title="t", model="A3"))[0]


def test_model_must_appear_in_the_title_when_unstructured():
    query = q()
    assert query.matches(CarListing(source="t", url="u", year=2022,
                                    title="2022 Audi A3 Komfort"))[0]
    assert not query.matches(CarListing(source="t", url="u", year=2022,
                                        title="2022 Audi A4 Komfort"))[0]


# ── source parsers ──────────────────────────────────────────────────────
AUTOTRADER_HTML = """
<html><body><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"SearchResultsPage",
 "mainEntity":{"@type":"ItemList","itemListElement":[
  {"@type":"ListItem","position":1,"item":{"@type":["Car","Product"],
   "name":"Audi A3 Progressiv","brand":{"@type":"Brand","name":"Audi"},"model":"A3",
   "vehicleConfiguration":"Progressiv",
   "mileageFromOdometer":{"@type":"QuantitativeValue","value":61351,"unitCode":"KMT"},
   "fuelType":"Gas","vehicleTransmission":"Automatic",
   "itemCondition":"https://schema.org/UsedCondition",
   "offers":{"@type":"Offer","price":25995,"priceCurrency":"CAD",
    "url":"https://www.autotrader.ca/offers/audi-a3-progressiv-2022-abc",
    "seller":{"@type":"AutoDealer","name":"Amiral Autos",
     "address":{"addressLocality":"Laval","addressRegion":"QC"}}}}}]}}]}
</script></body></html>
"""


def test_autotrader_reads_the_json_ld_item_list():
    found = autotrader.parse_results(AUTOTRADER_HTML, q())
    assert len(found) == 1
    car = found[0]
    assert car.price == Decimal("25995") and car.currency == "CAD"
    assert car.year == 2022 and car.trim == "Progressiv"
    assert car.mileage_km == 61351
    assert car.seller_name == "Amiral Autos" and car.seller_type == "dealer"
    assert car.city == "Laval" and car.distance_km is not None


def test_autotrader_converts_an_odometer_given_in_miles():
    html = AUTOTRADER_HTML.replace('"unitCode":"KMT"', '"unitCode":"SMI"')
    assert autotrader.parse_results(html, q())[0].mileage_km == 98735


KIJIJI_HTML = """
<html><script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"__APOLLO_STATE__":{
 "AutosListing:1":{"__typename":"AutosListing","id":"1",
  "title":"2022 Audi A3 Komfort quattro","description":"cuir, toit ouvrant",
  "url":"https://www.kijiji.ca/v-cars-trucks/montreal/audi-a3/1",
  "price":{"__typename":"AutosDealerAmountPrice","amount":2549500,
           "classification":{"rating":"GOOD"}},
  "location":{"name":"City of Montréal","coordinates":{"latitude":45.5019,"longitude":-73.5674}},
  "posterInfo":{"name":"Automobile Gabriel"},
  "attributes":{"all":[
    {"canonicalName":"caryear","canonicalValues":["2022"]},
    {"canonicalName":"carmake","canonicalValues":["audi"]},
    {"canonicalName":"carmodel","canonicalValues":["a3"]},
    {"canonicalName":"cartrim","canonicalValues":["Komfort quattro"]},
    {"canonicalName":"carmileageinkms","canonicalValues":["60994"]},
    {"canonicalName":"vin","canonicalValues":["WAUGUCGY9NA012345"]},
    {"canonicalName":"forsaleby","canonicalValues":["delr"]},
    {"canonicalName":"drivetrain","canonicalValues":["awd"]}]}}}}}}
</script></html>
"""


def test_kijiji_reads_the_apollo_cache():
    car = kijiji.parse_results(KIJIJI_HTML, q())[0]
    assert car.price == Decimal("25495")          # cents in the payload
    assert car.vin == "WAUGUCGY9NA012345"
    assert car.mileage_km == 60994
    assert car.price_rating == "GOOD"
    assert car.seller_type == "dealer" and car.channel == RETAIL
    assert car.distance_km is not None and car.distance_km < 25


def test_kijiji_does_not_pass_a_location_off_as_a_dealer():
    car = kijiji.parse_results(KIJIJI_HTML.replace('"posterInfo":{"name":"Automobile Gabriel"},', ""), q())[0]
    assert car.seller_name is None


def test_kijiji_private_seller_is_a_private_channel():
    car = kijiji.parse_results(KIJIJI_HTML.replace('["delr"]', '["ownr"]'), q())[0]
    assert car.channel == PRIVATE and car.seller_type == "private"


def test_kijiji_location_tree_lookup():
    tree = {"id": 0, "nameEn": "Canada", "children": [
        {"id": 9001, "nameEn": "Quebec", "nameFr": "Québec", "children": [
            {"id": 80002, "nameEn": "Greater Montréal", "children": [
                {"id": 1700278, "nameEn": "Laval / North Shore", "nameFr": "Laval/Rive Nord",
                 "children": []}]}]}]}
    assert kijiji.find_location_id(tree, "Laval") == 1700278
    assert kijiji.find_location_id(tree, "Québec") == 9001
    assert kijiji.find_location_id(tree, "Nowhere") is None


CARPAGES_HTML = """
<html><script type="application/ld+json">
{"@context":"https://schema.org","@type":["Product","Car"],"name":"Cars","offers":[
 {"@type":"Offer","url":"https://www.carpages.ca/used-cars/quebec/saint-hubert/2022-audi-a3-14633547/",
  "itemOffered":"Audi A3 40 PROGRESSIV S-LINE QUATTRO","priceCurrency":"CAD","price":32480}]}
</script></html>
"""


def test_carpages_recovers_year_and_place_from_the_url():
    car = carpages.parse_results(CARPAGES_HTML, q())[0]
    assert car.year == 2022
    assert car.price == Decimal("32480")
    assert (car.city, car.region) == ("Saint Hubert", "QC")
    assert car.make == "audi" and car.model == "a3"


COPART_JSON = {"data": {"results": {"content": [{
    "lotNumberStr": "49645426", "ldu": "2022-audi-a3-qc-montreal", "lcy": 2022,
    "mkn": "AUDI", "lm": "A3", "ltd": "PREMIUM", "ld": "2022 AUDI A3 PREMIUM",
    "hb": 6200.0, "cuc": "CAD", "orr": 48000.0, "locCity": "MONTREAL",
    "locState": "QC", "locCountry": "CAN", "lat": 45.5019, "long": -73.5674,
    "ad": 1788451200000, "dd": "FRONT END", "drv": "ALL WHEEL DRIVE",
    "ft": "GAS", "fv": "WAUFNCF58LA******"}]}}}


def test_copart_lot_is_auction_and_salvage_with_no_asking_price():
    car = copart.parse_results(COPART_JSON, q(include_salvage=True))[0]
    assert car.channel == AUCTION and car.condition == SALVAGE
    assert car.price is None                       # a bid is not an asking price
    assert car.current_bid == Decimal("6200.0")
    assert car.effective_price == Decimal("6200.0")
    assert car.extra["damage"] == "FRONT END"
    assert car.vin is None                         # masked upstream, not invented


def test_auctions_are_excluded_unless_asked_for():
    assert copart.CopartSource().supports(q(include_auctions=True))
    assert not copart.CopartSource().supports(q(include_auctions=False))


FACEBOOK_HTML = (
    'junk{"__typename":"GroupCommerceProductItem","id":"2595725624206039",'
    '"listing_price":{"formatted_amount":"CA$23,550","amount":"23550.00"},'
    '"location":{"reverse_geocode":{"city":"Laval","state":"QC"}},'
    '"is_sold":false,"marketplace_listing_title":"2022 Audi A3 Premium Quattro",'
    '"custom_title":"88 917 km"}more junk'
)


def test_facebook_listing_is_recovered_from_the_inline_blob():
    car = facebook.parse_results(FACEBOOK_HTML, q())[0]
    assert car.price == Decimal("23550.00")
    assert car.year == 2022 and car.city == "Laval"
    assert car.mileage_km == 88917
    assert car.channel == PRIVATE
    assert car.url.endswith("/marketplace/item/2595725624206039")


def test_facebook_skips_sold_listings():
    assert facebook.parse_results(FACEBOOK_HTML.replace('"is_sold":false', '"is_sold":true'), q()) == []


# ── deduplication ───────────────────────────────────────────────────────
def car(**kwargs) -> CarListing:
    base = dict(source="autotrader", url=f"u{kwargs.get('price', 0)}{kwargs.get('source','')}",
                title="2022 Audi A3", year=2022, model="A3")
    base.update(kwargs)
    return CarListing(**base)


def test_same_car_merges_across_sources_even_when_only_one_has_a_vin():
    listings = [
        car(source="kijiji", url="k1", price=Decimal("22298"), mileage_km=99000,
            vin="WAUGUCGY4NA000001", trim="40 Komfort"),
        car(source="autotrader", url="a1", price=Decimal("22298"), mileage_km=99000),
    ]
    merged, duplicates = swarm.deduplicate(listings)
    assert len(merged) == 1 and duplicates == 1
    assert merged[0].vin == "WAUGUCGY4NA000001"
    assert merged[0].extra["also_on"][0]["source"] == "autotrader"


def test_different_cars_at_the_same_price_stay_apart():
    listings = [
        car(source="autotrader", url="a1", price=Decimal("26995"), mileage_km=40100),
        car(source="autotrader", url="a2", price=Decimal("26995"), mileage_km=90539),
    ]
    merged, duplicates = swarm.deduplicate(listings)
    assert len(merged) == 2 and duplicates == 0


def test_unknown_odometer_does_not_collapse_distinct_cars():
    # Three Marketplace ads at one price, none stating mileage: three cars.
    listings = [car(source="facebook", url=f"f{i}", price=Decimal("26995"))
                for i in range(3)]
    merged, _ = swarm.deduplicate(listings)
    assert len(merged) == 3


def test_odometerless_listing_attaches_to_an_unambiguous_match():
    listings = [
        car(source="autotrader", url="a1", price=Decimal("23550"), mileage_km=88917,
            distance_km=0, trim="Premium Quattro"),
        car(source="facebook", url="f1", price=Decimal("23550"), distance_km=0,
            trim="Premium Quattro"),
    ]
    merged, duplicates = swarm.deduplicate(listings)
    assert len(merged) == 1 and duplicates == 1


def test_odometerless_listing_is_left_alone_when_ambiguous():
    listings = [
        car(source="autotrader", url="a1", price=Decimal("26995"), mileage_km=40100),
        car(source="autotrader", url="a2", price=Decimal("26995"), mileage_km=90539),
        car(source="facebook", url="f1", price=Decimal("26995")),
    ]
    merged, _ = swarm.deduplicate(listings)
    assert len(merged) == 3


# ── valuation ───────────────────────────────────────────────────────────
def market(n: int = 8) -> list[CarListing]:
    # a clean −$100 per 1,000 km market
    return [car(source="s", url=f"u{i}", price=Decimal(str(32000 - i * 1000)),
                mileage_km=10000 * i, seller_type="dealer", seller_name=f"Dealer {i}")
            for i in range(1, n + 1)]


def test_price_curve_recovers_the_depreciation_rate():
    curve = valuation.fit_price_curve(market())
    assert curve is not None
    assert -110 < curve.slope * 1000 < -90
    assert curve.r_squared > 0.95


def test_a_nonsense_curve_is_thrown_away():
    rising = [car(source="s", url=f"u{i}", price=Decimal(str(20000 + i * 1000)),
                  mileage_km=10000 * i) for i in range(1, 8)]
    assert valuation.fit_price_curve(rising) is None


def test_curve_needs_enough_points():
    assert valuation.fit_price_curve(market(3)) is None


def test_deal_score_is_measured_against_the_curve():
    listings = market()
    bargain = car(source="s", url="bargain", price=Decimal("25000"), mileage_km=30000)
    listings.append(bargain)
    stats = valuation.market_stats(listings)
    curve = valuation.fit_price_curve(listings)
    valuation.score_listings(listings, curve, stats)
    assert bargain.deal_score > 5                  # ~29,000 expected, asking 25,000
    assert bargain.extra["score_basis"] == "mileage-adjusted"


def test_a_listing_without_an_odometer_is_scored_on_the_median_and_says_so():
    listings = market()
    vague = car(source="s", url="vague", price=Decimal("20000"))
    listings.append(vague)
    stats = valuation.market_stats(listings)
    valuation.score_listings(listings, valuation.fit_price_curve(listings), stats)
    assert vague.extra["score_basis"] == "median-only"


def test_best_value_ignores_the_incomparable_median_only_scores():
    listings = market()
    listings.append(car(source="s", url="vague", price=Decimal("1000")))   # no odometer
    analysis = valuation.analyse(listings)
    assert analysis.best_value is not None
    assert analysis.best_value.url != "vague"


def test_pareto_frontier_drops_dominated_cars():
    good_cheap = car(source="s", url="a", price=Decimal("20000"), mileage_km=50000)
    dominated = car(source="s", url="b", price=Decimal("25000"), mileage_km=80000)
    low_km = car(source="s", url="c", price=Decimal("30000"), mileage_km=20000)
    frontier = valuation.pareto_frontier([good_cheap, dominated, low_km])
    urls = {x.url for x in frontier}
    assert urls == {"a", "c"}


def test_auction_lots_stay_out_of_the_retail_market():
    lot = car(source="copart", url="lot", price=None, current_bid=Decimal("6200"),
              channel=AUCTION, condition=SALVAGE, mileage_km=48000)
    analysis = valuation.analyse(market() + [lot])
    assert analysis.auction_floor == 6200.0
    assert analysis.stats.count == 8               # the wreck is not one of them
    assert analysis.stats.low == 24000.0           # unaffected by the $6,200 bid
    assert lot not in analysis.frontier


def test_dealer_postures_exclude_private_sellers():
    listings = market() + [car(source="fb", url="p", price=Decimal("21000"),
                               seller_type="private", seller_name="Someone")]
    postures = valuation.dealer_postures(listings)
    assert "Someone" not in {p.name for p in postures}
    assert len(postures) == 8


# ── report ──────────────────────────────────────────────────────────────
def build_result(listings):
    from pricewatch.cars.listing import OK, SourceOutcome
    return swarm.SwarmResult(
        query=q(), listings=listings,
        coverage=[SourceOutcome(source="autotrader", status=OK, listings=listings),
                  SourceOutcome(source="kijiji", status="blocked", note="challenge")])


def test_report_renders_and_states_what_it_could_not_see():
    listings = market()
    analysis = valuation.analyse(listings)
    text = report.render(build_result(listings), analysis)
    assert "# 2022 Audi A3 near Laval" in text
    assert "Shortlist" in text and "Coverage" in text
    assert "kijiji" in text and "blocked" in text
    assert "Caveat" in text


def test_report_escapes_pipes_so_tables_survive():
    listings = market()
    listings[0].trim = "40 Komfort | Cuir brun | Quattro"
    analysis = valuation.analyse(listings)
    text = report.render(build_result(listings), analysis)
    row = next(line for line in text.splitlines()
               if "Komfort" in line and line.startswith("|"))
    assert row.count("|") - row.count("\\|") == 9      # 8 columns


# ── model output handling ───────────────────────────────────────────────
def test_json_is_recovered_from_a_chatty_model():
    assert parse_json('Here you go:\n```json\n{"make": "Audi"}\n```') == {"make": "Audi"}
    assert parse_json("<think>hmm</think>\n{\"a\": 1}") == {"a": 1}
    assert parse_json("no json here") is None


# ── data quality guards ─────────────────────────────────────────────────
def test_an_absurd_price_does_not_set_the_market():
    real = [car(source="s", url=f"u{i}", price=Decimal("32000"), mileage_km=50000)
            for i in range(5)]
    scam = car(source="facebook", url="scam", price=Decimal("250"))
    analysis = valuation.analyse(real + [scam])
    assert analysis.stats.low == 32000.0
    assert analysis.stats.count == 5
    assert scam.extra["not_counted"]


def test_a_genuine_bargain_is_still_counted():
    listings = [car(source="s", url=f"u{i}", price=Decimal("32000"), mileage_km=50000)
                for i in range(5)]
    bargain = car(source="s", url="deal", price=Decimal("24000"), mileage_km=90000)
    analysis = valuation.analyse(listings + [bargain])
    assert analysis.stats.low == 24000.0
    assert "not_counted" not in bargain.extra


def test_plausibility_needs_a_peer_group():
    two = [car(source="s", url="a", price=Decimal("30000")),
           car(source="s", url="b", price=Decimal("300"))]
    assert valuation.analyse(two).stats.count == 2
