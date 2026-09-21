"""Expose one local file over an ephemeral Cloudflare quick tunnel for the few
seconds Instagram needs to fetch it, then tear it down.

Requires the `cloudflared` binary on PATH (no Cloudflare account needed for quick
tunnels). Range requests are handled by Flask's send_from_directory(conditional=1)
— Instagram's fetcher relies on them.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests
from flask import Flask, abort, send_from_directory
from werkzeug.serving import make_server

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


class _FileServer(threading.Thread):
    """Serves exactly the files in `directory` on 127.0.0.1:<random port>."""

    def __init__(self, directory: Path):
        super().__init__(daemon=True)
        app = Flask(__name__)
        names = {p.name for p in directory.iterdir() if p.is_file()}

        @app.get("/<path:name>")
        def _get(name: str):
            if name not in names:
                abort(404)
            return send_from_directory(directory, name, conditional=True)

        self._srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.port = self._srv.server_port

    def run(self) -> None:
        self._srv.serve_forever()

    def stop(self) -> None:
        self._srv.shutdown()


def _publicly_resolvable(host: str, *, timeout: float = 5.0) -> bool:
    """DNS-over-HTTPS lookup via Cloudflare's public resolver, bypassing
    whatever this machine's own resolver does. Some routers/ISPs slow-walk or
    outright fail to resolve freshly minted trycloudflare.com names for
    minutes at a time (observed here) even though general internet access and
    the tunnel's own connection to Cloudflare are fine — but Instagram's
    fetcher isn't on this network or subject to that, so confirming the name
    is publicly resolvable is the more relevant signal than whether our own
    machine's resolver has caught up yet."""
    try:
        resp = requests.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": host, "type": "A"},
            headers={"accept": "application/dns-json"}, timeout=timeout,
        )
        return resp.ok and bool(resp.json().get("Answer"))
    except requests.RequestException:
        return False


def _wait_serving(url: str, *, timeout: float = 45.0, interval: float = 2.0) -> None:
    """Block until `url` is reachable: a direct HEAD (works wherever the local
    resolver behaves normally), falling back to the public-DNS check above
    once the local resolver has had a few seconds and still can't find it."""
    host = url.split("/")[2]
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            resp = requests.head(url, timeout=5)
            if resp.ok:
                return
        except requests.RequestException as exc:
            last_exc = exc
            if _publicly_resolvable(host):
                return
        time.sleep(interval)
    detail = f" ({last_exc})" if last_exc else ""
    raise RuntimeError(f"tunnel URL never became reachable: {url}{detail}")


def _open_tunnel(file_path: Path, *, ready_timeout: float, serving_timeout: float):
    """One attempt: stand up the local file server + a cloudflared quick tunnel,
    wait for it to actually serve the file, and return (url, cleanup). On any
    failure, cleans up after itself and raises — the caller decides whether to
    retry with a fresh tunnel (a new hostname gets its own, independent shot at
    fast DNS/edge propagation)."""
    staging = Path(tempfile.mkdtemp(prefix="sp-ig-"))
    server: _FileServer | None = None
    proc: subprocess.Popen | None = None

    def cleanup() -> None:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if server is not None:
            server.stop()
        shutil.rmtree(staging, ignore_errors=True)

    try:
        link = staging / file_path.name
        try:
            os.link(file_path, link)
        except OSError:
            shutil.copy2(file_path, link)

        server = _FileServer(staging)
        server.start()

        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "--url",
             f"http://127.0.0.1:{server.port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

        base = None
        deadline = time.time() + ready_timeout
        while time.time() < deadline and proc.poll() is None:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                continue
            m = _URL_RE.search(line)
            if m:
                base = m.group(0)
                break
        if not base:
            raise RuntimeError("cloudflared did not report a tunnel URL in time")

        url = f"{base}/{file_path.name}"
        _wait_serving(url, timeout=serving_timeout)
        return url, cleanup
    except Exception:
        cleanup()
        raise


@contextmanager
def public_file(
    file_path: Path, *, ready_timeout: float = 40.0, serving_timeout: float = 45.0, attempts: int = 3,
):
    """Yield an https URL serving `file_path` for the life of the `with` block.

    Quick tunnels get a brand-new trycloudflare.com hostname every time, and
    Cloudflare's own banner warns it "may take some time to be reachable" —
    propagation is occasionally much slower than a few seconds. Retrying with a
    fresh tunnel/hostname on failure is cheap and gives each attempt its own
    independent shot, rather than waiting arbitrarily long on one that's stuck.
    """
    if shutil.which("cloudflared") is None:
        raise RuntimeError(
            "cloudflared not found on PATH — install it "
            "(https://developers.cloudflare.com/cloudflare-one/connections/"
            "connect-networks/downloads/) or set INSTAGRAM_PUBLIC_BASE_URL to a "
            "host you control"
        )

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            url, cleanup = _open_tunnel(file_path, ready_timeout=ready_timeout, serving_timeout=serving_timeout)
            break
        except (RuntimeError, TimeoutError) as exc:
            last_exc = exc
            log.warning("instagram: tunnel attempt %d/%d failed: %s", attempt, attempts, exc)
    else:
        raise RuntimeError(f"tunnel never became reachable after {attempts} attempts") from last_exc

    log.info("instagram: tunnel serving %s at %s", file_path.name, url)
    try:
        yield url
    finally:
        cleanup()
