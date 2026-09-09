"""Cross-platform desktop launcher for the packaged GQI Talent Radar app."""

from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path


APP_NAME = "GQI Talent Radar"
# Keep the legacy folder name so upgrades retain every task and contact.
APP_DATA_DIR_NAME = "TalentMiner"


def app_data_dir() -> Path:
    """Return a per-user writable directory that survives app upgrades."""
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    path = root / APP_DATA_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_runtime() -> Path:
    data_dir = app_data_dir()
    uploads = data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TALENTMINER_DATA_DIR", str(data_dir))
    os.environ.setdefault("TALENTMINER_UPLOAD_DIR", str(uploads))
    os.environ.setdefault("TALENT_DB_PATH", str(data_dir / "talentminer.db"))
    try:
        import certifi

        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ImportError:
        pass
    return data_dir


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # server is still starting
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"GQI Talent Radar 后端启动超时：{last_error}")


def main() -> None:
    configure_runtime()

    import uvicorn
    import webview
    from app import app

    port = free_local_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    server_thread = threading.Thread(target=server.run, name="gqi-talent-radar-server", daemon=True)
    server_thread.start()
    wait_until_ready(url)

    webview.create_window(
        APP_NAME,
        url,
        width=1440,
        height=900,
        min_size=(1050, 680),
    )
    try:
        webview.start(debug=False)
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
