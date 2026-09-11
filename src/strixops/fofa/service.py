"""A single persisted FOFA search worker; historical views never requery FOFA."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

from .client import FofaClient
from .errors import FofaError
from .settings import checked_root, default_root, lock, read_settings
from .store import Store, integer, prepare_row

SEARCH_TIMEOUT = 180.0
MAX_SEARCH_BYTES = 32 * 1024 * 1024


class FofaService:
    def __init__(self, root: Path, *, client: FofaClient | None = None, page_interval: float = 1.0):
        self.root = Path(root)
        self.store = Store(root)
        self.client = client or FofaClient()
        self.page_interval = page_interval
        self.tasks: dict[str, asyncio.Task] = {}
        self._recovered = False

    def recover_if_idle(self):
        if self._recovered or not checked_root(self.root):
            return
        try:
            with lock(self.root, ".search.lock", nonblocking=True):
                self.store.recover()
        except FofaError as exc:
            if exc.code != "search_running":
                raise
        self._recovered = True

    async def start(self, body: dict) -> dict:
        if set(body) - {"query", "max_results"}:
            raise FofaError("invalid_request")
        query = body.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 4096 or "\x00" in query:
            raise FofaError("invalid_request")
        try:
            query.encode("utf-8")
        except UnicodeError:
            raise FofaError("invalid_request") from None
        max_results = integer(body.get("max_results"), 100, 1, 10000)
        settings = read_settings(self.root)
        if not settings["enabled"]:
            raise FofaError("disabled")
        if not settings["key"]:
            raise FofaError("not_configured")
        if settings["key"] in query:
            raise FofaError("invalid_request")
        guard = lock(self.root, ".search.lock", nonblocking=True)
        guard.__enter__()
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                guard.__exit__(None, None, None)

        try:
            self.store.recover()
            self._recovered = True
            search = self.store.create_search(query.strip(), max_results)
            task = asyncio.create_task(self._run(search, settings, release), name="fofa-search")
            self.tasks[search["search_id"]] = task

            def completed(done):
                # Cancellation before the coroutine first runs still releases
                # its account lock and records an interrupted search.
                if not released:
                    with suppress(FofaError):
                        self.store.status(search["search_id"], "failed", "interrupted")
                release()
                self.tasks.pop(search["search_id"], None)

            task.add_done_callback(completed)
            return search
        except BaseException:
            release()
            raise

    async def _run(self, search: dict, settings: dict, release):
        search_id, loaded, failure, saved_bytes = search["search_id"], 0, None, 0
        try:
            self.store.status(search_id, "running")
            # Keep size stable because FOFA's page offset is based on page*size.
            # Choose an exact divisor to avoid fetching more records than asked.
            # Large prime limits use one page (FOFA supports size <= 10000).
            maximum = search["max_results"]
            divisors = [
                size
                for size in range(min(maximum, 500), 0, -1)
                if maximum % size == 0 and maximum // size <= 100
            ]
            size = divisors[0] if divisors else maximum
            async with asyncio.timeout(SEARCH_TIMEOUT):
                for page in range(1, maximum // size + 1):
                    if page > 1:
                        await asyncio.sleep(self.page_interval)
                    values, total = await self.client.page(search["query"], page, size, settings)
                    rows = [
                        prepare_row(row, loaded + index + 1, settings["key"])
                        for index, row in enumerate(values)
                    ]
                    saved_bytes += len(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
                    if saved_bytes > MAX_SEARCH_BYTES:
                        raise FofaError("response_too_large", 502)
                    self.store.append_page(search_id, rows, total)
                    loaded += len(rows)
                    if len(rows) < size or (total is not None and loaded >= total):
                        break
            self.store.status(search_id, "completed")
        except asyncio.CancelledError:
            failure = "interrupted"
            raise
        except TimeoutError:
            failure = "timeout"
        except FofaError as exc:
            failure = exc.code
        except Exception:
            failure = "provider_error"
        finally:
            if failure:
                with suppress(FofaError):
                    self.store.status(search_id, "partial" if loaded else "failed", failure)
            release()

    async def delete(self, search_id: str):
        self.recover_if_idle()
        search = self.store.search(search_id)
        task = self.tasks.get(search_id)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        elif search["status"] in {"queued", "running"}:
            raise FofaError("search_running", 409)
        self.store.delete(search_id)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


_SERVICES: dict[str, FofaService] = {}


def get_service(root: Path | None = None) -> FofaService:
    selected = Path(root) if root is not None else default_root()
    key = str(selected.absolute())
    if key not in _SERVICES:
        _SERVICES[key] = FofaService(selected)
    return _SERVICES[key]


def read_draft(draft_id: str, *, root: Path | None = None) -> dict:
    """Server-side source snapshot lookup, independent of client source claims."""
    return Store(Path(root) if root is not None else default_root()).draft(draft_id)


async def close_existing(root: Path | None = None):
    """Shutdown never initializes the optional feature or touches its storage."""
    selected = Path(root) if root is not None else default_root()
    key = str(selected.absolute())
    if service := _SERVICES.pop(key, None):
        await service.close()
