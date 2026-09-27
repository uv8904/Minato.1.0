"""Delayed message deletion without occupying a Telegram dispatcher worker."""
import asyncio
import logging

logger = logging.getLogger(__name__)
_running = set()


def delete_later(delay, *messages):
    """Keep the same deletion deadline, but return to the dispatcher immediately.

    A timer allocates no sleeping task/handler stack. Once due, each message is
    attempted independently: a missing result must not prevent request cleanup.
    Returns a TimerHandle so callers/tests can cancel scheduled cleanup.
    """
    async def remove():
        for message in messages:
            try:
                await message.delete()
            except Exception:
                logger.debug("Delayed message deletion failed", exc_info=True)

    def start():
        task = asyncio.create_task(remove())
        _running.add(task)
        task.add_done_callback(_running.discard)

    return asyncio.get_running_loop().call_later(max(0, delay), start)
