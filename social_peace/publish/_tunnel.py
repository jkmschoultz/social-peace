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


@contextmanager
def public_file(file_path: Path, *, ready_timeout: float = 40.0):
    """Yield an https URL serving `file_path` for the life of the `with` block."""
    if shutil.which("cloudflared") is None:
        raise RuntimeError(
            "cloudflared not found on PATH — install it "
            "(https://developers.cloudflare.com/cloudflare-one/connections/"
            "connect-networks/downloads/) or set INSTAGRAM_PUBLIC_BASE_URL to a "
            "host you control"
        )

    staging = Path(tempfile.mkdtemp(prefix="sp-ig-"))
    server: _FileServer | None = None
    proc: subprocess.Popen | None = None
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

        time.sleep(4)  # let the edge finish provisioning the route
        url = f"{base}/{file_path.name}"
        log.info("instagram: tunnel serving %s at %s", file_path.name, url)
        yield url
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if server is not None:
            server.stop()
        shutil.rmtree(staging, ignore_errors=True)
