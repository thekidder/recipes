#!/usr/bin/env python3
"""Install this checkout as the current user's macOS login service."""
import argparse
import fcntl
import ipaddress
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


LABEL = "com.erecipe.server"
ROOT = Path(__file__).resolve().parents[2]


def prepare_install(home):
    """Stage a local snapshot; import existing data only on the first install."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    stage = Path(tempfile.mkdtemp(prefix="install-", dir=home))
    try:
        print("Copying server and virtualenv to local application storage...", flush=True)
        shutil.copytree(ROOT / "server", stage / "server", symlinks=True,
                        ignore=shutil.ignore_patterns("data", ".venv", "__pycache__", ".pytest_cache"),
                        copy_function=shutil.copyfile)
        shutil.copytree(ROOT / "server/.venv", stage / "server/.venv", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__"), copy_function=shutil.copyfile)
        if (stage / "server/.env").exists():
            (stage / "server/.env").chmod(0o600)
        # Test imports from the deployed paths before replacing a working runtime.
        subprocess.run([str(stage / "server/.venv/bin/python"), "-c", "import server.__main__"],
                       cwd=stage, check=True)
        data = home / "data"
        if not data.exists():
            source = ROOT / "server/data"
            staged_data = stage / "initial-data"
            if source.exists():
                with (source / "server.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise SystemExit("Stop the repository's server before importing its data")
                    shutil.copytree(source, staged_data, copy_function=shutil.copyfile,
                                    ignore=shutil.ignore_patterns("server.lock", "recipes.sqlite3*"))
                    database = source / "recipes.sqlite3"
                    if database.exists():
                        original = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                        backup = sqlite3.connect(staged_data / "recipes.sqlite3")
                        try:
                            original.backup(backup)
                            if backup.execute("PRAGMA quick_check").fetchone() != ("ok",):
                                raise RuntimeError("Initial database copy failed its integrity check")
                        finally:
                            backup.close()
                            original.close()
            else:
                staged_data.mkdir(mode=0o700)
            staged_data.rename(data)
            print(f"Imported existing library into {data}", flush=True)
        runtime = home / "runtime"
        previous = home / "runtime.previous"
        if previous.exists():
            shutil.rmtree(previous)
        if runtime.exists():
            runtime.rename(previous)
        stage.rename(runtime)
        return runtime
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Reader LAN origin, including :10000")
    parser.add_argument("--app-origin", required=True, help="Tailscale HTTPS origin, including :10000")
    parser.add_argument("--dry-run", action="store_true", help="Print the plist without installing or starting")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This installer requires macOS")
    if os.getuid() == 0:
        parser.error("Run as your login user, without sudo")

    python = ROOT / "server/.venv/bin/python"
    if not python.is_file():
        parser.error("Create server/.venv and install server/requirements.txt first")
    # Keep installation independent of application imports and the live database.
    try:
        app = urlsplit(args.app_origin)
        if (app.scheme != "https" or not app.hostname or app.port != 10000
                or app.username or app.password or app.query or app.fragment
                or app.path not in ("", "/") or "%" in app.netloc
                or any(c.isspace() for c in args.app_origin)):
            raise ValueError("--app-origin must be an HTTPS origin on port 10000")
        hostname = app.hostname.encode("idna").decode().lower()
        origin = f"https://{'[' + hostname + ']' if ':' in hostname else hostname}:10000"
        lan = urlsplit(args.base_url)
        if (lan.scheme != "http" or not lan.hostname or lan.port != 10000
                or lan.username or lan.password or lan.query or lan.fragment
                or lan.path not in ("", "/")
                or lan.hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "::")):
            raise ValueError("--base-url must be a reachable LAN HTTP origin on port 10000")
        address = ipaddress.ip_address(lan.hostname)
        if address.version != 4 or address.is_loopback or address.is_unspecified:
            raise ValueError("--base-url must use this Mac's LAN IPv4 address")
    except (ValueError, UnicodeError) as error:
        parser.error(str(error))

    logs = Path.home() / "Library/Logs/eRecipe"
    install_home = Path.home() / "Library/Application Support/eRecipe"
    runtime = install_home / "runtime"
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            str(runtime / "server/.venv/bin/python"), "-u", "-m", "server",
            # A wildcard bind conflicts with Tailscale's macOS listener on the
            # same port. Bind the reader to its LAN interface explicitly.
            "--host", str(address), "--port", "10000",
            "--serve-port", "10001",
            "--base-url", args.base_url.rstrip("/"),
            "--app-origin", origin,
            "--data-dir", str(install_home / "data"),
        ],
        "WorkingDirectory": str(runtime),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 90,
        "Umask": 0o077,
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
    }
    contents = plistlib.dumps(plist)
    if args.dry_run:
        sys.stdout.buffer.write(contents)
        return

    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".plist.tmp")
    temporary.write_bytes(contents)
    temporary.chmod(0o600)
    subprocess.run(["/usr/bin/plutil", "-lint", str(temporary)], check=True)
    # Reinstalling replaces only this service. A booted-out job stays stopped
    # despite KeepAlive; launchd terminates its process before the new job starts.
    loaded = subprocess.run(["/bin/launchctl", "print", service], capture_output=True).returncode == 0
    if loaded:
        subprocess.run(["/bin/launchctl", "bootout", service], check=True)
    prepare_install(install_home)
    temporary.replace(destination)
    subprocess.run(["/bin/launchctl", "enable", service], check=True)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(destination)], check=True)
    print(f"Installed and started {destination}")
    print(f"Reader: {args.base_url.rstrip('/')}\nDashboard and application: {origin}")
    print(f"Logs: {logs}\nCheck: launchctl print {service}")
    print(f"Active data: {install_home / 'data'}")


if __name__ == "__main__":
    main()
