"""Referral tracking (async — never block the event loop with sync pymongo).

Points are updated with an atomic ``$inc`` so two friends joining at the same
moment can never lose each other's points (the old get-then-set race).
"""
import motor.motor_asyncio
from pymongo import ReturnDocument
from info import DATABASE_URI, DATABASE_NAME
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)


class UserTracker:
    def __init__(self, database=None):
        if database is None:
            database = motor.motor_asyncio.AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]
        self.user_collection = database["referusers"]
        self.refer_collection = database["refers"]

    async def add_user(self, user_id):
        if not await self.is_user_in_list(user_id):
            await self.user_collection.insert_one({'user_id': user_id})

    async def remove_user(self, user_id):
        await self.user_collection.delete_one({'user_id': user_id})

    async def is_user_in_list(self, user_id):
        return bool(await self.user_collection.find_one({'user_id': user_id}))

    async def add_refer_points(self, user_id: int, points: int):
        """Set an absolute points total (used for resets)."""
        await self.refer_collection.update_one(
            {'user_id': user_id},
            {'$set': {'points': points}},
            upsert=True
        )

    async def increment_refer_points(self, user_id: int, delta: int) -> int:
        """Atomically add ``delta`` points and return the new total."""
        doc = await self.refer_collection.find_one_and_update(
            {'user_id': user_id},
            {'$inc': {'points': delta}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int((doc or {}).get('points', 0))

    async def get_refer_points(self, user_id: int):
        user = await self.refer_collection.find_one({'user_id': user_id})
        return user.get('points') if user else 0


referdb = UserTracker()
