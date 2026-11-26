#!/usr/bin/env python3
"""Hardened, read-only 2-way Telegram monitoring bot for vickyhome."""

import base64
from collections import Counter, deque
import datetime
import glob
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import cv2
    import numpy as np
    cv2.setNumThreads(2)
except Exception:
    cv2 = None
    np = None

# Prefer IPv4 for outbound HTTPS because tailscale0 advertises an IPv6 address while ISP IPv6 is unreachable.
_ORIG_GETADDRINFO = socket.getaddrinfo


def _ipv4_first_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    res = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 if v4 else res


socket.getaddrinfo = _ipv4_first_getaddrinfo

CONF_PATH = "/etc/vickyhome-tgbot.conf"
OFFSET_PATH = "/var/lib/vickyhome-tgbot/offset"
LOCAL_WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=13.0827&longitude=80.2707"
    "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,sunrise,sunset,daylight_duration"
    "&timezone=Asia%2FKolkata&forecast_days=1"
)
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Light rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Light snow",
    80: "Light rain showers",
    81: "Moderate rain showers",
    82: "Heavy rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Severe thunderstorm",
}

MONITORED_SERVICES = [
    ("AdGuardHome", "AdGuardHome.service"),
    ("Tailscale", "tailscaled.service"),
    ("Copyparty", "copyparty.service"),
    ("FileBrowser", "filebrowser.service"),
    ("Samba SMB", "smbd.service"),
    ("Samba NMB", "nmbd.service"),
    ("SSH Server", "ssh.service"),
    ("Auth Alerts", "vickyhome-auth-alert.service"),
    ("Telegram Bot", "vickyhome-telegram-bot.service"),
]

BOT_COMMANDS = [
    {"command": "ai", "description": "Ask Gemini AI (or just send plain text)"},
    {"command": "security_on", "description": "Arm USB webcam motion & person security"},
    {"command": "security_off", "description": "Disarm USB webcam security monitoring"},
    {"command": "security_status", "description": "Check security camera status & recent log"},
    {"command": "snapshot", "description": "Instant photo from security USB webcam"},
    {"command": "snap", "description": "Take a live photo from built-in camera"},
    {"command": "flights", "description": "Live planes within 10 km of Chennai"},
    {"command": "recap", "description": "Today's 10 km Chennai flight recap"},
    {"command": "sky", "description": "Local sky, ISS overhead & ISRO Sriharikota"},
    {"command": "briefing", "description": "Local weather, health & AdGuard summary"},
    {"command": "status", "description": "Full server health summary"},
    {"command": "temp", "description": "CPU & board temperature"},
    {"command": "ram", "description": "Memory, swap & top processes"},
    {"command": "disk", "description": "Disk & USB storage usage"},
    {"command": "services", "description": "Running & failed services"},
    {"command": "adguard", "description": "AdGuard Home ad-blocking stats"},
    {"command": "top", "description": "Top CPU processes"},
    {"command": "clear", "description": "Clear AI conversation memory"},
    {"command": "help", "description": "Show available commands"},
]


def load_conf():
    cfg = {}
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def load_offset():
    try:
        with open(OFFSET_PATH, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def save_offset(offset):
    try:
        with open(OFFSET_PATH, "w", encoding="utf-8") as f:
            f.write(str(offset))
    except Exception as e:
        print(f"[offset write error] {e}", file=sys.stderr, flush=True)


def run_cmd(args, timeout=8):
    try:
        res = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


_ERR_LOG_LOCK = threading.Lock()
_ERR_LOG_STATE = {}


def _log_throttled(key, msg, min_interval=180):
    now = time.monotonic()
    with _ERR_LOG_LOCK:
        prev_ts, skipped = _ERR_LOG_STATE.get(key, (0.0, 0))
        if now - prev_ts < min_interval:
            _ERR_LOG_STATE[key] = (prev_ts, skipped + 1)
            return
        _ERR_LOG_STATE[key] = (now, 0)
    suffix = f" (suppressed {skipped} similar)" if skipped else ""
    print(f"{msg}{suffix}", file=sys.stderr, flush=True)


def _is_retryable_exc(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429, 500, 502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, OSError))


def tg_api(token, method, data=None, timeout=35):
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = None
    headers = {"Connection": "close"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(token, chat_id, text):
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        last_err = None
        for attempt in range(3):
            try:
                tg_api(token, "sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=12)
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < 2 and _is_retryable_exc(e):
                    time.sleep(2 * (attempt + 1))
                else:
                    break
        if last_err is not None:
            print(f"[sendMessage error] {last_err}", file=sys.stderr, flush=True)


def read_uptime_load():
    uptime_str = "unknown"
    _, out, _ = run_cmd(["uptime", "-p"])
    if out:
        uptime_str = out.replace("up ", "")
    load_str = "?"
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            parts = f.read().split()
            load_str = f"{parts[0]} / {parts[1]} / {parts[2]}"
    except Exception:
        pass
    return uptime_str, load_str


def read_temps():
    readings = []
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name_path = os.path.join(hw, "name")
        name = os.path.basename(hw)
        if os.path.exists(name_path):
            try:
                with open(name_path, "r", encoding="utf-8") as f:
                    name = f.read().strip()
            except Exception:
                pass
        for tp in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            try:
                with open(tp, "r", encoding="utf-8") as f:
                    val_c = int(f.read().strip()) / 1000.0
                readings.append((name, val_c))
            except Exception:
                pass
    if not readings:
        for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            tp = os.path.join(tz, "temp")
            if os.path.exists(tp):
                try:
                    with open(tp, "r", encoding="utf-8") as f:
                        val_c = int(f.read().strip()) / 1000.0
                    readings.append((os.path.basename(tz), val_c))
                except Exception:
                    pass
    return readings


def read_memory():
    _, out, _ = run_cmd(["free", "-m"])
    mem_line = {}
    swap_line = {}
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == "Mem:":
            mem_line = {
                "total": int(parts[1]),
                "used": int(parts[2]),
                "free": int(parts[3]),
                "avail": int(parts[6]) if len(parts) > 6 else int(parts[3]),
            }
        elif parts and parts[0] == "Swap:":
            swap_line = {
                "total": int(parts[1]),
                "used": int(parts[2]),
                "free": int(parts[3]),
            }
    return mem_line, swap_line


def service_state(unit):
    _, out, _ = run_cmd(["systemctl", "is-active", unit])
    return out or "unknown"


def build_multipart_form(fields, file_field, filename, content_type, data_bytes):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(data_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _send_media(
    token,
    endpoint,
    file_field,
    filename,
    content_type,
    data_bytes,
    chat_id,
    caption="",
    extra_fields=None,
    timeout=30,
):
    url = f"https://api.telegram.org/bot{token}/{endpoint}"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    if extra_fields:
        for k, v in extra_fields.items():
            fields[k] = str(v)
    body, ctype = build_multipart_form(fields, file_field, filename, content_type, data_bytes)
    last_err = None
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": ctype, "Connection": "close"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < 2 and _is_retryable_exc(e):
                time.sleep(2 + attempt * 3)
            else:
                raise
    raise last_err


def send_photo(token, chat_id, jpg_bytes, caption="", timeout=25):
    return _send_media(
        token,
        "sendPhoto",
        "photo",
        "snap.jpg",
        "image/jpeg",
        jpg_bytes,
        chat_id,
        caption=caption,
        timeout=timeout,
    )


def send_video(token, chat_id, mp4_bytes, caption="", duration=8, width=640, height=480, timeout=60):
    return _send_media(
        token,
        "sendVideo",
        "video",
        "security_clip.mp4",
        "video/mp4",
        mp4_bytes,
        chat_id,
        caption=caption,
        extra_fields={
            "supports_streaming": "true",
            "duration": max(1, int(round(duration))),
            "width": int(width),
            "height": int(height),
        },
        timeout=timeout,
    )


DEFAULT_SNAP_DEVICE = "/dev/v4l/by-id/usb-Image_Processor_Lenovo_EasyCamera-video-index0"
DEFAULT_SECURITY_DEVICE = "/dev/v4l/by-id/usb-Generic_HD_camera-video-index0"
SECURITY_STATE_PATH = "/var/lib/vickyhome-tgbot/security_state.json"
SECURITY_LOG_PATH = "/var/lib/vickyhome-tgbot/security_events.log"
SECURITY_WORK_DIR = "/var/lib/vickyhome-tgbot"
SECURITY_STATE_LOCK = threading.Lock()
SNAP_LOCK = threading.Lock()


def resolve_v4l_device(preferred_path, fallback_path="/dev/video0"):
    """Resolve persistent /dev/v4l/by-id path or fallback node to (realpath, v4l_index)."""
    for candidate in (preferred_path, fallback_path):
        if candidate and os.path.exists(candidate):
            real = os.path.realpath(candidate)
            base = os.path.basename(real)
            idx = None
            if base.startswith("video") and base[5:].isdigit():
                idx = int(base[5:])
            return real, idx
    return None, None


def capture_snapshot(device=None, fallback_device="/dev/video2", resolution="1024x768", skip_frames=10):
    if not SNAP_LOCK.acquire(blocking=False):
        return None, "Camera is already busy capturing a photo. Try again in a few seconds."
    try:
        resolved, _ = resolve_v4l_device(device or DEFAULT_SNAP_DEVICE, fallback_device)
        if not resolved:
            return None, f"Camera device {device or DEFAULT_SNAP_DEVICE} is not connected."
        with tempfile.TemporaryDirectory(prefix="vickysnap_") as tmpdir:
            out_path = os.path.join(tmpdir, "snap.jpg")
            res = subprocess.run(
                [
                    "fswebcam",
                    "-d",
                    resolved,
                    "-r",
                    resolution,
                    "-S",
                    str(skip_frames),
                    "--no-banner",
                    "--jpeg",
                    "85",
                    out_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=18,
                shell=False,
            )
            if res.returncode != 0 or not os.path.exists(out_path):
                err = (res.stderr or res.stdout or "capture failed").strip().splitlines()[-1:] or ["capture failed"]
                return None, f"Camera capture failed ({err[0]})."
            with open(out_path, "rb") as f:
                data = f.read()
            if len(data) < 1024 or not data.startswith(b"\xff\xd8"):
                return None, "Camera returned an invalid or empty image."
            return data, None
    except subprocess.TimeoutExpired:
        return None, "Camera capture timed out."
    except Exception as e:
        return None, f"Camera error ({e})."
    finally:
        SNAP_LOCK.release()


class _FfmpegV4L2Stream:
    """Reliable BGR24 V4L2 frame stream via ffmpeg (avoids OpenCV 3.2 UVC probe errors)."""

    def __init__(self, device, width=640, height=480, fps=None, max_frames=None):
        self.device = device
        self.width = int(width)
        self.height = int(height)
        self.frame_bytes = self.width * self.height * 3
        self.closed = False
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "yuyv422",
            "-video_size", f"{self.width}x{self.height}",
            "-i", device,
        ]
        if fps is not None:
            cmd.extend(["-vf", f"fps={float(fps):.2f}"])
        if max_frames is not None:
            cmd.extend(["-frames:v", str(int(max_frames))])
        cmd.extend(["-f", "rawvideo", "-pix_fmt", "bgr24", "-"])
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=self.frame_bytes * 2,
            shell=False,
        )

    def read(self):
        if self.closed or self.proc is None or self.proc.stdout is None:
            return False, None
        need = self.frame_bytes
        chunks = []
        got = 0
        try:
            while got < need and not self.closed:
                part = self.proc.stdout.read(need - got)
                if not part:
                    return False, None
                chunks.append(part)
                got += len(part)
        except Exception:
            return False, None
        if got != need:
            return False, None
        buf = b"".join(chunks) if len(chunks) > 1 else chunks[0]
        frame = np.frombuffer(buf, dtype=np.uint8).reshape((self.height, self.width, 3)).copy()
        return True, frame

    def close(self, read_err=False):
        if self.closed:
            return ""
        self.closed = True
        p = self.proc
        if p is None:
            return ""
        if p.stdout is not None:
            try:
                p.stdout.close()
            except Exception:
                pass
        try:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=2.0)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=1.0)
            except Exception:
                pass
        err_msg = ""
        if read_err and p.stderr is not None:
            try:
                err_msg = (p.stderr.read() or b"").decode("utf-8", "ignore").strip()[-220:]
            except Exception:
                pass
        if p.stderr is not None:
            try:
                p.stderr.close()
            except Exception:
                pass
        return err_msg


def _open_security_stream(device, width=640, height=480, fps=None, max_frames=None, retries=2):
    """Open V4L2 camera stream via ffmpeg with retry for USB webcam wake glitches."""
    last_err = "no frames received"
    for attempt in range(max(1, retries)):
        stream = _FfmpegV4L2Stream(device, width=width, height=height, fps=fps, max_frames=max_frames)
        ok, first_fr = stream.read()
        if ok and first_fr is not None:
            return stream, first_fr, None
        err = stream.close(read_err=True)
        if err:
            last_err = err
        if attempt + 1 < retries:
            time.sleep(0.45)
    return None, None, last_err


def _stamp_security_frame(frame, label_text, bar_color=(24, 34, 52), boxes=None):
    out = frame.copy()
    h, w = out.shape[:2]
    if boxes:
        for x, y, bw, bh, tag, col in boxes:
            x1, y1 = max(0, int(x)), max(30, int(y))
            x2, y2 = min(w - 1, int(x + bw)), min(h - 1, int(y + bh))
            cv2.rectangle(out, (x1, y1), (x2, y2), col, 2)
            if tag:
                cv2.rectangle(out, (x1, max(28, y1 - 18)), (min(w - 1, x1 + max(72, len(tag) * 9)), y1), col, -1)
                cv2.putText(out, tag, (x1 + 4, max(41, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (15, 23, 42), 1, cv2.LINE_AA)
    cv2.rectangle(out, (0, 0), (w, 28), bar_color, -1)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    txt = f"{stamp} | {label_text}"
    cv2.putText(out, txt, (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (248, 250, 252), 1, cv2.LINE_AA)
    return out


class _ClipRecorder:
    """Streams raw BGR frames directly to ffmpeg H.264 MP4 without buffering large arrays."""

    def __init__(self, width=640, height=480, fps=6.0):
        parent = SECURITY_WORK_DIR if os.path.isdir(SECURITY_WORK_DIR) else None
        self.tmpdir = tempfile.mkdtemp(prefix="secclip_", dir=parent)
        self.mp4_path = os.path.join(self.tmpdir, "clip.mp4")
        self.fps = float(fps)
        self.width = int(width)
        self.height = int(height)
        self.frame_count = 0
        self.closed = False
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}", "-r", f"{self.fps:.2f}",
            "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", "-crf", "28",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", self.mp4_path,
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)

    def write_frame(self, frame):
        if self.closed or not self.proc or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write(frame.tobytes())
            self.frame_count += 1
            return True
        except Exception:
            return False

    def finish(self):
        if self.closed:
            return None, "clip already finalized"
        self.closed = True
        try:
            if self.proc.stdin:
                try:
                    self.proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                self.proc.stdin = None
            _, err = self.proc.communicate(timeout=25)
            if self.proc.returncode != 0 or not os.path.exists(self.mp4_path):
                err_msg = (err or b"").decode("utf-8", "ignore").strip() or f"ffmpeg rc={self.proc.returncode}"
                return None, err_msg
            with open(self.mp4_path, "rb") as f:
                data = f.read()
            if len(data) < 512:
                return None, "empty video clip"
            return data, None
        except Exception as e:
            try:
                self.proc.kill()
            except Exception:
                pass
            return None, str(e)
        finally:
            try:
                if os.path.exists(self.mp4_path):
                    os.remove(self.mp4_path)
                os.rmdir(self.tmpdir)
            except Exception:
                pass


def _set_security_armed_flag(armed):
    with SECURITY_STATE_LOCK:
        st = _atomic_json_read(SECURITY_STATE_PATH, {})
        st["armed"] = bool(armed)
        try:
            _atomic_json_write(SECURITY_STATE_PATH, st, prefix="secst_", mode=0o644)
        except Exception as e:
            print(f"[security armed flag error] {e}", file=sys.stderr, flush=True)


def _append_security_event(event_type, detail_str, snapshot_sent=False, clip_sec=0.0):
    now_dt = datetime.datetime.now()
    ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"{ts_str} | {event_type} | {detail_str}\n"
    with SECURITY_STATE_LOCK:
        try:
            os.makedirs(SECURITY_WORK_DIR, exist_ok=True)
            lines = []
            if os.path.exists(SECURITY_LOG_PATH):
                with open(SECURITY_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            lines.append(log_line)
            if len(lines) > 1500:
                lines = lines[-1000:]
            fd, tmp_p = tempfile.mkstemp(prefix="seclog_", suffix=".log", dir=SECURITY_WORK_DIR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.chmod(tmp_p, 0o600)
            os.replace(tmp_p, SECURITY_LOG_PATH)
        except Exception as e:
            print(f"[security log error] {e}", file=sys.stderr, flush=True)

        st = _atomic_json_read(SECURITY_STATE_PATH, {})
        st["total_events"] = int(st.get("total_events", 0)) + 1
        if event_type == "PERSON":
            st["person_events"] = int(st.get("person_events", 0)) + 1
        else:
            st["motion_events"] = int(st.get("motion_events", 0)) + 1
        entry = {
            "ts": ts_str,
            "type": event_type,
            "detail": detail_str,
            "clip_sec": round(clip_sec, 1),
        }
        st["last_event"] = entry
        recent = list(st.get("recent_events") or [])
        recent.append(entry)
        st["recent_events"] = recent[-10:]
        try:
            _atomic_json_write(SECURITY_STATE_PATH, st, prefix="secst_", mode=0o644)
        except Exception as e:
            print(f"[security state write error] {e}", file=sys.stderr, flush=True)


class SecurityMonitor:
    """On-demand OpenCV motion/person detector for the secondary USB webcam."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._cam_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread = None
        self._stream = None
        self._armed = False
        self._started_at = 0.0
        self._session_events = 0
        self._latest_frame = None
        self._latest_ts = 0.0
        self._last_alert_ts = 0.0
        self._resolved_dev = ""
        self._cooldown_sec = 45
        self._clip_sec = 8

    def is_armed(self):
        with self._lock:
            return bool(self._armed and self._thread and self._thread.is_alive())

    def _safe_send(self, token, chat_id, send_fn, *args, **kwargs):
        try:
            send_fn(token, chat_id, *args, **kwargs)
        except Exception as e:
            print(f"[security telegram error] {e}", file=sys.stderr, flush=True)

    def arm(self, cfg, token, chat_id):
        if cv2 is None or np is None:
            return False, "OpenCV (cv2) is not available on this server."
        pref = cfg.get("SECURITY_DEVICE", DEFAULT_SECURITY_DEVICE)
        resolved, _ = resolve_v4l_device(pref, "/dev/video0")
        if not resolved:
            return False, f"Security webcam ({pref}) is not connected."

        with self._lock:
            if self._armed and self._thread and self._thread.is_alive():
                up_min = max(0, int((time.time() - self._started_at) // 60))
                return True, (
                    f"Security mode is already ARMED ({resolved}).\n"
                    f"Active: {up_min}m | Session alerts: {self._session_events}"
                )
            self._stop_evt.clear()
            self._armed = True
            self._started_at = time.time()
            self._session_events = 0
            self._last_alert_ts = 0.0
            self._resolved_dev = resolved
            self._cooldown_sec = max(15, int(cfg.get("SECURITY_COOLDOWN", 45) or 45))
            self._clip_sec = max(5, min(15, int(cfg.get("SECURITY_CLIP_SECONDS", 8) or 8)))
            ready_evt = threading.Event()
            start_status = {"ok": False, "err": "Camera startup timed out."}
            self._thread = threading.Thread(
                target=self._monitor_loop,
                args=(cfg, token, chat_id, resolved, ready_evt, start_status),
                name="security-monitor",
                daemon=True,
            )
            self._thread.start()

        ready_evt.wait(timeout=8.0)
        if not start_status["ok"]:
            self.disarm()
            return False, f"Failed to start security camera ({start_status['err']})."

        _set_security_armed_flag(True)
        return True, (
            "Security Mode ENABLED\n\n"
            f"- Camera: USB HD Webcam ({resolved}, 640x480)\n"
            "- Detection: OpenCV Motion + HOG Person\n"
            f"- Alert Output: Instant JPEG + {self._clip_sec}s MP4 clip\n"
            f"- Cooldown: {self._cooldown_sec}s between alerts\n"
            "Send /security_off when you return home."
        )

    def disarm(self):
        with self._lock:
            was_armed = self._armed or bool(self._thread and self._thread.is_alive())
            thr = self._thread
            stream = self._stream
            started_at = self._started_at
            events = self._session_events
            self._armed = False
            self._stop_evt.set()
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        if thr and thr.is_alive() and thr is not threading.current_thread():
            thr.join(timeout=8.0)
        with self._lock:
            self._thread = None
            self._stream = None
        with self._frame_lock:
            self._latest_frame = None
            self._latest_ts = 0.0
        _set_security_armed_flag(False)
        if not was_armed:
            return "Security mode is already OFF (webcam inactive)."
        dur_s = max(0, int(time.time() - started_at)) if started_at else 0
        dur_txt = f"{dur_s // 3600}h {(dur_s % 3600) // 60}m" if dur_s >= 3600 else f"{dur_s // 60}m {dur_s % 60}s"
        return (
            "Security Mode DISABLED\n\n"
            f"- Session duration: {dur_txt}\n"
            f"- Alerts triggered: {events}\n"
            "- Webcam released cleanly (0% idle CPU)."
        )

    def capture_instant_snapshot(self, cfg):
        if cv2 is None or np is None:
            pref = cfg.get("SECURITY_DEVICE", DEFAULT_SECURITY_DEVICE)
            return capture_snapshot(device=pref, fallback_device="/dev/video0", resolution="640x480", skip_frames=8)

        if self.is_armed():
            deadline = time.time() + 1.5
            while time.time() < deadline:
                with self._frame_lock:
                    fr = self._latest_frame.copy() if (self._latest_frame is not None and (time.time() - self._latest_ts) < 5.0) else None
                if fr is not None:
                    stamped = _stamp_security_frame(fr, "LIVE SNAPSHOT [ARMED]", bar_color=(20, 48, 36))
                    ok_enc, buf = cv2.imencode(".jpg", stamped, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok_enc:
                        return buf.tobytes(), None
                    break
                time.sleep(0.15)
            return None, "Security camera is warming up. Try /snapshot again in a moment."

        if not self._cam_lock.acquire(blocking=False):
            return None, "Security camera is busy right now. Try again in a few seconds."
        stream = None
        try:
            pref = cfg.get("SECURITY_DEVICE", DEFAULT_SECURITY_DEVICE)
            resolved, _ = resolve_v4l_device(pref, "/dev/video0")
            if not resolved:
                return None, f"Security webcam ({pref}) is not connected."
            stream, fr, err = _open_security_stream(resolved, width=640, height=480, fps=None, max_frames=8, retries=2)
            if stream is None or fr is None:
                return None, f"Could not open security webcam ({resolved}: {err})."
            for _ in range(7):
                ok_r, tmp_fr = stream.read()
                if not ok_r or tmp_fr is None:
                    break
                fr = tmp_fr
            stamped = _stamp_security_frame(fr, "SECURITY SNAPSHOT [ON-DEMAND]", bar_color=(15, 23, 42))
            ok_enc, buf = cv2.imencode(".jpg", stamped, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok_enc:
                return None, "Failed to encode snapshot JPEG."
            return buf.tobytes(), None
        except Exception as e:
            return None, f"Security snapshot error ({e})."
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            self._cam_lock.release()

    def status_text(self, cfg):
        pref = cfg.get("SECURITY_DEVICE", DEFAULT_SECURITY_DEVICE)
        resolved, _ = resolve_v4l_device(pref, "/dev/video0")
        dev_str = resolved or f"DISCONNECTED ({pref})"
        armed = self.is_armed()
        with SECURITY_STATE_LOCK:
            st = _atomic_json_read(SECURITY_STATE_PATH, {})
        lines = [
            "vickyhome Security Camera Status",
            "",
            f"Mode: {'ARMED (Monitoring Active)' if armed else 'INACTIVE (Default Standby)'}",
            f"Security Cam: External USB HD Camera ({dev_str})",
            "Photo Cam (/snap): Built-in Lenovo EasyCamera",
        ]
        if armed:
            up_s = max(0, int(time.time() - self._started_at))
            up_txt = f"{up_s // 3600}h {(up_s % 3600) // 60}m" if up_s >= 3600 else f"{up_s // 60}m {up_s % 60}s"
            lines.append(f"Session: Active {up_txt} | Alerts this session: {self._session_events}")
        lines.append(
            f"Settings: Cooldown {max(15, int(cfg.get('SECURITY_COOLDOWN', 45) or 45))}s | "
            f"Clip ~{max(5, min(15, int(cfg.get('SECURITY_CLIP_SECONDS', 8) or 8)))}s MP4 + JPEG"
        )
        lines.append(
            f"Total Logged Events: {st.get('total_events', 0)} "
            f"(Person: {st.get('person_events', 0)}, Motion: {st.get('motion_events', 0)})"
        )
        last_ev = st.get("last_event")
        if last_ev:
            lines.append(f"Last Event: {last_ev.get('ts', '?')} [{last_ev.get('type', '?')}] - {last_ev.get('detail', '')}")
        else:
            lines.append("Last Event: None recorded yet")
        recent = (st.get("recent_events") or [])[-3:]
        if recent:
            lines.append("")
            lines.append("Recent Detections:")
            for ev in reversed(recent):
                lines.append(f"- {ev.get('ts', '?')} | {ev.get('type', '?')} | {ev.get('detail', '')}")
        lines.append("")
        lines.append("Commands: /security_on | /security_off | /snapshot | /snap")
        return "\n".join(lines)

    def _finalize_and_send_clip(self, recorder, token, chat_id, caption, target_dur):
        mp4_bytes, err = recorder.finish()
        if mp4_bytes:
            self._safe_send(
                token,
                chat_id,
                send_video,
                mp4_bytes,
                caption=caption,
                duration=max(1, int(round(target_dur))),
                width=640,
                height=480,
                timeout=60,
            )
        elif err:
            print(f"[security clip error] {err}", file=sys.stderr, flush=True)

    def _monitor_loop(self, cfg, token, chat_id, resolved, ready_evt, start_status):
        fps = max(3.0, min(10.0, float(cfg.get("SECURITY_FPS", 6.0) or 6.0)))
        cooldown = self._cooldown_sec
        clip_sec = self._clip_sec
        min_area_small = max(180, int(cfg.get("SECURITY_MIN_AREA", 1800) or 1800) // 4)

        stream = None
        recorder = None
        recorder_start_ts = 0.0
        recorder_end_ts = 0.0
        recorder_caption = ""

        with self._cam_lock:
            try:
                stream, first_fr, open_err = _open_security_stream(
                    resolved, width=640, height=480, fps=fps, retries=2
                )
                if stream is None or first_fr is None:
                    start_status["err"] = open_err or f"cannot open {resolved}"
                    ready_evt.set()
                    return
                with self._lock:
                    self._stream = stream
                start_status["ok"] = True
                ready_evt.set()

                bg_sub = cv2.createBackgroundSubtractorMOG2(history=120, varThreshold=32, detectShadows=True)
                kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
                hog = cv2.HOGDescriptor()
                hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

                pre_buf = deque(maxlen=max(6, int(round(fps * 1.8))))
                warmup_left = 10
                motion_hits = 0
                pending_fr = first_fr

                while not self._stop_evt.is_set():
                    if pending_fr is not None:
                        ok, frame = True, pending_fr
                        pending_fr = None
                    else:
                        ok, frame = stream.read()
                    t_loop = time.time()

                    if not ok or frame is None:
                        if not self._stop_evt.is_set():
                            self._safe_send(
                                token,
                                chat_id,
                                send_message,
                                f"Security Alert: USB webcam stream ended ({resolved}). Disarming.",
                            )
                        break

                    with self._frame_lock:
                        self._latest_frame = frame.copy()
                        self._latest_ts = t_loop

                    pre_buf.append((t_loop, frame.copy()))

                    # Active clip recording phase
                    if recorder is not None:
                        rec_fr = _stamp_security_frame(frame, "RECORDING SECURITY CLIP", bar_color=(127, 29, 29))
                        recorder.write_frame(rec_fr)
                        if t_loop >= recorder_end_ts or self._stop_evt.is_set():
                            rec_done = recorder
                            cap_done = recorder_caption
                            dur_done = max(2.0, t_loop - recorder_start_ts + 1.5)
                            recorder = None
                            threading.Thread(
                                target=self._finalize_and_send_clip,
                                args=(rec_done, token, chat_id, cap_done, dur_done),
                                daemon=True,
                            ).start()

                    # OpenCV motion + conditional HOG person detection
                    small = cv2.resize(frame, (320, 240))
                    blur = cv2.GaussianBlur(small, (5, 5), 0)
                    fg = bg_sub.apply(blur, learningRate=0.08 if warmup_left > 0 else 0.015)

                    if warmup_left > 0:
                        warmup_left -= 1
                        continue

                    fg_bin = ((fg > 200).astype(np.uint8)) * 255
                    fg_bin = cv2.erode(fg_bin, kernel, iterations=1)
                    fg_bin = cv2.dilate(fg_bin, kernel, iterations=2)
                    cnt_res = cv2.findContours(fg_bin, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    contours = cnt_res[0] if len(cnt_res) == 2 else cnt_res[1]

                    motion_boxes = []
                    total_area = 0.0
                    for c in contours:
                        area = cv2.contourArea(c)
                        if area < min_area_small:
                            continue
                        total_area += area
                        x, y, bw, bh = cv2.boundingRect(c)
                        motion_boxes.append((x * 2, y * 2, bw * 2, bh * 2))

                    motion_frac = total_area / float(320 * 240)
                    if motion_boxes and motion_frac < 0.90:
                        motion_hits += 1
                    else:
                        motion_hits = 0

                    if (
                        motion_hits >= 2
                        and recorder is None
                        and (t_loop - self._last_alert_ts) >= cooldown
                    ):
                        motion_hits = 0
                        self._last_alert_ts = t_loop
                        self._session_events += 1

                        rects, weights = hog.detectMultiScale(
                            small, winStride=(8, 8), padding=(4, 4), scale=1.08
                        )
                        person_boxes = []
                        if len(rects) > 0:
                            w_arr = list(weights) if weights is not None else []
                            for idx_r, (rx, ry, rw, rh) in enumerate(rects):
                                wt = float(np.asarray(w_arr[idx_r]).item()) if idx_r < len(w_arr) else 1.0
                                if wt >= 0.25:
                                    person_boxes.append((int(rx) * 2, int(ry) * 2, int(rw) * 2, int(rh) * 2))

                        draw_boxes = []
                        for x, y, bw, bh in motion_boxes[:5]:
                            draw_boxes.append((x, y, bw, bh, "MOTION", (0, 195, 255)))
                        for x, y, bw, bh in person_boxes[:4]:
                            draw_boxes.append((x, y, bw, bh, "PERSON", (34, 197, 94)))

                        ev_type = "PERSON" if person_boxes else "MOTION"
                        ts_disp = datetime.datetime.now().strftime("%d %b %Y, %I:%M:%S %p")
                        detail_s = (
                            f"{len(person_boxes)} person box(es), {len(motion_boxes)} motion zone(s), "
                            f"coverage={motion_frac * 100:.1f}%"
                        )
                        banner = (
                            f"PERSON DETECTED ({len(person_boxes)})"
                            if person_boxes
                            else f"MOTION DETECTED ({motion_frac * 100:.1f}%)"
                        )
                        annotated = _stamp_security_frame(frame, banner, bar_color=(153, 27, 27), boxes=draw_boxes)
                        ok_jpg, jpg_buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        snap_caption = f"SECURITY ALERT: {banner}\nTime: {ts_disp}\nCamera: USB HD Webcam"
                        if ok_jpg:
                            threading.Thread(
                                target=self._safe_send,
                                args=(token, chat_id, send_photo, jpg_buf.tobytes()),
                                kwargs={"caption": snap_caption},
                                daemon=True,
                            ).start()

                        _append_security_event(ev_type, detail_s, snapshot_sent=ok_jpg, clip_sec=clip_sec)

                        try:
                            rec = _ClipRecorder(640, 480, fps=fps)
                            for _, p_fr in list(pre_buf)[:-1]:
                                rec.write_frame(_stamp_security_frame(p_fr, "PRE-EVENT BUFFER", bar_color=(30, 41, 59)))
                            rec.write_frame(annotated)
                            recorder = rec
                            recorder_start_ts = t_loop
                            recorder_end_ts = t_loop + max(3.5, float(clip_sec) - 1.5)
                            recorder_caption = f"Security Clip (~{clip_sec}s) | {ev_type} @ {ts_disp}"
                        except Exception as e:
                            print(f"[security recorder start error] {e}", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[security monitor error] {e}", file=sys.stderr, flush=True)
                start_status["err"] = str(e)
                ready_evt.set()
            finally:
                if recorder is not None:
                    self._finalize_and_send_clip(
                        recorder,
                        token,
                        chat_id,
                        recorder_caption or "Security Clip (stopped)",
                        max(2.0, time.time() - recorder_start_ts),
                    )
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                with self._lock:
                    self._stream = None
                    self._armed = False
                _set_security_armed_flag(False)


SECURITY_MONITOR = SecurityMonitor()


def cmd_help(_cfg):
    return (
        "vickyhome Telegram Bot (Monitoring + Security)\n\n"
        "Security Camera (External USB HD Webcam):\n"
        "/security_on (or send 'security') - Arm motion & person detection\n"
        "/security_off - Disarm monitoring & turn camera off\n"
        "/security_status - Check armed status, cooldown & event log\n"
        "/snapshot - Capture instant photo from security webcam\n\n"
        "System, Sky & Photos:\n"
        "/snap - Capture live photo from built-in camera\n"
        "/ai <question> - Ask Gemini AI (or send plain text)\n"
        "/clear - Clear short-term AI chat memory\n"
        "/flights - Live planes within 10 km of Chennai\n"
        "/recap - Today's 10 km flight recap & special sightings\n"
        "/sky - Local sky: Sun/Moon, ISS overhead & ISRO\n"
        "/briefing - Local weather, server health & AdGuard summary\n"
        "/status - Full server health overview\n"
        "/temp - CPU & thermal sensor temps\n"
        "/ram - RAM, swap & top memory apps\n"
        "/top - Top CPU processes\n"
        "/disk - Disk & storage space\n"
        "/services - Service & daemon statuses\n"
        "/adguard - AdGuard Home DNS & block stats\n"
        "/help - Show this command list"
    )


def cmd_temp(_cfg):
    temps = read_temps()
    uptime_str, load_str = read_uptime_load()
    mhz_list = []
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("cpu mhz"):
                    mhz_list.append(float(line.split(":", 1)[1].strip()))
    except Exception:
        pass

    lines = ["vickyhome Temperature & CPU", ""]
    if temps:
        max_t = max(t for _, t in temps)
        badge = "Cool" if max_t < 60 else ("Warm" if max_t < 75 else "HOT!")
        lines.append(f"Status: {badge} (peak {max_t:.1f} C)")
        for name, val in temps:
            lines.append(f"- {name}: {val:.1f} C")
    else:
        lines.append("No thermal sensors found.")

    if mhz_list:
        lines.append(f"CPU Freq: {', '.join(f'{m:.0f} MHz' for m in mhz_list)}")
    lines.append(f"Load (1/5/15m): {load_str}")
    lines.append(f"Uptime: {uptime_str}")
    return "\n".join(lines)


def cmd_ram(_cfg):
    mem, swap = read_memory()
    _, ps_out, _ = run_cmd(["ps", "-eo", "pid,user,%mem,rss,comm", "--sort=-rss"])
    top_lines = ps_out.splitlines()[1:6]
    lines = ["vickyhome Memory & Top Apps", ""]
    if mem:
        lines.append(f"RAM: {mem['used']} / {mem['total']} MiB used ({mem['avail']} MiB available)")
    if swap:
        lines.append(f"Swap: {swap['used']} / {swap['total']} MiB used")
    lines.append("")
    lines.append("Top 5 by RAM:")
    for row in top_lines:
        parts = row.split(None, 4)
        if len(parts) == 5:
            _pid, user, pmem, rss_kb, comm = parts
            rss_mb = int(rss_kb) / 1024.0
            lines.append(f"- {comm} ({user}): {rss_mb:.1f} MiB [{pmem}%]")
    return "\n".join(lines)


def cmd_top(_cfg):
    _, ps_cpu, _ = run_cmd(["ps", "-eo", "pid,user,%cpu,%mem,comm", "--sort=-%cpu"])
    lines = ["vickyhome Top CPU Processes", ""]
    for row in ps_cpu.splitlines()[1:6]:
        parts = row.split(None, 4)
        if len(parts) == 5:
            _pid, user, pcpu, pmem, comm = parts
            lines.append(f"- {comm} ({user}): CPU {pcpu}% | RAM {pmem}%")
    return "\n".join(lines)


def cmd_disk(_cfg):
    _, df_out, _ = run_cmd(["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay"])
    _, lsblk_out, _ = run_cmd(["lsblk", "-o", "NAME,SIZE,MOUNTPOINT"])
    return f"vickyhome Storage\n\n{df_out}\n\nBlock Devices:\n{lsblk_out}"


def cmd_services(_cfg):
    lines = ["vickyhome Services", ""]
    for label, unit in MONITORED_SERVICES:
        st = service_state(unit)
        icon = "OK" if st == "active" else st.upper()
        lines.append(f"- {label} ({unit}): {icon}")

    rc_dash, dash_pid, _ = run_cmd(["pgrep", "-f", "/home/vicky/newdash25.py"])
    lines.append(
        f"- Dashboard (newdash25.py): {'OK (PID ' + dash_pid.splitlines()[0] + ')' if rc_dash == 0 and dash_pid else 'STOPPED'}"
    )

    _, failed_out, _ = run_cmd(["systemctl", "--failed", "--no-legend", "--no-pager"])
    if failed_out:
        lines.append("")
        lines.append("Failed systemd units:")
        for fl in failed_out.splitlines():
            unit_name = fl.lstrip("● ").split()[0]
            lines.append(f"- {unit_name}")
    return "\n".join(lines)


def agh_get(cfg, path):
    base = cfg.get("ADGUARD_URL", "http://127.0.0.1:8081").rstrip("/")
    user = cfg.get("ADGUARD_USER", "")
    pwd = cfg.get("ADGUARD_PASS", "")
    headers = {}
    if user or pwd:
        token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(f"{base}/control/{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def cmd_adguard(cfg):
    try:
        st = agh_get(cfg, "status")
        flt = agh_get(cfg, "filtering/status")
        stats = agh_get(cfg, "stats")
        sb = agh_get(cfg, "safebrowsing/status")
    except Exception as e:
        return f"AdGuard API error: {e}"

    enabled_lists = [f for f in flt.get("filters", []) if f.get("enabled")]
    total_rules = sum(f.get("rules_count", 0) for f in enabled_lists)
    queries = stats.get("num_dns_queries", sum(stats.get("dns_queries", [])))
    blocked = stats.get("num_blocked_filtering", sum(stats.get("blocked_filtering", [])))
    pct = (blocked / queries * 100.0) if queries else 0.0

    return (
        "vickyhome AdGuard Home\n\n"
        f"Protection: {'ON' if st.get('protection_enabled') else 'OFF'}\n"
        f"Safe Browsing: {'ON' if sb.get('enabled') else 'OFF'}\n"
        f"Active Filter Lists: {len(enabled_lists)}\n"
        f"Total Compiled Rules: {total_rules:,}\n"
        f"24h DNS Queries: {queries:,}\n"
        f"24h Blocked: {blocked:,} ({pct:.1f}%)"
    )


def cmd_status(_cfg):
    uptime_str, load_str = read_uptime_load()
    temps = read_temps()
    mem, swap = read_memory()
    _, df_root, _ = run_cmd(["df", "-h", "/"])
    disk_line = df_root.splitlines()[-1].split() if len(df_root.splitlines()) >= 2 else []
    disk_summary = f"{disk_line[2]} / {disk_line[1]} used ({disk_line[4]}, {disk_line[3]} free)" if len(disk_line) >= 5 else "?"

    core_units = [
        ("AdGuardHome", "AdGuardHome.service"),
        ("Tailscale", "tailscaled.service"),
        ("Copyparty", "copyparty.service"),
        ("FileBrowser", "filebrowser.service"),
        ("Samba", "smbd.service"),
        ("SSH", "ssh.service"),
        ("AuthAlert", "vickyhome-auth-alert.service"),
    ]
    svc_parts = [f"{name}:{'OK' if service_state(u) == 'active' else 'DOWN'}" for name, u in core_units]
    temp_summary = ", ".join(f"{n}:{t:.1f}C" for n, t in temps) if temps else "n/a"

    sec_state = "ARMED" if SECURITY_MONITOR.is_armed() else "Inactive"
    return (
        "vickyhome Status\n\n"
        f"Uptime: {uptime_str}\n"
        f"Load: {load_str}\n"
        f"Temp: {temp_summary}\n"
        f"RAM: {mem.get('used', '?')}/{mem.get('total', '?')} MiB ({mem.get('avail', '?')} MiB avail)\n"
        f"Swap: {swap.get('used', '?')}/{swap.get('total', '?')} MiB\n"
        f"Disk (/): {disk_summary}\n"
        f"Security Cam: {sec_state}\n"
        f"Services: {' | '.join(svc_parts)}"
    )


def _fmt_hhmm_ampm(iso_str):
    if not iso_str or "T" not in iso_str:
        return "?"
    try:
        dt = datetime.datetime.strptime(iso_str[:16], "%Y-%m-%dT%H:%M")
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return iso_str.split("T", 1)[1][:5]


def moon_phase_info(dt_utc=None):
    if dt_utc is None:
        dt_utc = datetime.datetime.now(datetime.timezone.utc)
    elif dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=datetime.timezone.utc)
    ref = datetime.datetime(2000, 1, 6, 18, 14, tzinfo=datetime.timezone.utc)
    synodic = 29.53058867
    phase_days = ((dt_utc - ref).total_seconds() / 86400.0) % synodic
    frac = phase_days / synodic
    index = int(frac * 8.0 + 0.5) % 8
    names = (
        "New Moon",
        "Waxing Crescent",
        "First Quarter",
        "Waxing Gibbous",
        "Full Moon",
        "Waning Gibbous",
        "Last Quarter",
        "Waning Crescent",
    )
    illum = int(round((1.0 - math.cos(2.0 * math.pi * frac)) * 50.0))
    return names[index], int(round(phase_days)), illum


def fetch_local_weather_and_sun():
    """Fetch current weather, forecast, and sunrise/sunset for Chennai/Chennai from Open-Meteo."""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(LOCAL_WEATHER_URL, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            cur = data.get("current", {})
            daily = data.get("daily", {})
            c_temp = cur.get("temperature_2m", "?")
            c_hum = cur.get("relative_humidity_2m", "?")
            c_wind = cur.get("wind_speed_10m", "?")
            c_desc = WMO_CODES.get(cur.get("weather_code"), "Mixed")

            t_min = daily.get("temperature_2m_min", ["?"])[0]
            t_max = daily.get("temperature_2m_max", ["?"])[0]
            rain_p = daily.get("precipitation_probability_max", ["?"])[0]
            d_desc = WMO_CODES.get(daily.get("weather_code", [None])[0], c_desc)
            sr = _fmt_hhmm_ampm((daily.get("sunrise") or [""])[0])
            ss = _fmt_hhmm_ampm((daily.get("sunset") or [""])[0])
            dl_sec = (daily.get("daylight_duration") or [None])[0]
            dl_txt = ""
            if dl_sec is not None:
                mins = int(round(float(dl_sec) / 60.0))
                dl_txt = f" ({mins // 60}h {mins % 60}m daylight)"

            m_name, m_day, m_illum = moon_phase_info()
            weather_lines = [
                f"- Now: {c_temp} C, {c_desc} (Humidity {c_hum}%, Wind {c_wind} km/h)",
                f"- Today: {t_min} - {t_max} C | {d_desc} | Rain chance {rain_p}%",
            ]
            sky_lines = [
                f"- Sun: Sunrise {sr} | Sunset {ss}{dl_txt}",
                f"- Moon: {m_name} (Day {m_day}, ~{m_illum}% lit)",
            ]
            return weather_lines, sky_lines
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
            else:
                m_name, m_day, m_illum = moon_phase_info()
                return [f"- Weather unavailable ({e})"], [f"- Moon: {m_name} (Day {m_day}, ~{m_illum}% lit)"]


def fetch_local_weather():
    weather_lines, _ = fetch_local_weather_and_sun()
    return weather_lines


def cmd_briefing(cfg):
    now_str = datetime.datetime.now().strftime("%a, %d %b %Y (%I:%M %p)")
    uptime_str, load_str = read_uptime_load()
    temps = read_temps()
    mem, swap = read_memory()
    _, df_root, _ = run_cmd(["df", "-h", "/"])
    disk_line = df_root.splitlines()[-1].split() if len(df_root.splitlines()) >= 2 else []
    disk_summary = (
        f"{disk_line[2]} / {disk_line[1]} used ({disk_line[4]}, {disk_line[3]} free)"
        if len(disk_line) >= 5 else "?"
    )

    core_units = [
        ("AdGuardHome", "AdGuardHome.service"),
        ("Tailscale", "tailscaled.service"),
        ("Copyparty", "copyparty.service"),
        ("FileBrowser", "filebrowser.service"),
        ("Samba", "smbd.service"),
        ("SSH", "ssh.service"),
        ("AuthAlert", "vickyhome-auth-alert.service"),
        ("TelegramBot", "vickyhome-telegram-bot.service"),
    ]
    down = [name for name, unit in core_units if service_state(unit) != "active"]
    svc_summary = (
        f"{len(core_units)}/{len(core_units)} core OK"
        if not down else f"{len(core_units) - len(down)}/{len(core_units)} OK (DOWN: {', '.join(down)})"
    )
    temp_summary = ", ".join(f"{n} {t:.1f} C" for n, t in temps) if temps else "n/a"

    weather_lines, sky_lines = fetch_local_weather_and_sun()
    try:
        sp_note = get_briefing_sky_note()
        if sp_note:
            sky_lines.append(f"- Space: {sp_note}")
    except Exception:
        pass

    try:
        flt = agh_get(cfg, "filtering/status")
        stats = agh_get(cfg, "stats")
        sb = agh_get(cfg, "safebrowsing/status")
        enabled_lists = [f for f in flt.get("filters", []) if f.get("enabled")]
        total_rules = sum(f.get("rules_count", 0) for f in enabled_lists)
        queries = stats.get("num_dns_queries", sum(stats.get("dns_queries", [])))
        blocked = stats.get("num_blocked_filtering", sum(stats.get("blocked_filtering", [])))
        pct = (blocked / queries * 100.0) if queries else 0.0
        top_doms = []
        for item in stats.get("top_blocked_domains", [])[:3]:
            if isinstance(item, dict):
                for dom, cnt in item.items():
                    top_doms.append(f"{dom} ({cnt})")
        agh_lines = [
            f"- Queries: {queries:,} | Blocked: {blocked:,} ({pct:.1f}%)",
            f"- Safe Browsing: {'ON' if sb.get('enabled') else 'OFF'} | Active Rules: {total_rules:,}",
        ]
        if top_doms:
            agh_lines.append(f"- Top blocked: {', '.join(top_doms)}")
    except Exception as e:
        agh_lines = [f"- AdGuard stats unavailable ({e})"]

    lines = [
        f"Morning Briefing - {now_str}",
        "",
        "Local Weather",
        *weather_lines,
        "",
        "Chennai Sky",
        *sky_lines,
        "",
        "Server Health (vickyhome)",
        f"- Uptime: {uptime_str} (Load: {load_str})",
        f"- Temp: {temp_summary}",
        f"- RAM: {mem.get('used', '?')}/{mem.get('total', '?')} MiB ({mem.get('avail', '?')} MiB avail) | Swap: {swap.get('used', '?')}/{swap.get('total', '?')} MiB",
        f"- Disk (/): {disk_summary}",
        f"- Services: {svc_summary}",
        "",
        "AdGuard Home (Last 24h)",
        *agh_lines,
    ]
    return "\n".join(lines)


HOME_LAT = 13.0827
HOME_LON = 80.2707
FLIGHT_RADIUS_KM = 10.0
FLIGHT_LOG_PATH = "/var/lib/vickyhome-tgbot/flights_log.json"
FLIGHT_LOG_LOCK = threading.Lock()
LOW_ALT_FT = 10000

KNOWN_AIRLINE_PREFIXES = {
    "IGO": "IndiGo",
    "AXB": "Air India Express",
    "AIC": "Air India",
    "SEJ": "SpiceJet",
    "AKJ": "Akasa Air",
    "VTI": "Vistara",
    "LLR": "Alliance Air",
    "ALK": "SriLankan Airlines",
    "UAE": "Emirates",
    "ETD": "Etihad Airways",
    "QTR": "Qatar Airways",
    "SIA": "Singapore Airlines",
    "MAS": "Malaysia Airlines",
    "GFA": "Gulf Air",
    "OMA": "Oman Air",
    "SVA": "Saudia",
    "KAC": "Kuwait Airways",
    "FDB": "flydubai",
    "ABY": "Air Arabia",
    "THY": "Turkish Airlines",
    "BAW": "British Airways",
    "DLH": "Lufthansa",
}

HELI_RE = re.compile(
    r"\b(helicopter|heli|robinson|r44|r66|aw139|aw-139|agusta|ec135|ec145|h125|h135|h145|s-76|dhruv|cheetak|cheetah|mi-17|chinook|apache)\b",
    re.I,
)
MIL_RE = re.compile(
    r"\b(c-17|c-130|hercules|an-32|antonov|il-76|p-8|poseidon|dornier|do-228|netra|tejas|su-30|rafale|mirage|mig-)\b",
    re.I,
)
BIZ_AC_RE = re.compile(
    r"\b(falcon|gulfstream|challenger|global\s+\d+|global\s+express|learjet|citation|hawker|phenom|praetor|legacy|king\s+air|beech|pilatus|pc-12|pc-24|cessna|piper|cirrus|mooney|diamond|hondajet|emb-135|embraer\s+135|f2th|fa20|fa50|fa7x|fa8x|glf4|glf5|glf6|g200|g280|g550|g650|glex|cl30|cl35|cl60|lj35|lj45|lj60|c25a|c25b|c56x|c680|h25b|be20|be30|b350|pc12|pc24)\b",
    re.I,
)


def infer_airline_name(fl):
    airline = (fl.get("airline") or "").strip()
    if airline:
        return airline
    cs = (fl.get("callsign") or "").strip().upper()
    if len(cs) >= 4 and cs[:3] in KNOWN_AIRLINE_PREFIXES and cs[3:4].isdigit():
        return KNOWN_AIRLINE_PREFIXES[cs[:3]]
    return ""


def classify_flight_tags(fl):
    tags = []
    alt_val = fl.get("alt_ft")
    if alt_val is None:
        alt_val = fl.get("min_alt_ft")
    if alt_val is not None and int(alt_val) < LOW_ALT_FT:
        tags.append("Low Altitude (< 10,000 ft)")

    ac_lower = (fl.get("aircraft") or "").lower()
    cs = (fl.get("callsign") or "").strip().upper()
    reg_norm = (fl.get("reg") or "").replace("-", "").strip().upper()
    has_airline = bool(infer_airline_name(fl) or (fl.get("iata") or "").strip())
    is_reg_cs = bool(reg_norm and cs == reg_norm) or bool(re.match(r"^VT[A-Z]{3}$", cs))

    if HELI_RE.search(ac_lower):
        tags.append("Helicopter")
    elif MIL_RE.search(ac_lower) or cs.startswith("IFC"):
        tags.append("Military / Govt")
    elif BIZ_AC_RE.search(ac_lower) or (is_reg_cs and not has_airline):
        tags.append("Private / Business Jet")
    return tags


def _atomic_json_read(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _atomic_json_write(path, data, prefix="st_", mode=0o600):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_flight_log():
    return _atomic_json_read(FLIGHT_LOG_PATH, {})


def save_flight_log(store):
    keep_dates = sorted(store.keys())[-7:]
    trimmed = {d: store[d] for d in keep_dates}
    _atomic_json_write(FLIGHT_LOG_PATH, trimmed, prefix="fl_")


def record_flight_crossing(fl, now_ts=None):
    if now_ts is None:
        now_ts = time.time()
    hx = (fl.get("hex") or fl.get("callsign") or "").strip().upper()
    if not hx:
        return
    date_key = datetime.datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")
    alt_val = int(fl["alt_ft"]) if fl.get("alt_ft") is not None else None
    dist_val = float(fl["dist_km"]) if fl.get("dist_km") is not None else None
    compass = (fl.get("compass") or "").strip()
    tags_now = classify_flight_tags(fl)
    airline = infer_airline_name(fl)

    with FLIGHT_LOG_LOCK:
        store = load_flight_log()
        visits = store.setdefault(date_key, [])
        match = None
        for v in reversed(visits):
            if v.get("hex") == hx and abs(now_ts - float(v.get("last_seen", 0))) < 1500:
                match = v
                break

        changed = False
        if match is None:
            visits.append(
                {
                    "hex": hx,
                    "callsign": (fl.get("callsign") or hx).strip().upper(),
                    "iata": (fl.get("iata") or "").strip(),
                    "reg": (fl.get("reg") or "").strip().upper(),
                    "airline": airline,
                    "aircraft": (fl.get("aircraft") or "").strip(),
                    "orig_code": (fl.get("orig_code") or "").strip(),
                    "orig_city": (fl.get("orig_city") or "").strip(),
                    "dst_code": (fl.get("dst_code") or "").strip(),
                    "dst_city": (fl.get("dst_city") or "").strip(),
                    "min_alt_ft": alt_val,
                    "min_dist_km": dist_val,
                    "min_dist_compass": compass,
                    "first_seen": now_ts,
                    "last_seen": now_ts,
                    "tags": tags_now,
                }
            )
            changed = True
        else:
            if alt_val is not None and (match.get("min_alt_ft") is None or alt_val < match["min_alt_ft"]):
                match["min_alt_ft"] = alt_val
                changed = True
            if dist_val is not None and (match.get("min_dist_km") is None or dist_val < match["min_dist_km"]):
                match["min_dist_km"] = dist_val
                match["min_dist_compass"] = compass
                changed = True
            for k, val in (
                ("airline", airline),
                ("iata", (fl.get("iata") or "").strip()),
                ("reg", (fl.get("reg") or "").strip().upper()),
                ("aircraft", (fl.get("aircraft") or "").strip()),
                ("orig_code", (fl.get("orig_code") or "").strip()),
                ("orig_city", (fl.get("orig_city") or "").strip()),
                ("dst_code", (fl.get("dst_code") or "").strip()),
                ("dst_city", (fl.get("dst_city") or "").strip()),
            ):
                if val and not match.get(k):
                    match[k] = val
                    changed = True
            merged_tags = sorted(set(match.get("tags") or []) | set(tags_now))
            if merged_tags != (match.get("tags") or []):
                match["tags"] = merged_tags
                changed = True
            if now_ts - float(match.get("last_seen", 0)) >= 60:
                match["last_seen"] = now_ts
                changed = True

        if changed:
            save_flight_log(store)


def _haversine_km_bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brng = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    dirs = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return round(km, 1), dirs[int((brng + 22.5) // 45) % 8]


def _direct_adsb_fallback(radius_km=FLIGHT_RADIUS_KM):
    scan_nm = max(6, int(math.ceil(float(radius_km) / 1.852)) + 1)
    url = f"https://opendata.adsb.fi/api/v2/lat/{HOME_LAT}/lon/{HOME_LON}/dist/{scan_nm}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        ac_list = json.loads(r.read().decode("utf-8")).get("ac") or []

    flights = []
    for a in ac_list:
        a_lat, a_lon = a.get("lat"), a.get("lon")
        alt_raw = a.get("alt_baro")
        if a_lat is None or a_lon is None or alt_raw in (None, "ground"):
            continue
        alt_ft = int(round(float(alt_raw)))
        if alt_ft < 100:
            continue
        dist_km, compass = _haversine_km_bearing(HOME_LAT, HOME_LON, float(a_lat), float(a_lon))
        if dist_km > radius_km:
            continue
        cs = (a.get("flight") or "").strip().upper()
        gs_kt = a.get("gs")
        fl = {
            "hex": (a.get("hex") or cs or "UNK").upper(),
            "callsign": cs or (a.get("r") or a.get("hex") or "UNKNOWN").upper(),
            "raw_callsign": cs,
            "reg": (a.get("r") or "").strip().upper(),
            "aircraft": (a.get("desc") or a.get("t") or "Aircraft").strip(),
            "alt_ft": alt_ft,
            "speed_kmh": int(round(float(gs_kt) * 1.852)) if gs_kt is not None else None,
            "dist_km": dist_km,
            "compass": compass,
            "track": int(round(float(a["track"]))) if a.get("track") is not None else None,
        }
        if len(cs) >= 3 and cs.isalnum():
            try:
                rreq = urllib.request.Request(
                    f"https://api.adsbdb.com/v0/callsign/{cs}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(rreq, timeout=4) as rr:
                    fr = (json.loads(rr.read().decode("utf-8")).get("response") or {}).get("flightroute") or {}
                orig = fr.get("origin") or {}
                dst = fr.get("destination") or {}
                fl["airline"] = (fr.get("airline") or {}).get("name") or ""
                fl["iata"] = fr.get("callsign_iata") or ""
                fl["orig_code"] = orig.get("iata_code") or orig.get("icao_code") or ""
                fl["orig_city"] = orig.get("municipality") or ""
                fl["dst_code"] = dst.get("iata_code") or dst.get("icao_code") or ""
                fl["dst_city"] = dst.get("municipality") or ""
                if fl["orig_code"] or fl["dst_code"]:
                    fl["route_valid"] = True
            except Exception:
                pass
        flights.append(fl)

    flights.sort(key=lambda x: x["dist_km"])
    return {
        "location": "Chennai",
        "radius_km": radius_km,
        "radius_nm": round(radius_km / 1.852, 2),
        "flights": flights[:5],
        "preview_flights": flights[:5],
    }


def fetch_flights_data(cfg):
    dash_url = cfg.get("DASH_URL", "http://127.0.0.1:8765").rstrip("/")
    dash_tok = cfg.get("DASH_TOKEN", "")
    if dash_url and dash_tok:
        try:
            q = urllib.parse.urlencode({"token": dash_tok})
            req = urllib.request.Request(f"{dash_url}/api/flights?{q}")
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            pass
    return _direct_adsb_fallback(FLIGHT_RADIUS_KM)


def _format_route_line(fl):
    oc = fl.get("orig_code") or ""
    dc = fl.get("dst_code") or ""
    if oc or dc:
        o_txt = f"{oc} ({fl['orig_city']})" if fl.get("orig_city") else oc
        d_txt = f"{dc} ({fl['dst_city']})" if fl.get("dst_city") else dc
        return f"{o_txt or '?'} -> {d_txt or '?'}"
    return "Unavailable"


def format_flight_card(fl, location="Chennai"):
    cs = fl.get("callsign") or "UNKNOWN"
    iata = fl.get("iata") or ""
    title_cs = f"{iata} ({cs})" if iata and iata != cs else cs
    tags = classify_flight_tags(fl)
    airline = infer_airline_name(fl) or ("Private / Unlisted" if any(t != "Low Altitude (< 10,000 ft)" for t in tags) else "Unknown / Private")
    aircraft = fl.get("aircraft") or "Aircraft"
    alt = f"{int(fl['alt_ft']):,} ft" if fl.get("alt_ft") is not None else "?"
    spd = f"{fl['speed_kmh']} km/h" if fl.get("speed_kmh") is not None else "?"
    dist = f"{fl.get('dist_km', '?')} km {fl.get('compass', '')}".strip()
    track_txt = f" (Heading {fl['track']} deg)" if fl.get("track") is not None else ""
    header = (
        f"[SPECIAL: {', '.join(tags)}] Flight Over {location} "
        if tags
        else f"Flight Over {location} "
    )
    highlight_line = f"Highlight: {', '.join(tags)}\n" if tags else ""
    return (
        f"{header}\n\n"
        f"{highlight_line}"
        f"Flight: {title_cs}\n"
        f"Airline: {airline}\n"
        f"Route: {_format_route_line(fl)}\n"
        f"Aircraft: {aircraft}\n"
        f"Altitude: {alt} | Speed: {spd}\n"
        f"Distance: {dist}{track_txt}"
    )


def cmd_flights(cfg):
    try:
        data = fetch_flights_data(cfg)
    except Exception as e:
        return f"Flight radar unavailable ({e})."

    loc = data.get("location") or "Chennai"
    radius_km = float(data.get("radius_km") or FLIGHT_RADIUS_KM)
    radius_lbl = f"{int(radius_km) if radius_km.is_integer() else radius_km} km"
    overhead = [
        fl for fl in (data.get("flights") or [])
        if fl.get("dist_km") is None or float(fl["dist_km"]) <= radius_km
    ]

    if overhead:
        cards = [format_flight_card(fl, loc) for fl in overhead[:3]]
        return "\n\n---\n\n".join(cards)

    return f"No flights within {radius_lbl} of {loc}  right now."


def build_flight_recap(date_str=None):
    now = datetime.datetime.now()
    if date_str is None:
        date_str = now.strftime("%Y-%m-%d")
    try:
        date_lbl = datetime.datetime.strptime(date_str, "%Y-%m-%d").strftime("%a, %d %b %Y")
    except ValueError:
        date_lbl = date_str

    with FLIGHT_LOG_LOCK:
        all_visits = list(load_flight_log().get(date_str, []))
    visits = [
        v for v in all_visits
        if v.get("min_dist_km") is None or float(v["min_dist_km"]) <= FLIGHT_RADIUS_KM
    ]

    radius_lbl = f"{int(FLIGHT_RADIUS_KM) if float(FLIGHT_RADIUS_KM).is_integer() else FLIGHT_RADIUS_KM} km"
    header = f"Chennai Flight Recap - {date_lbl}"
    if not visits:
        return f"{header}\n\nNo aircraft crossed inside {radius_lbl} today yet."

    lines = [header, "", f"Total Overhead Crossings ({radius_lbl}): {len(visits)}", ""]

    specials = []
    airline_counts = Counter()
    routes_seen = set()
    lowest_alt_v = None
    closest_v = None

    for v in visits:
        tags = sorted(set(v.get("tags") or []) | set(classify_flight_tags(v)))
        if tags:
            specials.append((v, tags))
        airline = infer_airline_name(v)
        if airline:
            airline_counts[airline] += 1
        elif any(t != "Low Altitude (< 10,000 ft)" for t in tags):
            airline_counts["Private / Unlisted"] += 1
        else:
            airline_counts["Unlisted Carrier"] += 1

        oc = v.get("orig_code") or ""
        dc = v.get("dst_code") or ""
        if oc and dc:
            routes_seen.add(f"{oc} -> {dc}")

        alt_v = v.get("min_alt_ft")
        if alt_v is not None and (lowest_alt_v is None or alt_v < lowest_alt_v["min_alt_ft"]):
            lowest_alt_v = v

        dist_v = v.get("min_dist_km")
        if dist_v is not None and (closest_v is None or dist_v < closest_v["min_dist_km"]):
            closest_v = v

    if specials:
        lines.append(f"Special & Interesting Sightings ({len(specials)})")
        for v, tags in specials[:10]:
            cs = v.get("iata") or v.get("callsign") or "UNKNOWN"
            ac = v.get("aircraft") or "Aircraft"
            oc, dc = v.get("orig_code") or "", v.get("dst_code") or ""
            rt = f"{oc} -> {dc}" if oc or dc else "Route Unavailable"
            alt_txt = f"{int(v['min_alt_ft']):,} ft" if v.get("min_alt_ft") is not None else "Alt n/a"
            dist_txt = f"{v['min_dist_km']} km {v.get('min_dist_compass', '')}".strip() if v.get("min_dist_km") is not None else "?"
            lines.append(f"- {cs} | {ac} | {rt} | {alt_txt} | {dist_txt} [{', '.join(tags)}]")
    else:
        lines.append("Special & Interesting Sightings: None today (all standard cruisers)")

    lines.append("")
    lines.append("Top Airlines")
    for name, cnt in airline_counts.most_common(5):
        lines.append(f"- {name}: {cnt}")

    lines.append("")
    if lowest_alt_v is not None:
        cs = lowest_alt_v.get("iata") or lowest_alt_v.get("callsign") or "UNKNOWN"
        al = infer_airline_name(lowest_alt_v) or "Private"
        oc, dc = lowest_alt_v.get("orig_code") or "", lowest_alt_v.get("dst_code") or ""
        rt = f" | {oc} -> {dc}" if oc or dc else ""
        lines.append("Lowest Altitude Recorded")
        lines.append(f"- {cs} ({al}): {int(lowest_alt_v['min_alt_ft']):,} ft{rt}")
    else:
        lines.append("Lowest Altitude Recorded: Pending live crossing")

    if closest_v is not None:
        cs = closest_v.get("iata") or closest_v.get("callsign") or "UNKNOWN"
        al = infer_airline_name(closest_v) or "Private"
        oc, dc = closest_v.get("orig_code") or "", closest_v.get("dst_code") or ""
        rt = f" | {oc} -> {dc}" if oc or dc else ""
        lines.append("")
        lines.append("Closest Pass")
        lines.append(f"- {cs} ({al}): {closest_v['min_dist_km']} km {closest_v.get('min_dist_compass', '')}".strip() + rt)

    lines.append("")
    if routes_seen:
        sorted_routes = sorted(routes_seen)
        shown = sorted_routes[:20]
        extra = f" (+{len(sorted_routes) - 20} more)" if len(sorted_routes) > 20 else ""
        lines.append(f"Unique Routes Seen ({len(sorted_routes)})")
        lines.append("- " + ", ".join(shown) + extra)
    else:
        lines.append("Unique Routes Seen: None resolved yet")

    return "\n".join(lines)


def cmd_flight_recap(_cfg):
    return build_flight_recap()


def flight_alert_worker(cfg, token, chat_id):
    seen = {}
    today_key = datetime.datetime.now().strftime("%Y-%m-%d")
    now0 = time.time()
    with FLIGHT_LOG_LOCK:
        for v in load_flight_log().get(today_key, []):
            hx = (v.get("hex") or "").upper()
            ts = float(v.get("last_seen", 0))
            if hx and now0 - ts < 1500:
                seen[hx] = ts

    while True:
        try:
            live_cfg = load_conf()
            now = time.time()
            for hx in [k for k, ts in seen.items() if now - ts > 1800]:
                seen.pop(hx, None)
            data = fetch_flights_data(live_cfg)
            loc = data.get("location") or "Chennai"
            max_km = float(data.get("radius_km") or FLIGHT_RADIUS_KM)
            for fl in data.get("flights") or []:
                if fl.get("dist_km") is not None and float(fl["dist_km"]) > max_km:
                    continue
                record_flight_crossing(fl, now)
                if live_cfg.get("FLIGHT_ALERTS", "1") != "1":
                    continue
                hx = (fl.get("hex") or fl.get("callsign") or "").upper()
                if not hx or (now - seen.get(hx, 0)) < 1500:
                    continue
                seen[hx] = now
                send_message(token, chat_id, format_flight_card(fl, loc))
        except Exception as e:
            _log_throttled("flight_alert", f"[flight alert error] {e}", min_interval=180)
        time.sleep(20)


SPACE_CACHE_PATH = "/var/lib/vickyhome-tgbot/space_cache.json"
SPACE_CACHE_LOCK = threading.Lock()
EARTH_MU = 398600.4418
EARTH_RE = 6378.137
EARTH_J2 = 1.08262668e-3
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)


def _deg_to_compass(deg):
    dirs = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return dirs[int((float(deg) + 22.5) // 45) % 8]


def _fmt_countdown(sec):
    sec = max(0, int(round(sec)))
    if sec < 3600:
        return f"{max(1, sec // 60)}m"
    hrs, rem = divmod(sec // 60, 60)
    if hrs < 48:
        return f"{hrs}h {rem}m"
    return f"{hrs // 24}d {hrs % 24}h"


def _parse_iss_tle(l1, l2):
    yy = int(l1[18:20])
    year = (2000 + yy) if yy < 57 else (1900 + yy)
    doy = float(l1[20:32])
    epoch_ts = (datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=doy - 1.0)).timestamp()
    return (
        epoch_ts,
        float(l1[33:43]),
        math.radians(float(l2[8:16])),
        math.radians(float(l2[17:25])),
        float("0." + l2[26:33].strip()),
        math.radians(float(l2[34:42])),
        math.radians(float(l2[43:51])),
        float(l2[52:63]),
    )


def _iss_pos_sun_at(tp, ts):
    epoch_ts, ndot, incl, raan0, ecc, argp0, m0, n_rev_day = tp
    dt = ts - epoch_ts
    n0 = n_rev_day * 2.0 * math.pi / 86400.0
    a1 = (EARTH_MU / (n0 ** 2)) ** (1.0 / 3.0)
    cos_i = math.cos(incl)
    k_c = 0.75 * EARTH_J2 * (3.0 * cos_i * cos_i - 1.0) / ((1.0 - ecc ** 2) ** 1.5)
    d1 = k_c * ((EARTH_RE / a1) ** 2)
    a0 = a1 * (1.0 - d1 / 3.0 - d1 * d1 - (134.0 / 81.0) * (d1 ** 3))
    d0 = k_c * ((EARTH_RE / a0) ** 2)
    n_b = n0 / (1.0 + d0)
    a = a0 / (1.0 - d0)
    p = a * (1.0 - ecc * ecc)
    fac = -1.5 * n_b * EARTH_J2 * ((EARTH_RE / p) ** 2)
    raan = raan0 + fac * cos_i * dt
    argp = argp0 + (-0.5 * fac) * (5.0 * cos_i * cos_i - 1.0) * dt
    M = (m0 + n0 * dt + (ndot * 2.0 * math.pi / (86400.0 ** 2)) * dt * dt) % (2.0 * math.pi)
    E = M
    for _ in range(6):
        E -= (E - ecc * math.sin(E) - M) / (1.0 - ecc * math.cos(E))
    x_orb = a * (math.cos(E) - ecc)
    y_orb = a * math.sqrt(max(0.0, 1.0 - ecc * ecc)) * math.sin(E)
    cr, sr = math.cos(raan), math.sin(raan)
    ca, sa = math.cos(argp), math.sin(argp)
    si = math.sin(incl)
    x_eci = x_orb * (cr * ca - sr * sa * cos_i) + y_orb * (-cr * sa - sr * ca * cos_i)
    y_eci = x_orb * (sr * ca + cr * sa * cos_i) + y_orb * (-sr * sa + cr * ca * cos_i)
    z_eci = x_orb * (sa * si) + y_orb * (ca * si)

    d_j2 = ts / 86400.0 + 2440587.5 - 2451545.0
    gmst = math.radians((280.46061837 + 360.98564736629 * d_j2) % 360.0)
    cg, sg = math.cos(gmst), math.sin(gmst)
    sat_ef = (x_eci * cg + y_eci * sg, -x_eci * sg + y_eci * cg, z_eci)

    sun_l = math.radians((280.460 + 0.9856474 * d_j2) % 360.0)
    sun_g = math.radians((357.528 + 0.9856003 * d_j2) % 360.0)
    lam = sun_l + math.radians(1.915) * math.sin(sun_g) + math.radians(0.020) * math.sin(2.0 * sun_g)
    eps = math.radians(23.439 - 0.0000004 * d_j2)
    s_eci = (math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam))
    proj = x_eci * s_eci[0] + y_eci * s_eci[1] + z_eci * s_eci[2]
    sunlit = True if proj >= 0 else (x_eci * x_eci + y_eci * y_eci + z_eci * z_eci - proj * proj) > (EARTH_RE * EARTH_RE)
    sun_ef = (s_eci[0] * cg + s_eci[1] * sg, -s_eci[0] * sg + s_eci[1] * cg, s_eci[2])
    return sat_ef, sunlit, sun_ef


def _obs_frame(lat_deg=HOME_LAT, lon_deg=HOME_LON, alt_km=0.14):
    lr, ln = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lr), math.cos(lr)
    sn, cn = math.sin(ln), math.cos(ln)
    n_val = EARTH_RE / math.sqrt(1.0 - WGS84_E2 * sl * sl)
    ox = (n_val + alt_km) * cl * cn
    oy = (n_val + alt_km) * cl * sn
    oz = (n_val * (1.0 - WGS84_E2) + alt_km) * sl
    return ox, oy, oz, sl, cl, sn, cn


def _topocentric_metrics(sat_ef, sun_ef, obs):
    ox, oy, oz, sl, cl, sn, cn = obs
    dx, dy, dz = sat_ef[0] - ox, sat_ef[1] - oy, sat_ef[2] - oz
    south = sl * cn * dx + sl * sn * dy - cl * dz
    east = -sn * dx + cn * dy
    zen = cl * cn * dx + cl * sn * dy + sl * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    el = math.degrees(math.asin(max(-1.0, min(1.0, zen / max(1e-9, rng)))))
    az = (math.degrees(math.atan2(east, -south)) + 360.0) % 360.0
    sun_zen = cl * cn * sun_ef[0] + cl * sn * sun_ef[1] + sl * sun_ef[2]
    sun_el = math.degrees(math.asin(max(-1.0, min(1.0, sun_zen))))
    return el, az, sun_el, rng


def _fetch_iss_tle():
    hdrs = {"User-Agent": "Mozilla/5.0 vickyhome"}
    try:
        req = urllib.request.Request("https://api.wheretheiss.at/v1/satellites/25544/tles", headers=hdrs)
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("line1") and d.get("line2"):
            return d["line1"].strip(), d["line2"].strip()
    except Exception:
        pass
    req = urllib.request.Request("https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=tle", headers=hdrs)
    with urllib.request.urlopen(req, timeout=8) as r:
        lines = [ln.strip() for ln in r.read().decode("utf-8").splitlines() if ln.strip()]
    l1 = next(ln for ln in lines if ln.startswith("1 25544"))
    l2 = next(ln for ln in lines if ln.startswith("2 25544"))
    return l1, l2


def predict_iss_passes(days=21, force=False):
    now = time.time()
    with SPACE_CACHE_LOCK:
        cache = _atomic_json_read(SPACE_CACHE_PATH, {})
        cached_iss = cache.get("iss_scan") or {}
        if not force and cached_iss and (now - cached_iss.get("fetched_at", 0)) < 21600 and cached_iss.get("scan_end", 0) > now + 86400:
            passes = [p for p in cached_iss.get("passes", []) if p.get("end", 0) > now]
            return passes

    l1, l2 = _fetch_iss_tle()
    tp = _parse_iss_tle(l1, l2)
    obs = _obs_frame(HOME_LAT, HOME_LON, 0.14)
    start_ts = int(now)
    end_ts = start_ts + days * 86400

    # Two-stage scan: 60s coarse scan, then 10s edge/peak refinement per pass
    sin_10 = math.sin(math.radians(10.0))
    brackets = []
    b_start = None
    for t in range(start_ts, end_ts, 60):
        sat_ef, _, _ = _iss_pos_sun_at(tp, t)
        dx, dy, dz = sat_ef[0] - obs[0], sat_ef[1] - obs[1], sat_ef[2] - obs[2]
        zen = obs[4] * obs[6] * dx + obs[4] * obs[5] * dy + obs[3] * dz
        if zen > 0 and (zen * zen) >= (sin_10 * sin_10) * (dx * dx + dy * dy + dz * dz):
            if b_start is None:
                b_start = max(start_ts, t - 60)
        else:
            if b_start is not None:
                brackets.append((b_start, min(end_ts, t + 60)))
                b_start = None
    if b_start is not None:
        brackets.append((b_start, end_ts))

    passes = []
    for bs, be in brackets:
        cur = None
        for t in range(bs, be + 1, 10):
            sat_ef, lit, sun_ef = _iss_pos_sun_at(tp, t)
            el, az, sun_el, _ = _topocentric_metrics(sat_ef, sun_ef, obs)
            if el >= 10.0:
                if cur is None:
                    cur = {
                        "start": t,
                        "start_az": az,
                        "max_el": el,
                        "max_t": t,
                        "max_az": az,
                        "end": t,
                        "end_az": az,
                        "vis_pts": 0,
                        "sun_el": sun_el,
                        "lit": lit,
                    }
                if el > cur["max_el"]:
                    cur["max_el"] = round(el, 1)
                    cur["max_t"] = t
                    cur["max_az"] = round(az, 1)
                    cur["sun_el"] = round(sun_el, 1)
                    cur["lit"] = lit
                cur["end"] = t
                cur["end_az"] = round(az, 1)
                if lit and sun_el <= -5.5:
                    cur["vis_pts"] += 1
            elif cur is not None:
                cur["start_az"] = round(cur["start_az"], 1)
                cur["max_el"] = round(cur["max_el"], 1)
                cur["visible"] = cur["vis_pts"] >= 2
                passes.append(cur)
                cur = None
        if cur is not None:
            cur["start_az"] = round(cur["start_az"], 1)
            cur["max_el"] = round(cur["max_el"], 1)
            cur["visible"] = cur["vis_pts"] >= 2
            passes.append(cur)

    with SPACE_CACHE_LOCK:
        cache = _atomic_json_read(SPACE_CACHE_PATH, {})
        cache["iss_scan"] = {"fetched_at": now, "scan_end": end_ts, "passes": passes}
        _atomic_json_write(SPACE_CACHE_PATH, cache, prefix="sp_")
    return [p for p in passes if p.get("end", 0) > now]


def fetch_live_iss():
    req = urllib.request.Request("https://api.wheretheiss.at/v1/satellites/25544", headers={"User-Agent": "Mozilla/5.0 vickyhome"})
    with urllib.request.urlopen(req, timeout=6) as r:
        d = json.loads(r.read().decode("utf-8"))
    lat = float(d["latitude"])
    lon = float(d["longitude"])
    alt = float(d["altitude"])
    vel = int(round(float(d["velocity"])))
    dist_km, compass = _haversine_km_bearing(HOME_LAT, HOME_LON, lat, lon)
    return {
        "lat": lat,
        "lon": lon,
        "alt_km": round(alt, 1),
        "speed_kmh": vel,
        "dist_km": int(round(dist_km)),
        "compass": compass,
        "visibility": d.get("visibility") or "unknown",
    }


def _parse_utc_iso(iso_s):
    if not iso_s:
        return None
    try:
        dt = datetime.datetime.strptime(iso_s[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return None


def _simplify_launch(item):
    return {
        "id": item.get("id") or "",
        "name": item.get("name") or "Unknown Mission",
        "lsp": (item.get("launch_service_provider") or {}).get("name") or "",
        "pad": ((item.get("pad") or {}).get("location") or {}).get("name") or "",
        "net_ts": _parse_utc_iso(item.get("net")),
        "status": (item.get("status") or {}).get("abbrev") or "?",
    }


def fetch_launches(force=False):
    now = time.time()
    with SPACE_CACHE_LOCK:
        cache = _atomic_json_read(SPACE_CACHE_PATH, {})
        cached_l = cache.get("launches") or {}
        if not force and cached_l and (now - cached_l.get("fetched_at", 0)) < 14400:
            return cached_l

    hdrs = {"User-Agent": "Mozilla/5.0 vickyhome"}
    isro_list = []
    with urllib.request.urlopen(urllib.request.Request("https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=5&location__ids=14", headers=hdrs), timeout=10) as r:
        for it in (json.loads(r.read().decode("utf-8")).get("results") or []):
            isro_list.append(_simplify_launch(it))

    res = {"fetched_at": now, "isro": isro_list}
    with SPACE_CACHE_LOCK:
        cache = _atomic_json_read(SPACE_CACHE_PATH, {})
        cache["launches"] = res
        _atomic_json_write(SPACE_CACHE_PATH, cache, prefix="sp_")
    return res


def format_iss_pass_line(p):
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    st = datetime.datetime.fromtimestamp(p["start"], ist).strftime("%a %d %b, %I:%M %p")
    et = datetime.datetime.fromtimestamp(p["end"], ist).strftime("%I:%M %p")
    dur = max(10, int(p["end"] - p["start"]))
    az_path = f"{_deg_to_compass(p['start_az'])} -> {_deg_to_compass(p['end_az'])}"
    return f"{st} - {et} IST ({dur // 60}m {dur % 60}s) | Max {p['max_el']:.0f} deg | {az_path}"


def get_briefing_sky_note():
    now = time.time()
    parts = []
    try:
        passes = [
            p for p in predict_iss_passes(days=21)
            if p.get("visible") and p.get("max_el", 0) >= 25.0 and 0 < (p["start"] - now) <= 86400
        ]
        if passes:
            parts.append(f"Visible ISS pass over Home: {format_iss_pass_line(passes[0])}")
    except Exception:
        pass
    try:
        ln = fetch_launches()
        for x in (ln.get("isro") or []):
            nt = x.get("net_ts")
            if nt and 0 < (nt - now) <= 86400 and x.get("status") in ("Go", "TBC"):
                parts.append(f"ISRO launch in {_fmt_countdown(nt - now)}: {x['name']}")
                break
    except Exception:
        pass
    return " | ".join(parts)


def cmd_sky(_cfg):
    now = time.time()
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    lines = ["Chennai Sky Report ", ""]

    _, sky_lines = fetch_local_weather_and_sun()
    lines.append("Sun & Moon Today")
    lines.extend(sky_lines)
    lines.append("")

    lines.append("ISS Over Home Now")
    try:
        live = fetch_live_iss()
        if live["dist_km"] <= 1600:
            lines.append(
                f"- OVERHEAD SKY: {live['dist_km']:,} km {live['compass']} of Chennai "
                f"({live['alt_km']} km alt, {live['speed_kmh']:,} km/h, {live['visibility']})"
            )
        else:
            lines.append("- Not over Home horizon right now")
    except Exception as e:
        lines.append(f"- ISS check unavailable ({e})")

    lines.append("")
    lines.append("Visible ISS Passes Over Home (Max >= 25 deg)")
    try:
        all_p = predict_iss_passes(days=21)
        vis_p = [
            p for p in all_p
            if p.get("visible") and p.get("max_el", 0) >= 25.0 and p.get("start", 0) > now
        ]
        if vis_p:
            for p in vis_p[:3]:
                lines.append(f"- {format_iss_pass_line(p)} (in {_fmt_countdown(p['start'] - now)})")
        else:
            lines.append("- None in the next 21 days (orbits hit daylight/Earth shadow)")
            hi_p = [p for p in all_p if p.get("max_el", 0) >= 30.0 and p.get("start", 0) > now]
            if hi_p:
                nx = hi_p[0]
                why = "daytime" if nx.get("sun_el", 0) > -5.5 else "Earth shadow"
                lines.append(f"- Next non-visible Local pass ({why}): {format_iss_pass_line(nx)}")
    except Exception as e:
        lines.append(f"- ISS pass scan unavailable ({e})")

    lines.append("")
    lines.append("ISRO Launches (Sriharikota East Sky)")
    try:
        ln = fetch_launches()
        isro_items = [x for x in (ln.get("isro") or []) if not x.get("net_ts") or x["net_ts"] > now - 1800]
        if isro_items:
            for x in isro_items[:3]:
                nt = x.get("net_ts")
                t_str = datetime.datetime.fromtimestamp(nt, ist).strftime("%d %b %Y, %I:%M %p IST") if nt else "TBD"
                if x.get("status") == "TBD" and nt:
                    t_str = datetime.datetime.fromtimestamp(nt, ist).strftime("~%b %Y (TBD)")
                lines.append(f"- {x['name']} | {t_str} [{x['status']}]")
        else:
            lines.append("- No upcoming Sriharikota launches listed")
    except Exception as e:
        lines.append(f"- ISRO schedule unavailable ({e})")

    return "\n".join(lines)


def space_alert_worker(cfg, token, chat_id):
    time.sleep(15)
    while True:
        try:
            live_cfg = load_conf()
            if live_cfg.get("SPACE_ALERTS", "1") == "1":
                now = time.time()
                with SPACE_CACHE_LOCK:
                    cache = _atomic_json_read(SPACE_CACHE_PATH, {})
                alerted_iss = [ts for ts in cache.get("alerted_iss", []) if now - ts < 7 * 86400]
                alerted_ln = dict(cache.get("alerted_launches", {}))

                for p in predict_iss_passes(days=21):
                    if not p.get("visible") or p.get("max_el", 0) < 25.0:
                        continue
                    dt_s = p["start"] - now
                    if 300 <= dt_s <= 1800 and not any(abs(a - p["start"]) < 900 for a in alerted_iss):
                        msg = (
                            f"ISS Visible Pass Over Chennai in ~{max(1, int(round(dt_s / 60)))} min!\n\n"
                            f"Window: {format_iss_pass_line(p)}\n"
                            f"Look {_deg_to_compass(p['start_az'])} -> {_deg_to_compass(p['end_az'])} (clear dark sky)."
                        )
                        send_message(token, chat_id, msg)
                        alerted_iss.append(p["start"])

                ln = fetch_launches()
                for x in (ln.get("isro") or []):
                    lid = x.get("id")
                    nt = x.get("net_ts")
                    if lid and nt and x.get("status") == "Go":
                        dt_l = nt - now
                        if 900 <= dt_l <= 5400 and lid not in alerted_ln:
                            ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
                            t_str = datetime.datetime.fromtimestamp(nt, ist).strftime("%I:%M %p IST")
                            msg = (
                                f"ISRO Rocket Launch in ~{max(1, int(round(dt_l / 60)))} min!\n\n"
                                f"Mission: {x['name']}\n"
                                f"Pad: {x.get('pad') or 'Satish Dhawan Space Centre'}\n"
                                f"Liftoff (NET): {t_str} [Go]"
                            )
                            send_message(token, chat_id, msg)
                            alerted_ln[lid] = int(now)

                # Prune old launch keys (> 30 days)
                alerted_ln = {k: v for k, v in alerted_ln.items() if now - v < 30 * 86400}
                with SPACE_CACHE_LOCK:
                    c2 = _atomic_json_read(SPACE_CACHE_PATH, {})
                    c2["alerted_iss"] = alerted_iss
                    c2["alerted_launches"] = alerted_ln
                    _atomic_json_write(SPACE_CACHE_PATH, c2, prefix="sp_")
        except Exception as e:
            _log_throttled("space_alert", f"[space alert error] {e}", min_interval=300)
        time.sleep(300)


def cmd_security_status(cfg):
    return SECURITY_MONITOR.status_text(cfg)


COMMANDS = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/security_status": cmd_security_status,
    "/flights": cmd_flights,
    "/recap": cmd_flight_recap,
    "/flightrecap": cmd_flight_recap,
    "/sky": cmd_sky,
    "/iss": cmd_sky,
    "/space": cmd_sky,
    "/launches": cmd_sky,
    "/briefing": cmd_briefing,
    "/status": cmd_status,
    "/temp": cmd_temp,
    "/ram": cmd_ram,
    "/mem": cmd_ram,
    "/top": cmd_top,
    "/disk": cmd_disk,
    "/storage": cmd_disk,
    "/services": cmd_services,
    "/adguard": cmd_adguard,
}

LAST_CMD_TS = 0.0
AI_HISTORY = deque(maxlen=6)

AI_SYSTEM_PROMPT = (
    "You are a helpful, concise personal AI assistant running on the user's "
    "vickyhome Telegram bot. Keep replies clear and under 1500 characters unless "
    "more detail is asked for. You have no access to server files, passwords, "
    "IP addresses, or shell commands."
)


def _reply_with_photo(token, owner_chat_id, capture_fn, caption):
    try:
        tg_api(token, "sendChatAction", {"chat_id": owner_chat_id, "action": "upload_photo"}, timeout=5)
    except Exception:
        pass
    jpg_bytes, err = capture_fn()
    if jpg_bytes is None:
        send_message(token, owner_chat_id, err or "Camera capture failed.")
        return
    try:
        send_photo(token, owner_chat_id, jpg_bytes, caption=caption)
    except Exception as e:
        print(f"[sendPhoto error] {e}", file=sys.stderr, flush=True)
        send_message(token, owner_chat_id, f"Failed to send photo ({e}).")


def ask_gemini(cfg, prompt):
    api_key = cfg.get("GEMINI_API_KEY", "")
    if not api_key:
        return "GEMINI_API_KEY is not configured."

    primary = cfg.get("GEMINI_MODEL", "gemini-2.5-flash")
    models = []
    for m in (primary, "gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-flash-latest"):
        if m and m not in models:
            models.append(m)

    contents = list(AI_HISTORY) + [{"role": "user", "parts": [{"text": prompt}]}]
    payload = {
        "systemInstruction": {"parts": [{"text": AI_SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.7},
    }
    body = json.dumps(payload).encode("utf-8")

    last_err = "Unknown error"
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8"))
            cands = data.get("candidates") or []
            if not cands:
                return "AI returned no response (blocked or empty)."
            parts = cands[0].get("content", {}).get("parts") or []
            answer = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if not answer:
                answer = "".join(p.get("text", "") for p in parts).strip()
            if not answer:
                return "AI returned an empty answer."
            AI_HISTORY.append({"role": "user", "parts": [{"text": prompt}]})
            AI_HISTORY.append({"role": "model", "parts": [{"text": answer}]})
            return answer
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")[:180]
            except Exception:
                pass
            last_err = f"HTTP {e.code}"
            print(f"[gemini error {model}] {e.code} {err_body}", file=sys.stderr, flush=True)
            if e.code in (401, 403):
                return "AI API key rejected (401/403). Please check Gemini API key."
            if e.code not in (404, 429, 500, 503):
                break
        except Exception as e:
            last_err = str(e)
            print(f"[gemini exc {model}] {e}", file=sys.stderr, flush=True)
    return f"AI is temporarily busy/offline ({last_err}). Try again shortly."


def handle_message(cfg, token, owner_chat_id, msg):
    global LAST_CMD_TS
    chat = msg.get("chat", {})
    frm = msg.get("from", {})

    # Strict private-chat owner-only authorization and anti-forward guard.
    if (
        chat.get("type") != "private"
        or str(chat.get("id", "")) != owner_chat_id
        or str(frm.get("id", "")) != owner_chat_id
        or frm.get("is_bot", False)
        or "forward_from" in msg
        or "forward_sender_name" in msg
        or "via_bot" in msg
    ):
        print(
            f"[ignored unauthorized] type={chat.get('type')} chat={chat.get('id')} from={frm.get('id')}",
            file=sys.stderr,
            flush=True,
        )
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return
    if len(text) > 1500:
        send_message(token, owner_chat_id, "Message too long (max 1500 characters).")
        return

    now = time.monotonic()
    if now - LAST_CMD_TS < 1.0:
        return
    LAST_CMD_TS = now

    parts = text.split(None, 1)
    head = parts[0].lower().split("@", 1)[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    norm_text = " ".join(text.lower().split())

    ai_prompt = None
    if head in ("hi", "hello", "hey", "menu", "?"):
        reply = cmd_help(cfg)
    elif head in ("/security_on", "/security") or norm_text in ("security", "security on"):
        _, reply = SECURITY_MONITOR.arm(cfg, token, owner_chat_id)
    elif head == "/security_off" or norm_text == "security off":
        reply = SECURITY_MONITOR.disarm()
    elif norm_text == "security status":
        reply = cmd_security_status(cfg)
    elif head == "/snapshot":
        stamp = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
        _reply_with_photo(
            token,
            owner_chat_id,
            lambda: SECURITY_MONITOR.capture_instant_snapshot(cfg),
            f"vickyhome Security Webcam Snapshot ({stamp})",
        )
        return
    elif head in ("/snap", "/cam", "/photo"):
        dev = cfg.get("SNAP_DEVICE", DEFAULT_SNAP_DEVICE)
        stamp = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
        _reply_with_photo(
            token,
            owner_chat_id,
            lambda: capture_snapshot(device=dev),
            f"vickyhome Built-in Camera Snapshot ({stamp})",
        )
        return
    elif head == "/clear":
        AI_HISTORY.clear()
        reply = "AI conversation memory cleared."
    elif head == "/ai":
        if not rest:
            reply = "Usage: /ai <your question>\nTip: you can also send normal text without / to ask AI!"
        else:
            ai_prompt = rest
    elif head in COMMANDS:
        reply = COMMANDS[head](cfg)
    elif text.startswith("/"):
        reply = "Unknown command.\nSend /help to see allowed commands."
    else:
        ai_prompt = text

    if ai_prompt is not None:
        try:
            tg_api(token, "sendChatAction", {"chat_id": owner_chat_id, "action": "typing"}, timeout=5)
        except Exception:
            pass
        reply = ask_gemini(cfg, ai_prompt)

    send_message(token, owner_chat_id, reply)


def _send_scheduled_report(token, chat_id, text, label):
    for attempt in range(3):
        try:
            tg_api(token, "sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)
            return
        except Exception as e:
            print(f"[{label} send error attempt={attempt + 1}] {e}", file=sys.stderr, flush=True)
            time.sleep(5)
    sys.exit(1)


def main():
    cfg = load_conf()
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = cfg.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in /etc/vickyhome-tgbot.conf", file=sys.stderr, flush=True)
        time.sleep(30)
        sys.exit(1)

    if "--send-briefing" in sys.argv[1:]:
        _send_scheduled_report(token, chat_id, cmd_briefing(cfg), "briefing")
        return

    if "--send-flight-recap" in sys.argv[1:]:
        _send_scheduled_report(token, chat_id, build_flight_recap(), "flight-recap")
        return

    _set_security_armed_flag(False)

    for _ in range(6):
        try:
            tg_api(token, "setMyCommands", {"commands": BOT_COMMANDS}, timeout=10)
            break
        except Exception:
            time.sleep(5)

    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            up_sec = float(f.read().split()[0])
        if up_sec < 300:
            send_message(token, chat_id, "vickyhome is back online!\nSend /status or /help.")
    except Exception:
        pass

    threading.Thread(
        target=flight_alert_worker,
        args=(cfg, token, chat_id),
        daemon=True,
    ).start()

    threading.Thread(
        target=space_alert_worker,
        args=(cfg, token, chat_id),
        daemon=True,
    ).start()

    offset = load_offset()
    poll_backoff = 3
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            res = tg_api(token, "getUpdates", params, timeout=35)
            poll_backoff = 3
            for upd in res.get("result", []):
                uid = upd.get("update_id")
                if uid is not None:
                    offset = uid + 1
                    save_offset(offset)
                msg = upd.get("message")
                if msg:
                    handle_message(cfg, token, chat_id, msg)
        except urllib.error.HTTPError as e:
            _log_throttled("tg_http", f"[tg http error] {e.code} {e.reason}", min_interval=120)
            time.sleep(poll_backoff)
            poll_backoff = min(poll_backoff * 2, 30)
        except Exception as e:
            _log_throttled("tg_poll", f"[tg poll error] {e}", min_interval=120)
            time.sleep(poll_backoff)
            poll_backoff = min(poll_backoff * 2, 30)


if __name__ == "__main__":
    main()
