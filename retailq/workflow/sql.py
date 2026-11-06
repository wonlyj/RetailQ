"""A short SQL write must finish before its owning lease can be released."""
import asyncio
from functools import partial


async def write_sql(function,*args,**kwargs):
    task=asyncio.create_task(asyncio.to_thread(partial(function,*args,**kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # asyncio Task.cancel is independent of AnyIO cancellation scopes.
        # Observe even a late exception before propagating cancellation.
        while not task.done():
            try:await asyncio.shield(task)
            except asyncio.CancelledError:continue
            except Exception:break
        try:task.result()
        except BaseException:pass
        raise
