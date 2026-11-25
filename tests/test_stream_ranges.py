"""HTTP Range handling per RFC 7233 — legal ranges must not 500.

The old ``split("-") + int()`` crashed on suffix ranges (``bytes=-500``,
used by Safari and some players) and multi-ranges.
"""
import asyncio
from types import SimpleNamespace

import pytest

import plugins.route as route


class _FakeStreamer:
    def __init__(self, _client):
        self.file = SimpleNamespace(
            unique_id="abcdef123456", file_size=100,
            mime_type="video/mp4", file_name="movie.mp4",
        )

    async def get_file_properties(self, _id):
        return self.file

    async def yield_file(self, *_args, **_kwargs):
        yield b""


def _request(monkeypatch, range_header=None):
    monkeypatch.setattr(route, "work_loads", {0: 0})
    monkeypatch.setattr(route, "multi_clients", {0: object()})
    monkeypatch.setattr(route, "class_cache", {})
    monkeypatch.setattr(route, "ByteStreamer", _FakeStreamer)
    headers = {} if range_header is None else {"Range": range_header}
    return SimpleNamespace(headers=headers, remote="127.0.0.1",
                           http_range=SimpleNamespace(start=None, stop=None))


def _stream(monkeypatch, range_header):
    request = _request(monkeypatch, range_header)
    return asyncio.run(route.media_streamer(request, 123, "abcdef"))


def test_suffix_range_serves_the_last_n_bytes(monkeypatch):
    resp = _stream(monkeypatch, "bytes=-40")
    assert resp.status == 206
    assert resp.headers["Content-Range"] == "bytes 60-99/100"
    assert resp.headers["Content-Length"] == "40"


def test_open_ended_range(monkeypatch):
    resp = _stream(monkeypatch, "bytes=30-")
    assert resp.status == 206
    assert resp.headers["Content-Range"] == "bytes 30-99/100"
    assert resp.headers["Content-Length"] == "70"


def test_single_byte_range(monkeypatch):
    resp = _stream(monkeypatch, "bytes=0-0")
    assert resp.status == 206
    assert resp.headers["Content-Length"] == "1"


def test_out_of_bounds_range_is_416(monkeypatch):
    resp = _stream(monkeypatch, "bytes=200-300")
    assert resp.status == 416
    assert resp.headers["Content-Range"] == "bytes */100"


def test_zero_suffix_is_unsatisfiable(monkeypatch):
    assert _stream(monkeypatch, "bytes=-0").status == 416


@pytest.mark.parametrize("header", ["bytes=abc-def", "bytes=-", "bytes=0-9,20-29", "items=0-9"])
def test_malformed_or_multi_ranges_are_ignored_not_500(monkeypatch, header):
    resp = _stream(monkeypatch, header)
    assert resp.status == 200
    assert "Content-Range" not in resp.headers
    assert resp.headers["Content-Length"] == "100"


def test_no_range_header_is_a_plain_200(monkeypatch):
    resp = _stream(monkeypatch, None)
    assert resp.status == 200
    assert "Content-Range" not in resp.headers
