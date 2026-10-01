"""PM-only token intake deletes credentials before validation or launch."""
import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("pyrogram")
pytest.importorskip("imdb")

import plugins.clone as clone_plugin


class FakeMessage:
    def __init__(self, owner_id=42, text="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"):
        self.from_user = SimpleNamespace(id=owner_id)
        self.chat = SimpleNamespace(type=clone_plugin.enums.ChatType.PRIVATE)
        self.text = text
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeClient:
    def __init__(self):
        self.sent = []
        self.me = SimpleNamespace(id=999)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class FakeCommandMessage:
    def __init__(self, *, owner_id=42, chat_type=None, command=None):
        self.from_user = SimpleNamespace(id=owner_id) if owner_id is not None else None
        self.chat = SimpleNamespace(type=chat_type or clone_plugin.enums.ChatType.PRIVATE)
        self.command = command or ["clone"]
        self.deleted = False
        self.replies = []

    async def delete(self):
        self.deleted = True

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


_DEFAULT = object()


class FakeManager:
    def __init__(self, message, events, validation=_DEFAULT, result=None):
        self.message = message
        self.events = events
        self.validation = (
            {"id": 123456, "username": "TestCloneBot", "first_name": "Test"}
            if validation is _DEFAULT else validation
        )
        self.result = result or {"status": "started", "clone": {"username": "TestCloneBot", "status": "active"}}

    def valid_token_format(self, token):
        return token.startswith("123456789:")

    async def validate_token(self, _token):
        assert self.message.deleted
        self.events.append("validated")
        return self.validation

    async def create_clone(self, *, owner_id, bot, token):
        assert self.message.deleted
        self.events.append(("created", owner_id, bot["id"], token))
        return self.result


def sample_secret():
    return "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


async def dispatch_token(client, message):
    try:
        await clone_plugin.receive_clone_token(client, message)
    except clone_plugin.StopPropagationError:
        return True
    return False


def test_token_message_is_deleted_before_validation_and_never_echoed(monkeypatch):
    async def scenario():
        message = FakeMessage()
        client = FakeClient()
        events = []
        manager = FakeManager(message, events)
        monkeypatch.setattr(clone_plugin, "CLONE_CHILD_MODE", False)
        monkeypatch.setattr(clone_plugin, "get_clone_manager", lambda _bot_id=None: manager)
        monkeypatch.setattr(
            clone_plugin,
            "_pending",
            {42: {"expires": time.monotonic() + 60, "attempts": 0}},
        )

        assert await dispatch_token(client, message)

        assert message.deleted
        assert events[0] == "validated"
        assert events[1][0:3] == ("created", 42, 123456)
        assert sample_secret() == events[1][3]
        assert all(sample_secret() not in text for _chat, text, _kwargs in client.sent)
        assert client.sent and "@TestCloneBot" in client.sent[0][1]

    asyncio.run(scenario())


def test_unprompted_token_like_message_is_deleted_before_search_handlers(monkeypatch):
    async def scenario():
        message = FakeMessage()
        client = FakeClient()
        monkeypatch.setattr(clone_plugin, "CLONE_CHILD_MODE", False)
        monkeypatch.setattr(clone_plugin, "_pending", {})

        assert await dispatch_token(client, message)

        assert message.deleted
        assert client.sent and "did not use or store" in client.sent[0][1]
        assert sample_secret() not in client.sent[0][1]

    asyncio.run(scenario())


def test_token_like_text_in_group_is_removed_before_group_handlers(monkeypatch):
    async def scenario():
        message = FakeMessage()
        message.chat.type = clone_plugin.enums.ChatType.GROUP
        client = FakeClient()
        monkeypatch.setattr(clone_plugin, "CLONE_CHILD_MODE", False)
        monkeypatch.setattr(clone_plugin, "_pending", {})

        assert await dispatch_token(client, message)

        assert message.deleted
        assert client.sent and "did not use it" in client.sent[0][1]
        assert sample_secret() not in client.sent[0][1]

    asyncio.run(scenario())


def test_expired_prompt_deletes_message_and_stops_other_handlers(monkeypatch):
    async def scenario():
        message = FakeMessage()
        client = FakeClient()
        monkeypatch.setattr(clone_plugin, "CLONE_CHILD_MODE", False)
        monkeypatch.setattr(
            clone_plugin,
            "_pending",
            {42: {"expires": time.monotonic() - 1, "attempts": 0}},
        )

        assert await dispatch_token(client, message)

        assert message.deleted
        assert client.sent and "expired" in client.sent[0][1]
        assert sample_secret() not in client.sent[0][1]

    asyncio.run(scenario())


def test_token_in_group_command_is_removed_and_never_used(monkeypatch):
    async def scenario():
        message = FakeCommandMessage(
            chat_type=clone_plugin.enums.ChatType.GROUP,
            command=["clone", sample_secret()],
        )
        client = FakeClient()
        def forbidden_manager(*_args):
            raise AssertionError("must not validate group token")
        monkeypatch.setattr(clone_plugin, "get_clone_manager", forbidden_manager)

        await clone_plugin.clone_command(client, message)

        assert message.deleted
        assert client.sent and client.sent[0][0] == 42
        assert sample_secret() not in client.sent[0][1]
        assert all(sample_secret() not in text for text in message.replies)

    asyncio.run(scenario())


def test_token_is_never_validated_or_stored_when_message_deletion_fails(monkeypatch):
    async def scenario():
        message = FakeMessage()
        async def fail_delete():
            raise RuntimeError("cannot delete")
        message.delete = fail_delete
        client = FakeClient()
        events = []
        manager = FakeManager(message, events)
        pending = {42: {"expires": time.monotonic() + 60, "attempts": 0}}
        monkeypatch.setattr(clone_plugin, "CLONE_CHILD_MODE", False)
        monkeypatch.setattr(clone_plugin, "get_clone_manager", lambda _bot_id=None: manager)
        monkeypatch.setattr(clone_plugin, "_pending", pending)

        assert await dispatch_token(client, message)

        assert events == []
        assert pending == {}
        assert "did not use or store" in client.sent[0][1]
        assert sample_secret() not in client.sent[0][1]

    asyncio.run(scenario())


def test_invalid_telegram_token_is_deleted_and_does_not_create_clone(monkeypatch):
    async def scenario():
        message = FakeMessage()
        client = FakeClient()
        events = []
        manager = FakeManager(message, events, validation=None)
        monkeypatch.setattr(clone_plugin, "CLONE_CHILD_MODE", False)
        monkeypatch.setattr(clone_plugin, "get_clone_manager", lambda _bot_id=None: manager)
        monkeypatch.setattr(
            clone_plugin,
            "_pending",
            {42: {"expires": time.monotonic() + 60, "attempts": 0}},
        )

        assert await dispatch_token(client, message)

        assert message.deleted
        assert events == ["validated"]
        assert "token" in client.sent[0][1].lower()
        assert sample_secret() not in client.sent[0][1]

    asyncio.run(scenario())
