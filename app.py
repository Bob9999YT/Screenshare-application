"""
Screen Share App
-----------------
A desktop app that:
  - Captures your screen and serves it over HTTP (MJPEG stream + a
    base64 PNG "snapshot" endpoint, e.g. for feeding into Roblox/other tools)
  - Lets you change FPS / resolution / JPEG quality live from a GUI
  - Can spin up a Cloudflare Quick Tunnel to get a public HTTPS URL
    with one click (no port forwarding / no account needed)

bob9999 says hi

Run as a script:   python app.py
Build as an EXE:   see build.bat
"""

import io
import os
import re
import sys
import time
import json
import queue
import base64
import shutil
import secrets
import platform
import subprocess
import threading
import urllib.request

import mss
from PIL import Image
from flask import Flask, Response, request

import tkinter as tk
from tkinter import ttk, messagebox


# --------------------------------------------------------------------------
# Paths (works both as a plain script and as a frozen PyInstaller exe)
# --------------------------------------------------------------------------

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
CLOUDFLARED_PATH = os.path.join(
    APP_DIR, "cloudflared.exe" if platform.system() == "Windows" else "cloudflared"
)

CLOUDFLARED_DOWNLOAD_URLS = {
    "Windows": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    "Darwin": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
    "Linux": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
}


# --------------------------------------------------------------------------
# App identity
#
# A persistent, random ID unique to this install. It's saved next to the
# exe/script and reused on every run, so the same "app_id" always shows up
# in /snapshot responses coming from this particular app instance. Paste it
# into your Roblox script (or copy it from the GUI) so the script can check
# a response actually came from your own server, not some other endpoint
# that happens to return similarly-shaped JSON.
# --------------------------------------------------------------------------

APP_NAME = "ScreenShareApp"
APP_ID_PATH = os.path.join(APP_DIR, "app_id.txt")


def load_or_create_app_id():
    try:
        if os.path.isfile(APP_ID_PATH):
            with open(APP_ID_PATH, "r") as f:
                existing = f.read().strip()
            if existing:
                return existing
    except Exception:
        pass

    new_id = secrets.token_hex(16)  # 32 hex chars
    try:
        with open(APP_ID_PATH, "w") as f:
            f.write(new_id)
    except Exception:
        pass  # if we can't persist it, still use it for this session
    return new_id


APP_ID = load_or_create_app_id()


# --------------------------------------------------------------------------
# Shared, thread-safe settings
# --------------------------------------------------------------------------

class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        self.fps = 6
        self.monitor_index = 1
        self.output_width = 480
        self.output_height = 360
        self.stream_width = 800
        self.stream_height = 450
        self.jpeg_quality = 60
        self.port = 5000
        self.allowed_username = ""  # empty = no restriction

    def snapshot(self):
        with self.lock:
            return dict(
                fps=self.fps,
                monitor_index=self.monitor_index,
                output_width=self.output_width,
                output_height=self.output_height,
                stream_width=self.stream_width,
                stream_height=self.stream_height,
                jpeg_quality=self.jpeg_quality,
                port=self.port,
                allowed_username=self.allowed_username,
            )

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, v)


SETTINGS = Settings()


# --------------------------------------------------------------------------
# Access requests / approved sessions
#
# Instead of a static shared secret (username, app id, etc. -- anything
# that's just a string someone could copy out of the script), access is
# granted per-session, live, by a human clicking "Allow" in the desktop
# app. A requester calls /request-access, the app shows a prompt, and only
# on approval does the requester get a one-time session token that then
# gates /snapshot, /stream, and /.
# --------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 120  # pending requests older than this auto-expire

requests_lock = threading.Lock()
pending_requests = {}   # request_id -> {username, user_id, status, created, session_token}
approved_sessions = {}  # session_token -> {username, user_id, approved_at}

# GUI polls this queue for new request_ids that need a human decision.
approval_queue = queue.Queue()

SESSION_HEADER = "X-Session-Token"

latest_png_base64 = None
latest_jpeg = None
frame_lock = threading.Lock()

log_queue = queue.Queue()


def log(msg):
    print(msg)
    log_queue.put(msg)


# --------------------------------------------------------------------------
# Capture loop (reads settings live, so GUI changes apply without restart)
# --------------------------------------------------------------------------

_sct = None
_current_monitor_index = None


def get_monitor(index):
    global _sct, _current_monitor_index
    if _sct is None:
        _sct = mss.mss()
    monitors = _sct.monitors
    index = max(0, min(index, len(monitors) - 1))
    return _sct, monitors[index]


def capture_loop(stop_event):
    global latest_png_base64, latest_jpeg

    while not stop_event.is_set():
        start = time.perf_counter()
        cfg = SETTINGS.snapshot()

        try:
            sct, monitor = get_monitor(cfg["monitor_index"])
            screenshot = sct.grab(monitor)

            img = Image.frombytes(
                "RGB", screenshot.size, screenshot.bgra, "raw", "BGRX"
            )

            # Snapshot frame (e.g. for Roblox / other consumers)
            png_img = img.resize(
                (cfg["output_width"], cfg["output_height"]),
                Image.Resampling.BILINEAR,
            )
            png_buffer = io.BytesIO()
            png_img.save(png_buffer, format="PNG", compress_level=0)
            png_base64 = base64.b64encode(png_buffer.getvalue()).decode("ascii")

            # Browser MJPEG frame
            jpeg_img = img.resize(
                (cfg["stream_width"], cfg["stream_height"]),
                Image.Resampling.BILINEAR,
            )
            jpeg_buffer = io.BytesIO()
            jpeg_img.save(
                jpeg_buffer, format="JPEG", quality=cfg["jpeg_quality"], optimize=False
            )
            jpeg_bytes = jpeg_buffer.getvalue()

            with frame_lock:
                latest_png_base64 = png_base64
                latest_jpeg = jpeg_bytes

        except Exception as e:
            log(f"[capture] error: {e}")
            time.sleep(0.5)
            continue

        interval = 1 / max(1, cfg["fps"])
        elapsed = time.perf_counter() - start
        remaining = interval - elapsed
        if remaining > 0:
            time.sleep(remaining)


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------

flask_app = Flask(__name__)


def is_authorized(req):
    """
    Returns True if this request carries a valid, previously-approved
    session token. There is no static secret to leak or replay here --
    every session token only exists because a human clicked "Allow" in
    the desktop app for that specific request.
    """
    token = req.headers.get(SESSION_HEADER, "").strip()
    if not token:
        return False
    with requests_lock:
        return token in approved_sessions


def forbidden():
    return Response("Forbidden: not an approved session", status=403)


def _expire_stale_requests():
    now = time.time()
    with requests_lock:
        for rid, entry in pending_requests.items():
            if entry["status"] == "pending" and now - entry["created"] > REQUEST_TIMEOUT_SECONDS:
                entry["status"] = "expired"


@flask_app.route("/request-access", methods=["POST"])
def request_access():
    """
    A viewer calls this first, with who they are. It does NOT grant
    access by itself -- it just queues a prompt for a human to approve
    or deny in the desktop app, and hands back a request_id to poll.
    """
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()[:100]
    user_id = str(data.get("user_id", "")).strip()[:50]

    if not username:
        return Response(
            json.dumps({"error": "username is required"}),
            status=400,
            mimetype="application/json",
        )

    _expire_stale_requests()

    cfg = SETTINGS.snapshot()
    allowed = cfg["allowed_username"].strip()

    request_id = secrets.token_hex(8)
    entry = {
        "username": username,
        "user_id": user_id,
        "status": "pending",
        "created": time.time(),
        "session_token": None,
    }

    if allowed and username.lower() != allowed.lower():
        # Doesn't match the configured allowlist name -- auto-deny quietly,
        # no prompt shown, no interruption for the app's owner.
        entry["status"] = "denied"
        with requests_lock:
            pending_requests[request_id] = entry
        log(f"[access] auto-denied request from '{username}' (doesn't match allowlist)")
    else:
        with requests_lock:
            pending_requests[request_id] = entry
        approval_queue.put(request_id)
        log(f"[access] new request from '{username}' (id={user_id}) -- waiting for approval")

    return Response(
        json.dumps({"request_id": request_id, "status": entry["status"]}),
        mimetype="application/json",
    )


@flask_app.route("/check-access")
def check_access():
    """Viewer polls this after calling /request-access, until approved/denied."""
    request_id = request.args.get("request_id", "")
    _expire_stale_requests()

    with requests_lock:
        entry = pending_requests.get(request_id)
        if not entry:
            return Response(json.dumps({"status": "unknown"}), status=404, mimetype="application/json")

        result = {"status": entry["status"]}
        if entry["status"] == "approved":
            result["session_token"] = entry["session_token"]

    return Response(json.dumps(result), mimetype="application/json")


@flask_app.route("/")
def index():
    if not is_authorized(request):
        return forbidden()
    return """
<!DOCTYPE html>
<html>
<head><title>Screen Share</title></head>
<body style="margin:0;background:black">
<img src="/stream" style="width:100%;height:auto">
</body>
</html>
"""


def generate_stream():
    while True:
        with frame_lock:
            frame = latest_jpeg
        if frame:
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
        cfg = SETTINGS.snapshot()
        time.sleep(1 / max(1, cfg["fps"]))


@flask_app.route("/stream")
def stream():
    if not is_authorized(request):
        return forbidden()
    return Response(
        generate_stream(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@flask_app.route("/snapshot")
def snapshot():
    if not is_authorized(request):
        return forbidden()

    with frame_lock:
        image = latest_png_base64

    if image is None:
        return Response("No frame yet", status=503)

    cfg = SETTINGS.snapshot()
    return Response(
        json.dumps(
            {
                "app": APP_NAME,
                "app_id": APP_ID,
                "image": image,
                "width": cfg["output_width"],
                "height": cfg["output_height"],
            }
        ),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


def run_flask(port):
    # use_reloader must stay False since this runs in a background thread
    flask_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# --------------------------------------------------------------------------
# Cloudflared tunnel management
# --------------------------------------------------------------------------

TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")


class TunnelManager:
    def __init__(self):
        self.process = None
        self.thread = None
        self.url = None
        self.on_url = None  # callback(url)
        self.on_log = None  # callback(line)

    def ensure_binary(self):
        if os.path.isfile(CLOUDFLARED_PATH):
            return True

        system = platform.system()
        url = CLOUDFLARED_DOWNLOAD_URLS.get(system)
        if not url:
            log(f"[tunnel] Unsupported OS for auto-download: {system}")
            return False

        log("[tunnel] cloudflared not found, downloading...")
        try:
            tmp_path = CLOUDFLARED_PATH + ".download"
            urllib.request.urlretrieve(url, tmp_path)

            if url.endswith(".tgz"):
                import tarfile
                with tarfile.open(tmp_path) as tf:
                    tf.extractall(APP_DIR)
                os.remove(tmp_path)
                # macOS tarball extracts as "cloudflared"
            else:
                os.replace(tmp_path, CLOUDFLARED_PATH)

            if platform.system() != "Windows":
                os.chmod(CLOUDFLARED_PATH, 0o755)

            log("[tunnel] cloudflared downloaded successfully.")
            return True
        except Exception as e:
            log(f"[tunnel] Failed to download cloudflared: {e}")
            return False

    def start(self, local_port):
        if self.process is not None:
            log("[tunnel] already running")
            return

        if not self.ensure_binary():
            return

        cmd = [CLOUDFLARED_PATH, "tunnel", "--url", f"http://localhost:{local_port}"]
        log(f"[tunnel] starting: {' '.join(cmd)}")

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def reader():
            for line in self.process.stdout:
                line = line.rstrip()
                if line:
                    log(f"[cloudflared] {line}")
                match = TUNNEL_URL_RE.search(line)
                if match and self.url is None:
                    self.url = match.group(0)
                    if self.on_url:
                        self.on_url(self.url)
            log("[tunnel] process ended")
            self.process = None
            self.url = None

        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()

    def stop(self):
        if self.process is not None:
            log("[tunnel] stopping...")
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None
            self.url = None


TUNNEL = TunnelManager()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Screen Share")
        self.geometry("480x620")
        self.resizable(False, False)

        self.stop_event = threading.Event()
        self.flask_thread = None

        self._build_ui()
        self._start_server()
        self.after(100, self._poll_log)
        self.after(100, self._poll_approval_queue)

    # ---------------- UI layout ----------------

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # ---- Capture settings ----
        mode_frame = ttk.LabelFrame(self, text="Capture Settings - Will be limited to 3 FPS in public servers due to roblox restrictions")
        mode_frame.pack(fill="x", **pad)

        self.mode_var = tk.StringVar(value="basic")
        mode_row = ttk.Frame(mode_frame)
        mode_row.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Radiobutton(
            mode_row, text="Basic", value="basic", variable=self.mode_var,
            command=self._on_mode_change,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            mode_row, text="Advanced", value="advanced", variable=self.mode_var,
            command=self._on_mode_change,
        ).pack(side="left")

        # ---- Presets ----
        self.PRESETS = {
            "low": dict(fps=1, output_width=48, output_height=36,
                        stream_width=48, stream_height=36),
            "medium": dict(fps=3, output_width=240, output_height=180,
                           stream_width=240, stream_height=180),
            "high": dict(fps=6, output_width=480, output_height=360,
                         stream_width=480, stream_height=360),
        }

        self.basic_frame = ttk.Frame(mode_frame)
        self.basic_frame.pack(fill="x", padx=8, pady=8)

        self.preset_var = tk.StringVar(value="high")
        ttk.Radiobutton(
            self.basic_frame, text="Low (1 fps, 48x36)", value="low",
            variable=self.preset_var, command=self._apply_preset,
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            self.basic_frame, text="Medium (2 fps, 240x180)", value="medium",
            variable=self.preset_var, command=self._apply_preset,
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            self.basic_frame, text="High (6 fps, 480x360)", value="high",
            variable=self.preset_var, command=self._apply_preset,
        ).pack(anchor="w", pady=2)

        # ---- Advanced (manual) settings ----
        # Independent width/height, capped at 1440x1080.
        self.advanced_frame = ttk.Frame(mode_frame)

        MAX_WIDTH = 1440
        MAX_HEIGHT = 1080
        MIN_SIZE = 1

        self.fps_var = tk.IntVar(value=SETTINGS.fps)
        self.width_var = tk.IntVar(value=min(SETTINGS.output_width, MAX_WIDTH))
        self.height_var = tk.IntVar(value=min(SETTINGS.output_height, MAX_HEIGHT))

        ttk.Label(self.advanced_frame, text="Capture FPS").grid(
            row=0, column=0, sticky="w", padx=8, pady=4
        )
        ttk.Spinbox(self.advanced_frame, from_=1, to=60, textvariable=self.fps_var, width=10).grid(
            row=0, column=1, sticky="w", padx=8, pady=4
        )

        ttk.Label(self.advanced_frame, text=f"Width (max {MAX_WIDTH}px)").grid(
            row=1, column=0, sticky="w", padx=8, pady=4
        )
        width_box = ttk.Spinbox(
            self.advanced_frame, from_=MIN_SIZE, to=MAX_WIDTH, textvariable=self.width_var, width=10
        )
        width_box.grid(row=1, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(self.advanced_frame, text=f"Height (max {MAX_HEIGHT}px)").grid(
            row=2, column=0, sticky="w", padx=8, pady=4
        )
        height_box = ttk.Spinbox(
            self.advanced_frame, from_=MIN_SIZE, to=MAX_HEIGHT, textvariable=self.height_var, width=10
        )
        height_box.grid(row=2, column=1, sticky="w", padx=8, pady=4)

        def _clamp_on_focus_out(widget, var, lo, hi):
            def _on_focus_out(_event):
                try:
                    current = var.get()
                except tk.TclError:
                    # Non-numeric/empty when focus left -- fall back to the
                    # minimum rather than leaving it broken.
                    var.set(lo)
                    return
                clamped = max(lo, min(current, hi))
                if clamped != current:
                    var.set(clamped)
            widget.bind("<FocusOut>", _on_focus_out)

        # Clamping only happens once you tab/click away from the box, so you
        # can freely type something like "240" (or even briefly "2401") while
        # editing without it snapping back mid-keystroke.
        _clamp_on_focus_out(width_box, self.width_var, MIN_SIZE, MAX_WIDTH)
        _clamp_on_focus_out(height_box, self.height_var, MIN_SIZE, MAX_HEIGHT)

        ttk.Button(self.advanced_frame, text="Apply Settings", command=self._apply_settings).grid(
            row=3, column=0, columnspan=2, pady=8
        )

        # Start on Basic mode with the "high" preset applied (matches old defaults)
        self._apply_preset()
        self._on_mode_change()

        # ---- URLs ----
        url_frame = ttk.LabelFrame(self, text="Server")
        url_frame.pack(fill="x", **pad)

        self.app_id_var = tk.StringVar(value=APP_ID)
        ttk.Label(url_frame, text="App ID (paste into your script):").grid(
            row=0, column=0, sticky="w", padx=8, pady=4
        )
        ttk.Entry(url_frame, textvariable=self.app_id_var, width=40, state="readonly").grid(
            row=0, column=1, padx=8, pady=4
        )
        ttk.Button(url_frame, text="Copy", command=lambda: self._copy(self.app_id_var.get())).grid(
            row=0, column=2, padx=4
        )

        self.local_url_var = tk.StringVar(value="starting...")
        ttk.Label(url_frame, text="Local browser URL:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(url_frame, textvariable=self.local_url_var, width=40, state="readonly").grid(
            row=1, column=1, padx=8, pady=4
        )
        ttk.Button(url_frame, text="Open", command=self._open_local).grid(row=1, column=2, padx=4)

        self.snapshot_url_var = tk.StringVar(value="starting...")
        ttk.Label(url_frame, text="Local snapshot URL:").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(url_frame, textvariable=self.snapshot_url_var, width=40, state="readonly").grid(
            row=2, column=1, padx=8, pady=4
        )
        ttk.Button(url_frame, text="Copy", command=lambda: self._copy(self.snapshot_url_var.get())).grid(
            row=2, column=2, padx=4
        )

        ttk.Separator(url_frame, orient="horizontal").grid(
            row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=6
        )

        self.username_var = tk.StringVar(value="")
        ttk.Label(
            url_frame,
            text="Only prompt me for this Roblox username\n(optional -- blank = prompt for anyone who asks):",
        ).grid(row=4, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(url_frame, textvariable=self.username_var, width=25).grid(
            row=4, column=1, sticky="w", padx=8, pady=4
        )
        ttk.Button(url_frame, text="Set", command=self._apply_username_restriction).grid(
            row=4, column=2, padx=4
        )

        ttk.Button(url_frame, text="Revoke All Active Access", command=self._revoke_all_access).grid(
            row=5, column=0, columnspan=3, pady=(6, 4)
        )

        # ---- Tunnel ----
        tunnel_frame = ttk.LabelFrame(self, text="Public Tunnel (Cloudflare)")
        tunnel_frame.pack(fill="x", **pad)

        btn_row = ttk.Frame(tunnel_frame)
        btn_row.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn_row, text="Start Tunnel", command=self._start_tunnel).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Stop Tunnel", command=self._stop_tunnel).pack(side="left", padx=4)

        self.public_url_var = tk.StringVar(value="(not started)")
        ttk.Label(tunnel_frame, text="Public URL:").pack(anchor="w", padx=8)
        entry_row = ttk.Frame(tunnel_frame)
        entry_row.pack(fill="x", padx=8, pady=4)
        ttk.Entry(entry_row, textvariable=self.public_url_var, width=40, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(entry_row, text="Copy", command=lambda: self._copy(self.public_url_var.get())).pack(
            side="left", padx=4
        )

        # ---- Log ----
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=12, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

        TUNNEL.on_url = self._on_tunnel_url

    # ---------------- actions ----------------

    def _on_mode_change(self):
        if self.mode_var.get() == "basic":
            self.advanced_frame.pack_forget()
            self.basic_frame.pack(fill="x", padx=8, pady=8)
            self._apply_preset()
        else:
            self.basic_frame.pack_forget()
            self.advanced_frame.pack(fill="x", padx=8, pady=8)
            # reflect whatever is currently active (e.g. last preset) into the
            # manual fields so Advanced starts from the current state
            cfg = SETTINGS.snapshot()
            self.fps_var.set(cfg["fps"])
            self.width_var.set(min(cfg["output_width"], 1440))
            self.height_var.set(min(cfg["output_height"], 1080))

    def _apply_preset(self):
        preset = self.PRESETS[self.preset_var.get()]
        SETTINGS.update(**preset)
        log(f"[settings] preset '{self.preset_var.get()}' applied: " + json.dumps(preset))

    def _apply_settings(self):
        try:
            fps = int(self.fps_var.get())
            width = max(1, min(int(self.width_var.get()), 1440))
            height = max(1, min(int(self.height_var.get()), 1080))
            SETTINGS.update(
                fps=fps,
                output_width=width,
                output_height=height,
                stream_width=width,
                stream_height=height,
            )
            log("[settings] applied: " + json.dumps(SETTINGS.snapshot()))
        except Exception as e:
            messagebox.showerror("Invalid settings", str(e))

    def _start_server(self):
        port = SETTINGS.snapshot()["port"]

        self.flask_thread = threading.Thread(target=run_flask, args=(port,), daemon=True)
        self.flask_thread.start()

        threading.Thread(target=capture_loop, args=(self.stop_event,), daemon=True).start()

        self.base_local_url = f"http://127.0.0.1:{port}/"
        self.base_snapshot_url = f"http://127.0.0.1:{port}/snapshot"
        self._refresh_url_displays()
        log(f"[server] running on port {port}")
        log(f"[server] App ID: {APP_ID}  (shown for reference; access is now controlled via approval prompts, not this ID)")

    def _refresh_url_displays(self):
        self.local_url_var.set(self.base_local_url)
        self.snapshot_url_var.set(self.base_snapshot_url)
        if TUNNEL.url:
            self.public_url_var.set(TUNNEL.url)

    def _apply_username_restriction(self):
        username = self.username_var.get().strip()
        SETTINGS.update(allowed_username=username)
        if username:
            log(f"[security] will only prompt for approval requests from: {username} "
                f"(others are auto-denied without a popup)")
        else:
            log("[security] no username pre-filter -- you'll be prompted for anyone who requests access")

    def _revoke_all_access(self):
        with requests_lock:
            count = len(approved_sessions)
            approved_sessions.clear()
        log(f"[security] revoked {count} active session(s) -- everyone must request access again")
        messagebox.showinfo("Access revoked", f"Revoked {count} active session(s).")

    def _open_local(self):
        import webbrowser
        webbrowser.open(self.local_url_var.get())

    def _start_tunnel(self):
        port = SETTINGS.snapshot()["port"]
        self.public_url_var.set("starting...")
        threading.Thread(target=TUNNEL.start, args=(port,), daemon=True).start()

    def _stop_tunnel(self):
        TUNNEL.stop()
        self.public_url_var.set("(stopped)")

    def _on_tunnel_url(self, url):
        # called from the tunnel's reader thread -> hop to GUI thread
        self.after(0, lambda: self.public_url_var.set(url))
        self.after(0, lambda: log(f"[tunnel] public URL ready: {url}"))

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _poll_log(self):
        try:
            while True:
                line = log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        finally:
            self.after(150, self._poll_log)

    def _poll_approval_queue(self):
        try:
            while True:
                request_id = approval_queue.get_nowait()
                try:
                    self._show_approval_prompt(request_id)
                except Exception as e:
                    # Never let one bad request silently kill the polling
                    # loop -- log it so it's visible even in --windowed
                    # builds with no console.
                    log(f"[error] approval prompt failed for request {request_id}: {e}")
        except queue.Empty:
            pass
        finally:
            # This must always run, no matter what happened above, or the
            # app stops checking for new access requests permanently.
            self.after(400, self._poll_approval_queue)

    def _show_approval_prompt(self, request_id):
        with requests_lock:
            entry = pending_requests.get(request_id)
        if entry is None or entry["status"] != "pending":
            return  # expired or already handled

        username = entry["username"]
        user_id = entry["user_id"] or "unknown"
        allow = messagebox.askyesno(
            "Access request",
            f"'{username}' (ID: {user_id}) wants to view your screen.\n\nAllow?",
        )

        with requests_lock:
            entry = pending_requests.get(request_id)
            if entry is None or entry["status"] != "pending":
                return  # expired while the dialog was open
            if allow:
                token = secrets.token_hex(16)
                entry["status"] = "approved"
                entry["session_token"] = token
                approved_sessions[token] = {
                    "username": username,
                    "user_id": user_id,
                    "approved_at": time.time(),
                }
            else:
                entry["status"] = "denied"

        log(f"[security] {'approved' if allow else 'denied'} access for '{username}' (ID: {user_id})")




if __name__ == "__main__":
    app = App()
    app.mainloop()
