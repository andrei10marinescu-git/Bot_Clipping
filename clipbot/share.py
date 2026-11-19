"""Sharing the public gallery: on the local network (LAN) and/or on the internet via a Cloudflare quick tunnel."""

import re
import socket
import subprocess
import threading
import time

import uvicorn

from .config import TOOLS_DIR, load_config, save_config, settings
from .jobs import logger

CLOUDFLARED = TOOLS_DIR / "cloudflared.exe"
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
_TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def lan_ips() -> list[str]:
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


class ShareManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.host: str | None = None
        self.server_error: str | None = None
        self.tunnel: subprocess.Popen | None = None
        self.public_url: str | None = None
        self.tunnel_status = "oprit"
        self._want_tunnel = False
        self._gen = 0

    # ------------------------------------------------------------------ public server
    def _port(self) -> int:
        return int(settings().get("public_port") or 8766)

    def _start_server(self, host: str) -> None:
        from .public import app

        self._stop_server()
        cfg = uvicorn.Config(app, host=host, port=self._port(), log_level="warning", access_log=False)
        server = uvicorn.Server(cfg)
        th = threading.Thread(target=server.run, daemon=True, name=f"public-{host}")
        th.start()
        for _ in range(100):
            if server.started or not th.is_alive():
                break
            time.sleep(0.05)
        if not server.started:
            self.server_error = f"Portul {self._port()} nu a putut fi deschis (e folosit de alt program?)"
            server.should_exit = True
            return
        self.server, self.thread, self.host, self.server_error = server, th, host, None
        logger.info("public gallery on %s:%s", host, self._port())

    def _stop_server(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
            if self.thread:
                self.thread.join(timeout=5)
        self.server, self.thread, self.host = None, None, None

    # ------------------------------------------------------------------ tunnel
    def _ensure_cloudflared(self) -> None:
        if CLOUDFLARED.exists():
            return
        from curl_cffi import requests as creq

        self.tunnel_status = "descarc cloudflared (~60 MB)…"
        part = CLOUDFLARED.with_suffix(".part")
        with creq.Session(impersonate="chrome", timeout=300) as s, open(part, "wb") as f:
            r = s.get(CLOUDFLARED_URL, stream=True)
            r.raise_for_status()
            for chunk in r.iter_content():
                f.write(chunk)
        part.replace(CLOUDFLARED)

    def _tunnel_loop(self, gen: int) -> None:
        alive = lambda: self._want_tunnel and self._gen == gen  # noqa: E731
        while alive():
            try:
                self._ensure_cloudflared()
                if not alive():
                    break
                self.tunnel_status = "pornesc tunelul…"
                self.public_url = None
                proc = subprocess.Popen(
                    [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self._port()}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.tunnel = proc
                for line in proc.stderr:
                    m = _TUNNEL_RE.search(line)
                    if m and not self.public_url:
                        self.public_url = m.group(0)
                        self.tunnel_status = "activ"
                        logger.info("public tunnel: %s", self.public_url)
                    if not alive():
                        break
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
            except Exception as e:  # noqa: BLE001
                self.tunnel_status = f"eroare: {str(e)[:120]}"
                logger.warning("tunnel: %s", e)
            finally:
                if self._gen == gen:
                    self.public_url = None
            if alive():
                self.tunnel_status = "reconectare…"
                time.sleep(5)
        if self._gen == gen:
            self.tunnel_status = "oprit"

    def _stop_tunnel(self) -> None:
        self._want_tunnel = False
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.kill()
        self.tunnel = None
        self.public_url = None
        self.tunnel_status = "oprit"

    # ------------------------------------------------------------------ api
    def apply(self) -> None:
        with self._lock:
            s = settings()
            lan, inet = bool(s.get("share_lan")), bool(s.get("share_internet"))
            if lan or inet:
                host = "0.0.0.0" if lan else "127.0.0.1"
                if self.server is None or self.host != host:
                    self._start_server(host)
            else:
                self._stop_server()
            if inet and not self._want_tunnel:
                self._want_tunnel = True
                self._gen += 1
                threading.Thread(target=self._tunnel_loop, args=(self._gen,), daemon=True, name="tunnel").start()
            elif not inet and self._want_tunnel:
                self._gen += 1
                self._stop_tunnel()

    def set(self, lan: bool | None = None, internet: bool | None = None) -> dict:
        cfg = load_config()
        if lan is not None:
            cfg["settings"]["share_lan"] = bool(lan)
        if internet is not None:
            cfg["settings"]["share_internet"] = bool(internet)
        save_config(cfg)
        self.apply()
        return self.status()

    def status(self) -> dict:
        s = settings()
        port = self._port()
        running = self.server is not None
        return {
            "lan": bool(s.get("share_lan")),
            "internet": bool(s.get("share_internet")),
            "running": running,
            "local_url": f"http://127.0.0.1:{port}" if running else None,
            "lan_urls": [f"http://{ip}:{port}" for ip in lan_ips()] if running and self.host == "0.0.0.0" else [],
            "public_url": self.public_url,
            "tunnel_status": self.tunnel_status if s.get("share_internet") else "oprit",
            "error": self.server_error,
        }


share_manager = ShareManager()
