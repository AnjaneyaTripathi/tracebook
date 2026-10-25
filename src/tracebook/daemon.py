from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from collections.abc import Iterator

import uvicorn

from tracebook.adapters import adapters
from tracebook.analysis import analyze
from tracebook.api import create_api, create_otlp
from tracebook.capture import Capture
from tracebook.config import Config
from tracebook.platform import daemon_lock, private_dir
from tracebook.pricing import PriceBook
from tracebook.privacy import Redactor
from tracebook.reports import deliver_async, report_due
from tracebook.store import Store


class Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve(config: Config) -> None:
    with daemon_lock(config.root):
        await _serve(config)


async def _serve(config: Config) -> None:
    registry = adapters()
    store = Store(
        config.database, Redactor(config.redact_patterns, capture_content=config.capture_content)
    )
    spool = private_dir(config.root / "spool")
    capture = Capture(store, registry)
    api = Server(
        uvicorn.Config(
            create_api(store, config, registry, capture),
            host="127.0.0.1",
            port=config.port,
            access_log=False,
            log_level="warning",
        )
    )
    otlp = Server(
        uvicorn.Config(
            create_otlp(store, registry),
            host="127.0.0.1",
            port=config.otlp_port,
            access_log=False,
            log_level="warning",
        )
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def ingest() -> None:
        last_analysis = float("-inf")
        dirty = True
        last_revision = -1
        while not stop.is_set():
            try:
                changed = await capture.poll_async(spool, config.scan_seconds)
                dirty = dirty or bool(changed) or store.revision != last_revision
                if time.monotonic() - last_analysis > 60:
                    if dirty:
                        PriceBook(config.root / "pricing.toml").apply(store)
                        analyze(store)
                        dirty = False
                        last_revision = store.revision
                    report_due(store, config, registry)
                    await deliver_async(store, config)
                    last_analysis = time.monotonic()
            except Exception as exc:
                # Surface health errors while leaving APIs and later retries alive.
                capture.errors["daemon"] = type(exc).__name__
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=config.poll_seconds)

    tasks = [
        asyncio.create_task(api.serve()),
        asyncio.create_task(otlp.serve()),
        asyncio.create_task(ingest()),
        asyncio.create_task(stop.wait()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        api.should_exit = otlp.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()
