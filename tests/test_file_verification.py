"""Regression tests for the file shortlink verification round trip."""
import asyncio
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database.users_chats_db import Database
from plugins import commands
from Script import script


def run(coro):
    return asyncio.run(coro)


class Message:
    def __init__(self, payload, user=42, photo_fails=False):
        self.command = ['/start', payload]
        self.from_user = SimpleNamespace(id=user, mention='tester')
        self.chat = SimpleNamespace(type=commands.enums.ChatType.PRIVATE, id=user)
        self.photo_fails = photo_fails
        self.responses = []

    async def reply(self, text, **kwargs):
        self.responses.append((text, kwargs))

    async def reply_text(self, text=None, **kwargs):
        self.responses.append((text, kwargs))
        return SimpleNamespace(delete=AsyncMock())

    async def reply_photo(self, **kwargs):
        if self.photo_fails:
            raise RuntimeError('invalid image')
        self.responses.append((kwargs['caption'], kwargs))
        return SimpleNamespace(delete=AsyncMock())


@pytest.fixture
def setup_flow(monkeypatch):
    db = SimpleNamespace(
        get_verify_id_info=AsyncMock(), update_verify_id_info=AsyncMock(),
        update_notcopy_user=AsyncMock(), is_user_verified=AsyncMock(return_value=True),
        user_verified=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(commands, 'db', db)
    monkeypatch.setattr(commands, 'EMOJI_MODE', False)
    monkeypatch.setattr(commands.temp, 'U_NAME', 'testbot')
    monkeypatch.setattr(commands, 'get_settings', AsyncMock(return_value={'log': -1, 'verify_time': 3600}))
    monkeypatch.setattr(commands.asyncio, 'sleep', AsyncMock())
    return db


def test_completion_keeps_group_and_stage_and_survives_log_and_photo_failure(setup_flow, monkeypatch):
    db = setup_flow
    db.get_verify_id_info.return_value = {'verified': False, 'group_id': -123, 'stage': 'last_verified'}
    # Simulate restart or a newer link from a different group.
    monkeypatch.setattr(commands.temp, 'VERIFICATIONS', {42: -456})
    client = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('log unavailable')))
    message = Message('notcopy_42_ABC1234_file_id', photo_fails=True)
    run(commands.start(client, message))
    commands.get_settings.assert_awaited_once_with(-123)
    assert list(db.update_notcopy_user.await_args.args[1]) == ['last_verified']
    assert db.update_verify_id_info.await_args.args == (42, 'ABC1234', {'verified': True})
    assert '1ꜱᴛ' in message.responses[-1][0]
    button = message.responses[-1][1]['reply_markup'].inline_keyboard[0][0]
    assert button.url.endswith('start=file_-123_file_id')


def test_other_user_cannot_consume_link(setup_flow):
    message = Message('notcopy_42_ABC1234_file', user=99)
    run(commands.start(SimpleNamespace(), message))
    setup_flow.get_verify_id_info.assert_not_awaited()
    setup_flow.update_notcopy_user.assert_not_awaited()
    assert message.responses


def test_expired_and_malformed_links_do_not_update(setup_flow):
    setup_flow.get_verify_id_info.return_value = {'verified': True}
    run(commands.start(SimpleNamespace(), Message('notcopy_42_ABC1234_file')))
    run(commands.start(SimpleNamespace(), Message('notcopy_nonnumeric_ABC1234_file')))
    setup_flow.update_notcopy_user.assert_not_awaited()


def test_legacy_link_uses_memory_and_detects_first_stage(setup_flow, monkeypatch):
    setup_flow.get_verify_id_info.return_value = {'verified': False}
    setup_flow.is_user_verified.return_value = False
    monkeypatch.setattr(commands.temp, 'VERIFICATIONS', {42: -789})
    message = Message('notcopy_42_ABC1234_file')
    run(commands.start(SimpleNamespace(send_message=AsyncMock()), message))
    commands.get_settings.assert_awaited_once_with(-789)
    assert list(setup_flow.update_notcopy_user.await_args.args[1]) == ['last_verified']


def test_new_verification_user_returns_document_not_insert_result():
    database = Database.__new__(Database)
    database.misc = SimpleNamespace(find_one=AsyncMock(return_value=None), insert_one=AsyncMock(return_value=object()))
    user = run(database.get_notcopy_user(42))
    assert user['user_id'] == 42
    assert 'last_verified' in user


def test_verification_record_stores_group_and_stage():
    database = Database.__new__(Database)
    database.verify_id = SimpleNamespace(insert_one=AsyncMock())
    run(database.create_verify_id(42, 'ABC1234', group_id=-123, stage='second_time_verified'))
    assert database.verify_id.insert_one.await_args.args[0] == {
        'user_id': 42, 'hash': 'ABC1234', 'verified': False,
        'group_id': -123, 'stage': 'second_time_verified',
    }


def test_third_verification_html_has_balanced_tags():
    class Checker(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []
        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)
        def handle_endtag(self, tag):
            assert self.tags.pop() == tag
    checker = Checker()
    checker.feed(script.THIRDT_VERIFICATION_TEXT)
    assert not checker.tags


@pytest.mark.parametrize('verified,second,third,stage,shortener_flags', [
    (False, False, True, 'last_verified', (False, False)),
    (True, True, False, 'second_time_verified', (True, False)),
    (True, False, True, 'third_time_verified', (False, True)),
])
def test_file_link_records_requested_stage(setup_flow, monkeypatch, verified, second, third, stage, shortener_flags):
    db = setup_flow
    db.has_premium_access = AsyncMock(return_value=False)
    db.create_verify_id = AsyncMock()
    db.is_user_verified.return_value = verified
    db.use_second_shortener = AsyncMock(return_value=second)
    db.use_third_shortener = AsyncMock(return_value=third)
    monkeypatch.setattr(commands, 'effective_fsub_channels', AsyncMock(return_value=[]))
    monkeypatch.setattr(commands, 'AUTH_REQ_CHANNELS', [])
    monkeypatch.setattr(commands, 'get_settings', AsyncMock(return_value={'is_verify': True, 'verify_time': 3600}))
    monkeypatch.setattr(commands, 'get_shortlink', AsyncMock(return_value='https://short.example/a'))
    message = Message('file_-123_ABCDEF')
    message.delete = AsyncMock()
    run(commands.start(SimpleNamespace(), message))
    assert db.create_verify_id.await_args.kwargs == {'group_id': -123, 'stage': stage}
    assert commands.get_shortlink.await_args.args[2:] == shortener_flags
