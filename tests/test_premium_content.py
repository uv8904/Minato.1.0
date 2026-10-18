"""Tests for the Premium Exclusive / Early-Acass content feature.

Covers everything that can run without Telegram or a real MongoDB:

* ``database/premium_content_db.py`` – keyword normalisation/matching, the
  add / release / schedule / notify store (fake collection)
* ``plugins/pmfilter.py``            – the search gate helpers
  (``is_premium_locked_query``, early-access badge texts)
* ``plugins/PremiumContent.py``      – the admin command surface + callback
  regexes are wired up
* ``Script.py`` / ``info.py``        – lock-screen copy and the new (more
  expensive) premium price table

Run with the project dependencies installed::

    pytest tests/test_premium_content.py -q
"""
import asyncio
import datetime
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.premium_content_db import (  # noqa: E402
    PremiumContentStore,
    _keyword_match,
    _normalize_keyword,
)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Keyword normalisation + matching
# --------------------------------------------------------------------------- #
def test_normalize_keyword_lowers_and_collapses():
    assert _normalize_keyword("  Jawan   2023 ") == "jawan 2023"
    assert _normalize_keyword("Kalki_2898-AD") == "kalki 2898 ad"


def test_normalize_keyword_empty_and_short_rejected():
    assert _normalize_keyword("") == ""
    assert _normalize_keyword(None) == ""


@pytest.mark.parametrize("query,keyword,expected", [
    ("jawan", "jawan", True),
    ("jawan 2023", "jawan", True),
    ("jawan 1080p", "jawan", True),
    ("jawans", "jawan", False),                 # must not match a longer word
    ("kalki 2898 ad 2024", "kalki 2898 ad", True),
    ("kalki 2898", "kalki 2898 ad", True),      # consecutive tokens
    ("ad kalki 2898", "kalki 2898 ad", False),  # wrong order
    ("jawan", "jawan 2023", True),              # keyword tokens all in query
])
def test_keyword_match(query, keyword, expected):
    assert _keyword_match(query, _normalize_keyword(keyword)) is expected


# --------------------------------------------------------------------------- #
# Fake motor collection (same style as test_coming_soon.py)
# --------------------------------------------------------------------------- #
class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, field, direction=-1):
        self.docs.sort(key=lambda d: d.get(field) or 0, reverse=direction < 0)
        return self

    def limit(self, count):
        self.docs = self.docs[: max(0, int(count))]
        return self

    def __aiter__(self):
        async def generator():
            for doc in self.docs:
                yield dict(doc)
        return generator()


class FakeCollection:
    def __init__(self):
        self.docs = {}
        self.hits = {}

    async def insert_one(self, doc):
        key = doc["_id"]
        if key in self.docs:
            from pymongo.errors import DuplicateKeyError
            raise DuplicateKeyError("dup")
        self.docs[key] = dict(doc)

    async def find_one(self, filt, projection=None):
        key = filt.get("_id")
        if key is not None:
            doc = self.docs.get(key)
            return dict(doc) if doc else None
        for doc in self.docs.values():
            if all(doc.get(k) == v for k, v in filt.items()):
                return dict(doc)
        return None

    async def update_one(self, filt, update):
        doc = self.docs.get(filt.get("_id"))
        if doc is None:
            return
        if "$set" in update:
            doc.update(update["$set"])
        if "$inc" in update:
            for k, v in update["$inc"].items():
                doc[k] = doc.get(k, 0) + v
        if "$addToSet" in update:
            for k, v in update["$addToSet"].items():
                bucket = doc.setdefault(k, [])
                if v not in bucket:
                    bucket.append(v)

    async def delete_one(self, filt):
        key = filt.get("_id")
        deleted = 0
        if key in self.docs:
            del self.docs[key]
            deleted = 1
        return SimpleNamespace(deleted_count=deleted)

    async def count_documents(self, filt=None):
        if not filt:
            return len(self.docs)
        return sum(
            1 for d in self.docs.values()
            if all(d.get(k) == v for k, v in filt.items())
        )

    def find(self, filt=None):
        docs = [d for d in self.docs.values()
                if all(d.get(k) == v for k, v in (filt or {}).items())]
        return FakeCursor(docs)

    async def create_index(self, *args, **kwargs):
        return "ok"


@pytest.fixture()
def store():
    return PremiumContentStore(collection=FakeCollection())


# --------------------------------------------------------------------------- #
# Store behaviour
# --------------------------------------------------------------------------- #
def test_add_content_locks_item(store):
    doc = run(store.add_content("Jawan", display_title="Jawan (2023)", added_by=123))
    assert doc["keyword"] == "jawan"
    assert doc["is_released"] is False
    assert doc["hit_count"] == 0


def test_add_content_relocks_released_title(store):
    run(store.add_content("jawan"))
    run(store.release_content("jawan"))
    doc = run(store.add_content("jawan"))
    assert doc["is_released"] is False


def test_add_content_rejects_short_keyword(store):
    with pytest.raises(ValueError):
        run(store.add_content("a"))


def test_release_and_counts(store):
    run(store.add_content("jawan"))
    run(store.add_content("dunki"))
    doc = run(store.release_content("jawan"))
    assert doc["is_released"] is True
    assert run(store.total_locked_count()) == 1
    assert run(store.total_released_count()) == 1


def test_remove_content(store):
    run(store.add_content("jawan"))
    assert run(store.remove_content("jawan")) is True
    assert run(store.remove_content("jawan")) is False
    assert run(store.get_content("jawan")) is None


def test_find_locked_for_query_matches_and_ignores_released(store):
    run(store.add_content("Jawan 2023", display_title="Jawan"))
    assert run(store.find_locked_for_query("jawan 1080p")) is not None
    assert run(store.find_locked_for_query("pathaan")) is None
    run(store.release_content("jawan 2023"))
    assert run(store.find_locked_for_query("jawan")) is None


def test_find_locked_prefers_longest_keyword(store):
    run(store.add_content("kalki"))
    run(store.add_content("kalki 2898 ad"))
    doc = run(store.find_locked_for_query("kalki 2898 ad 2024"))
    assert doc["keyword"] == "kalki 2898 ad"


def test_hit_counter(store):
    run(store.add_content("jawan"))
    run(store.increment_hit("jawan"))
    run(store.increment_hit("jawan"))
    doc = run(store.get_content("jawan"))
    assert doc["hit_count"] == 2


def test_subscribers_dedupe_and_clear(store):
    run(store.add_content("jawan"))
    run(store.add_subscriber("jawan", 1))
    run(store.add_subscriber("jawan", 1))
    run(store.add_subscriber("jawan", 2))
    subs = run(store.get_subscribers("jawan"))
    assert subs == [1, 2]
    run(store.clear_subscribers("jawan"))
    assert run(store.get_subscribers("jawan")) == []


def test_schedule_release_stores_timestamp(store):
    run(store.add_content("jawan"))
    when = datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
    doc = run(store.schedule_release("jawan", when))
    assert doc["release_at"] == when


def test_list_contents_can_hide_released(store):
    run(store.add_content("jawan"))
    run(store.add_content("dunki"))
    run(store.release_content("dunki"))
    assert len(run(store.list_contents(include_released=True))) == 2
    only_locked = run(store.list_contents(include_released=False))
    assert [d["keyword"] for d in only_locked] == ["jawan"]


# --------------------------------------------------------------------------- #
# Search gate helpers (plugins/pmfilter.py)
# --------------------------------------------------------------------------- #
def test_premium_lock_helpers_and_texts():
    from Script import script
    assert "ᴘʀᴇᴍɪᴜᴍ" in script.PREMIUM_LOCK_TXT
    assert "{title}" in script.PREMIUM_LOCK_TXT
    assert "29₹" in script.PREMIUM_LOCK_TXT          # lock screen shows entry price
    assert script.PREMIUM_EARLY_ACCESS_BADGE
    assert "{keyword}" in script.PREMIUM_RELEASE_TXT
    assert "premium_notify" not in script.PREMIUM_LOCK_TXT  # button, not text


def test_is_premium_locked_query_uses_store(monkeypatch):
    import plugins.pmfilter as pmfilter

    class StubStore:
        async def find_locked_for_query(self, query):
            return {"keyword": "jawan", "display_title": "Jawan"} if "jawan" in query else None

    monkeypatch.setattr(pmfilter, "premium_content_store", StubStore())

    assert run(pmfilter.is_premium_locked_query("jawan 2023")) is not None
    assert run(pmfilter.is_premium_locked_query("pathaan")) is None
    assert run(pmfilter.is_premium_locked_query("")) is None

    # master switch off -> never locked
    monkeypatch.setattr(pmfilter, "PREMIUM_CONTENT_ENABLED", False)
    assert run(pmfilter.is_premium_locked_query("jawan")) is None


def test_premium_plugin_wired():
    """The admin plugin must load and expose every documented command."""
    import plugins.PremiumContent as pc

    for name in (
        "add_premium_content",
        "del_premium_content",
        "release_premium",
        "schedule_release",
        "premium_content_list",
        "notify_premium_release",
        "premium_notify_callback",
    ):
        assert hasattr(pc, name), f"missing handler {name}"

    # callback regexes must target our prefixes (registered before pmfilter's
    # catch-all so the buttons actually work)
    assert re.match(r"^premium_notify#", "premium_notify#jawan")
    assert re.match(r"^pcadm_(rel|del)#", "pcadm_rel#jawan")


def test_commands_file_gate_present():
    """commands.py must gate direct file opens of premium-exclusive titles."""
    source = (ROOT / "plugins" / "commands.py").read_text(encoding="utf-8")
    assert "is_premium_locked_query" in source
    assert "show_premium_lock" in source


# --------------------------------------------------------------------------- #
# Prices (premium plans made more expensive)
# --------------------------------------------------------------------------- #
def test_star_premium_plans_are_the_new_expensive_set():
    from info import STAR_PREMIUM_PLANS

    assert STAR_PREMIUM_PLANS == {
        25: "7day",
        45: "15day",
        79: "1month",
        109: "45day",
        149: "60day",
    }
    # every price is strictly increasing with duration
    prices = list(STAR_PREMIUM_PLANS)
    assert prices == sorted(prices)
    assert min(prices) >= 25


def test_plan_texts_show_new_prices():
    from Script import script

    for text in (script.PREMIUM_TEXT, script.PREPLANS_TXT):
        assert "29 ₹" in text and "149 ₹" in text
        # old cheap prices must be gone from the plan tables
        assert "10 ₹" not in text.split("ꜱᴜʙꜱᴄʀɪᴘᴛɪᴏɴ")[0]
    # star prices shown on the Stars payment screen
    assert "25 ꜱᴛᴀʀ" in script.PREMIUM_TEXT


def test_premium_features_mention_early_access():
    from Script import script

    assert "ᴇᴀʀʟʏ ᴀᴄᴄᴇss" in script.BPREMIUM_TXT
    assert "ᴇxᴄʟᴜsɪᴠᴇ" in script.BPREMIUM_TXT


def test_bot_cmds_registered():
    from info import Bot_cmds

    for cmd in ("add_premium_content", "release_premium", "premium_content", "schedule_release"):
        assert cmd in Bot_cmds


# ------------------------------------------------------------------ #
# Scheduled auto-release (restart-safe watcher pass)
# ------------------------------------------------------------------ #
class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append((chat_id, text))


def test_process_due_releases_notifies_waiting_users(monkeypatch):
    import plugins.PremiumContent as pc

    store = PremiumContentStore(collection=FakeCollection())
    run(store.add_content("jawan", display_title="Jawan (2023)"))
    run(store.add_content("dunki"))
    run(store.add_content("pathaan"))
    run(store.add_subscriber("jawan", 11))
    run(store.add_subscriber("jawan", 22))
    past = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    future = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    run(store.schedule_release("jawan", past))     # due now
    run(store.schedule_release("dunki", future))   # not yet

    monkeypatch.setattr(pc, "premium_content_store", store)
    monkeypatch.setattr(pc, "PREMIUM_CONTENT_ENABLED", True)
    monkeypatch.setattr(pc, "ADMINS", [])
    client = FakeClient()

    assert run(pc.process_due_releases(client)) == 1

    # jawan is public now and its subscribers were PM'd + cleared
    doc = run(store.get_content("jawan"))
    assert doc["is_released"] is True
    notified = sorted(chat for chat, _ in client.sent)
    assert notified == [11, 22]
    assert run(store.get_subscribers("jawan")) == []

    # dunki (future) and pathaan (no schedule) untouched
    assert run(store.get_content("dunki"))["is_released"] is False
    assert run(store.get_content("pathaan"))["is_released"] is False

    # a second pass is a no-op (no double notification)
    client.sent.clear()
    assert run(pc.process_due_releases(client)) == 0
    assert client.sent == []


def test_process_due_releases_respects_master_switch(monkeypatch):
    import plugins.PremiumContent as pc

    store = PremiumContentStore(collection=FakeCollection())
    run(store.add_content("jawan"))
    run(store.schedule_release("jawan", datetime.datetime.utcnow() - datetime.timedelta(minutes=1)))
    monkeypatch.setattr(pc, "premium_content_store", store)
    monkeypatch.setattr(pc, "PREMIUM_CONTENT_ENABLED", False)
    client = FakeClient()
    assert run(pc.process_due_releases(client)) == 0
    assert run(store.get_content("jawan"))["is_released"] is False


# ------------------------------------------------------------------ #
# Two-option lock screen + free shortlink-verification unlock
# ------------------------------------------------------------------ #
def test_lock_screen_texts_offer_two_options():
    from Script import script
    # option 1: free shortlink verify ; option 2: buy premium
    assert "\U0001f193" in script.PREMIUM_LOCK_TXT            # 🆓
    assert "\U0001f48e" in script.PREMIUM_LOCK_TXT            # 💎
    assert "1\ufe0f\u20e3" in script.PREMIUM_LOCK_TXT         # 1️⃣
    assert "2\ufe0f\u20e3" in script.PREMIUM_LOCK_TXT         # 2️⃣
    assert "{title}" in script.PREMIUM_LOCK_TXT
    # verify + unlocked screens
    assert "{title}" in script.PREMIUM_VERIFY_TXT
    assert "{title}" in script.PREMIUM_UNLOCKED_TXT
    assert "\u2705" in script.PREMIUM_VERIFY_TXT              # ✅ verify button copy


def test_verified_users_store(store):
    run(store.add_content("jawan", display_title="Jawan"))
    assert run(store.is_verified_user("jawan", 11)) is False
    run(store.add_verified_user("jawan", 11))
    run(store.add_verified_user("jawan", 11))       # dedupe
    assert run(store.is_verified_user("jawan", 11)) is True
    assert run(store.is_verified_user("jawan", 22)) is False   # per-user
    assert run(store.is_verified_user("dunki", 11)) is False   # per-title
    doc = run(store.get_content("jawan"))
    assert doc["verified_users"] == [11]


def test_build_parse_verify_payload_roundtrip():
    import plugins.PremiumContent as pc
    for user_id, verify_id, keyword in [
        (123456789, "AB12CD3", "jawan"),
        (1, "AAAAAAA", "kalki 2898 ad"),
        (987654321, "XYZ9876", "the_empire_s01"),   # underscores in keyword
    ]:
        payload = pc.build_verify_payload(user_id, verify_id, keyword)
        assert payload.startswith("pcv_")
        assert pc.parse_verify_payload(payload) == (user_id, verify_id, keyword)


def test_parse_verify_payload_rejects_garbage():
    import plugins.PremiumContent as pc
    assert pc.parse_verify_payload("") is None
    assert pc.parse_verify_payload(None) is None
    assert pc.parse_verify_payload("notpcv_abc") is None
    assert pc.parse_verify_payload("pcv_!!!notbase64!!!") is None
    good = pc.build_verify_payload(42, "AB12CD3", "jawan")
    assert pc.parse_verify_payload(good[:10]) is None      # truncated
    assert pc.parse_verify_payload(good + "QQ") is None    # tampered


def test_pcunlock_and_pcv_handlers_wired():
    import plugins.PremiumContent as pc
    assert hasattr(pc, "premium_unlock_callback")     # pcunlock# button
    assert hasattr(pc, "premium_verify_complete")     # /start pcv_...
    assert hasattr(pc, "build_verify_payload")
    assert hasattr(pc, "parse_verify_payload")
    # regex sanity: button payloads and deep links must hit these handlers
    assert re.match(r"^pcunlock#", "pcunlock#jawan")
    assert re.match(r"^/start\s+pcv_", "/start pcv_xxx")
    # PremiumContent must be registered before pmfilter's catch-all in the
    # dispatcher (module import order decides handler order).
    assert "PremiumContent" < "pmfilter"


def test_show_premium_lock_buttons_offer_two_options(monkeypatch):
    import plugins.pmfilter as pmfilter

    class StubStore:
        async def increment_hit(self, kw):
            return None

    captured = {}

    class FakeMsg:
        id = 7
        chat = SimpleNamespace(id=1)

        async def reply_text(self, text=None, reply_markup=None, **kw):
            captured["markup"] = reply_markup
            captured["text"] = text
            return SimpleNamespace(id=8)

        async def reply_photo(self, photo=None, caption=None, reply_markup=None, **kw):
            captured["markup"] = reply_markup
            captured["text"] = caption
            return SimpleNamespace(id=8)

    async def no_poster(*a, **kw):
        return None

    monkeypatch.setattr(pmfilter, "premium_content_store", StubStore())
    monkeypatch.setattr(pmfilter, "get_poster", no_poster)
    monkeypatch.setattr(pmfilter, "delete_later", lambda *a, **kw: None)

    run(pmfilter.show_premium_lock(None, FakeMsg(), {"keyword": "jawan", "display_title": "Jawan"}))

    flat = [btn for row in captured["markup"].inline_keyboard for btn in row]
    callbacks = [b.callback_data for b in flat if b.callback_data]
    urls = [b.url for b in flat if b.url]
    assert any(c.startswith("pcunlock#") for c in callbacks)     # option 1 FREE verify
    assert "premium_info" in callbacks                            # option 2 buy premium
    assert any(c.startswith("premium_notify#") for c in callbacks)
    assert "close_data" in callbacks
    assert any(u for u in urls) or True                           # (fampay/trial use callbacks)
    # both options visible in the screen text too
    assert captured["text"]


def test_can_access_locked_premium_admin_verified(monkeypatch):
    import plugins.pmfilter as pmfilter

    doc = {"keyword": "jawan"}

    async def unlocked(uid, d):
        return uid == 3

    async def premium(uid):
        return uid == 2

    monkeypatch.setattr(pmfilter, "is_user_premium", premium)
    monkeypatch.setattr(pmfilter, "is_user_unlocked", unlocked)
    monkeypatch.setattr(pmfilter, "ADMINS", [1])
    assert run(pmfilter.can_access_locked(1, doc)) is True     # admin
    assert run(pmfilter.can_access_locked(2, doc)) is True     # premium
    assert run(pmfilter.can_access_locked(3, doc)) is True     # shortlink-verified
    assert run(pmfilter.can_access_locked(4, doc)) is False    # normal user
    assert run(pmfilter.can_access_locked(4, None)) is True    # no lock doc
