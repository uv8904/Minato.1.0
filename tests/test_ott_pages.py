"""Offline render tests for the JioHotstar storefront pages (Option A).

The pages are rendered with *fake* catalog payloads, so these tests pin down the
contract the browser code depends on — element ids, ``data-*`` attributes, the
``#ott-config`` / ``#ott-boot`` JSON bootstraps and the "never link to nothing"
rule — without needing MongoDB, Telegram or TMDB::

    pytest tests/test_ott_pages.py -q
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dreamxbotz.util import ott_catalog, ott_pages  # noqa: E402

BOT = "MyMovieBot"

SITE = {
    "home_path": "/home",
    "search_path": "/search",
    "home_api": "/api/ott/home",
    "search_api": "/api/ott/search",
    "genres_api": "/api/ott/genres",
    "movie_api": "/api/ott/movie",
    "suggest_api": "/api/ott/suggest",
    "rail_limit": 18,
    "hero_limit": 6,
    "search_limit": 24,
    "search_max_limit": 60,
    "suggest_limit": 8,
    "poll": 90,
    "browser_ttl": 60,
    "trailer_volume": True,
    "bot_username": BOT,
}


def card(movie_id="jawan-2023", title="Jawan", **over):
    """One public card, exactly as ``ott_catalog.card_for`` builds it."""
    data = {
        "id": movie_id,
        "title": title,
        "year": 2023,
        "quality": "1080p",
        "quality_label": "1080p, 720p",
        "bucket": "1080p",
        "qualities": ["1080p", "720p"],
        "genres": ["Action", "Thriller"],
        "rating": 7.0,
        "runtime": 169,
        "overview": "A prison warden recruits inmates to fight crime.",
        "trailer": "MwoUr5wPz9o",
        "poster": f"/api/movies/poster/{movie_id}?v=abc",
        "backdrop": f"/api/movies/backdrop/{movie_id}?v=abc",
        "deeplink": f"https://t.me/{BOT}?start=movie_{movie_id}",
        "search_url": f"/search?q={title}",
        "link": f"https://t.me/{BOT}?start=movie_{movie_id}",
        "added": "2 hours ago",
        "added_at": "2026-09-29T10:00:00+00:00",
        "files": 3,
        "bytes": 4 * 1024 ** 3,
        "is_series": False,
        "upcoming": False,
        "release_label": "",
        "release_date": "",
        "waiting": 0,
    }
    data.update(over)
    return data


def rail(rail_id="new", title="New on MinatoVerse", kind="new", cards=None, **over):
    data = {
        "id": rail_id,
        "title": title,
        "subtitle": "Freshly indexed",
        "kind": kind,
        "icon": "🎬",
        "genre": over.pop("genre", ""),
        "cards": cards if cards is not None else [card()],
    }
    data.update(over)
    return data


def payload(rails=6, hero=None):
    hero_cards = hero if hero is not None else [
        card("jawan-2023", "Jawan"),
        card("kalki-2898-ad", "Kalki 2898 AD", year=2024, genres=["Science Fiction"]),
    ]
    rail_list = [rail()]
    for index in range(1, rails):
        rail_list.append(
            rail(
                rail_id=f"genre-{index}",
                title=f"Genre {index}",
                kind="genre",
                genre=f"Genre {index}",
                cards=[card(f"movie-{index}-a", f"Movie {index} A"), card(f"movie-{index}-b", f"Movie {index} B")],
            )
        )
    return {
        "ok": True,
        "hero": hero_cards,
        "rails": rail_list,
        "genres": [{"name": "Action", "count": 4, "label": "Action"}],
        "counts": {"movies": 12, "with_trailer": 3, "rails": len(rail_list)},
        "updated_at": "2026-09-30 08:00:00",
    }


def search_payload(results=None, total=None, facets=None):
    rows = results if results is not None else [card()]
    return {
        "ok": True,
        "query": "jawan",
        "results": rows,
        "total": len(rows) if total is None else total,
        "facets": facets or {
            "genres": [{"name": "Action", "count": 4}],
            "qualities": [{"name": "1080p", "count": 3}, {"name": "4K", "count": 1}],
            "years": [2024, 2023],
        },
        "has_more": False,
        "limit": 24,
        "offset": 0,
        "page": 1,
        "pages": 1,
    }


def patch_catalog(monkeypatch, *, home=None, search=None, error=None):
    async def fake_home(**_kwargs):
        if error:
            raise RuntimeError(error)
        return home if home is not None else payload()

    async def fake_search(_query="", **_kwargs):
        if error:
            raise RuntimeError(error)
        return search if search is not None else search_payload()

    monkeypatch.setattr(ott_catalog, "cached_home_payload", fake_home)
    monkeypatch.setattr(ott_catalog, "cached_search", fake_search)
    monkeypatch.setattr(ott_catalog, "site_context", lambda: dict(SITE))


def render_home(context=None):
    return asyncio.run(ott_pages.render_home(context))


def render_search(context=None):
    return asyncio.run(ott_pages.render_search(context))


def embedded_json(html, element_id):
    match = re.search(
        r'<script type="application/json" id="%s">(.*?)</script>' % re.escape(element_id),
        html,
        re.S,
    )
    assert match, f"#{element_id} is missing from the page"
    return json.loads(match.group(1))


# --------------------------------------------------------------------------- #
# Home
# --------------------------------------------------------------------------- #
def test_home_renders_hero_rails_and_bootstraps(monkeypatch):
    patch_catalog(monkeypatch)

    html = render_home({"view": ""})

    # hero + trailer layer + controls
    for needle in (
        'id="otHero"',
        'id="otTrailerLayer"',
        'id="otHeroSlides"',
        'id="otHeroPrev"',
        'id="otHeroNext"',
        'class="ot-dot',
        "data-trailer=\"MwoUr5wPz9o\"",
        "Play in Telegram",
    ):
        assert needle in html, needle

    # rails (server rendered) + cards
    assert 'id="otRailList"' in html
    assert 'data-rail="new"' in html
    assert html.count('class="ot-card') >= 3
    assert 'data-role="mylist"' in html
    assert 'data-role="info"' in html

    # assets (versioned so a deploy busts the browser cache)
    assert re.search(r"/static/ott_home\.css\?v=[\w.\-]+", html)
    assert re.search(r"/static/ott_home\.js\?v=[\w.\-]+", html)

    config = embedded_json(html, "ott-config")
    assert config["bot"] == BOT
    assert config["open_url"] == f"https://t.me/{BOT}?start=ott"
    assert config["home_api"] == "/api/ott/home"
    assert config["limits"]["rail"] == 18
    # My List / Continue Watching share the watch page's storage keys
    assert config["keys"] == {"continue": "dx:cw:list", "mylist": "dx:mylist", "resume": "dx:resume:"}

    boot = embedded_json(html, "ott-boot")
    assert len(boot["hero"]) == 2
    assert len(boot["rails"]) == 6
    # the bootstrap is trimmed to what the client rendering needs
    assert "qualities" not in boot["rails"][0]["cards"][0]
    assert "runtime" not in boot["hero"][0]
    assert boot["rails"][0]["cards"][0]["deeplink"].endswith("start=movie_jawan-2023")


def test_home_has_no_empty_links_and_keeps_secrets_out(monkeypatch):
    patch_catalog(monkeypatch)

    html = render_home({})

    assert not re.search(r'\b(href|src)=""', html)
    assert "start=movie_jawan-2023" in html
    # nothing private is ever part of a page
    for secret in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "api_hash"):
        assert secret not in html


def test_home_renders_only_the_first_rails_server_side(monkeypatch):
    patch_catalog(monkeypatch, home=payload(rails=8))

    html = render_home({})

    assert html.count('class="ot-rail"') == ott_catalog.SERVER_RENDERED_RAILS == 4
    assert 'data-rail="genre-4"' not in html          # rail #5 is client-mounted only
    # …while the bootstrap still carries every rail for the client to mount
    rails = embedded_json(html, "ott-boot")["rails"]
    assert [row["id"] for row in rails] == ["new"] + [f"genre-{index}" for index in range(1, 8)]


def test_home_falls_back_to_an_empty_storefront_when_the_catalog_fails(monkeypatch):
    patch_catalog(monkeypatch, error="mongo is down")

    html = render_home({})

    assert 'id="otHero"' in html
    assert 'data-empty="1"' in html                 # hero shows the empty state
    assert "ot-hero-empty" in html
    assert 'class="ot-alert"' in html               # …and the visitor gets a hint
    assert "Open the bot" in html
    assert embedded_json(html, "ott-boot")["rails"] == []


def test_home_renders_the_fallback_page_when_the_template_is_missing(monkeypatch):
    patch_catalog(monkeypatch)
    monkeypatch.setattr(ott_pages, "_load_template", lambda _name: None)

    html = render_home({})

    assert html.startswith("<!DOCTYPE html>")
    assert "noindex, nofollow" in html
    assert "storefront template is missing" in html


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def test_search_renders_results_filters_and_the_inline_hint(monkeypatch):
    patch_catalog(monkeypatch, search=search_payload())

    html = render_search({"query": "jawan", "genre": "Action", "year": "2023", "quality": "1080p", "sort": "rating"})

    for needle in (
        'id="otGrid"',
        'id="otFilters"',
        'id="otLoadMore"',
        'id="otShown"',
        'id="otTotal"',
        'id="otSearchCount"',
        'id="otSearchInput"',
        'id="otSuggest"',
        'id="otSearchForm"',
        'id="otEmpty"',
        'id="otSheet"',
        'data-role="trailer"',
    ):
        assert needle in html, needle

    assert "data-quality=\"1080p\"" in html
    assert "data-genre=\"Action\"" in html
    assert "jawan" in html
    # Option B is advertised right on the search page
    assert "ot-inline-hint" in html
    assert f"@{BOT} jawan" in html
    assert re.search(r"/static/ott_search\.(css|js)\?v=[\w.\-]+", html)

    boot = embedded_json(html, "ott-boot")
    assert boot["query"] == "jawan"
    assert boot["results"][0]["id"] == "jawan-2023"
    assert embedded_json(html, "ott-config")["search_api"] == "/api/ott/search"


def test_search_renders_the_empty_state(monkeypatch):
    patch_catalog(monkeypatch, search=search_payload(results=[], total=0))

    html = render_search({"query": "nope"})

    # the empty state is visible (no `hidden`) and the "Load more" button is not
    assert re.search(r'id="otEmpty"(?![^>]*\bhidden\b)', html)
    assert re.search(r'id="otLoadMore"[^>]*\bhidden\b', html)
    assert "Nothing matched" in html


def test_search_context_never_collides_with_the_site_keys(monkeypatch):
    """Regression: merging the filters into ``site`` raised
    ``Template.render() got multiple values for keyword argument 'query'``."""
    patch_catalog(monkeypatch)

    html = render_search({"query": "jawan", "year": 2023, "sort": "newest", "quality": "4K", "genre": "Action"})

    assert 'id="otGrid"' in html
    assert embedded_json(html, "ott-boot")["year"] == "2023"


def test_search_survives_a_failing_catalog(monkeypatch):
    patch_catalog(monkeypatch, error="boom")

    html = render_search({"query": "jawan"})

    assert 'id="otGrid"' in html
    assert 'class="ot-alert"' in html
    assert "Search is warming up" in html


# --------------------------------------------------------------------------- #
# Template ↔ JavaScript contract
# --------------------------------------------------------------------------- #
def test_js_and_template_contract_stays_in_sync(monkeypatch):
    """Cheap guard for the hand-written JS (no browser needed).

    Every element id the scripts look up must exist in one of the two rendered
    pages, and the rails payload key stays ``cards`` — ``items`` would resolve to
    ``dict.items`` inside Jinja and break the rails (a real regression).
    """
    patch_catalog(monkeypatch)
    html = render_home({}) + render_search({"query": "jawan"})

    scripts = "\n".join(
        (ROOT / "dreamxbotz" / "static" / name).read_text(encoding="utf-8")
        for name in ("ott_home.js", "ott_search.js")
    )
    created_at_runtime = {"otSearchLive"}  # inserted by ott_search.js::statusLine()
    ids = set(re.findall(r'getElementById\(\s*"([\w-]+)"', scripts))
    assert ids - created_at_runtime
    for element_id in sorted(ids - created_at_runtime):
        assert f'id="{element_id}"' in html, element_id

    home_template = (ROOT / "dreamxbotz" / "template" / "ott_home.html").read_text(encoding="utf-8")
    assert "rail.cards" in home_template and "rail.items" not in home_template
    assert "items: cards" not in scripts, "client-built rails must use the `cards` key"
    boot = embedded_json(html, "ott-boot")  # the home page's bootstrap
    assert boot["rails"] and all("cards" in rail for rail in boot["rails"])


def test_cards_never_render_an_empty_href_without_a_bot_username(monkeypatch):
    """A half-configured bot (no BOT_USERNAME) must still produce real links."""
    site = dict(SITE, bot_username="")
    monkeypatch.setattr(ott_catalog, "site_context", lambda: site)
    orphan = card("orphan-2020", "Orphan", deeplink="", link="/search?q=Orphan")
    payload_ = payload(hero=[orphan])
    payload_["rails"] = [rail(cards=[orphan])]

    async def fake_home(**_kwargs):
        return payload_

    monkeypatch.setattr(ott_catalog, "cached_home_payload", fake_home)

    html = render_home({})

    assert not re.search(r'\b(href|src)=""', html)
    assert "Browse the catalogue" in html          # hero play button falls back
    assert 'href="/search?q=Orphan"' in html
    # the "Open the bot" buttons fall back to the updates channel / telegram.org
    import info

    assert embedded_json(html, "ott-config")["open_url"] == (
        str(getattr(info, "UPDATE_CHNL_LNK", "") or "https://telegram.org")
    )
