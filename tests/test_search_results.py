"""Offline presentation tests: python -m unittest discover -s tests -p test_search_results.py"""
import ast
import asyncio
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from dreamxbotz.util.search_results import (
    DIAMOND_DIVIDER, result_files, result_footer, result_header,
)

ROOT = Path(__file__).resolve().parents[1]


class TelegramHTML(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.stack, self.links, self.text = [], [], ''
        self.feed(text)
        assert not self.stack, self.stack

    def handle_starttag(self, tag, attrs):
        assert tag in {'b', 'i', 'code', 'a'}
        self.stack.append(tag)
        if tag == 'a':
            self.links.append(dict(attrs)['href'])

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text += data


def load_caption_functions():
    # Execute the real functions without importing Telegram/DB credentials.
    tree = ast.parse((ROOT / 'utils.py').read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in {'search_file_list', 'get_cap'}]
    env = dict(result_header=result_header, result_files=result_files,
               result_footer=result_footer,
               clean_filename=lambda value: value, get_size=lambda value: '1.43 GB',
               temp=SimpleNamespace(U_NAME='MinatoBot', B_LINK='Minato', IMDB_CAP={}),
               get_poster=AsyncMock(return_value=None),
               script=SimpleNamespace(IMDB_TEMPLATE_TXT='<b>{title}</b>'))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'utils.py', 'exec'), env)
    return env


class SearchResultsTests(unittest.TestCase):
    def test_boxed_header_with_full_metadata(self):
        text = result_header('Marco <2024> & friends', 400,
                             '<a href="tg://user?id=42">Yuvi</a>', '5.02')
        parsed = TelegramHTML(text)
        self.assertIn('╔══', text)
        self.assertIn('CINEMA HUB', parsed.text)
        self.assertIn('SEARCH RESULTS', parsed.text)
        self.assertIn('╚══', text)
        self.assertIn(DIAMOND_DIVIDER, text)
        self.assertIn('Marco &lt;2024&gt; &amp; friends', text)
        self.assertIn('Total Files : <b>400</b>', text)
        self.assertIn('Time Taken : <code>5.02s</code>', text)
        self.assertIn('Requested By : <a href="tg://user?id=42">Yuvi</a>', text)

    def test_header_omits_time_line_when_elapsed_is_unknown(self):
        text = result_header('Marco', 1, None)
        parsed = TelegramHTML(text)
        self.assertIn('CINEMA HUB', parsed.text)
        self.assertNotIn('Time Taken', parsed.text)
        self.assertIn('Movie fan', parsed.text)

    def test_file_rows_format_links_escaping_and_footer(self):
        rows = [('abc_DEF-123', 'Marco <Hindi> & Tamil 720p mkv', '1.43 GB')]
        text = result_files(rows, 'MinatoBot', -10042)
        parsed = TelegramHTML(text)
        self.assertEqual(parsed.links, ['https://telegram.me/MinatoBot?start=file_-10042_abc_DEF-123'])
        self.assertIn('🔹', parsed.text)
        self.assertIn('[1.43 GB]', parsed.text)
        self.assertIn('&lt;Hindi&gt; &amp; Tamil', text)
        self.assertIn('Powered by Minato', parsed.text)
        self.assertEqual(text.count(DIAMOND_DIVIDER), 1)

    def test_offset_does_not_change_link_payloads(self):
        first = result_files([('id9', 'Marco', '1 GB')], 'bot', 42, offset=0)
        second = result_files([('id9', 'Marco', '1 GB')], 'bot', 42, offset=10)
        self.assertEqual(TelegramHTML(first).links, TelegramHTML(second).links)
        for text in (first, second):
            self.assertIn('🔹', text)
            self.assertIn('Powered by Minato', text)

    def test_button_mode_shows_hint_footer_and_no_links(self):
        text = result_files([('id', 'Marco', '1 GB')], 'bot', 42, button_mode=True)
        parsed = TelegramHTML(text)
        self.assertIn('Choose a file below', text)
        self.assertIn('Powered by Minato', parsed.text)
        self.assertEqual(parsed.links, [])

    def test_footer_escapes_custom_brand(self):
        text = result_footer('A & B <Movies>')
        parsed = TelegramHTML(text)
        self.assertIn('Powered by A &amp; B &lt;Movies&gt;', text)
        self.assertEqual(parsed.links, [])

    def test_long_filenames_fit_default_page_and_keep_all_links(self):
        rows = [(str(i), '😀<&> ' * 150 + '1080p', '1.43 GB') for i in range(10)]
        text = result_header('Marco', 400, 'Yuvi') + result_files(rows, 'bot', 42)
        parsed = TelegramHTML(text)
        self.assertEqual(len(parsed.links), 10)
        self.assertLess(len(parsed.text.encode('utf-16-le')) // 2, 4096)
        self.assertIn('…', parsed.text)

    def test_callbacks_use_shared_layout_and_zero_based_offsets(self):
        env = load_caption_functions()
        query = SimpleNamespace(from_user=SimpleNamespace(id=42, mention='Yuvi'),
                                message=SimpleNamespace(chat=SimpleNamespace(id=-10042, title='Minato')))
        files = [SimpleNamespace(file_id='abc', file_name='Marco 720p', file_size=123)]
        for imdb in (False, True):
            for offset in (0, 10):
                text = asyncio.run(env['get_cap']({'imdb': imdb}, '1.00', files, query, 400, 'Marco', offset))
                self.assertIn('CINEMA HUB', text)
                self.assertIn('Time Taken : <code>1.00s</code>', text)
                self.assertIn('🔹', text)
                self.assertIn('Powered by Minato', text)
                self.assertIn('Choose your download', text)
                TelegramHTML(text)

    def test_imdb_cached_and_custom_templates_keep_valid_markup(self):
        env = load_caption_functions()
        query = SimpleNamespace(from_user=SimpleNamespace(id=42, mention='Yuvi'),
                                message=SimpleNamespace(chat=SimpleNamespace(id=42, title='Minato')))
        files = [SimpleNamespace(file_id='abc', file_name='Marco 720p', file_size=123)]
        env['temp'].IMDB_CAP[42] = '<b>Cached movie</b>'
        text = asyncio.run(env['get_cap']({'imdb': True}, '1', files, query, 1, 'Marco'))
        self.assertTrue(text.startswith('<b>Cached movie</b>'))
        self.assertIn('🔹', text)
        self.assertIn('Powered by Minato', text)
        TelegramHTML(text)
        env['temp'].IMDB_CAP.clear()
        env['get_poster'].return_value = {'title': 'Marco', 'rating': '7.5'}
        text = asyncio.run(env['get_cap']({'imdb': True, 'template': '<b>{title}</b> · {rating} · {query}'},
                                        '1', files, query, 1, 'marco'))
        self.assertTrue(text.startswith('<b>Marco</b> · 7.5 · marco'))
        self.assertIn('Powered by Minato', text)
        TelegramHTML(text)

    def test_callback_callers_pass_zero_based_offsets(self):
        tree = ast.parse((ROOT / 'plugins/pmfilter.py').read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == 'get_cap']
        self.assertEqual(len(calls), 4)
        offsets = [ast.unparse(next(k.value for k in n.keywords if k.arg == 'offset')) for n in calls]
        self.assertEqual(offsets, ['offset', '0', '0', '0'])


if __name__ == '__main__':
    unittest.main()
