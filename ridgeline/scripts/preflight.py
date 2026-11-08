"""Pre-demo check for RIDGELINE. Run it in the 10 minutes before the judges arrive.

    python -I ridgeline/scripts/preflight.py            (server on 8768, ATAK link on 8087)
    python -I ridgeline/scripts/preflight.py --port 8779 --cot-port 8089

It only reads. It never changes the phone, adb or the server. Each line says PASS, WARN, FAIL or INFO and
what to do about it. The exit code is 1 when anything FAILs, so it can gate a demo script.

Python standard library only. adb (Android platform-tools) is optional, the phone checks are skipped
without it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PASS, WARN, FAIL, INFO = 'PASS', 'WARN', 'FAIL', 'INFO'
RIDGELINE = Path(__file__).resolve().parents[1]
RESTART = 'stop it (Ctrl+C in its window) and start it again from the repo root: python -I ridgeline/server.py'


# ---------- parsers (pure, tested in test/test_preflight.py) ----------

def parse_devices(out: str) -> list[tuple[str, str]]:
    """[(serial, state)] from `adb devices`, state is device, unauthorized, offline, ..."""
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(('List of devices', '*')):
            rows.append((parts[0], parts[1]))
    return rows


def has_reverse(out: str, port: int) -> bool:
    """True when `adb reverse --list` maps the phone's tcp:port to the PC's tcp:port."""
    want = (f'tcp:{port}', f'tcp:{port}')
    return any(tuple(line.split()[-2:]) == want for line in out.splitlines() if len(line.split()) >= 2)


def parse_battery(out: str) -> tuple[int | None, bool]:
    """(level in %, charging) from `adb shell dumpsys battery`. Only the indented summary lines count,
    because newer Android appends a log of ACTION_BATTERY_CHANGED lines that also contain level:."""
    level, powered = None, False
    for line in out.splitlines():
        s = line.strip()
        if not line.startswith(' '):
            continue
        if s.startswith('level:'):
            try:
                level = int(s.split(':', 1)[1])
            except ValueError:
                pass
        elif s.split(':', 1)[0] in ('AC powered', 'USB powered', 'Wireless powered') and s.endswith('true'):
            powered = True
    return level, powered


def last_run(state: dict, now: float) -> tuple[int, str | None, float | None]:
    """(Lock count, who posted, seconds since) from /api/state."""
    locks = len(((state or {}).get('obs') or {}).get('locks') or [])
    updated = (state or {}).get('updated')
    age = now - updated if isinstance(updated, (int, float)) else None
    return locks, (state or {}).get('source'), age


def ago(s: float | None) -> str:
    if s is None:
        return 'at an unknown time'
    if s < 90:
        return f'{round(s)} s ago'
    if s < 5400:
        return f'{round(s / 60)} min ago'
    return f'{s / 3600:.1f} h ago'


def check_build(build: dict | None, code_now: str | None, now: float) -> tuple[str, str]:
    """Is the running server on the Python in this folder? build is /api/build (None when the server
    is too old to have it), code_now is server.code_id() on the files on disk."""
    if not isinstance(build, dict):
        return FAIL, f'the server is older than this check, so it runs old code. To use the final build, {RESTART}'
    started = ago(now - build['started']) if isinstance(build.get('started'), (int, float)) else 'at an unknown time'
    if code_now is None:
        return WARN, f'could not read server.py in this folder to compare (server started {started})'
    if build.get('code') == code_now:
        return PASS, f'the server runs the code in this folder (started {started})'
    where = f' It was started from {build["folder"]}.' if build.get('folder') else ''
    return FAIL, f'the server runs different code than this folder (started {started}).{where} To fix it, {RESTART}'


def repo_code_id() -> str | None:
    """server.code_id() for the files on disk, by loading server.py without starting it."""
    try:
        spec = importlib.util.spec_from_file_location('ridgeline_server_on_disk', RIDGELINE / 'server.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.code_id()
    except Exception:   # a broken server.py is reported as "could not read", never a crash of the check
        return None


# ---------- probes ----------

def get(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except OSError:
        return 0, b''


def adb(*args: str, timeout: float = 8.0) -> str | None:
    try:
        return subprocess.run(['adb', *args], capture_output=True, text=True, timeout=timeout,
                              encoding='utf-8', errors='replace').stdout
    except (OSError, subprocess.SubprocessError):
        return None


def run(port: int, cot_port: int) -> list[tuple[str, str, str]]:
    return server_checks(port) + phone_checks(port, cot_port)


def server_checks(port: int) -> list[tuple[str, str, str]]:
    rows = []

    def add(level, what, say):
        rows.append((level, what, say))

    base = f'http://127.0.0.1:{port}'
    code, body = get(base + '/api/state')
    state = None
    if code == 200:
        state = json.loads(body.decode('utf-8'))
        sizes = {p: len(get(base + p)[1]) for p in ('/', '/board', '/data/moran.json')}
        missing = [p for p, n in sizes.items() if n == 0]
        if missing:
            add(FAIL, 'server', f'{base} answers, but {", ".join(missing)} came back empty. Run it from the repo root')
        else:
            add(PASS, 'server', f'{base} serves the phone, the board and the terrain ({sizes["/data/moran.json"] // 1024} KB)')
        code, body = get(base + '/api/build')
        level, say = check_build(json.loads(body.decode('utf-8')) if code == 200 else None, repo_code_id(), time.time())
        add(level, 'code', say)
    else:
        add(FAIL, 'server', f'nothing on {base}. Start it from the repo root with: python -I ridgeline/server.py')

    code, body = get(base + '/api/atak')
    if code == 200:
        st = json.loads(body.decode('utf-8'))
        if st.get('listening'):
            n = st.get('clients') or 0
            add(PASS if n else INFO, 'atak feed', f"CoT listening on {st.get('cot_port')}, {n} ATAK client{'s' if n != 1 else ''} connected"
                + ('' if n else '. Only needed if you show ATAK'))
        elif st.get('mode') == 'off':
            add(INFO, 'atak feed', 'CoT is switched off on this server')
        else:
            add(WARN, 'atak feed', f"CoT is not listening ({st.get('last_error') or 'unknown reason'})")

    if state is not None:
        locks, source, age = last_run(state, time.time())
        if locks:
            add(INFO, 'last run', f'{locks} Lock{"s" if locks != 1 else ""} from the {source or "unknown"} {ago(age)}. '
                'Clear it before the judge starts (C on the board, or reset on the phone)')
        else:
            add(PASS, 'last run', 'the board is clear')
    return rows


def phone_checks(port: int, cot_port: int) -> list[tuple[str, str, str]]:
    rows = []

    def add(level, what, say):
        rows.append((level, what, say))

    if not shutil.which('adb'):
        add(WARN, 'adb', 'adb is not on PATH, phone checks skipped. Install Android platform-tools')
        return rows
    devices = parse_devices(adb('devices') or '')
    ready = [s for s, st in devices if st == 'device']
    if not devices:
        add(FAIL, 'phone', 'no phone on USB. Plug in the cable and turn on USB debugging')
        return rows
    if not ready:
        add(FAIL, 'phone', f'phone {devices[0][0]} is {devices[0][1]}. Unlock it and accept the USB debugging prompt')
        return rows
    if len(ready) > 1:
        add(WARN, 'phone', f'{len(ready)} phones attached ({", ".join(ready)}). adb may talk to the wrong one, unplug the spare')
    else:
        add(PASS, 'phone', f'{ready[0]} connected over USB')

    rev = adb('reverse', '--list') or ''
    if has_reverse(rev, port):
        add(PASS, 'usb link', f'adb reverse tcp:{port} is set, the phone reaches http://localhost:{port}')
    else:
        add(FAIL, 'usb link', f'run: adb reverse tcp:{port} tcp:{port}')
    if has_reverse(rev, cot_port):
        add(PASS, 'atak link', f'adb reverse tcp:{cot_port} is set for ATAK on the phone')
    else:
        add(INFO, 'atak link', f'only for ATAK on the phone: adb reverse tcp:{cot_port} tcp:{cot_port}')

    airplane = (adb('shell', 'settings', 'get', 'global', 'airplane_mode_on') or '').strip()
    if airplane == '1':
        add(PASS, 'airplane', 'airplane mode is on, so the phone has no GPS fix or signal to lean on. USB keeps working')
    elif airplane == '0':
        add(WARN, 'airplane', 'airplane mode is off. Turn it on before the judges arrive, the USB link keeps working')

    level, powered = parse_battery(adb('shell', 'dumpsys', 'battery') or '')
    if level is not None:
        charge = ', charging' if powered else ''
        if level < 20:
            add(FAIL, 'battery', f'{level}%{charge}. Charge the phone before the demo')
        elif level < 50:
            add(WARN, 'battery', f'{level}%{charge}. Keep it on the cable until the demo')
        else:
            add(PASS, 'battery', f'{level}%{charge}')
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Read-only pre-demo check for RIDGELINE.')
    ap.add_argument('--port', type=int, default=8768, help='RIDGELINE server port (default 8768)')
    ap.add_argument('--cot-port', type=int, default=8087, help='CoT port for ATAK (default 8087)')
    args = ap.parse_args(argv)
    rows = run(args.port, args.cot_port)
    print(f'RIDGELINE preflight, {time.strftime("%H:%M:%S")}')
    for level, what, say in rows:
        print(f'{level:<5} {what:<10} {say}')
    bad = sum(1 for r in rows if r[0] == FAIL)
    print('Ready for the demo.' if not bad else f'{bad} thing{"s" if bad != 1 else ""} to fix before the demo.')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
