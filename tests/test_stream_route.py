"""Regression tests for the streaming/download route's response headers."""
import asyncio
from types import SimpleNamespace

import pytest

import plugins.route as route


class _FakeStreamer:
    def __init__(self, _client):
        self.file = SimpleNamespace(
            unique_id="abcdef123456",
            file_size=100,
            mime_type=None,          # indexed rows can lack a MIME type
            file_name="movie.mp4",
        )

    async def get_file_properties(self, _id):
        return self.file

    async def yield_file(self, *_args, **_kwargs):
        yield b""


def test_mime_type_is_a_plain_string_when_it_is_guessed_from_the_filename(monkeypatch):
    monkeypatch.setattr(route, "work_loads", {0: 0})
    monkeypatch.setattr(route, "multi_clients", {0: object()})
    monkeypatch.setattr(route, "class_cache", {})
    monkeypatch.setattr(route, "ByteStreamer", _FakeStreamer)
    request = SimpleNamespace(
        headers={},
        http_range=SimpleNamespace(start=None, stop=None),
        remote="127.0.0.1",
    )

    response = asyncio.run(route.media_streamer(request, 123, "abcdef"))

    assert response.status == 200
    assert response.headers["Content-Type"] == "video/mp4"
    assert response.headers["Content-Disposition"] == 'attachment; filename="movie.mp4"'


def test_untrusted_filename_cannot_break_the_disposition_header(monkeypatch):
    monkeypatch.setattr(route, "work_loads", {0: 0})
    monkeypatch.setattr(route, "multi_clients", {0: object()})
    monkeypatch.setattr(route, "class_cache", {})

    class _Hostile(_FakeStreamer):
        def __init__(self, client):
            super().__init__(client)
            self.file.file_name = 'evil"filename.mp4'

    monkeypatch.setattr(route, "ByteStreamer", _Hostile)
    request = SimpleNamespace(
        headers={}, http_range=SimpleNamespace(start=None, stop=None), remote="127.0.0.1"
    )

    response = asyncio.run(route.media_streamer(request, 123, "abcdef"))

    assert response.headers["Content-Disposition"] == 'attachment; filename="evilfilename.mp4"'
