from motor.motor_asyncio import AsyncIOMotorClient
from info import DATABASE_URI
from datetime import datetime

class Database:
    def __init__(self, uri, db_name):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]
        self.col = self.db.user
        self.config_col = self.db.configuration

    async def update_top_messages(self, user_id, message_text):
        user = await self.col.find_one({"user_id": user_id, "messages.text": message_text})
        
        if not user:
            await self.col.update_one(
                {"user_id": user_id},
                {"$push": {"messages": {"text": message_text, "count": 1}}},
                upsert=True
            )
        else:
            await self.col.update_one(
                {"user_id": user_id, "messages.text": message_text},
                {"$inc": {"messages.$.count": 1}}
            )

    async def get_top_messages(self, limit=30):
        pipeline = [
            {"$unwind": "$messages"},
            {"$group": {"_id": "$messages.text", "count": {"$sum": "$messages.count"}}},
            {"$sort": {"count": -1}},
            {"$limit": limit}
        ]
        results = await self.col.aggregate(pipeline).to_list(limit)
        return [result['_id'] for result in results]

    async def get_config(self, key, default=None):
        """Read one runtime-config value (e.g. start-flash emoji/sticker)."""
        doc = await self.config_col.find_one({"key": key})
        return doc.get("value", default) if doc else default

    async def set_config(self, key, value):
        """Persist one runtime-config value across restarts."""
        await self.config_col.update_one(
            {"key": key},
            {"$set": {"value": value, "updated_at": datetime.now()}},
            upsert=True
        )

mdb = Database(DATABASE_URI, "admin_database")
