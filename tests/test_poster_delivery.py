"""Poster delivery regressions: real chunked HTTP, full decoding and cache repair.

Run without Telegram/MongoDB: pytest tests/test_poster_delivery.py -q
"""
import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from dreamxbotz.server import movie_api


def poster_bytes():
    """Different colours at the top/bottom expose incomplete or cropped images."""
    image = Image.new("RGB", (600, 900), (220, 35, 45))
    image.paste((20, 190, 160), (0, 450, 600, 900))
    output = BytesIO()
    image.save(output, "JPEG", quality=90)
    return output.getvalue()


@pytest.fixture(autouse=True)
def clean_cache():
    movie_api._POSTER_CACHE.clear()
    movie_api._POSTER_INFLIGHT.clear()
    movie_api._POSTER_CACHE_BYTES = 0
    yield
    movie_api._POSTER_CACHE.clear()
    movie_api._POSTER_INFLIGHT.clear()
    movie_api._POSTER_CACHE_BYTES = 0


@pytest.mark.parametrize("content_length", [True, False])
def test_entire_image_is_read_across_http_packets(content_length):
    """StreamReader.read(n) returns *up to* n bytes, not necessarily EOF."""
    original = poster_bytes()

    async def scenario():
        async def stream(request):
            headers = {"Content-Type": "image/jpeg"}
            if content_length:
                headers["Content-Length"] = str(len(original))
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            try:
                for offset in range(0, len(original), 1024):
                    await response.write(original[offset:offset + 1024])
                    await asyncio.sleep(0.002)  # a later network packet
                await response.write_eof()
            except ConnectionResetError:  # old implementation closes after packet 1
                pass
            return response

        app = web.Application()
        app.router.add_get("/poster.jpg", stream)
        async with TestServer(app) as server, ClientSession() as session:
            return await movie_api._download_and_encode(
                str(server.make_url("/poster.jpg")), 200, session
            )

    result = asyncio.run(scenario())
    assert result is not None
    body, content_type = result
    assert content_type == "image/jpeg"
    with Image.open(BytesIO(body)) as image:
        image.load()  # headers alone are not proof of a complete JPEG
        assert image.size == (200, 300)
        assert image.getpixel((100, 10))[0] > 200  # red top
        assert image.getpixel((100, 290))[1] > 170  # green bottom, not blank


class PacketResponse:
    def __init__(self, data, headers=None, packet_size=256):
        self.status = 200
        self.headers = headers or {"Content-Type": "image/jpeg"}
        self.data = data
        self.packet_size = packet_size
        self.bytes_read = 0
        self.content = SimpleNamespace(iter_chunked=self.iter_chunked)

    async def iter_chunked(self, size):
        size = min(size, self.packet_size)
        for offset in range(0, len(self.data), size):
            chunk = self.data[offset:offset + size]
            self.bytes_read += len(chunk)
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def session_for(response):
    return SimpleNamespace(get=lambda *args, **kwargs: response)


@pytest.mark.parametrize("headers", [
    {"Content-Type": "image/jpeg"},
    {"Content-Type": "image/jpeg", "Content-Length": "bad"},
    {"Content-Type": "image/jpeg", "Content-Length": "1"},
])
def test_stream_size_limit_is_enforced_even_without_a_trustworthy_length(monkeypatch, headers):
    monkeypatch.setattr(movie_api, "MAX_POSTER_BYTES", 1024)
    response = PacketResponse(b"x" * 10000, headers)
    result = asyncio.run(movie_api._download_and_encode("https://image.tmdb.org/x", 200, session_for(response)))
    assert result is None
    assert response.bytes_read <= 1024 + response.packet_size


def test_declared_oversize_image_is_rejected_before_reading(monkeypatch):
    monkeypatch.setattr(movie_api, "MAX_POSTER_BYTES", 1024)
    response = PacketResponse(b"x" * 2048, {"Content-Type": "image/jpeg", "Content-Length": "2048"})
    assert asyncio.run(movie_api._download_and_encode("https://image.tmdb.org/x", 200, session_for(response))) is None
    assert response.bytes_read == 0


@pytest.mark.parametrize("data", [b"", b"not an image", poster_bytes()[:1500]])
def test_broken_images_are_not_served_or_cached_as_successful_artwork(data):
    response = PacketResponse(data)
    result = asyncio.run(movie_api._fetch_poster(
        "rrr:200:v2", "https://image.tmdb.org/rrr.jpg", 200, session_for(response)
    ))
    assert result is None
    assert movie_api._cache_get("rrr:200:v2") is None
    assert not movie_api._POSTER_INFLIGHT


def test_corrupt_telegram_art_is_not_cached_or_passed_through(monkeypatch):
    async def download(file_id):
        return poster_bytes()[:1500]

    monkeypatch.setattr(movie_api, "download_telegram_file", download)
    result = asyncio.run(movie_api._fetch_telegram_poster("rrr:200:v2", "file123", 200))
    assert result is None
    assert movie_api._cache_get("rrr:200:v2") is None
    assert movie_api._cache_get("tgraw:file123") is None


def test_encoding_revision_busts_existing_partial_image_urls(monkeypatch):
    doc = {"_id": "rrr-2022", "title": "RRR", "poster_url": "https://image.tmdb.org/rrr.jpg"}
    art = {"poster": doc["poster_url"], "backdrop": "https://image.tmdb.org/wide.jpg"}
    before = movie_api.public_movie(doc), movie_api.public_art("rrr-2022", art)
    monkeypatch.setattr(movie_api, "POSTER_ENCODING_VERSION", "next")
    after = movie_api.public_movie(doc), movie_api.public_art("rrr-2022", art)
    for old, new in zip(before, after):
        assert old["poster"] != new["poster"]
        assert old["backdrop"] != new["backdrop"]


@pytest.mark.parametrize("path", [movie_api.POSTER_PATH, movie_api.BACKDROP_PATH])
@pytest.mark.parametrize("upstream_fails", [False, True])
def test_first_placeholder_response_never_gets_the_day_long_artwork_ttl(monkeypatch, path, upstream_fails):
    async def resolve(*args, **kwargs):
        return {"known": True, "title": "RRR",
                "poster": "https://image.tmdb.org/broken.jpg" if upstream_fails else None}

    monkeypatch.setattr(movie_api, "resolve_art", resolve)
    monkeypatch.setattr(movie_api, "_session", lambda *args: session_for(PacketResponse(b"bad JPEG")))

    async def scenario():
        app = web.Application()
        app.add_routes(movie_api.routes)
        async with TestClient(TestServer(app)) as client:
            first = await client.get(path + "/rrr-2022?v=2")
            body = await first.read()
            cached = await client.get(path + "/rrr-2022?v=2")
            await cached.read()
            return first, body, cached

    first, body, cached = asyncio.run(scenario())
    assert first.status == 200
    assert first.headers["Content-Type"] == "image/svg+xml"
    assert b"<svg" in body and b"bad JPEG" not in body
    assert first.headers["Cache-Control"] == f"public, max-age={movie_api.PLACEHOLDER_TTL}"
    assert int(cached.headers["Cache-Control"].split("max-age=")[1]) <= movie_api.PLACEHOLDER_TTL
