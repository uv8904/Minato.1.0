"""Resource-bounded, process-isolated user bot clones.

A clone runs the same bot code in a small child process with its own bot token,
owner, session file and Mongo database. Process isolation matters here: plugin
handlers and most configuration in this project are module globals, so running
user-owned bots in the main process would leak admin access and settings between
users. The manager keeps only a bounded number of children alive and encrypts
bot tokens at rest.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # Fail closed; cloning is unavailable without token encryption.
    Fernet = None

    class InvalidToken(Exception):
        pass

logger = logging.getLogger(__name__)

_ACTIVE_STATES = ("starting", "active")
_RECOVERABLE_STATES = ("starting", "active", "paused_capacity", "paused_memory")
_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{20,}$")
_READY_PREFIX = "MINATO_CLONE_READY:"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ManagedClone:
    bot_id: int
    process: asyncio.subprocess.Process
    output_task: asyncio.Task
    monitor_task: Optional[asyncio.Task] = None
    stopping: bool = False


class CloneManager:
    """Own clone records and supervise a capped number of child processes."""

    def __init__(
        self,
        collection,
        *,
        encryption_key: str,
        enabled: bool = True,
        max_bots: int = 1,
        max_records: int = 100,
        min_free_ram_mb: int = 256,
        start_timeout: int = 60,
        main_bot_id: Optional[int] = None,
        repo_root: Path = _REPO_ROOT,
        session_workdir: Path = Path("/tmp/minato_clone_sessions"),
        python_executable: str = sys.executable,
        base_environment: Optional[dict[str, str]] = None,
        process_factory=None,
    ):
        self.collection = collection
        self.enabled = bool(enabled)
        self.max_bots = max(0, min(int(max_bots), 10))
        self.max_records = max(1, min(int(max_records), 1000))
        self.min_free_ram_mb = max(0, int(min_free_ram_mb))
        self.start_timeout = max(5, int(start_timeout))
        self.main_bot_id = int(main_bot_id) if main_bot_id else None
        self.repo_root = Path(repo_root)
        self.session_workdir = Path(session_workdir).expanduser()
        self._session_workdir_ready: Optional[bool] = None
        self.python_executable = python_executable
        self.base_environment = dict(os.environ if base_environment is None else base_environment)
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.children: dict[int, ManagedClone] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._shutting_down = False
        self._vault = self._make_vault(encryption_key)

    @staticmethod
    def _make_vault(key: str):
        if Fernet is None or not key:
            return None
        try:
            return Fernet(key.encode("ascii"))
        except (TypeError, ValueError, UnicodeEncodeError):
            return None

    @property
    def configured(self) -> bool:
        return self.enabled and self._vault is not None and self.max_bots > 0

    @staticmethod
    def valid_token_format(token: str) -> bool:
        return bool(_TOKEN_RE.fullmatch((token or "").strip()))

    @staticmethod
    async def validate_token(token: str) -> Optional[dict[str, Any]]:
        """Ask Telegram getMe; never include the bearer token in an exception/log."""
        token = (token or "").strip()
        if not CloneManager.valid_token_format(token):
            return None
        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"https://api.telegram.org/bot{token}/getMe") as response:
                    if response.status != 200:
                        return None
                    payload = await response.json(content_type=None)
            if not payload.get("ok") or not isinstance(payload.get("result"), dict):
                return None
            bot = payload["result"]
            bot_id = bot.get("id")
            username = bot.get("username")
            if not isinstance(bot_id, int) or not username:
                return None
            return {"id": bot_id, "username": str(username), "first_name": str(bot.get("first_name") or "Bot")}
        except Exception:
            # aiohttp exception messages can contain the request URL (and thus
            # the token); keep this deliberately silent and return one safe error.
            return None

    @staticmethod
    def available_memory_mb() -> Optional[int]:
        """Return cgroup-aware free memory when available, else host free RAM."""
        for limit_path, current_path in (
            (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
            (Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"), Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")),
        ):
            try:
                raw_limit = limit_path.read_text().strip()
                if raw_limit == "max":
                    continue
                limit, current = int(raw_limit), int(current_path.read_text().strip())
                # cgroup v1 uses a huge sentinel for "unlimited".
                if 0 < limit < (1 << 60):
                    return max(0, (limit - current) // (1024 * 1024))
            except (OSError, ValueError):
                continue
        try:
            import psutil

            return int(psutil.virtual_memory().available // (1024 * 1024))
        except Exception:
            # If RAM cannot be measured, don't claim that there is headroom.
            return None

    def _ensure_session_workdir(self) -> bool:
        """Create a private directory for Telegram's plaintext auth-session DBs."""
        if self._session_workdir_ready is not None:
            return self._session_workdir_ready
        try:
            existed = self.session_workdir.exists()
            self.session_workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not existed:
                os.chmod(self.session_workdir, 0o700)
            mode = self.session_workdir.stat().st_mode
            is_private = (mode & 0o077) == 0
            self._session_workdir_ready = (
                self.session_workdir.is_dir()
                and is_private
                and os.access(self.session_workdir, os.R_OK | os.W_OK | os.X_OK)
            )
        except OSError as exc:
            self._session_workdir_ready = False
            logger.error(
                "Could not prepare private clone session directory %s (%s)",
                self.session_workdir,
                type(exc).__name__,
            )
        if not self._session_workdir_ready:
            logger.error("Clone creation is unavailable: session directory is not private and writable")
        return self._session_workdir_ready

    async def start(self) -> int:
        """Recover previously active clones, one at a time, within the cap."""
        if self._started:
            return 0
        self._started = True
        if not self.configured:
            self._cleanup_session_files(set())
            return 0
        if not self._ensure_session_workdir():
            return 0
        try:
            await self.collection.create_index("bot_id", unique=True)
            await self.collection.create_index("owner_id", unique=True)
            docs = await self.collection.find({"status": {"$in": list(_RECOVERABLE_STATES)}}).to_list(length=self.max_records)
        except Exception:
            logger.exception("Could not load persisted bot clones; main bot will continue without them")
            return 0

        self._cleanup_session_files({int(record["bot_id"]) for record in docs})
        restored = 0
        for record in docs:
            if len(self.children) >= self.max_bots:
                await self._set_status(record["bot_id"], "paused_capacity")
                continue
            if not self._has_memory_headroom():
                await self._set_status(record["bot_id"], "paused_memory")
                continue
            try:
                await self._launch(record)
                restored += 1
            except Exception:
                logger.warning("Saved clone %s did not start; it can be retried or removed by its owner", record.get("bot_id"), exc_info=True)
                await self._set_status(record["bot_id"], "failed")
        return restored

    def _has_memory_headroom(self) -> bool:
        available = self.available_memory_mb()
        # Unknown cgroup/host limits fail closed for new clones. The operator
        # can set a measured reserve via CLONE_MIN_FREE_RAM_MB.
        return available is not None and available >= self.min_free_ram_mb

    def _cleanup_session_files(self, keep_bot_ids: set[int]) -> None:
        """Remove orphaned Pyrogram auth-session files without touching live IDs."""
        if not self.session_workdir.exists():
            return
        for path in self.session_workdir.glob("minato_clone_*.session*"):
            match = re.fullmatch(r"minato_clone_(\d+)\.session(?:[-.].*)?", path.name)
            if not match or int(match.group(1)) in keep_bot_ids:
                continue
            try:
                path.unlink()
            except OSError:
                logger.warning("Could not remove an orphaned clone session file: %s", path.name)

    def _remove_session_files(self, bot_id: int) -> None:
        """Erase the Telegram authorization key after an owner deletes a clone."""
        if not self.session_workdir.exists():
            return
        for path in self.session_workdir.glob(f"minato_clone_{int(bot_id)}.session*"):
            try:
                path.unlink()
            except OSError:
                logger.warning("Could not remove session file for deleted clone %s", bot_id)

    async def preflight(self, owner_id: int) -> dict[str, Any]:
        """Check capacity before asking a user to send a BotFather token."""
        if not self.configured:
            return {"status": "disabled"}
        if not self._ensure_session_workdir():
            return {"status": "session_error"}
        existing = await self.collection.find_one({"owner_id": int(owner_id)})
        if existing:
            return {"status": "already_exists", "clone": self.public_record(existing)}
        if await self.collection.count_documents({}) >= self.max_records:
            return {"status": "record_limit"}
        active_count = await self.collection.count_documents({"status": {"$in": list(_ACTIVE_STATES)}})
        if active_count >= self.max_bots:
            return {"status": "capacity"}
        if not self._has_memory_headroom():
            return {"status": "memory"}
        return {"status": "ready"}

    async def create_clone(self, *, owner_id: int, bot: dict[str, Any], token: str) -> dict[str, Any]:
        """Persist an encrypted token and start its isolated bot process."""
        if not self.configured:
            return {"status": "disabled"}
        if not self._ensure_session_workdir():
            return {"status": "session_error"}
        owner_id = int(owner_id)
        bot_id = int(bot["id"])
        if bot_id == self.main_bot_id:
            return {"status": "main_bot"}
        if not self.valid_token_format(token):
            return {"status": "invalid_token"}

        async with self._lock:
            existing = await self.collection.find_one({"owner_id": owner_id})
            if existing:
                return {"status": "already_exists", "clone": self.public_record(existing)}
            if await self.collection.count_documents({}) >= self.max_records:
                return {"status": "record_limit"}
            duplicate = await self.collection.find_one({"bot_id": bot_id})
            if duplicate:
                return {"status": "bot_already_registered"}
            active_count = await self.collection.count_documents({"status": {"$in": list(_ACTIVE_STATES)}})
            if active_count >= self.max_bots:
                return {"status": "capacity"}
            if not self._has_memory_headroom():
                return {"status": "memory"}

            record = {
                "bot_id": bot_id,
                "owner_id": owner_id,
                "username": str(bot["username"]).lstrip("@"),
                "token_ciphertext": self._vault.encrypt(token.strip().encode("utf-8")).decode("ascii"),
                "status": "starting",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            await self.collection.insert_one(record)
            try:
                await self._launch(record)
            except Exception:
                # Keep the encrypted record so /myclone explains the failure;
                # no plaintext token is persisted or written to logs.
                record["status"] = "failed"
                await self._set_status(bot_id, "failed")
                return {"status": "start_failed", "clone": self.public_record(record)}
            record["status"] = "active"
            return {"status": "started", "clone": self.public_record(record)}

    async def restart_clone(self, owner_id: int) -> dict[str, Any]:
        """Retry the owner's failed/paused clone without asking for its token again."""
        if not self.configured:
            return {"status": "disabled"}
        if not self._ensure_session_workdir():
            return {"status": "session_error"}
        async with self._lock:
            record = await self.collection.find_one({"owner_id": int(owner_id)})
            if not record:
                return {"status": "not_found"}
            bot_id = int(record["bot_id"])
            child = self.children.get(bot_id)
            if (
                record.get("status") in _ACTIVE_STATES
                and child is not None
                and child.process.returncode is None
            ):
                return {"status": "already_active", "clone": self.public_record(record)}
            active_count = await self.collection.count_documents({"status": {"$in": list(_ACTIVE_STATES)}})
            if record.get("status") in _ACTIVE_STATES:
                active_count = max(0, active_count - 1)
            if active_count >= self.max_bots:
                return {"status": "capacity"}
            if not self._has_memory_headroom():
                return {"status": "memory"}
            await self._set_status(record["bot_id"], "starting")
            try:
                await self._launch(record)
            except Exception:
                await self._set_status(record["bot_id"], "failed")
                return {"status": "start_failed", "clone": self.public_record(record)}
            record["status"] = "active"
            return {"status": "started", "clone": self.public_record(record)}

    async def get_owner_clone(self, owner_id: int) -> Optional[dict[str, Any]]:
        record = await self.collection.find_one({"owner_id": int(owner_id)})
        return self.public_record(record) if record else None

    async def delete_clone(self, owner_id: int) -> bool:
        """Stop the owner's process and erase the encrypted credential."""
        async with self._lock:
            record = await self.collection.find_one({"owner_id": int(owner_id)})
            if not record:
                return False
            bot_id = int(record["bot_id"])
            child = self.children.pop(bot_id, None)
            if child:
                child.stopping = True
                await self._stop_process(child)
            self._remove_session_files(bot_id)
            await self.collection.delete_one({"bot_id": bot_id})
            return True

    @staticmethod
    def public_record(record: dict[str, Any]) -> dict[str, Any]:
        """Expose metadata only; never return ciphertext or token fields."""
        return {
            "bot_id": int(record.get("bot_id", 0)),
            "owner_id": int(record.get("owner_id", 0)),
            "username": str(record.get("username", "")),
            "status": str(record.get("status", "unknown")),
            "created_at": record.get("created_at"),
        }

    async def _launch(self, record: dict[str, Any]) -> None:
        bot_id = int(record["bot_id"])
        if bot_id in self.children and self.children[bot_id].process.returncode is None:
            return
        try:
            token = self._vault.decrypt(record["token_ciphertext"].encode("ascii")).decode("utf-8")
        except (InvalidToken, KeyError, ValueError, UnicodeError) as exc:
            raise RuntimeError("saved clone token cannot be decrypted") from exc

        env = self._child_environment(record, token)
        process = await self.process_factory(
            self.python_executable,
            str(self.repo_root / "bot.py"),
            cwd=str(self.repo_root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        ready = asyncio.Event()
        output_task = asyncio.create_task(self._drain_output(process, bot_id, ready))
        ready_wait = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait(
                {ready_wait, output_task},
                timeout=self.start_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_wait not in done or not ready.is_set() or process.returncode is not None:
                raise RuntimeError("clone process did not become ready")
        except Exception:
            await self._terminate_process(process)
            output_task.cancel()
            await asyncio.gather(output_task, return_exceptions=True)
            raise
        finally:
            if not ready_wait.done():
                ready_wait.cancel()

        child = ManagedClone(bot_id=bot_id, process=process, output_task=output_task)
        self.children[bot_id] = child
        await self._set_status(bot_id, "active")
        child.monitor_task = asyncio.create_task(self._monitor(child))

    async def _drain_output(self, process, bot_id: int, ready: asyncio.Event) -> None:
        """Drain child output so its pipe cannot block; retain only readiness."""
        stream = process.stdout
        if stream is None:
            return
        sentinel = f"{_READY_PREFIX}{bot_id}"
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                if line.decode("utf-8", "replace").strip() == sentinel:
                    ready.set()
        except (asyncio.CancelledError, Exception):
            return

    async def _monitor(self, child: ManagedClone) -> None:
        try:
            code = await child.process.wait()
            await child.output_task
        except asyncio.CancelledError:
            return
        finally:
            current = self.children.get(child.bot_id)
            if current is child:
                self.children.pop(child.bot_id, None)
                if not child.stopping and not self._shutting_down:
                    await self._set_status(child.bot_id, "failed", exit_code=child.process.returncode)
                    logger.warning("Clone process %s exited (code=%s)", child.bot_id, child.process.returncode)

    async def _stop_process(self, child: ManagedClone) -> None:
        await self._terminate_process(child.process)
        if child.monitor_task and child.monitor_task is not asyncio.current_task():
            child.monitor_task.cancel()
            await asyncio.gather(child.monitor_task, return_exceptions=True)
        if not child.output_task.done():
            child.output_task.cancel()
            await asyncio.gather(child.output_task, return_exceptions=True)

    @staticmethod
    async def _terminate_process(process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await process.wait()
            except Exception:
                pass

    async def shutdown(self) -> None:
        """Stop child processes but keep their records for the next boot."""
        self._shutting_down = True
        children = list(self.children.values())
        self.children.clear()
        for child in children:
            child.stopping = True
        await asyncio.gather(*(self._stop_process(child) for child in children), return_exceptions=True)

    async def _set_status(self, bot_id: int, status: str, **fields) -> None:
        fields.update({"status": status, "updated_at": datetime.now(timezone.utc)})
        try:
            await self.collection.update_one({"bot_id": int(bot_id)}, {"$set": fields})
        except Exception:
            logger.warning("Could not update clone status for bot %s", bot_id, exc_info=True)

    def _child_environment(self, record: dict[str, Any], token: str) -> dict[str, str]:
        # Pass only runtime essentials. In particular, do not inherit payment,
        # IMAP, user-session, cloud-deploy, analytics or external API secrets.
        allowed_runtime = (
            "PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LD_LIBRARY_PATH",
            "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
        )
        env = {key: self.base_environment[key] for key in allowed_runtime if self.base_environment.get(key)}
        for key in ("API_ID", "API_HASH", "DATABASE_URI", "DATABASE_URI2", "MULTIPLE_DB", "COLLECTION_NAME"):
            value = self.base_environment.get(key)
            if value:
                env[key] = value

        owner_id = int(record["owner_id"])
        bot_id = int(record["bot_id"])
        env.update({
            "BOT_TOKEN": token,
            "SESSION": f"minato_clone_{bot_id}",
            "SESSION_WORKDIR": str(self.session_workdir),
            "DATABASE_NAME": f"minato_clone_{bot_id}",
            "ADMINS": str(owner_id),
            "CHANNELS": "",
            "AUTH_CHANNELS": "",
            "AUTH_REQ_CHANNELS": "",
            "AUTH_USERS": "",
            "LOG_CHANNEL": str(owner_id),
            "INDEX_REQ_CHANNEL": str(owner_id),
            "BIN_CHANNEL": str(owner_id),
            "PREMIUM_LOGS": str(owner_id),
            "MOVIE_UPDATE_CHANNEL": str(owner_id),
            "USER_SESSION": "",
            "FAMPAY_ENABLED": "False",
            "STREAM_MODE": "False",
            "PREMIUM_STREAM_MODE": "False",
            "NEW_UPLOADED_MOVIES": "False",
            "COMING_SOON": "False",
            "WATCH_HERO": "False",
            "OTT_HOME": "False",
            "PORT": "0",
            "WORKERS": "2",
            "CLONE_ENABLED": "False",
            "CLONE_MAX_BOTS": "0",
            "MINATO_CLONE_CHILD": "1",
            "MINATO_CLONE_ID": str(bot_id),
            "MINATO_CLONE_OWNER_ID": str(owner_id),
        })
        return env


_manager: Optional[CloneManager] = None
_manager_mongo_client = None


def get_clone_manager(main_bot_id: Optional[int] = None) -> CloneManager:
    """Build one process-wide manager after the main bot has loaded its env."""
    global _manager, _manager_mongo_client
    if _manager is None:
        from motor.motor_asyncio import AsyncIOMotorClient
        from info import (
            CLONE_COLLECTION,
            CLONE_ENABLED,
            CLONE_ENCRYPTION_KEY,
            CLONE_MAX_BOTS,
            CLONE_MAX_RECORDS,
            CLONE_MIN_FREE_RAM_MB,
            CLONE_SESSION_WORKDIR,
            CLONE_START_TIMEOUT,
            DATABASE_NAME,
            DATABASE_URI,
        )

        _manager_mongo_client = AsyncIOMotorClient(DATABASE_URI, serverSelectionTimeoutMS=5000)
        collection = _manager_mongo_client[DATABASE_NAME][CLONE_COLLECTION]
        _manager = CloneManager(
            collection,
            encryption_key=CLONE_ENCRYPTION_KEY,
            enabled=CLONE_ENABLED,
            max_bots=CLONE_MAX_BOTS,
            max_records=CLONE_MAX_RECORDS,
            min_free_ram_mb=CLONE_MIN_FREE_RAM_MB,
            start_timeout=CLONE_START_TIMEOUT,
            main_bot_id=main_bot_id,
            session_workdir=CLONE_SESSION_WORKDIR,
        )
    elif main_bot_id and _manager.main_bot_id is None:
        _manager.main_bot_id = int(main_bot_id)
    return _manager


async def start_clone_service(main_bot_id: Optional[int] = None) -> int:
    """Start/recover clones; the caller treats errors as optional startup work."""
    return await get_clone_manager(main_bot_id).start()


async def stop_clone_service() -> None:
    global _manager_mongo_client
    if _manager is not None:
        await _manager.shutdown()
    if _manager_mongo_client is not None:
        _manager_mongo_client.close()
        _manager_mongo_client = None
