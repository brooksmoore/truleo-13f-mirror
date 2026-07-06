#!/usr/bin/env python3
"""Standalone Robinhood Trading MCP transport — decouples the broker from the agent runtime.

Provides a sync `call(full_tool_name, **params) -> dict` (the shape `RealRobinhoodClient`'s
`live_call_fn` seam expects) backed by the official remote MCP at agent.robinhood.com/mcp/trading,
using the OAuth session cached by spike_oauth.py (refresh-token auto-renew → unattended cron).

Usage:
    from live_broker.rh_transport import RobinhoodMCPBridge
    with RobinhoodMCPBridge() as bridge:
        client = RealRobinhoodClient(live_call_fn=bridge.call)   # inject the seam
        ...                                                       # run a normal cycle

The MCP SDK is async; this runs one asyncio loop in a background thread and marshals sync calls
to it, keeping a single MCP session open for the life of the `with` block.
"""
from __future__ import annotations
import asyncio, json, re, threading, webbrowser
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

from live_broker.spike_oauth import FileTokenStorage, _wait_for_callback, SERVER_URL, REDIRECT_URI, TOKENS_PATH

# Bot uses full names like "mcp__robinhood-trading__get_equity_quotes"; the server's tools are unprefixed.
_PREFIX = "mcp__robinhood-trading__"


def _build_oauth() -> OAuthClientProvider:
    storage = FileTokenStorage(TOKENS_PATH)

    async def redirect_handler(url: str) -> None:
        print(">>> Robinhood re-auth needed. Opening browser; if it doesn't open, paste:\n", url)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    async def callback_handler():
        return await asyncio.to_thread(_wait_for_callback)

    return OAuthClientProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="truleo_agent live broker",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


def _extract_dict(result) -> dict:
    """Return the raw {"data": ...} envelope the RealRobinhoodClient parsers expect."""
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool error: {getattr(result, 'content', result)}")
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict) and "data" in sc:
        return sc
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    if not text:
        raise ValueError("MCP tool returned no text/structured content")
    return json.loads(text)


class RobinhoodMCPBridge:
    """Sync facade over the async Robinhood MCP.

    Keeps ONE warm session for speed (the remote does ~30 calls/cycle and per-call handshakes are slow).
    The long-lived connection is intermittently flaky (observed transient RuntimeError/CancelledError),
    so call() transparently REBUILDS the session and retries on any failure — warm path is fast, the rare
    drop self-heals. A background event loop hosts the coroutines; cached OAuth → no re-login.
    """
    def __init__(self, ready_timeout: float = 30.0, call_timeout: float = 60.0, retries: int = 5):
        self._ready_timeout = ready_timeout
        self._call_timeout = call_timeout
        self._retries = retries
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._oauth = None
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._ready = threading.Event()

    # ---- lifecycle ----
    def __enter__(self) -> "RobinhoodMCPBridge":
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(self._ready_timeout):
            raise TimeoutError("bridge event loop did not start in time")
        self._oauth = _build_oauth()
        # Warm up the session now (with retries) so the first real call isn't a cold/flaky connect.
        try:
            asyncio.run_coroutine_threadsafe(self.warmup(), self._loop).result(timeout=self._call_timeout * 2)
        except Exception:
            pass  # non-fatal — first call() will connect+retry on its own
        return self

    def __exit__(self, *exc):
        if self._loop and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._teardown(), self._loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        try:
            pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        self._loop.close()

    async def _connect(self):
        stack = AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(SERVER_URL, auth=self._oauth, terminate_on_close=False)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=self._call_timeout)  # don't hang on a dead connect
            self._stack, self._session = stack, session
        except BaseException:
            # connect failed partway — close the partial stack so we don't leak / wedge the next attempt
            try:
                await stack.aclose()
            except BaseException:
                pass
            self._stack, self._session = None, None
            raise

    async def warmup(self):
        """Best-effort: establish the session once at startup so the first real call isn't a cold connect."""
        for _ in range(self._retries):
            try:
                await self._connect()
                return
            except BaseException:
                await self._teardown()
                await asyncio.sleep(1.0)

    async def _teardown(self):
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except BaseException:
                pass
        self._stack, self._session = None, None

    async def _call_with_reconnect(self, name: str, params: dict) -> dict:
        last: BaseException | None = None
        conn_attempts = 0
        throttle_waits = 0
        while conn_attempts < self._retries and throttle_waits < 6:
            try:
                if self._session is None:
                    await self._connect()
                result = await self._session.call_tool(name, params)
                return _extract_dict(result)
            except (Exception, asyncio.CancelledError) as e:  # CancelledError is BaseException, not Exception
                last = e
                msg = str(e) or type(e).__name__
                if "throttled" in msg or "429" in msg:
                    # Respect the server's backoff (don't hammer / don't reconnect — the connection is fine).
                    m = re.search(r"available in (\d+) seconds", msg)
                    await asyncio.sleep((int(m.group(1)) + 1) if m else 5)
                    throttle_waits += 1
                    continue
                if re.search(r"API error 4\d\d", msg) and "429" not in msg:
                    raise  # client error (e.g. 400 bad qty) — will never succeed on retry, surface it now
                await self._teardown()  # connection-level error → fresh session next attempt
                conn_attempts += 1
                if conn_attempts < self._retries:
                    await asyncio.sleep(min(1.5 * conn_attempts, 6.0))  # backoff: let a transient drop pass before reconnect
        raise RuntimeError(f"MCP {name} failed after {conn_attempts} reconnects: {type(last).__name__}: {last}")

    # ---- the seam the bot calls ----
    def call(self, full_tool_name: str, **params) -> dict:
        name = full_tool_name[len(_PREFIX):] if full_tool_name.startswith(_PREFIX) else full_tool_name
        fut = asyncio.run_coroutine_threadsafe(self._call_with_reconnect(name, params), self._loop)
        return fut.result(timeout=self._call_timeout * (self._retries + 1) + 30)  # cover retries + backoff sleeps
