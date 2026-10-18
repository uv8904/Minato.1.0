"""Verify-shortlink plumbing (utils.get_shortlink + info shortener config).

The verify button (file-verify AND premium ``pcunlock`` free unlock) is only as
good as the shortlink behind it. Shortzy builds ``https://{site}/api`` and
``https://{site}/st`` from the configured *site*, so the site must arrive as a
bare domain (``arolinks.com``). A full URL (``https://arolinks.com``) used to
produce ``https://https://arolinks.com/api`` — every verify link broke.

Covers:

* ``info.normalize_shortener_site`` / ``info.is_valid_http_url``
* ``info`` shortener defaults are bare domains
* ``utils.get_shortlink`` sends Shortzy a bare domain (old DB values with a
  scheme included), falls back to a valid quick link, and refuses to return a
  malformed link / work without an API key.

Run with the project dependencies installed::

    pytest tests/test_shortlink.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import info  # noqa: E402
import utils  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ #
# normalize_shortener_site
# ------------------------------------------------------------------ #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("arolinks.com", "arolinks.com"),
        ("https://arolinks.com", "arolinks.com"),
        ("http://arolinks.com", "arolinks.com"),
        ("https://arolinks.com/", "arolinks.com"),
        ("https://arolinks.com/api", "arolinks.com"),
        ("https://arolinks.com/st?api=abc&url=x", "arolinks.com"),
        ("arolinks.com/st", "arolinks.com"),
        ("arolinks.com/api/", "arolinks.com"),
        ("HTTPS://AroLinks.COM", "arolinks.com"),
        ("  https://arolinks.com  ", "arolinks.com"),
        ("https://user:pass@arolinks.com", "arolinks.com"),
        ("shareus.in", "shareus.in"),          # Shortzy's special-cased sites
        ("https://shareus.io/", "shareus.io"),
        ("www.arolinks.com", "www.arolinks.com"),
        ("", ""),
        (None, ""),
        ("!!!", ""),                      # garbage is not a host
        ("https://", ""),
        ("not a domain", ""),
    ],
)
def test_normalize_shortener_site(raw, expected):
    assert info.normalize_shortener_site(raw) == expected


def test_normalize_shortener_site_unquotes_paste():
    assert info.normalize_shortener_site('"https://arolinks.com"') == "arolinks.com"
    assert info.normalize_shortener_site("'arolinks.com'") == "arolinks.com"


# ------------------------------------------------------------------ #
# is_valid_http_url
# ------------------------------------------------------------------ #
def test_is_valid_http_url():
    ok = info.is_valid_http_url
    assert ok("https://arolinks.com/st?api=1&url=2") is True
    assert ok("http://example.com/x") is True
    assert ok("https://https://arolinks.com/st") is False   # doubled scheme
    assert ok("arolinks.com/st") is False                   # not absolute
    assert ok("") is False
    assert ok(None) is False
    assert ok("https://arolinks .com/x") is False           # whitespace in URL
    assert ok(123) is False


# ------------------------------------------------------------------ #
# info defaults
# ------------------------------------------------------------------ #
def test_info_shortener_defaults_are_bare_domains():
    for site in (info.SHORTENER_WEBSITE, info.SHORTENER_WEBSITE2, info.SHORTENER_WEBSITE3):
        assert site, "shortener site must be set"
        assert "://" not in site, f"{site!r} must not carry a scheme (Shortzy adds it)"
        assert "/" not in site, f"{site!r} must be a bare domain"
        assert " " not in site
    for api in (info.SHORTENER_API, info.SHORTENER_API2, info.SHORTENER_API3):
        assert api and api.strip() == api


# ------------------------------------------------------------------ #
# get_shortlink
# ------------------------------------------------------------------ #
class FakeShortzy:
    """Captures the (api, site) Shortzy is built with; scripted responses."""

    instances = []
    convert_result = "https://short.example/abc123"
    convert_raises = False
    quick_link_bad = False

    def __init__(self, api, site):
        self.api, self.site = api, site
        FakeShortzy.instances.append(self)

    async def convert(self, link, **kwargs):
        if FakeShortzy.convert_raises:
            raise RuntimeError("shortener api down")
        return FakeShortzy.convert_result

    async def get_quick_link(self, link, alias="", **kwargs):
        if FakeShortzy.quick_link_bad:
            return f"https://https://{self.site}/st?api={self.api}"
        return f"https://{self.site}/st?api={self.api}&url={link}"


@pytest.fixture
def fake_shortzy(monkeypatch):
    FakeShortzy.instances = []
    FakeShortzy.convert_result = "https://short.example/abc123"
    FakeShortzy.convert_raises = False
    FakeShortzy.quick_link_bad = False
    monkeypatch.setattr(utils, "Shortzy", FakeShortzy)
    return FakeShortzy


def _fake_settings(monkeypatch, settings):
    async def get_settings(_grp_id):
        return dict(settings)

    monkeypatch.setattr(utils, "get_settings", get_settings)


DEEP = "https://telegram.me/TestBot?start=pcv_ABC1234"


def test_get_shortlink_normalizes_full_url_site(monkeypatch, fake_shortzy):
    # Old DB rows / env values keep the scheme — Shortzy must still get a host.
    _fake_settings(monkeypatch, {"api": "KEY123", "shortner": "https://arolinks.com"})
    out = run(utils.get_shortlink(DEEP, -100123))
    assert [(s.api, s.site) for s in fake_shortzy.instances] == [("KEY123", "arolinks.com")]
    assert out == "https://short.example/abc123"


def test_get_shortlink_bare_domain_untouched(monkeypatch, fake_shortzy):
    _fake_settings(monkeypatch, {"api": "KEY123", "shortner": "arolinks.com"})
    run(utils.get_shortlink(DEEP, -100123))
    assert fake_shortzy.instances[0].site == "arolinks.com"


def test_get_shortlink_second_and_third_shorteners(monkeypatch, fake_shortzy):
    _fake_settings(
        monkeypatch,
        {
            "api": "K1", "shortner": "one.com",
            "api_two": "K2", "shortner_two": "https://two.com",
            "api_three": "K3", "shortner_three": "three.com/api",
        },
    )
    run(utils.get_shortlink(DEEP, -1, is_second_shortener=True))
    run(utils.get_shortlink(DEEP, -1, is_third_shortener=True))
    assert [(s.api, s.site) for s in fake_shortzy.instances] == [
        ("K2", "two.com"),
        ("K3", "three.com"),
    ]


def test_get_shortlink_strips_api_whitespace(monkeypatch, fake_shortzy):
    _fake_settings(monkeypatch, {"api": "  KEY123 \n", "shortner": "arolinks.com"})
    run(utils.get_shortlink(DEEP, -1))
    assert fake_shortzy.instances[0].api == "KEY123"


def test_get_shortlink_quick_link_fallback_is_valid(monkeypatch, fake_shortzy):
    # JSON API failure must still yield a working /st quick link on the host.
    fake_shortzy.convert_raises = True
    _fake_settings(monkeypatch, {"api": "KEY123", "shortner": "https://arolinks.com"})
    out = run(utils.get_shortlink(DEEP, -1))
    assert out.startswith("https://arolinks.com/st?api=KEY123")
    assert info.is_valid_http_url(out)


def test_get_shortlink_rejects_malformed_result(monkeypatch, fake_shortzy):
    fake_shortzy.convert_result = "https://https://arolinks.com/x"
    fake_shortzy.convert_raises = False
    fake_shortzy.quick_link_bad = True
    _fake_settings(monkeypatch, {"api": "KEY123", "shortner": "https://arolinks.com"})
    with pytest.raises(ValueError):
        run(utils.get_shortlink(DEEP, -1))


def test_get_shortlink_requires_api_key(monkeypatch, fake_shortzy):
    _fake_settings(monkeypatch, {"api": "", "shortner": "https://arolinks.com"})
    with pytest.raises(ValueError):
        run(utils.get_shortlink(DEEP, -1))
    assert fake_shortzy.instances == []       # never even constructed


def test_get_shortlink_requires_site(monkeypatch, fake_shortzy):
    _fake_settings(monkeypatch, {"api": "KEY123", "shortner": "https://"})
    with pytest.raises(ValueError):
        run(utils.get_shortlink(DEEP, -1))
    assert fake_shortzy.instances == []


def test_get_shortlink_tolerates_missing_keys(monkeypatch, fake_shortzy):
    # Very old settings dicts without the shortener keys → clean error.
    _fake_settings(monkeypatch, {"button": True})
    with pytest.raises(ValueError):
        run(utils.get_shortlink(DEEP, -1))


# ------------------------------------------------------------------ #
# integration: what the verify button would actually receive
# ------------------------------------------------------------------ #
def test_verify_button_link_would_be_tappable(monkeypatch, fake_shortzy):
    """End-to-end of the failing flow: settings as literally saved by
    ``/set_shortner https://arolinks.com <api>`` (old code saved the scheme),
    convert() fails like the real API did, and the fallback quick link must be
    a single-scheme URL on the real host."""
    fake_shortzy.convert_raises = True
    _fake_settings(monkeypatch, {"api": "ef7e0434", "shortner": "https://arolinks.com"})
    out = run(utils.get_shortlink(DEEP, -1003536704514))
    assert info.is_valid_http_url(out)
    assert out.startswith("https://arolinks.com/st?")
    assert "https://https" not in out
