"""Bounded read-only periodic inspection transport; all consumption is on-loop."""
import asyncio
import threading


class PeriodicGatePoller:
    def __init__(self):
        self._admission=asyncio.Lock()
        self._job=None

    @property
    def busy(self):
        return self._job is not None

    async def read(self, reader, consume, current, timeout):
        # Do not create a separate acquisition task: cancellation after that
        # task acquired the lock could otherwise strand its ownership.
        async with asyncio.timeout(timeout):
            await self._admission.acquire()
        if not current():
            self._admission.release()
            return None
        loop=asyncio.get_running_loop()
        future=loop.create_future()
        # A canceled waiter does not cancel native work or its safety result.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        job={'future':future,'abandoned':False}
        self._job=job

        def complete(value,error):
            try:
                if self._job is not job:return
                result=consume(value,error,job['abandoned'])
                if not future.done():future.set_result(result)
            except BaseException as exc:
                if not future.done():future.set_exception(exc)
            finally:
                if self._job is job:
                    self._job=None
                    self._admission.release()

        def native_read():
            value=error=None
            try:value=reader()
            except BaseException as exc:error=exc
            try:loop.call_soon_threadsafe(complete,value,error)
            except RuntimeError:pass  # Closed owner loop: no authority or input survives.

        try:threading.Thread(target=native_read,name='sage-wow-periodic-gate',daemon=True).start()
        except BaseException:
            self._job=None
            self._admission.release()
            future.cancel()
            raise
        try:
            return await asyncio.wait_for(asyncio.shield(future),timeout)
        except BaseException:
            job['abandoned']=True
            raise

    async def drain(self, timeout):
        job=self._job
        if job:
            job['abandoned']=True
            try:await asyncio.wait_for(asyncio.shield(job['future']),timeout)
            except Exception:pass
