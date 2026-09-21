"""Render both public pages without Telegram or database connections."""
import asyncio
import importlib.util
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.append(dict(attrs).get('href'))


@pytest.mark.parametrize('mime_type', ['video/mp4', 'audio/mpeg', 'application/zip'])
@pytest.mark.parametrize('channel_url', [
    'https://t.me/wanda_movies_update',
    'https://t.me/custom_updates?foo=1&bar="two"',
])
def test_rendered_branding_and_channel_links(monkeypatch, mime_type, channel_url):
    monkeypatch.chdir(ROOT)
    file_data = SimpleNamespace(
        unique_id='abcdef123456', file_name='Example_file',
        mime_type=mime_type, file_size=1024,
    )
    client = SimpleNamespace(get_messages=AsyncMock())
    # Stub integrations only; run the real renderer and real Jinja templates.
    modules = {
        'info': dict(BIN_CHANNEL=-100123, URL='https://files.example/',
                     UPDATE_CHNL_LNK=channel_url),
        'dreamxbotz.Bot': dict(dreamxbotz=client),
        'dreamxbotz.util.human_readable': dict(humanbytes=lambda size: '1 KB'),
        'dreamxbotz.util.file_properties': dict(
            get_file_ids=AsyncMock(return_value=file_data)),
        'dreamxbotz.server.exceptions': dict(InvalidHash=ValueError),
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        'branding_renderer', ROOT / 'dreamxbotz/util/render_template.py')
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)

    session = MagicMock()
    session.__aenter__.return_value = session
    response = SimpleNamespace(headers={'Content-Length': '1024'})
    session.get.return_value.__aenter__.return_value = response
    monkeypatch.setattr(renderer.aiohttp, 'ClientSession', lambda: session)

    html = asyncio.run(renderer.render_page(42, 'abcdef'))
    links = PageLinks()
    links.feed(html)

    # Header, primary channel CTA, copyright, and footer update button.
    assert links.hrefs.count(channel_url) == 4
    assert 'dreamcinezone' not in html.lower()
    assert '<span class="brand-mark">MV</span>' in html
    assert 'MinatoVerse' in html
    assert '<title>MinatoVerse' in html
    assert 'https://files.example/42/Example_file?hash=abcdef' in links.hrefs
    assert '{{' not in html
