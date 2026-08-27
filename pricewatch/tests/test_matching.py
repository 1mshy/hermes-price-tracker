from types import SimpleNamespace

from pricewatch import matching


def test_same_product_different_naming():
    assert matching.is_match(
        "Bambu Lab X1-Carbon Combo", "BambuLab X1C 3D Printer w/ AMS", threshold=60)


def test_model_clash_is_a_different_product():
    same = matching.score("Prusa MK4S kit", "Original Prusa MK4S 3D Printer kit")
    clash = matching.score("Prusa MK4S kit", "Original Prusa MK3S+ 3D Printer kit")
    assert same > clash
    assert clash < 72          # must not pass the default match threshold


def test_vague_candidate_cannot_outrank_specific_query():
    specific = matching.score("Polymaker PolyTerra PLA 1kg matte black",
                              "Polymaker PolyTerra PLA matte black 1kg")
    vague = matching.score("Polymaker PolyTerra PLA 1kg matte black", "PLA")
    assert specific > vague
    assert vague < 72


def test_model_tokens():
    assert "p1s" in matching.model_tokens("Bambu Lab P1S Combo")
    assert "mk4s" in matching.model_tokens("Prusa MK4S")


def test_search_terms_compacts_long_descriptions():
    long_query = ("SUNLU AMS Heater compatible with Bambu Lab AMS Gen 1 "
                  "filament dryer 4-spool 70C smart humidity control")
    core = matching.search_terms(long_query)
    assert core.startswith("sunlu")
    assert len(core.split()) <= 5


def test_search_terms_keeps_model_designators():
    core = matching.search_terms(
        "Prusa Research Original Prusa printing machine assembled MK4S upgrade")
    assert "mk4s" in core.split()


def test_search_terms_short_query_passthrough():
    assert matching.search_terms("Prusa MK4S") == matching.normalise("Prusa MK4S")


def test_rank_filters_and_orders():
    candidates = [
        SimpleNamespace(title="Original Prusa MK4S 3D Printer kit"),
        SimpleNamespace(title="Garden hose 25ft"),
        SimpleNamespace(title="Prusa MK4S assembled"),
    ]
    ranked = matching.rank("Prusa MK4S", candidates, threshold=60)
    titles = [c.title for c in ranked]
    assert "Garden hose 25ft" not in titles
    assert len(titles) == 2
