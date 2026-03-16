#!/usr/bin/env python3
"""
EP2 Standalone TX Flash Tool
Flask backend with SSE streaming for real-time flash progress.
"""

import os
import re
import sys
import time
import queue
import shutil
import signal
import subprocess
import threading

from flask import Flask, render_template, request, Response, jsonify
import serial.tools.list_ports

app = Flask(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ELRS_DIR   = os.path.join(BASE_DIR, "..")
SRC_DIR    = os.path.join(ELRS_DIR, "src")
FIRMWARE   = os.path.join(SRC_DIR, ".pio", "build", "EP2_USB_CRSF_via_UART", "firmware.bin")
DEFINES    = os.path.join(SRC_DIR, "user_defines.txt")
ENV_NAME   = "EP2_USB_CRSF_via_UART"

PHRASE_RE  = re.compile(r'^-DMY_BINDING_PHRASE="(.+)"', re.MULTILINE)

# Global flash lock — only one flash at a time
_flash_lock  = threading.Lock()
_flash_queue = None   # queue.Queue used during an active flash


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_ports():
    """Return list of likely serial ports (USB/UART devices)."""
    ports = []
    for p in serial.tools.list_ports.comports():
        if any(kw in (p.description or "").lower() for kw in
               ("usb", "uart", "serial", "cp210", "ch340", "ftdi", "silabs")):
            ports.append({"port": p.device, "desc": p.description})
        elif p.device.startswith("/dev/cu.usbserial") or p.device.startswith("/dev/ttyUSB"):
            ports.append({"port": p.device, "desc": p.description or p.device})
    if not ports:
        # Fall back: show all non-Bluetooth ports
        ports = [{"port": p.device, "desc": p.description or p.device}
                 for p in serial.tools.list_ports.comports()
                 if "bluetooth" not in (p.description or "").lower()]
    return sorted(ports, key=lambda x: x["port"])


def current_phrase():
    """Read binding phrase from user_defines.txt."""
    try:
        with open(DEFINES) as f:
            txt = f.read()
        m = PHRASE_RE.search(txt)
        if m:
            return m.group(1)
    except FileNotFoundError:
        pass
    return ""


def set_phrase(phrase):
    """Write a new binding phrase into user_defines.txt."""
    with open(DEFINES) as f:
        txt = f.read()

    # Replace existing (commented or active) binding phrase line
    new_txt = re.sub(
        r'^#?-DMY_BINDING_PHRASE=".*"',
        f'-DMY_BINDING_PHRASE="{phrase}"',
        txt,
        flags=re.MULTILINE,
    )
    with open(DEFINES, "w") as f:
        f.write(new_txt)


def kill_port_holders(port):
    """Kill any process holding the serial port."""
    try:
        result = subprocess.run(
            ["lsof", "-t", port],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        for pid in pids:
            if pid:
                os.kill(int(pid), signal.SIGTERM)
                time.sleep(0.3)
    except Exception:
        pass


def emit(q, event, data):
    q.put(f"event: {event}\ndata: {data}\n\n")


def run_flash(port, phrase, force_build, q):
    """Run in a thread: optionally rebuild, then flash. Emits SSE."""
    try:
        # ── Step 1: update phrase if changed, or force rebuild ─────────────
        current = current_phrase()
        need_build = force_build or (phrase != current)

        emit(q, "log", f"Binding phrase: {phrase}")
        emit(q, "log", f"Port: {port}")

        if need_build:
            if force_build:
                emit(q, "log", "Force rebuild requested, rebuilding firmware…")
            else:
                emit(q, "log", f"Phrase changed ({current!r} → {phrase!r}), rebuilding firmware…")
            set_phrase(phrase)
            emit(q, "progress", "10")

            # ── Step 2: pio build ──────────────────────────────────────────
            pio = shutil.which("pio") or os.path.expanduser("~/.platformio/penv/bin/pio")
            proc = subprocess.Popen(
                [pio, "run", "-e", ENV_NAME],
                cwd=SRC_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                emit(q, "log", line.rstrip())
            proc.wait()
            if proc.returncode != 0:
                emit(q, "error", "Build failed — check log above.")
                return
            emit(q, "progress", "45")
            emit(q, "log", "Build succeeded.")
        else:
            emit(q, "log", "Phrase unchanged, using existing firmware.bin")
            emit(q, "progress", "45")

        # ── Step 3: kill port holders ──────────────────────────────────────
        emit(q, "log", f"Releasing port {port}…")
        kill_port_holders(port)
        time.sleep(0.5)
        emit(q, "progress", "50")

        # ── Step 4: build esptool command ─────────────────────────────────
        # Prefer: python3 -m esptool (works if installed in the venv)
        # Fallback: python3 <pio-bundled script>
        pio_esptool = os.path.expanduser(
            "~/.platformio/packages/tool-esptoolpy/esptool.py"
        )
        py3 = sys.executable  # same interpreter that is running Flask

        try:
            import esptool as _et  # noqa: F401  — just check it's importable
            esptool_cmd = [py3, "-m", "esptool"]
        except ImportError:
            if os.path.isfile(pio_esptool):
                esptool_cmd = [py3, pio_esptool]
            else:
                emit(q, "error", "esptool not found. Run: pip install esptool")
                return

        # ── Step 5: flash ──────────────────────────────────────────────────
        emit(q, "log", "Starting flash (this takes ~30s)…")
        emit(q, "log", f"Firmware: {FIRMWARE}")

        cmd_parts = esptool_cmd + [
            "-p", port, "-b", "57600", "-c", "esp8266",
            "--before", "no_reset", "--after", "soft_reset",
            "--no-stub", "write_flash", "0x0", FIRMWARE,
        ]

        proc = subprocess.Popen(
            cmd_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        progress_val = 50
        for line in proc.stdout:
            line = line.rstrip()
            emit(q, "log", line)
            # Parse esptool percentage from lines like "Writing at 0x... (45 %)"
            m = re.search(r'\((\d+) %\)', line)
            if m:
                pct = int(m.group(1))
                # Map 0-100% of flash step to 50-95% overall
                progress_val = 50 + int(pct * 0.45)
                emit(q, "progress", str(progress_val))

        proc.wait()
        if proc.returncode != 0:
            emit(q, "error", "Flash failed — check log above.")
            return

        emit(q, "progress", "100")
        emit(q, "done", "Flash complete! EP2 is ready.")

    except Exception as e:
        emit(q, "error", f"Unexpected error: {e}")
    finally:
        _flash_lock.release()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        ports=get_ports(),
        current_phrase=current_phrase(),
    )


@app.route("/ports")
def ports():
    return jsonify(get_ports())


@app.route("/flash", methods=["POST"])
def flash():
    global _flash_queue

    if not _flash_lock.acquire(blocking=False):
        return jsonify({"error": "Flash already in progress"}), 409

    port        = request.form.get("port", "").strip()
    phrase      = request.form.get("phrase", "").strip()
    force_build = request.form.get("force_build") == "1"

    if not port:
        _flash_lock.release()
        return jsonify({"error": "No port selected"}), 400
    if not phrase:
        _flash_lock.release()
        return jsonify({"error": "Binding phrase is required"}), 400
    if not force_build and not os.path.exists(FIRMWARE):
        _flash_lock.release()
        return jsonify({"error": f"firmware.bin not found — enable Force rebuild"}), 400

    _flash_queue = queue.Queue()
    threading.Thread(
        target=run_flash, args=(port, phrase, force_build, _flash_queue), daemon=True
    ).start()

    return jsonify({"status": "started"})


@app.route("/flash/stream")
def flash_stream():
    """SSE endpoint — streams events from the active flash queue."""
    def generate():
        deadline = time.time() + 600  # 10-minute absolute timeout
        while time.time() < deadline:
            if _flash_queue is None:
                yield "event: error\ndata: No flash in progress\n\n"
                return
            try:
                msg = _flash_queue.get(timeout=5)
                yield msg
                if msg.startswith("event: done") or msg.startswith("event: error"):
                    return
            except queue.Empty:
                # Send a keepalive comment so the browser doesn't drop the connection
                yield ": keepalive\n\n"
        yield "event: error\ndata: Flash timed out after 10 minutes\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5173, debug=False)
