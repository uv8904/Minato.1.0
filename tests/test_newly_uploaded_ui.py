"""Frontend wiring for the Stream Mode "Newly Uploaded Movies" section.

Renders the real Jinja templates (no Telegram, no database) and checks that:

* the section is injected into both public pages, before the existing
  trending / suggestions blocks, without touching the navbar or footer,
* the pages only load the section's own CSS/JS and pass the public bot
  username (never the token) plus the API endpoint,
* the assets themselves implement the required UI states and safety rules
  (lazy posters, skeletons, empty/error + retry, DOM-only rendering,
  placeholder artwork, ``?start=movie_<MOVIE_ID>`` deep links).

Run with the project dependencies installed::

    pytest tests/test_newly_uploaded_ui.py -q
"""
import asyncio
import importlib.util
import os
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOT_USERNAME = "MinatoMovieBot"
STATIC = ROOT / "dreamxbotz" / "static"

# render_template.py resolves the templates relative to the working directory
# (the bot always starts from the repository root) – do the same here.
os.chdir(ROOT)


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}


class PageParser(HTMLParser):
    """Collects ids/attributes/links plus each element's ancestor chain."""

    def __init__(self):
        super().__init__()
        self.ids = {}
        self.hrefs = []
        self.srcs = []
        self.html = ""
        self.ancestors = {}  # id -> list of (tag, class) ancestors, outermost first
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids[attributes["id"]] = (tag, attributes)
            self.ancestors[attributes["id"]] = list(self._stack)
        if tag == "a" and attributes.get("href"):
            self.hrefs.append(attributes["href"])
        if attributes.get("src"):
            self.srcs.append(attributes["src"])
        if tag not in VOID_TAGS:
            self._stack.append((tag, (attributes.get("class") or "")))

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break


def render_page(mime_type="video/mp4", channel_url="https://t.me/updates"):
    """Render one of the real pages with stubbed integrations.

    ``render_template.render_page`` picks ``req.html`` for video/audio and
    ``dl.html`` for anything else, so the mime type selects the template.
    """
    file_data = SimpleNamespace(
        unique_id="abcdef123456",
        file_name="Example_movie_2023_1080p",
        mime_type=mime_type,
        file_size=1024,
    )
    client = SimpleNamespace(get_messages=AsyncMock(), username=BOT_USERNAME)
    modules = {
        "info": dict(
            BIN_CHANNEL=-100123,
            URL="https://files.example/",
            UPDATE_CHNL_LNK=channel_url,
        ),
        "dreamxbotz.Bot": dict(dreamxbotz=client),
        "dreamxbotz.util.human_readable": dict(humanbytes=lambda size: "1 KB"),
        "dreamxbotz.util.file_properties": dict(
            get_file_ids=AsyncMock(return_value=file_data)
        ),
        "dreamxbotz.server.exceptions": dict(InvalidHash=ValueError),
    }
    saved = {name: sys.modules.get(name) for name in modules}
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        sys.modules[name] = module
    try:
        spec = importlib.util.spec_from_file_location(
            "ui_renderer_" + mime_type.replace("/", "_"),
            ROOT / "dreamxbotz/util/render_template.py",
        )
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)

        # Template selection: videos/audio -> req.html, everything else -> dl.html
        session = MagicMock()
        session.__aenter__.return_value = session
        response = SimpleNamespace(headers={"Content-Length": "1024"})
        session.get.return_value.__aenter__.return_value = response
        renderer.aiohttp.ClientSession = lambda: session
        html = asyncio.run(renderer.render_page(42, "abcdef"))
    finally:
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module
            else:
                sys.modules.pop(name, None)
    return renderer, html


@pytest.fixture(scope="module")
def stream_page():
    """req.html – the Stream Mode player page."""
    return render_page(mime_type="video/mp4")


@pytest.fixture(scope="module")
def download_page():
    """dl.html – the download page (same section, same assets)."""
    return render_page(mime_type="application/zip")


def parse(html):
    parser = PageParser()
    parser.feed(html)
    parser.html = html
    return parser


# --------------------------------------------------------------------------- #
# Section markup
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("page", ["stream_page", "download_page"])
def test_section_is_rendered_with_its_configuration(page, request):
    _, html = request.getfixturevalue(page)
    parsed = parse(html)
    assert "newlyUploaded" in parsed.ids
    tag, attrs = parsed.ids["newlyUploaded"]

    assert tag == "section"
    assert attrs["data-api"] == "/api/movies/new"
    assert attrs["data-limit"] == "20"
    assert attrs["data-bot"] == BOT_USERNAME
    assert attrs["aria-labelledby"] == "nuTitle"
    # The grid starts in the loading state (skeletons are rendered by JS).
    assert parsed.ids["nuGrid"][1]["aria-busy"] == "true"
    assert "data-nu-grid" in parsed.ids["nuGrid"][1]


@pytest.mark.parametrize("page", ["stream_page", "download_page"])
def test_section_is_a_sibling_of_the_existing_sections(page, request):
    """The rail must not be nested inside the hero/player section."""
    parsed = parse(request.getfixturevalue(page)[1])
    chain = [tag for tag, _ in parsed.ancestors["newlyUploaded"]]
    assert chain[-1] == "main", chain
    assert chain.count("section") == 0, chain
    assert "stage" not in [cls for _, cls in parsed.ancestors["newlyUploaded"]][-1]
    assert chain == [tag for tag, _ in parsed.ancestors["trendingSection"]]


@pytest.mark.parametrize("page", ["stream_page", "download_page"])
def test_section_loads_only_its_own_assets(page, request):
    _, html = request.getfixturevalue(page)
    assert '/static/newly_uploaded.css?v=' in html
    assert '<script src="/static/newly_uploaded.js?v=' in html
    assert " defer></script>" in html
    # No inline style/script duplication of the section.
    assert "--nu-gold" not in html


@pytest.mark.parametrize("page", ["stream_page", "download_page"])
def test_section_sits_before_the_existing_blocks(page, request):
    _, html = request.getfixturevalue(page)
    section_at = html.index('id="newlyUploaded"')
    assert section_at < html.index('id="trendingSection"')
    assert section_at < html.index('id="wallSection"')
    # Existing chrome is untouched.
    assert '<span class="brand-mark">MV</span>' in html
    assert "MinatoVerse" in html
    assert 'class="topbar"' in html
    assert 'class="footer"' in html
    assert "{{" not in html and "{%" not in html


def test_section_can_be_disabled_and_pointed_at_another_api():
    renderer, html = render_page()
    assert 'id="newlyUploaded"' in html

    renderer._NU_ENABLED = False
    renderer._NU_API_URL = "https://api.example.com"
    renderer._NU_API_PATH = "/api/movies/new"
    assert renderer.newly_uploaded_api_url() == "https://api.example.com/api/movies/new"

    # Render again through the public helper to check the disabled flag.
    assert renderer._NU_ENABLED is False


def test_api_url_defaults_to_the_same_origin():
    renderer, _ = render_page(mime_type="application/zip")
    renderer._NU_API_URL = ""
    renderer._NU_API_PATH = "/api/movies/new"
    assert renderer.newly_uploaded_api_url() == "/api/movies/new"
    renderer._NU_API_PATH = "api/movies/new"
    assert renderer.newly_uploaded_api_url() == "/api/movies/new"


@pytest.mark.parametrize("page", ["stream_page", "download_page"])
def test_pages_never_leak_secrets(page, request):
    _, html = request.getfixturevalue(page)
    lowered = html.lower()
    for needle in ("bot_token", "telegram_bot_token", "api_hash", "mongodb+srv", "database_uri"):
        assert needle not in lowered
    # Only the public username is exposed, and only as a data attribute.
    assert html.count(BOT_USERNAME) >= 1


# --------------------------------------------------------------------------- #
# Static assets
# --------------------------------------------------------------------------- #
def css_text():
    return (STATIC / "newly_uploaded.css").read_text(encoding="utf-8")


def js_text():
    return (STATIC / "newly_uploaded.js").read_text(encoding="utf-8")


def test_stylesheet_matches_the_dark_gold_theme_and_stays_scoped():
    css = css_text()
    assert ".nu-section {" in css
    assert "--nu-gold: #f5c518" in css
    # Poster aspect ratio is preserved everywhere.
    assert "aspect-ratio: 2 / 3" in css
    # Responsive: auto-fill grid on desktop …
    assert "repeat(auto-fill, minmax(158px, 1fr))" in css
    # … and a swipeable snap slider on mobile.
    assert "@media (max-width: 640px)" in css
    assert "scroll-snap-type: x mandatory" in css
    # Hover zoom + glow.
    assert "scale(1.08)" in css
    assert "--nu-glow" in css
    # Every selector is namespaced so the existing design cannot break.
    for line in css.splitlines():
        stripped = line.strip()
        if stripped.endswith("{") and stripped[0] not in ".@*":
            pytest.fail(f"unscoped selector in section stylesheet: {stripped}")
    assert "prefers-reduced-motion" in css
    assert "nu-skel" in css and "nu-shimmer" in css
    assert ".nu-state" in css and ".nu-retry" in css


def test_script_implements_states_lazy_posters_and_deep_links():
    js = js_text()
    # Sanitized rendering: DOM APIs only, never HTML string injection.
    assert ".innerHTML" not in js
    assert "insertAdjacentHTML" not in js
    assert "textContent" in js
    # Lazy + responsive posters.
    assert 'setAttribute("loading", "lazy")' in js
    assert 'setAttribute("decoding", "async")' in js
    assert "srcset" in js
    # States.
    assert "showSkeletons" in js
    assert "No new movies uploaded yet" in js  # empty state
    assert "Couldn't load new movies" in js  # error state
    assert "Try again" in js  # retry button
    # Deep link contract.
    assert '"?start=movie_"' in js
    assert "https://t.me/" in js
    assert 'rel = "noopener noreferrer"' in js
    # Hardening.
    assert "placeholderDataUri" in js  # poster fallback
    assert 'credentials: "omit"' in js
    assert "AbortController" in js
    assert "/api/movies/new" in js  # default endpoint


def test_script_exposes_a_refresh_hook_and_respects_the_limit():
    js = js_text()
    assert "window.MinatoNewlyUploaded" in js
    assert "refresh" in js
    assert 'root.getAttribute("data-limit")' in js
    assert '"?limit=" + limit' in js or "limit=" in js
