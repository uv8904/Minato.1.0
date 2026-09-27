"""Small process-local TTL/LRU cache with cancellation-safe request coalescing."""
import asyncio
from collections import OrderedDict
from time import monotonic


class AsyncTTLCache:
    def __init__(self, maxsize=128, ttl=30):
        self.maxsize = max(0, maxsize)
        self.ttl = max(0, ttl)
        self._values = OrderedDict()
        self._pending = {}
        self._generation = 0

    def clear(self):
        self._generation += 1
        self._values.clear()
        # Old readers can finish, but new readers must not join stale work.
        self._pending.clear()

    async def get(self, key, load):
        if not self.maxsize or not self.ttl:
            return await load()
        now = monotonic()
        # Expiry order differs from LRU order after a hit.
        for old_key, (expires, _) in list(self._values.items()):
            if expires <= now:
                del self._values[old_key]
        if key in self._values:
            self._values.move_to_end(key)
            return self._values[key][1]
        task = self._pending.get(key)
        if task is None:
            # Bound retained in-flight bookkeeping as well as cached values.
            if len(self._pending) >= self.maxsize:
                return await load()
            generation = self._generation

            async def fill():
                try:
                    value = await load()
                    if generation == self._generation:
                        self._values[key] = (monotonic() + self.ttl, value)
                        self._values.move_to_end(key)
                        while len(self._values) > self.maxsize:
                            self._values.popitem(last=False)
                    return value
                finally:
                    if self._pending.get(key) is asyncio.current_task():
                        del self._pending[key]

            task = asyncio.create_task(fill())
            self._pending[key] = task
            # If all waiters cancel, still retrieve any eventual exception.
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return await asyncio.shield(task)
