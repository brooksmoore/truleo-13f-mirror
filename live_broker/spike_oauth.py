#!/usr/bin/env python3
"""OAuth SPIKE — Robinhood official Trading MCP (agent.robinhood.com/mcp/trading).

PURPOSE (throwaway, read-only): learn whether we can decouple the broker transport for
UNATTENDED runs. It connects with OAuth, makes read-only calls (accounts/portfolio), and
prints the decisive facts: is a REFRESH TOKEN issued, and how long does the access token live.

It places NO orders. Run on your desktop (needs a browser for the Robinhood login).

    cd live_broker
    ./venv/bin/python spike_oauth.py

Tokens are cached in live_broker/.rh_tokens.json (gitignored). Delete it to re-auth from scratch.
"""
from __future__ import annotations
import asyncio, json, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientMetadata, OAuthToken, OAuthClientInformationFull

SERVER_URL = "https://agent.robinhood.com/mcp/trading"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TOKENS_PATH = Path(__file__).parent / ".rh_tokens.json"


class FileTokenStorage(TokenStorage):
    """Persists OAuth client registration + tokens to a local (gitignored) json file."""
    def __init__(self, path: Path):
        self.path = path
        self._d = json.loads(path.read_text()) if path.exists() else {}

    def _save(self):
        self.path.write_text(json.dumps(self._d, indent=2))

    async def get_tokens(self):
        t = self._d.get("tokens")
        return OAuthToken(**t) if t else None

    async def set_tokens(self, tokens: OAuthToken):
        self._d["tokens"] = tokens.model_dump(exclude_none=True)
        self._save()

    async def get_client_info(self):
        c = self._d.get("client_info")
        return OAuthClientInformationFull(**c) if c else None

    async def set_client_info(self, info: OAuthClientInformationFull):
        self._d["client_info"] = info.model_dump(exclude_none=True, mode="json")
        self._save()


def _wait_for_callback() -> tuple[str, str | None]:
    """One-shot localhost server that captures ?code=&state= from the OAuth redirect."""
    holder: dict[str, str | None] = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            holder["code"] = (q.get("code") or [None])[0]
            holder["state"] = (q.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Auth complete - you can close this tab and return to the terminal.</h2>")
        def log_message(self, *a):
            pass

    srv = HTTPServer(("localhost", REDIRECT_PORT), H)
    srv.handle_request()  # blocks until exactly one request
    srv.server_close()
    return holder.get("code"), holder.get("state")


async def main():
    storage = FileTokenStorage(TOKENS_PATH)

    async def redirect_handler(url: str) -> None:
        print("\n>>> Opening browser for Robinhood login. If it doesn't open, paste this URL:\n", url, "\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    async def callback_handler() -> tuple[str, str | None]:
        # run the blocking one-shot server off the event loop
        return await asyncio.to_thread(_wait_for_callback)

    oauth = OAuthClientProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="truleo_agent broker spike",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    print(f"Connecting to {SERVER_URL} ...")
    async with streamablehttp_client(SERVER_URL, auth=oauth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("\n=== CONNECTED. Tools exposed ===")
            for n in names:
                print("  ", n)

            # read-only probes (no orders)
            for tool, args in [("get_accounts", {}), ("get_portfolio", {})]:
                if tool in names:
                    try:
                        r = await session.call_tool(tool, args)
                        txt = "".join(getattr(c, "text", "") for c in r.content)[:600]
                        print(f"\n=== {tool} -> ===\n{txt}")
                    except Exception as e:
                        print(f"\n{tool} call error: {e}")

    # THE DECISIVE OUTPUT
    tok = (await storage.get_tokens())
    print("\n========== TOKEN / SESSION FACTS (the point of this spike) ==========")
    if tok:
        print("  access_token present:", bool(tok.access_token))
        print("  token_type          :", tok.token_type)
        print("  expires_in (seconds):", tok.expires_in, f"(~{(tok.expires_in or 0)/3600:.1f}h)")
        print("  REFRESH TOKEN present:", bool(tok.refresh_token), "  <-- if True, unattended cron is viable")
        print("  scope               :", tok.scope)
    else:
        print("  no tokens stored (auth did not complete)")
    print("====================================================================")


if __name__ == "__main__":
    asyncio.run(main())
