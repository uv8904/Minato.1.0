"""User-created clone safety, isolation and resource-bound tests."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet

from dreamxbotz.util.clone_service import CloneManager


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs[:length] if length else self.docs)


class FakeCollection:
    def __init__(self):
        self.docs = []

    async def create_index(self, *_args, **_kwargs):
        return "ok"

    def find(self, query):
        states = query.get("status", {}).get("$in")
        docs = [doc.copy() for doc in self.docs if not states or doc.get("status") in states]
        return FakeCursor(docs)

    async def find_one(self, query):
        return next((doc.copy() for doc in self.docs if all(doc.get(k) == v for k, v in query.items())), None)

    async def count_documents(self, query):
        states = query.get("status", {}).get("$in")
        return sum(1 for doc in self.docs if not states or doc.get("status") in states)

    async def insert_one(self, doc):
        self.docs.append(doc.copy())
        return SimpleNamespace(inserted_id=doc["bot_id"])

    async def update_one(self, query, update):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, query):
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if not all(doc.get(k) == v for k, v in query.items())]
        return SimpleNamespace(deleted_count=before - len(self.docs))


class FakeProcess:
    def __init__(self, bot_id):
        self.returncode = None
        self._bot_id = int(bot_id)
        self._lines = [f"MINATO_CLONE_READY:{self._bot_id}\n".encode()]
        self._closed = asyncio.Event()
        self.stdout = self
        self.terminated = False

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await self._closed.wait()
        return b""

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._closed.set()

    def kill(self):
        self.returncode = -9
        self._closed.set()

    async def wait(self):
        await self._closed.wait()
        return self.returncode


def make_manager(*, max_bots=1, memory=1024, process_factory=None, session_workdir=Path("/tmp/minato-tests")):
    collection = FakeCollection()
    key = Fernet.generate_key().decode()
    manager = CloneManager(
        collection,
        encryption_key=key,
        enabled=True,
        max_bots=max_bots,
        min_free_ram_mb=256,
        start_timeout=2,
        main_bot_id=999,
        repo_root="/tmp/minato-tests",
        session_workdir=session_workdir,
        base_environment={
            "PATH": "/usr/bin",
            "API_ID": "12345",
            "API_HASH": "private-api-hash",
            "DATABASE_URI": "mongodb://db.internal",
            "GROQ_API_KEY": "must-not-reach-clone",
            "FAMPAY_EMAIL_PASSWORD": "must-not-reach-clone",
            "USER_SESSION": "must-not-reach-clone",
        },
        process_factory=process_factory,
    )
    manager.available_memory_mb = lambda: memory
    return manager, collection


def sample_bot(bot_id=123456, username="SampleCloneBot"):
    return {"id": bot_id, "username": username, "first_name": "Sample"}


def sample_token():
    return "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def test_token_format_and_invalid_encryption_key_fail_closed():
    assert CloneManager.valid_token_format(sample_token())
    assert not CloneManager.valid_token_format("not-a-token")
    manager = CloneManager(FakeCollection(), encryption_key="bad-key", max_bots=2)
    assert not manager.configured


def test_clone_session_workdir_is_created_with_private_permissions(tmp_path):
    session_dir = tmp_path / "private-sessions"
    manager, _ = make_manager(session_workdir=session_dir)

    result = asyncio.run(manager.preflight(42))

    assert result["status"] == "ready"
    assert session_dir.is_dir()
    assert session_dir.stat().st_mode & 0o777 == 0o700


def test_existing_shared_session_directory_fails_closed(tmp_path):
    session_dir = tmp_path / "shared-sessions"
    session_dir.mkdir(mode=0o777)
    session_dir.chmod(0o777)
    manager, _ = make_manager(session_workdir=session_dir)

    assert asyncio.run(manager.preflight(42))["status"] == "session_error"


def test_clone_starts_isolated_encrypted_worker_and_delete_removes_it(tmp_path):
    spawned = []

    async def factory(*_args, **kwargs):
        spawned.append(kwargs)
        return FakeProcess(kwargs["env"]["MINATO_CLONE_ID"])

    async def scenario():
        manager, collection = make_manager(process_factory=factory, session_workdir=tmp_path)
        result = await manager.create_clone(owner_id=42, bot=sample_bot(), token=sample_token())
        assert result["status"] == "started"
        assert result["clone"]["status"] == "active"
        assert len(spawned) == 1
        saved = collection.docs[0]
        assert saved["status"] == "active"
        assert sample_token() not in saved["token_ciphertext"]
        assert "token_ciphertext" not in result["clone"]

        env = spawned[0]["env"]
        assert env["BOT_TOKEN"] == sample_token()
        assert env["ADMINS"] == "42"
        assert env["DATABASE_NAME"] == "minato_clone_123456"
        assert env["CHANNELS"] == ""
        assert env["WORKERS"] == "2"
        assert env["STREAM_MODE"] == "False"
        assert env["MINATO_CLONE_CHILD"] == "1"
        assert env["SESSION_WORKDIR"] == str(tmp_path)
        assert "FAMPAY_EMAIL_PASSWORD" not in env
        assert "GROQ_API_KEY" not in env
        assert "USER_SESSION" in env and env["USER_SESSION"] == ""

        session_file = tmp_path / "minato_clone_123456.session"
        session_file.write_text("fake Telegram auth key")
        assert await manager.delete_clone(42)
        assert not session_file.exists()
        assert not collection.docs
        assert spawned[0]["env"]["BOT_TOKEN"] == sample_token()
        await manager.shutdown()

    asyncio.run(scenario())


def test_clone_refuses_capacity_or_low_memory_without_saving_token():
    async def scenario():
        manager, collection = make_manager(max_bots=0)
        assert (await manager.preflight(42))["status"] == "disabled"
        result = await manager.create_clone(owner_id=42, bot=sample_bot(), token=sample_token())
        assert result["status"] == "disabled"
        assert not collection.docs

        manager, collection = make_manager(memory=100)
        assert (await manager.preflight(42))["status"] == "memory"
        result = await manager.create_clone(owner_id=42, bot=sample_bot(), token=sample_token())
        assert result["status"] == "memory"
        assert not collection.docs

    asyncio.run(scenario())


def test_one_clone_per_owner_and_global_slot_limit():
    async def factory(*_args, **kwargs):
        return FakeProcess(kwargs["env"]["MINATO_CLONE_ID"])

    async def scenario():
        manager, collection = make_manager(max_bots=1, process_factory=factory)
        first = await manager.create_clone(owner_id=42, bot=sample_bot(111111, "OneBot"), token=sample_token())
        assert first["status"] == "started"
        duplicate_owner = await manager.create_clone(
            owner_id=42, bot=sample_bot(222222, "TwoBot"), token=sample_token()
        )
        assert duplicate_owner["status"] == "already_exists"
        full = await manager.create_clone(
            owner_id=43, bot=sample_bot(333333, "ThreeBot"), token=sample_token()
        )
        assert full["status"] == "capacity"
        assert len(collection.docs) == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_child_environment_uses_private_database_and_does_not_inherit_secrets():
    manager, _ = make_manager()
    env = manager._child_environment(
        {"bot_id": 123456, "owner_id": 77}, sample_token()
    )
    assert env["DATABASE_NAME"] == "minato_clone_123456"
    assert env["ADMINS"] == "77"
    assert env["LOG_CHANNEL"] == "77"
    assert env["PORT"] == "0"
    assert env["FAMPAY_ENABLED"] == "False"
    assert env["MINATO_CLONE_CHILD"] == "1"
    assert "FAMPAY_EMAIL_PASSWORD" not in env
    assert "GROQ_API_KEY" not in env
    assert "USER_SESSION" in env and not env["USER_SESSION"]
