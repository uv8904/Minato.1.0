"""Dependency-free checks of the audit handler with Telegram calls mocked."""
import ast
import asyncio
import logging
from html import escape
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock


def handler(enabled=True):
    source = Path('plugins/message_logging.py').read_text()
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef))
    fn.decorator_list = []
    scope = dict(asyncio=asyncio, escape=escape, logger=logging.getLogger('test'),
                 MESSAGE_LOGGING=enabled, LOG_CHANNEL=-100,
                 enums=NS(ParseMode=NS(HTML='html')))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<handler>', 'exec'), scope)
    return scope['log_incoming_message']


def message():
    return NS(id=9, chat=NS(id=123, title=None), sender_chat=None,
              from_user=NS(id=42, first_name='<Alice>', username='alice', is_bot=False))


def client():
    return NS(send_message=AsyncMock(return_value=NS(id=77)), copy_message=AsyncMock())


def test_logs_identity_and_copies_original_as_reply():
    bot = client()
    asyncio.run(handler()(bot, message()))
    assert '&lt;Alice&gt;' in bot.send_message.call_args.args[1]
    assert '<code>42</code>' in bot.send_message.call_args.args[1]
    bot.copy_message.assert_awaited_once_with(-100, 123, 9, reply_to_message_id=77)


def test_disabled_log_channel_and_bot_senders_are_skipped():
    for enabled, chat_id, is_bot in [(False, 123, False), (True, -100, False), (True, 123, True)]:
        bot, msg = client(), message()
        msg.chat.id, msg.from_user.is_bot = chat_id, is_bot
        asyncio.run(handler(enabled)(bot, msg))
        bot.send_message.assert_not_awaited()


def test_channel_failure_does_not_escape():
    bot = client()
    bot.send_message.side_effect = RuntimeError('no permission')
    asyncio.run(handler()(bot, message()))
    bot.copy_message.assert_not_awaited()


def test_anonymous_sender_and_protected_copy():
    bot, msg = client(), message()
    msg.from_user = None
    msg.sender_chat = NS(id=-456, title='Group <Admin>')
    bot.copy_message.side_effect = RuntimeError('protected')
    asyncio.run(handler()(bot, msg))
    assert 'Group &lt;Admin&gt;' in bot.send_message.call_args.args[1]
    assert '<code>-456</code>' in bot.send_message.call_args.args[1]
