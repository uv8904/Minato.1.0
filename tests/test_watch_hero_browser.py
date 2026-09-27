"""Opt-in, offline Chromium regressions against the real watch template/assets.

    pip install playwright
    playwright install chromium
    RUN_BROWSER_TESTS=1 pytest tests/test_watch_hero_browser.py -q

CHROMIUM_PATH optionally selects a system browser. No live bot, movie API,
Telegram, external artwork or video is needed. Production HTML/CSS/JS is used;
only network responses are replaced.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from jinja2 import Template

from dreamxbotz.util.watch_hero import build_context

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1", reason="opt-in browser tests (RUN_BROWSER_TESTS=1)"
)
playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://minato.test"
POSTER = b'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="900">
<rect width="600" height="450" fill="#de3540"/>
<rect y="450" width="600" height="450" fill="#16be9e"/>
<text x="50" y="830" font-size="60" fill="white">FULL POSTER</text></svg>'''
BACKDROP = b'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">
<rect width="1280" height="720" fill="#244d76"/></svg>'''


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as driver:
        options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if os.getenv("CHROMIUM_PATH"):
            options["executable_path"] = os.environ["CHROMIUM_PATH"]
        instance = driver.chromium.launch(**options)
        yield instance
        instance.close()


@pytest.fixture
def open_page(browser):
    contexts = []

    def open_hero(width=1280, *, javascript=True, init_script=None, clock=False,
                  filename="RRR 2022 Hindi HQ BluRay 1080p HEVC.mkv", bot="MinatoMovieBot"):
        context = browser.new_context(
            viewport={"width": width, "height": 900}, java_script_enabled=javascript
        )
        contexts.append(context)
        page = context.new_page()
        state = {
            "requests": [], "errors": [], "poster_fail": False, "api_fail": False,
            "art": {
                "ok": True, "has_poster": True, "has_backdrop": True,
                "poster": "/api/movies/poster/rrr-2022?v=2",
                "backdrop": "/api/movies/backdrop/rrr-2022?v=2",
            },
        }
        page.on("pageerror", lambda error: state["errors"].append(str(error)))
        html = Template((ROOT / "dreamxbotz/template/req.html").read_text()).render(
            **build_context(filename, bot, api_base="", enabled=True),
            file_name=filename, file_url="/sample.mp4", file_size="1.93 GiB",
            file_unique_id="abcdef123456", update_channel_url="https://t.me/updates",
            bot_username=bot, newly_uploaded_enabled=False, coming_soon_enabled=False,
            asset_version="test",
        )

        def respond(route):
            url = route.request.url
            state["requests"].append(url)
            parsed = urlparse(url)
            if parsed.netloc != "minato.test":
                return route.abort()
            path = parsed.path
            if path == "/watch/demo":
                return route.fulfill(content_type="text/html", body=html)
            if path.startswith("/static/"):
                asset = ROOT / "dreamxbotz/static" / Path(path).name
                return route.fulfill(
                    content_type="text/css" if asset.suffix == ".css" else "text/javascript",
                    body=asset.read_bytes(),
                )
            if path.startswith("/api/movies/art/"):
                return route.fulfill(status=503 if state["api_fail"] else 200,
                                     content_type="application/json", body=json.dumps(state["art"]))
            if path.startswith("/api/movies/poster/"):
                return route.fulfill(content_type="image/jpeg" if state["poster_fail"] else "image/svg+xml",
                                     body=b"broken" if state["poster_fail"] else POSTER)
            if path.startswith("/api/movies/backdrop/"):
                return route.fulfill(content_type="image/svg+xml", body=BACKDROP)
            if path == "/sample.mp4":
                return route.fulfill(status=204)
            return route.fulfill(content_type="application/json", body='{"ok":true,"movies":[]}')

        page.route("**/*", respond)
        if init_script:
            page.add_init_script(init_script)
        if clock:
            page.clock.install()
        # Caller may set API/image failure flags before the first request.
        return page, state

    yield open_hero
    for context in contexts:
        context.close()


def visit(page, ready=True):
    page.goto(ORIGIN + "/watch/demo", wait_until="domcontentloaded")
    if ready:
        page.wait_for_selector("#mhPosterCard.is-ready")


@pytest.mark.parametrize("width", [320, 390, 640, 768, 900, 1280, 1920])
def test_complete_poster_and_actions_fit_every_viewport(open_page, width):
    page, state = open_page(width)
    visit(page)
    dimensions = page.evaluate('''() => {
        const poster = document.getElementById('mhPoster');
        const hero = document.getElementById('movieHero').getBoundingClientRect();
        const card = document.getElementById('mhPosterCard').getBoundingClientRect();
        const controls = [...document.querySelectorAll('.mh-btn')].map(el => el.getBoundingClientRect());
        return {fit: getComputedStyle(poster).objectFit, loaded: poster.complete && poster.naturalHeight > 0,
            ratio: card.width / card.height, noOverflow: document.documentElement.scrollWidth <= innerWidth,
            controlsFit: controls.every(r => r.left >= hero.left && r.right <= hero.right && r.height >= 44)};
    }''')
    assert dimensions["loaded"] and dimensions["fit"] == "contain"
    assert dimensions["ratio"] == pytest.approx(2 / 3, abs=0.01)
    assert dimensions["noOverflow"] and dimensions["controlsFit"]
    assert not state["errors"]


def test_placeholder_stays_until_image_decode_finishes(open_page):
    page, state = open_page(init_script='''
        const decode = HTMLImageElement.prototype.decode;
        HTMLImageElement.prototype.decode = function () {
            const decoded = decode.call(this);
            if (this.id !== 'mhPoster') return decoded;
            return decoded.then(() => new Promise(resolve => { window.finishPosterDecode = resolve; }));
        };
    ''')
    visit(page, ready=False)
    page.wait_for_function("typeof window.finishPosterDecode === 'function'")
    assert not page.locator("#mhPosterCard").evaluate("el => el.classList.contains('is-ready')")
    assert page.locator(".mh-poster-fallback").is_visible()
    page.evaluate("window.finishPosterDecode()")
    page.wait_for_selector("#mhPosterCard.is-ready")
    assert page.locator("#mhPoster").is_visible()
    assert not state["errors"]


def test_failed_image_retries_then_recovers_with_the_whole_poster(open_page):
    page, state = open_page(clock=True)
    state["poster_fail"] = True
    visit(page, ready=False)
    page.wait_for_function("document.getElementById('mhPoster').complete")
    assert page.locator(".mh-poster-fallback").is_visible()
    state["poster_fail"] = False
    page.clock.fast_forward(1600)
    page.wait_for_selector("#mhPosterCard.is-ready")
    assert any("/poster/" in url and "&r=" in url for url in state["requests"])
    assert not state["errors"]


def test_exhausted_image_retries_keep_a_visible_placeholder(open_page):
    page, state = open_page(clock=True)
    state["poster_fail"] = True
    visit(page, ready=False)
    page.wait_for_function("document.getElementById('mhPoster').complete")
    for delay in (1600, 4100, 9100):
        with page.expect_response(lambda r: "/api/movies/poster/" in r.url):
            page.clock.fast_forward(delay)
        page.wait_for_function("document.getElementById('mhPoster').complete")
    page.wait_for_function("document.getElementById('mhStatus').textContent.includes('Poster coming soon')")
    assert page.locator(".mh-poster-fallback").is_visible()
    assert not page.locator("#mhPoster").is_visible()
    assert not state["errors"]


def test_new_upload_repolls_and_displays_its_poster_without_reloading(open_page):
    page, state = open_page(clock=True)
    state["art"]["has_poster"] = False
    visit(page, ready=False)
    page.wait_for_selector("#mhStatus:not([hidden])")
    state["art"]["has_poster"] = True
    page.clock.fast_forward(8100)
    page.wait_for_selector("#mhPosterCard.is-ready")
    assert len([url for url in state["requests"] if "/api/movies/art/" in url]) == 2


def test_api_outage_keeps_text_and_telegram_link_usable(open_page):
    page, state = open_page()
    state["api_fail"] = True
    visit(page, ready=False)
    page.wait_for_selector('#movieHero[data-mh-state="error"]')
    assert page.locator(".mh-poster-fallback").is_visible()
    assert page.locator("#mhTitle").is_visible()
    assert page.locator("#mhOpen").get_attribute("href").endswith("?start=movie_rrr-2022")
    assert not state["errors"]


def test_failed_refresh_keeps_already_loaded_artwork(open_page):
    page, state = open_page()
    visit(page)
    state["api_fail"] = True
    page.evaluate("window.MinatoMovieHero.refresh()")
    assert page.locator("#mhPosterCard.is-ready").is_visible()
    assert page.locator("#mhPoster").is_visible()
    assert not state["errors"]


def test_hostile_art_urls_are_never_loaded(open_page):
    page, state = open_page()
    state["art"].update(poster="https://evil.test/image.jpg", backdrop="//evil.test/wide.jpg")
    visit(page, ready=False)
    page.wait_for_selector("#mhStatus:not([hidden])")
    assert page.locator(".mh-poster-fallback").is_visible()
    assert not any("evil.test" in url for url in state["requests"])
    assert not state["errors"]


def test_watch_now_and_copy_buttons_work_without_autoplay(open_page):
    page, state = open_page(init_script='''
        window.playCalls = 0;
        HTMLMediaElement.prototype.play = function () { window.playCalls++; return Promise.resolve(); };
        Object.defineProperty(navigator, 'clipboard', {value: {
            writeText: text => { window.copiedLink = text; return Promise.resolve(); }
        }});
    ''')
    visit(page)
    assert page.evaluate("window.playCalls") == 0
    page.locator("#mhPlay").click()
    assert page.evaluate("window.playCalls") == 1
    assert page.locator("#player").evaluate("el => document.activeElement === el")
    page.get_by_role("button", name="Copy search link").click()
    assert page.evaluate("window.copiedLink") == "https://t.me/MinatoMovieBot?start=movie_rrr-2022"
    assert "copied" in page.locator("#mhStatus").inner_text()
    assert not state["errors"]


def test_rejected_clipboard_and_playback_promises_are_handled(open_page):
    page, state = open_page(init_script='''
        HTMLMediaElement.prototype.play = () => Promise.reject(new Error('Unsupported media'));
        Object.defineProperty(navigator, 'clipboard', {value: {
            writeText: () => Promise.reject(new Error('Permission denied'))
        }});
    ''')
    visit(page)
    page.locator("#mhPlay").click()
    assert "Playback could not start" in page.locator("#mhStatus").inner_text()
    page.locator("#mhCopy").click()
    assert "Copy failed" in page.locator("#mhStatus").inner_text()
    assert not state["errors"]


def test_long_title_missing_bot_and_reduced_motion(open_page):
    page, state = open_page(320, filename=("A Very Long Movie Name " * 5) + "2024 1080p.mkv", bot="")
    page.emulate_media(reduced_motion="reduce")
    visit(page)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#mhPosterCard").get_attribute("href") is None
    assert page.locator("#mhPosterCard").get_attribute("aria-disabled") == "true"
    assert page.locator("#mhPlay").is_visible()
    assert page.locator("#mhPoster").evaluate("el => getComputedStyle(el).transitionDuration") == "0s"
    page.locator("#mhPlay").focus()
    assert page.locator("#mhPlay").evaluate("el => getComputedStyle(el).outlineStyle") != "none"
    assert not state["errors"]


def test_no_javascript_keeps_server_rendered_links_and_placeholder(open_page):
    page, _ = open_page(390, javascript=False)
    visit(page, ready=False)
    assert page.locator("#mhTitle").is_visible()
    assert page.locator(".mh-poster-fallback").is_visible()
    assert page.locator("#mhPlay").get_attribute("href") == "#player"
    assert page.locator("#mhOpen").get_attribute("href").endswith("?start=movie_rrr-2022")
