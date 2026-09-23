"""Portable local launcher. Uses stdlib until the private environment is ready."""
from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "hackalem-contractors"


def read_json(url: str) -> dict | None:
    try:
        # Local requests must not follow redirects or use a configured proxy.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=1) as response:
            if response.status != 200:
                return None
            payload = response.read(65_537)
            if len(payload) > 65_536:
                return None
            result = json.loads(payload)
            return result if isinstance(result, dict) else None
    except (OSError, urllib.error.URLError, ValueError):
        return None


def is_demo_running(base_url: str) -> bool:
    health = read_json(base_url + "/health/ready")
    if not health or health.get("status") != "ready":
        return False
    if health.get("product") == PRODUCT:
        return True
    # Backward compatibility with the first delivered demo, without that marker.
    if health.get("rank_mode") != "frozen_semantic" or not isinstance(health.get("profiles"), int):
        return False
    info = read_json(base_url + "/api/meta")
    return bool(info and info.get("runtime", {}).get("embedding_model") == "Xenova/multilingual-e5-small"
                and info.get("date_min") == "2026-09-23" and "catalog" in info)


def port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def current_dependencies_ready() -> bool:
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        if not separator:
            return False
        try:
            if metadata.version(name) != version:
                return False
        except metadata.PackageNotFoundError:
            return False
    return True


def environment_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare_python() -> Path:
    # A pre-existing private virtualenv can serve the current workspace directly.
    if sys.prefix != sys.base_prefix and current_dependencies_ready():
        return Path(sys.executable)
    python = environment_python()
    if not python.exists():
        print("Preparing a private .venv (first launch only)...", flush=True)
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    check = subprocess.run([str(python), str(Path(__file__).resolve()), "--check-dependencies"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        print("Installing pinned dependencies. First preparation needs internet and may take several minutes.", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
                       cwd=ROOT, check=True)
    return python


def open_when_ready(base_url: str, stop: threading.Event) -> None:
    for _ in range(60):
        if stop.is_set():
            return
        if is_demo_running(base_url):
            webbrowser.open(base_url)
            return
        stop.wait(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the HackAlem demo locally; Ctrl+C stops the server.")
    parser.add_argument("--port", type=int, default=8000, help="Local port (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    parser.add_argument("--check", action="store_true", help="Check current server/environment without installing or starting")
    parser.add_argument("--check-dependencies", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        print("Python 3.12 or newer is required. Install Python and run start-demo.cmd again.", file=sys.stderr)
        return 2
    if args.check_dependencies:
        return 0 if current_dependencies_ready() else 1
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    base_url = f"http://127.0.0.1:{args.port}"
    if is_demo_running(base_url):
        print(f"HackAlem is already ready: {base_url}")
        print("After editing .env or code, restart the terminal that runs the server to apply changes.")
        if not args.no_browser and not args.check:
            webbrowser.open(base_url)
        return 0
    if not port_available(args.port):
        print(f"Port {args.port} is occupied by another service. Use --port 8001 or stop that service yourself.", file=sys.stderr)
        return 2
    if args.check:
        print(f"No demo is running on {base_url}; port is available.")
        print("Current Python dependencies: " + ("ready" if current_dependencies_ready() else "preparation required"))
        return 0
    try:
        python = prepare_python()
    except (OSError, subprocess.CalledProcessError):
        print("Could not prepare the private environment. Check Python/pip and internet access; then retry.", file=sys.stderr)
        return 2
    print(f"Starting HackAlem: {base_url}", flush=True)
    print("Keep this terminal open. Stop with Ctrl+C. The address is local to this computer.", flush=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "app") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    stopped = threading.Event()
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(base_url, stopped), daemon=True).start()
    try:
        result = subprocess.run([str(python), "-m", "uvicorn", "contractor_matching.api:app", "--app-dir", "app",
                                 "--host", "127.0.0.1", "--port", str(args.port)], cwd=ROOT, env=env)
        return result.returncode
    except KeyboardInterrupt:
        return 0
    except OSError:
        print("Could not start the server. Check that the project directory and Python still exist.", file=sys.stderr)
        return 2
    finally:
        stopped.set()


if __name__ == "__main__":
    raise SystemExit(main())
