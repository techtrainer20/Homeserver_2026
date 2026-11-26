#!/usr/bin/env python3
"""
newdash25.py (v1.2) - Redesigned, Modular, Low-overhead Fullscreen Dashboard
Engineered for Lenovo S10e (Debian i686, 1024x576 resolution, ~1GB RAM).
Strictly BMP-compliant (U+0000 to U+FFFF) for Debian 32-bit Tcl/Tk.
"""

DASHBOARD_VERSION = "1.2"

import os
import sys
import time
import glob
import math
import json
import random
import shutil
import socket
import socketserver
import urllib.parse
import threading
import subprocess
import datetime
from io import BytesIO
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import tkinter as tk
from PIL import Image, ImageTk, ImageOps, ImageEnhance, ImageFilter
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import feedparser

# Prefer IPv4 for outbound HTTPS because tailscale0 advertises an IPv6 address while ISP IPv6 is unreachable.
_ORIG_GETADDRINFO = socket.getaddrinfo

def _ipv4_first_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    res = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 if v4 else res

socket.getaddrinfo = _ipv4_first_getaddrinfo

def _build_http_session():
    sess = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=1,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12, max_retries=retry)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    sess.headers.update({"User-Agent": "Mozilla/5.0 homeserver-dashboard"})
    return sess

HTTP_SESSION = _build_http_session()
_NET_ERR_LOCK = threading.Lock()
_NET_ERR_LAST = {}

def log_net_error(key, msg, min_interval=180):
    now = time.time()
    with _NET_ERR_LOCK:
        prev_ts, suppressed = _NET_ERR_LAST.get(key, (0.0, 0))
        if now - prev_ts < min_interval:
            _NET_ERR_LAST[key] = (prev_ts, suppressed + 1)
            return
        _NET_ERR_LAST[key] = (now, 0)
    suffix = f" (suppressed {suppressed} similar)" if suppressed else ""
    log_msg(f"{msg}{suffix}")

# BMP text sanitizer for 32-bit Debian Tcl/Tk compatibility
def safe_tcl(text):
    if not isinstance(text, str):
        text = str(text)
    return "".join(c for c in text if ord(c) <= 0xFFFF)

# ================= PATHS & LOGGING =================
HOME_DIR = os.path.expanduser("~")
CONFIG_PATH = os.path.join(HOME_DIR, "dashboard_config.json")
EVENTS_PATH = os.path.join(HOME_DIR, "dashboard_events.json")
CACHE_DIR = os.path.join(HOME_DIR, ".cache", "dashboard")
MAP_TILE_CACHE = os.path.join(CACHE_DIR, "map_tiles")
LOG_PATH = os.path.join(HOME_DIR, "dash.log")
ICON_DIR = os.path.join(HOME_DIR, "weather_icons")
IMG_CACHE_DIR = os.path.join(HOME_DIR, "slides")
IMG_LOCAL_DIR = os.path.join(HOME_DIR, "slides_local")

for d in [CACHE_DIR, MAP_TILE_CACHE, ICON_DIR, IMG_CACHE_DIR]:
    Path(d).mkdir(parents=True, exist_ok=True)

def log_msg(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
            try:
                with open(LOG_PATH, "rb") as f:
                    f.seek(-200 * 1024, os.SEEK_END)
                    tail = f.read()
                with open(LOG_PATH, "wb") as f:
                    f.write(b"...[truncated]...\n" + tail)
            except Exception:
                pass
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

# ================= CONFIGURATION MANAGER =================
DEFAULT_CONFIG = {
    "location_name": "Chennai",
    "latitude": 13.0827,
    "longitude": 80.2707,
    "time_format_24h": False,
    "temperature_unit": "C",
    "theme": "auto",
    "weather_accent": True,
    "pages_enabled": ["main", "weather", "system", "money", "sky", "flight", "photos"],
    "page_cycle_seconds": 180,
    "extra_page_display_seconds": 15,
    "night_mode": {
        "enabled": True,
        "start_hour": 23,
        "end_hour": 6,
        "brightness_day": 1.0,
        "brightness_evening": 0.85,
        "brightness_night": 0.5
    },
    "phone_control": {
        "enabled": True,
        "port": 8765,
        "token": "CHANGE_ME_TOKEN"
    },
    "slideshow": {
        "interval_seconds": 15,
        "server_photo_interval_seconds": 8,
        "local_folder": "~/slides_local",
        "cache_folder": "~/slides",
        "max_cached_files": 10,
        "crossfade_steps": 8
    },
    "news": {
        "read_time_seconds": 10,
        "feeds": {
            "WORLD": "http://feeds.bbci.co.uk/news/world/rss.xml",
            "TECH": "http://feeds.bbci.co.uk/news/technology/rss.xml",
            "INDIA": "https://www.thehindu.com/news/national/feeder/default.rss",
            "TOI TECH": "https://timesofindia.indiatimes.com/rssfeeds/66949542.cms"
        },
        "max_per_feed": 5,
        "filter_keywords_hide": [
            "horoscope", "astrology", "cinema", "kollywood", "bollywood",
            "tollywood", "movie", "film", "trailer", "teaser", "box office",
            "actor", "actress", "bigg boss", "celebrity", "gossip", "ott",
            "series review", "music launch"
        ],
        "filter_keywords_highlight": ["India", "Tamil Nadu", "Chennai", "AI", "Space", "ISRO", "Breaking"]
    },
    "alerts": {
        "rain_alert_hours": 2,
        "heat_index_threshold_c": 38.0,
        "aqi_poor_threshold": 100,
        "battery_low_threshold": 20
    },
    "iot_energy": {
        "enabled": False,
        "url": "http://192.168.1.150/energy",
        "cost_per_kwh": 6.5
    },
    "adguard": {
        "enabled": True,
        "url": "http://127.0.0.1:8081/control/stats",
        "username": "admin",
        "password": ""
    },
    "flights": {
        "enabled": True,
        "radius_km": 10,
        "radius_nm": 5.4,
        "poll_seconds": 20,
        "max_altitude_ft": 50000
    },
    "sky_dome": {
        "enabled": True,
        "default_visible": False,
        "min_max_el_deg": 25.0,
        "auto_show_minutes": 20,
        "live_overhead_km": 1600
    }
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            log_msg("Created default dashboard_config.json")
        except Exception as e:
            log_msg(f"Failed to create default config: {e}")
        return DEFAULT_CONFIG

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = DEFAULT_CONFIG.copy()
        merged.update(cfg)
        return merged
    except Exception as e:
        log_msg(f"Error loading config, using defaults: {e}")
        return DEFAULT_CONFIG

CONFIG = load_config()

# Pillow resample compatibility
try:
    RESAMPLE_MODE = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_MODE = Image.ANTIALIAS

ICON_URLS = {
    "clear_day": "https://openweathermap.org/img/wn/01d@4x.png",
    "clear_night": "https://openweathermap.org/img/wn/01n@4x.png",
    "cloud": "https://openweathermap.org/img/wn/03d@4x.png",
    "rain": "https://openweathermap.org/img/wn/10d@4x.png",
    "storm": "https://openweathermap.org/img/wn/11d@4x.png",
    "snow": "https://openweathermap.org/img/wn/13d@4x.png",
    "mist": "https://openweathermap.org/img/wn/50d@4x.png"
}

# ================= THEMES =================
THEMES = {
    "day": {
        "bg": "#0B1120",
        "card_bg": "#151F32",
        "card_border": "#22324F",
        "fg": "#F8FAFC",
        "fg_sub": "#94A3B8",
        "accent": "#38BDF8",
        "news_bg": "#070C16",
        "alert_bg": "#DC2626"
    },
    "evening": {
        "bg": "#121118",
        "card_bg": "#1E1C27",
        "card_border": "#312E40",
        "fg": "#FAF5FF",
        "fg_sub": "#A1A1AA",
        "accent": "#F59E0B",
        "news_bg": "#0B0A0F",
        "alert_bg": "#DC2626"
    },
    "night": {
        "bg": "#000000",
        "card_bg": "#0A0A0A",
        "card_border": "#1A1A1A",
        "fg": "#E4E4E7",
        "fg_sub": "#71717A",
        "accent": "#D97706",
        "news_bg": "#000000",
        "alert_bg": "#991B1B"
    },
    "oled": {
        "bg": "#000000",
        "card_bg": "#05070B",
        "card_border": "#142033",
        "fg": "#F1F5F9",
        "fg_sub": "#64748B",
        "accent": "#38BDF8",
        "news_bg": "#000000",
        "alert_bg": "#7F1D1D"
    }
}

SECURITY_STATE_FILE = "/var/lib/vickyhome-tgbot/security_state.json"
CORE_SERVICE_SPECS = [
    ("AGH", "AdGuardHome.service"),
    ("FB", "filebrowser.service"),
    ("CP", "copyparty.service"),
    ("SMB", "smbd.service"),
    ("TS", "tailscaled.service"),
    ("BOT", "vickyhome-telegram-bot.service"),
]

# ================= STATE HOLDER =================
class DashboardState:
    def __init__(self):
        self.lock = threading.Lock()
        self.weather = {
            "temp": "--",
            "feels": "--",
            "temp_max": "--",
            "temp_min": "--",
            "humidity": "--",
            "wind": "--",
            "uv": "--",
            "aqi": "--",
            "cond": "Loading...",
            "icon_name": "clear_day",
            "sunrise": "--:--",
            "sunset": "--:--",
            "rain_chance": "--",
            "hourly": [],
            "daily": [],
            "updated_time": "--:--"
        }
        self.news = []
        self.system = {
            "cpu_percent": 0.0,
            "cpu_temp": 0.0,
            "ram_percent": 0.0,
            "ram_used_mb": 0,
            "ram_total_mb": 0,
            "disk_percent": 0.0,
            "disk_free_gb": 0.0,
            "uptime_str": "--",
            "battery_status": "AC",
            "battery_percent": 100,
            "wifi_signal": "--",
            "router_ping": "--",
            "inet_ping": "--",
            "tailscale_status": "Off",
            "docker_count": 0,
            "ads_blocked": "--",
            "ads_percent": "--",
            "dns_queries": "--",
            "lan_ip": "--",
            "net_rx_kbs": 0.0,
            "net_tx_kbs": 0.0,
            "net_history": [0.0] * 20,
            "usb_status": "Not mounted",
            "load_avg": "--",
            "services": {},
            "security_armed": False,
            "security_events": 0,
            "security_last": "",
            "cpu_history": [0.0] * 20,
            "ram_history": [0.0] * 20
        }
        self.finance = {
            "usd_inr": "--",
            "eur_inr": "--",
            "gbp_inr": "--",
            "updated_time": "--:--"
        }
        self.quote = {
            "text": "The secret of getting ahead is getting started.",
            "author": "Mark Twain"
        }
        self.events = []
        self.alerts = []
        self.user_message = None
        self.user_message_until = 0
        self.timer_seconds_remaining = None
        self.timer_is_ringing = False
        self.active_page = 0
        self.rotation_paused = False
        self.night_mode_active = False
        self.health_warning = None
        self.last_ui_tick = time.time()
        self.skip_photo_requested = False
        self.flights = []
        self.preview_flights = []
        self.last_flight_seen = None
        self.flight_map_toggle_req = False
        self.page_switch_req = None
        self.theme_switch_req = None
        self.time_fmt_toggle_req = False
        self.brightness_level = 1.0
        self.space = {
            "live": None,
            "passes": [],
            "next_shadow": None,
            "isro": [],
            "updated_at": 0.0
        }
        self.sky_dome_toggle_req = False
        self.sky_pass_cycle_req = False

STATE = DashboardState()

# Moon phase calculation using BMP characters only
def get_moon_phase(dt=None):
    if dt is None:
        dt = datetime.datetime.now()
    diff = dt - datetime.datetime(2000, 1, 6, 18, 14)
    days = diff.total_seconds() / 86400.0
    phase_days = days % 29.53058867
    index = int((phase_days / 29.53058867) * 8 + 0.5) % 8
    phases = [
        ("New Moon", "O"),
        ("Waxing Crescent", ")"),
        ("First Quarter", "(|"),
        ("Waxing Gibbous", "(O"),
        ("Full Moon", "(*)"),
        ("Waning Gibbous", "O)"),
        ("Last Quarter", "|)"),
        ("Waning Crescent", "(")
    ]
    return phases[index]

# ================= OFFLINE CACHE HELPERS =================
def save_cache(name, data):
    try:
        fpath = os.path.join(CACHE_DIR, f"{name}.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        log_msg(f"Failed to save cache {name}: {e}")

def load_cache(name):
    try:
        fpath = os.path.join(CACHE_DIR, f"{name}.json")
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log_msg(f"Failed to load cache {name}: {e}")
    return None

# ================= BRIGHTNESS & DISPLAY MANAGER =================
def get_xrandr_output():
    try:
        out = subprocess.check_output("xrandr", shell=True, stderr=subprocess.DEVNULL).decode()
        for line in out.splitlines():
            if " connected " in line:
                return line.split()[0]
    except Exception:
        pass
    return "LVDS1"

XRANDR_OUTPUT = get_xrandr_output()

def set_screen_brightness(level):
    try:
        lvl = max(0.2, min(1.0, float(level)))
        with STATE.lock:
            STATE.brightness_level = round(lvl, 2)
        subprocess.run(
            f"xrandr --output {XRANDR_OUTPUT} --brightness {lvl:.2f}",
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        log_msg(f"Brightness set error: {e}")

# ================= BACKGROUND WORKERS =================
def download_weather_icons():
    headers = {"User-Agent": "Mozilla/5.0"}
    for name, url in ICON_URLS.items():
        fname = os.path.join(ICON_DIR, f"{name}.png")
        if not os.path.exists(fname) or os.path.getsize(fname) == 0:
            try:
                r = HTTP_SESSION.get(url, headers=headers, timeout=(5, 8))
                if r.status_code == 200:
                    with open(fname, "wb") as f:
                        f.write(r.content)
            except Exception:
                pass

def weather_worker():
    headers = {"User-Agent": "Mozilla/5.0"}
    download_weather_icons()

    cached = load_cache("weather")
    if cached:
        with STATE.lock:
            STATE.weather.update(cached)

    lat = CONFIG["latitude"]
    lon = CONFIG["longitude"]
    weather_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m"
        "&hourly=temperature_2m,precipitation_probability,weather_code,uv_index"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max"
        "&timezone=auto&forecast_days=6"
    )
    aqi_url = (
        f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}"
        "&current=european_aqi,us_aqi,pm2_5"
    )
    fail_backoff = 30

    while True:
        sleep_sec = 900
        try:
            r = HTTP_SESSION.get(weather_url, headers=headers, timeout=(5, 10))
            r.raise_for_status()
            data = r.json()

            curr = data.get("current", {})
            hourly = data.get("hourly", {})
            daily = data.get("daily", {})

            temp = curr.get("temperature_2m", "--")
            feels = curr.get("apparent_temperature", "--")
            code = curr.get("weather_code", 0)
            is_day = curr.get("is_day", 1)
            hum = curr.get("relative_humidity_2m", "--")
            wind = curr.get("wind_speed_10m", "--")

            cond_text = "Clear Sky"
            icon_name = "clear_day" if is_day else "clear_night"
            if code in [1, 2, 3]:
                cond_text = "Partly Cloudy"
                icon_name = "cloud_day" if is_day else "cloud_night"
            elif code in [45, 48]:
                cond_text = "Foggy"
                icon_name = "mist"
            elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
                cond_text = "Rain"
                icon_name = "rain"
            elif code >= 95:
                cond_text = "Thunderstorm"
                icon_name = "storm"
            elif code in [71, 73, 75, 77, 85, 86]:
                cond_text = "Snow"
                icon_name = "snow"

            daily_list = []
            if daily and "time" in daily:
                days = daily.get("time", [])
                codes = daily.get("weather_code", [])
                maxs = daily.get("temperature_2m_max", [])
                mins = daily.get("temperature_2m_min", [])
                for i in range(min(5, len(days))):
                    d_obj = datetime.datetime.strptime(days[i], "%Y-%m-%d")
                    day_label = "Today" if i == 0 else d_obj.strftime("%a")
                    daily_list.append({
                        "day": day_label,
                        "code": codes[i] if i < len(codes) else 0,
                        "max": round(maxs[i]) if i < len(maxs) and maxs[i] is not None else "--",
                        "min": round(mins[i]) if i < len(mins) and mins[i] is not None else "--"
                    })

            hourly_list = []
            rain_within_2h = False
            if hourly and "time" in hourly:
                now_iso = datetime.datetime.now().strftime("%Y-%m-%dT%H:00")
                times = hourly.get("time", [])
                temps = hourly.get("temperature_2m", [])
                probs = hourly.get("precipitation_probability", [])

                start_idx = 0
                for idx, t_str in enumerate(times):
                    if t_str >= now_iso:
                        start_idx = idx
                        break

                for i in range(start_idx, min(start_idx + 12, len(times))):
                    t_val = temps[i] if i < len(temps) else "--"
                    p_val = probs[i] if i < len(probs) and probs[i] is not None else 0
                    t_hour = times[i].split("T")[1][:5] if "T" in times[i] else "--"
                    hourly_list.append({
                        "hour": t_hour,
                        "temp": round(t_val) if isinstance(t_val, (int, float)) else t_val,
                        "rain_prob": p_val
                    })
                    if (i - start_idx) < 2 and p_val >= 50:
                        rain_within_2h = True

            aqi_val = "--"
            try:
                r_aqi = HTTP_SESSION.get(aqi_url, headers=headers, timeout=(4, 6))
                if r_aqi.status_code == 200:
                    aqi_data = r_aqi.json().get("current", {})
                    aqi_val = aqi_data.get("european_aqi", aqi_data.get("us_aqi", "--"))
            except Exception:
                pass

            sunrise_str = "--:--"
            sunset_str = "--:--"
            uv_max = "--"
            if daily and "sunrise" in daily and len(daily["sunrise"]) > 0:
                s_raw = daily["sunrise"][0]
                sunrise_str = s_raw.split("T")[1][:5] if "T" in s_raw else s_raw
            if daily and "sunset" in daily and len(daily["sunset"]) > 0:
                s_raw = daily["sunset"][0]
                sunset_str = s_raw.split("T")[1][:5] if "T" in s_raw else s_raw
            if daily and "uv_index_max" in daily and len(daily["uv_index_max"]) > 0:
                uv_max = str(round(daily["uv_index_max"][0], 1))

            now_time = datetime.datetime.now().strftime("%H:%M")
            weather_update = {
                "temp": f"{temp}°C" if temp != "--" else "--",
                "feels": f"{feels}°C" if feels != "--" else "--",
                "temp_max": f"{daily_list[0]['max']}°C" if daily_list and daily_list[0]['max'] != '--' else "--",
                "temp_min": f"{daily_list[0]['min']}°C" if daily_list and daily_list[0]['min'] != '--' else "--",
                "humidity": f"{hum}%" if hum != "--" else "--",
                "wind": f"{wind} km/h" if wind != "--" else "--",
                "uv": uv_max,
                "aqi": str(aqi_val),
                "cond": cond_text,
                "icon_name": icon_name,
                "sunrise": sunrise_str,
                "sunset": sunset_str,
                "rain_chance": f"{hourly_list[0]['rain_prob']}%" if hourly_list else "--",
                "hourly": hourly_list,
                "daily": daily_list,
                "updated_time": now_time
            }

            with STATE.lock:
                STATE.weather.update(weather_update)
                save_cache("weather", weather_update)
                if STATE.health_warning and "Weather" in STATE.health_warning:
                    STATE.health_warning = None

                new_alerts = [a for a in STATE.alerts if "Rain expected" not in a and "Extreme Heat" not in a and "Poor Air Quality" not in a]
                if rain_within_2h:
                    new_alerts.append("[ALERT] Rain expected within 2 hours")
                if isinstance(feels, (int, float)) and feels >= CONFIG["alerts"]["heat_index_threshold_c"]:
                    new_alerts.append(f"[ALERT] Extreme Heat Alert: Feels like {feels}°C")
                if isinstance(aqi_val, (int, float)) and aqi_val > CONFIG["alerts"]["aqi_poor_threshold"]:
                    new_alerts.append(f"[ALERT] Poor Air Quality (AQI {aqi_val})")
                STATE.alerts = new_alerts
            fail_backoff = 30

        except Exception as e:
            log_net_error("weather", f"Weather worker error: {e}")
            with STATE.lock:
                STATE.health_warning = "Weather sync failed, retrying..."
            sleep_sec = fail_backoff
            fail_backoff = min(fail_backoff * 2, 300)

        time.sleep(sleep_sec)

def news_worker():
    cached = load_cache("news")
    if cached:
        with STATE.lock:
            STATE.news = cached
    headers = {"User-Agent": "Mozilla/5.0"}

    while True:
        all_items = []
        seen_titles = set()
        feeds = CONFIG["news"]["feeds"]
        hide_words = [w.lower() for w in CONFIG["news"].get("filter_keywords_hide", [])]
        high_words = [w.lower() for w in CONFIG["news"].get("filter_keywords_highlight", [])]

        for source_name, url in feeds.items():
            try:
                r_feed = HTTP_SESSION.get(url, headers=headers, timeout=(5, 10))
                r_feed.raise_for_status()
                feed = feedparser.parse(r_feed.content)
                count = 0
                for entry in feed.entries:
                    if count >= CONFIG["news"].get("max_per_feed", 5):
                        break
                    raw_title = safe_tcl(entry.title.strip())
                    clean_title = " ".join(raw_title.split())

                    title_lower = clean_title.lower()
                    if any(hw in title_lower for hw in hide_words):
                        continue

                    sim_key = clean_title.lower()[:40]
                    if sim_key in seen_titles:
                        continue
                    seen_titles.add(sim_key)

                    is_new = False
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        entry_ts = time.mktime(entry.published_parsed)
                        if time.time() - entry_ts < 1800:
                            is_new = True

                    is_highlight = any(hw in title_lower for hw in high_words)

                    all_items.append({
                        "source": source_name,
                        "title": clean_title,
                        "is_new": is_new,
                        "highlight": is_highlight
                    })
                    count += 1
            except Exception as e:
                log_net_error(f"news:{source_name}", f"Error parsing feed {source_name}: {e}")

        if all_items:
            random.shuffle(all_items)
            with STATE.lock:
                STATE.news = all_items
                save_cache("news", all_items)
                STATE.health_warning = None
            time.sleep(1800)
        else:
            time.sleep(120)

_FALLBACK_QUOTES = [
    ("The secret of getting ahead is getting started.", "Mark Twain"),
    ("Simplicity is the ultimate sophistication.", "Leonardo da Vinci"),
    ("Well begun is half done.", "Aristotle"),
    ("Small daily improvements over time lead to stunning results.", "Robin Sharma"),
    ("Action is the foundational key to all success.", "Pablo Picasso"),
]

def rates_and_quotes_worker():
    cached_rates = load_cache("rates")
    if cached_rates:
        with STATE.lock:
            STATE.finance.update(cached_rates)

    cached_quote = load_cache("quote")
    if cached_quote:
        with STATE.lock:
            STATE.quote.update(cached_quote)

    fail_backoff = 120
    while True:
        rates_ok = False
        quote_ok = False
        try:
            r = HTTP_SESSION.get("https://open.er-api.com/v6/latest/USD", timeout=(5, 8), headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                rates = r.json().get("rates", {})
                usd_inr = rates.get("INR", "--")
                eur = rates.get("EUR", 1.0)
                gbp = rates.get("GBP", 1.0)
                eur_inr = round(usd_inr / eur, 2) if eur and usd_inr != "--" else "--"
                gbp_inr = round(usd_inr / gbp, 2) if gbp and usd_inr != "--" else "--"

                rates_data = {
                    "usd_inr": f"Rs.{round(usd_inr, 2)}" if usd_inr != "--" else "--",
                    "eur_inr": f"Rs.{eur_inr}" if eur_inr != "--" else "--",
                    "gbp_inr": f"Rs.{gbp_inr}" if gbp_inr != "--" else "--",
                    "updated_time": datetime.datetime.now().strftime("%H:%M")
                }
                with STATE.lock:
                    STATE.finance.update(rates_data)
                    save_cache("rates", rates_data)
                rates_ok = True
        except Exception as e:
            log_net_error("rates", f"Rates fetch error: {e}")

        try:
            r_q = HTTP_SESSION.get("https://zenquotes.io/api/today", timeout=(5, 8), headers={"User-Agent": "Mozilla/5.0"})
            if r_q.status_code == 200:
                q_list = r_q.json()
                if q_list and isinstance(q_list, list) and len(q_list) > 0:
                    q_obj = {
                        "text": safe_tcl(q_list[0].get("q", "Keep moving forward.")),
                        "author": safe_tcl(q_list[0].get("a", "Unknown"))
                    }
                    with STATE.lock:
                        STATE.quote.update(q_obj)
                        save_cache("quote", q_obj)
                    quote_ok = True
        except Exception as e:
            try:
                r_q2 = HTTP_SESSION.get("https://dummyjson.com/quotes/random", timeout=(5, 8), headers={"User-Agent": "Mozilla/5.0"})
                if r_q2.status_code == 200:
                    qd = r_q2.json()
                    if qd.get("quote"):
                        q_obj = {
                            "text": safe_tcl(qd.get("quote")),
                            "author": safe_tcl(qd.get("author", "Unknown"))
                        }
                        with STATE.lock:
                            STATE.quote.update(q_obj)
                            save_cache("quote", q_obj)
                        quote_ok = True
            except Exception:
                log_net_error("quote", f"Quote fetch error: {e}")
                if not cached_quote:
                    qt, qa = random.choice(_FALLBACK_QUOTES)
                    with STATE.lock:
                        STATE.quote.update({"text": qt, "author": qa})

        if rates_ok and quote_ok:
            fail_backoff = 120
            time.sleep(3600)
        else:
            time.sleep(fail_backoff)
            fail_backoff = min(fail_backoff * 2, 900)

def _read_net_bytes():
    """Sum RX/TX bytes across active physical LAN/Wi-Fi interfaces from /proc/net/dev."""
    rx_tot = 0
    tx_tot = 0
    try:
        with open("/proc/net/dev", "r") as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo" or iface.startswith(("tailscale", "docker", "veth", "br-")):
                    continue
                cols = rest.split()
                if len(cols) >= 9:
                    rx_tot += int(cols[0])
                    tx_tot += int(cols[8])
    except Exception:
        pass
    return rx_tot, tx_tot

def _format_rate(kbs):
    val = max(0.0, float(kbs or 0.0))
    if val >= 1024.0:
        return f"{val / 1024.0:.1f} MB/s"
    if val >= 100.0:
        return f"{int(round(val))} KB/s"
    return f"{val:.1f} KB/s"

def _get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(1.0)
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return "--"
    finally:
        try:
            s.close()
        except Exception:
            pass

def _read_usb_status():
    for mp in ("/media/usbdrive", "/home/shared/USB-Drive"):
        try:
            real_mp = os.path.realpath(mp)
            if os.path.ismount(real_mp):
                st = os.statvfs(real_mp)
                tot_gb = (st.f_blocks * st.f_frsize) / float(1024 ** 3)
                free_gb = (st.f_bavail * st.f_frsize) / float(1024 ** 3)
                return f"USB: {free_gb:.1f}/{tot_gb:.0f} GB free"
        except Exception:
            pass
    return "USB: Not mounted"

def _read_core_services():
    units = [u for _, u in CORE_SERVICE_SPECS]
    out_map = {lbl: False for lbl, _ in CORE_SERVICE_SPECS}
    try:
        res = subprocess.run(
            ["systemctl", "is-active"] + units,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        lines = (res.stdout or b"").decode("utf-8", errors="ignore").splitlines()
        for idx, (lbl, _) in enumerate(CORE_SERVICE_SPECS):
            if idx < len(lines) and lines[idx].strip() == "active":
                out_map[lbl] = True
    except Exception:
        pass
    return out_map

def _read_security_status():
    armed = False
    events = 0
    last_str = ""
    try:
        if os.path.exists(SECURITY_STATE_FILE):
            with open(SECURITY_STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            armed = bool(st.get("armed", False))
            events = int(st.get("total_events", 0) or 0)
            lev = st.get("last_event") or {}
            if lev.get("ts"):
                last_str = f"{lev.get('type', 'MOTION')} {str(lev.get('ts'))[-8:-3]}"
    except Exception:
        pass
    if not armed:
        try:
            r = subprocess.run(
                ["pgrep", "-f", "ffmpeg.*-f v4l2"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if r.returncode == 0:
                armed = True
        except Exception:
            pass
    return armed, events, last_str

def system_stats_worker():
    prev_idle = 0
    prev_total = 0
    prev_rx, prev_tx = _read_net_bytes()
    prev_net_ts = time.time()
    adguard_tick = 0
    has_docker = bool(shutil.which("docker"))
    has_tailscale = bool(shutil.which("tailscale"))
    inet_fail_streak = 0
    last_good_inet_ms = "--"
    last_ts_status = "Off"
    last_services = {lbl: False for lbl, _ in CORE_SERVICE_SPECS}
    last_lan_ip = _get_lan_ip()
    last_usb = _read_usb_status()
    last_sec = (False, 0, "")

    while True:
        try:
            cpu_pct = 0.0
            with open("/proc/stat", "r") as f:
                line = f.readline()
                parts = [float(x) for x in line.split()[1:8]]
                idle = parts[3] + parts[4]
                total = sum(parts)
                if prev_total > 0:
                    diff_idle = idle - prev_idle
                    diff_total = total - prev_total
                    if diff_total > 0:
                        cpu_pct = max(0.0, min(100.0, (1.0 - diff_idle / diff_total) * 100.0))
                prev_idle = idle
                prev_total = total

            cpu_t = 0.0
            if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
                try:
                    with open("/sys/class/thermal/thermal_zone0/temp") as f:
                        cpu_t = round(float(f.read().strip()) / 1000.0, 1)
                except Exception:
                    pass

            load_avg = "--"
            try:
                with open("/proc/loadavg", "r") as f:
                    load_avg = f.read().split()[0]
            except Exception:
                pass

            ram_pct = 0.0
            mem_used_mb = 0
            mem_total_mb = 0
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
                mem_data = {}
                for l in lines[:6]:
                    sp = l.split(":")
                    if len(sp) == 2:
                        mem_data[sp[0].strip()] = int(sp[1].replace("kB", "").strip())
                tot = mem_data.get("MemTotal", 1024)
                avail = mem_data.get("MemAvailable", mem_data.get("MemFree", 0))
                used = tot - avail
                ram_pct = round((used / tot) * 100.0, 1)
                mem_used_mb = int(used / 1024)
                mem_total_mb = int(tot / 1024)

            st = os.statvfs("/")
            d_total = st.f_blocks * st.f_frsize
            d_free = st.f_bavail * st.f_frsize
            d_used = d_total - d_free
            disk_pct = round((d_used / d_total) * 100.0, 1) if d_total > 0 else 0.0
            disk_free_gb = round(d_free / (1024 ** 3), 1)

            uptime_str = "--"
            with open("/proc/uptime", "r") as f:
                up_sec = float(f.read().split()[0])
                days = int(up_sec // 86400)
                hrs = int((up_sec % 86400) // 3600)
                mins = int((up_sec % 3600) // 60)
                uptime_str = f"{days}d {hrs}h {mins}m" if days > 0 else f"{hrs}h {mins}m"

            now_net_ts = time.time()
            cur_rx, cur_tx = _read_net_bytes()
            dt_net = max(0.5, now_net_ts - prev_net_ts)
            net_rx_kbs = max(0.0, (cur_rx - prev_rx) / 1024.0 / dt_net) if cur_rx >= prev_rx else 0.0
            net_tx_kbs = max(0.0, (cur_tx - prev_tx) / 1024.0 / dt_net) if cur_tx >= prev_tx else 0.0
            prev_rx, prev_tx, prev_net_ts = cur_rx, cur_tx, now_net_ts

            bat_status = "AC Online"
            acad_path = "/sys/class/power_supply/ACAD/online"
            bat_path = "/sys/class/power_supply/BAT0"
            ac_online = True
            if os.path.exists(acad_path):
                try:
                    ac_online = open(acad_path).read().strip() == "1"
                except: pass

            if os.path.exists(bat_path):
                try:
                    b_stat = open(os.path.join(bat_path, "status")).read().strip()
                    if not ac_online:
                        bat_status = f"Battery ({b_stat})"
                    else:
                        bat_status = "AC Powered"
                except Exception:
                    pass

            router_ms = "--"
            inet_ms = last_good_inet_ms
            try:
                res = subprocess.run(
                    ["ping", "-n", "-c", "1", "-W", "1", "192.168.1.1"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                if res.returncode == 0:
                    out = res.stdout.decode("utf-8", errors="ignore")
                    if "time=" in out:
                        router_ms = out.split("time=")[1].split()[0] + "ms"
                ping_ok = False
                for probe_ip in ("1.1.1.1", "8.8.8.8"):
                    res2 = subprocess.run(
                        ["ping", "-n", "-c", "1", "-W", "1", probe_ip],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    if res2.returncode == 0:
                        out2 = res2.stdout.decode("utf-8", errors="ignore")
                        if "time=" in out2:
                            inet_ms = out2.split("time=")[1].split()[0] + "ms"
                            last_good_inet_ms = inet_ms
                        ping_ok = True
                        break
                if ping_ok:
                    inet_fail_streak = 0
                else:
                    inet_fail_streak += 1
                    if inet_fail_streak >= 3:
                        inet_ms = "Offline"
            except Exception:
                pass

            docker_cnt = 0
            if has_docker and adguard_tick % 6 == 0:
                try:
                    d_out = subprocess.check_output(
                        ["docker", "ps", "-q"],
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    ).decode("utf-8", errors="ignore").strip()
                    docker_cnt = len(d_out.splitlines()) if d_out else 0
                except Exception:
                    pass

            ts_status = last_ts_status
            if has_tailscale and adguard_tick % 3 == 0:
                try:
                    t_res = subprocess.run(
                        ["tailscale", "status"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    t_out = (t_res.stdout or b"").decode("utf-8", errors="ignore").splitlines()
                    first_ln = t_out[0].strip() if t_out else ""
                    if "linux" in first_ln.lower() or "100." in first_ln:
                        ts_status = "Connected"
                    else:
                        ts_status = "Disconnected"
                    last_ts_status = ts_status
                except Exception:
                    pass

            if adguard_tick % 3 == 0:
                last_services = _read_core_services()
                last_usb = _read_usb_status()
                last_lan_ip = _get_lan_ip()
            if adguard_tick % 2 == 0:
                last_sec = _read_security_status()

            ag_data = None
            if adguard_tick % 3 == 0:
                ag_cfg = CONFIG.get("adguard", {})
                if ag_cfg.get("enabled") and ag_cfg.get("password"):
                    try:
                        r_ag = HTTP_SESSION.get(
                            ag_cfg.get("url", "http://127.0.0.1:8081/control/stats"),
                            auth=(ag_cfg.get("username", "vicky"), ag_cfg["password"]),
                            timeout=(2, 3)
                        )
                        if r_ag.status_code == 200:
                            js = r_ag.json()
                            q_cnt = int(js.get("num_dns_queries", 0))
                            b_cnt = int(js.get("num_blocked_filtering", 0))
                            pct = (b_cnt / q_cnt * 100.0) if q_cnt > 0 else 0.0
                            ag_data = (f"{b_cnt:,}", f"{pct:.1f}%", f"{q_cnt:,}")
                    except Exception as ae:
                        log_net_error("adguard_stats", f"AdGuard stats error: {ae}")
            adguard_tick += 1

            # Normalize network rate to 0-100 for sparkline overlay (log-like cap at 2048 KB/s)
            net_tot_kbs = net_rx_kbs + net_tx_kbs
            net_pct = min(100.0, (math.log10(1.0 + net_tot_kbs) / math.log10(2049.0)) * 100.0) if net_tot_kbs > 0 else 0.0

            with STATE.lock:
                STATE.system["cpu_percent"] = round(cpu_pct, 1)
                STATE.system["cpu_temp"] = cpu_t
                STATE.system["load_avg"] = load_avg
                STATE.system["ram_percent"] = ram_pct
                STATE.system["ram_used_mb"] = mem_used_mb
                STATE.system["ram_total_mb"] = mem_total_mb
                STATE.system["disk_percent"] = disk_pct
                STATE.system["disk_free_gb"] = disk_free_gb
                STATE.system["usb_status"] = last_usb
                STATE.system["uptime_str"] = uptime_str
                STATE.system["battery_status"] = bat_status
                STATE.system["router_ping"] = router_ms
                STATE.system["inet_ping"] = inet_ms
                STATE.system["lan_ip"] = last_lan_ip
                STATE.system["net_rx_kbs"] = round(net_rx_kbs, 1)
                STATE.system["net_tx_kbs"] = round(net_tx_kbs, 1)
                STATE.system["tailscale_status"] = ts_status
                STATE.system["docker_count"] = docker_cnt
                STATE.system["services"] = dict(last_services)
                STATE.system["security_armed"] = last_sec[0]
                STATE.system["security_events"] = last_sec[1]
                STATE.system["security_last"] = last_sec[2]
                if ag_data is not None:
                    STATE.system["ads_blocked"], STATE.system["ads_percent"], STATE.system["dns_queries"] = ag_data

                c_hist = STATE.system["cpu_history"][1:] + [cpu_pct]
                r_hist = STATE.system["ram_history"][1:] + [ram_pct]
                n_hist = STATE.system["net_history"][1:] + [round(net_pct, 1)]
                STATE.system["cpu_history"] = c_hist
                STATE.system["ram_history"] = r_hist
                STATE.system["net_history"] = n_hist

                if inet_ms == "Offline":
                    if "[ALERT] Internet connection down" not in STATE.alerts:
                        STATE.alerts.append("[ALERT] Internet connection down")
                else:
                    STATE.alerts = [a for a in STATE.alerts if "Internet connection down" not in a]

        except Exception as e:
            log_msg(f"System stats worker error: {e}")

        time.sleep(5)

def events_worker():
    while True:
        try:
            if os.path.exists(EVENTS_PATH):
                with open(EVENTS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw_events = data.get("events", [])
                today = datetime.date.today()
                parsed = []
                for ev in raw_events:
                    try:
                        ev_d = datetime.datetime.strptime(ev["date"], "%Y-%m-%d").date()
                        delta = (ev_d - today).days
                        if delta >= 0:
                            if delta == 0:
                                countdown_str = "TODAY"
                            elif delta == 1:
                                countdown_str = "Tomorrow"
                            else:
                                countdown_str = f"in {delta} days"
                            parsed.append({
                                "title": safe_tcl(ev["title"]),
                                "date": ev["date"],
                                "days_left": delta,
                                "badge": countdown_str
                            })
                    except Exception:
                        pass
                parsed.sort(key=lambda x: x["days_left"])
                with STATE.lock:
                    STATE.events = parsed[:5]
        except Exception as e:
            log_msg(f"Events worker error: {e}")
        time.sleep(300)

def _haversine_nm_and_bearing(lat1, lon1, lat2, lon2):
    r_nm = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * (math.sin(dlmb / 2.0) ** 2)
    dist_nm = 2.0 * r_nm * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    brng = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return dist_nm, brng

def _compass_dir(deg):
    if deg is None:
        return ""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((float(deg) + 22.5) // 45) % 8]

def _format_aircraft_type(desc, t_code):
    raw = (desc or t_code or "").strip()
    if not raw:
        return "Aircraft"
    if raw.isupper() and len(raw) > 5:
        keep_upper = {"ATR", "MD", "ERJ", "CRJ", "EMB", "Q400"}
        words = []
        for w in raw.split():
            if w in keep_upper or (any(ch.isdigit() for ch in w) and len(w) <= 5):
                words.append(w)
            else:
                words.append(w.capitalize())
        return " ".join(words)
    return raw

def _lookup_flight_route(callsign, cache, headers):
    cs = (callsign or "").strip().upper()
    if len(cs) < 3 or not cs.isalnum():
        return None
    now = time.time()
    if cs in cache:
        cached_at, info = cache[cs]
        ttl = 21600 if info else 1800
        if now - cached_at < ttl:
            return dict(info) if info else None
    if len(cache) > 80:
        oldest = min(cache, key=lambda k: cache[k][0])
        cache.pop(oldest, None)
    try:
        r = HTTP_SESSION.get(f"https://api.adsbdb.com/v0/callsign/{cs}", headers=headers, timeout=(3, 4))
        if r.status_code == 200:
            fr = (r.json().get("response") or {}).get("flightroute") or {}
            orig = fr.get("origin") or {}
            dst = fr.get("destination") or {}
            airline = (fr.get("airline") or {}).get("name") or ""
            iata = fr.get("callsign_iata") or ""
            orig_code = orig.get("iata_code") or orig.get("icao_code") or ""
            orig_city = orig.get("municipality") or ""
            dst_code = dst.get("iata_code") or dst.get("icao_code") or ""
            dst_city = dst.get("municipality") or ""
            o_lat, o_lon = orig.get("latitude"), orig.get("longitude")
            d_lat, d_lon = dst.get("latitude"), dst.get("longitude")
            if orig_code or dst_code or airline:
                info = {
                    "airline": airline,
                    "iata": iata,
                    "orig_code": orig_code,
                    "orig_city": orig_city,
                    "orig_lat": float(o_lat) if o_lat is not None else None,
                    "orig_lon": float(o_lon) if o_lon is not None else None,
                    "dst_code": dst_code,
                    "dst_city": dst_city,
                    "dst_lat": float(d_lat) if d_lat is not None else None,
                    "dst_lon": float(d_lon) if d_lon is not None else None,
                }
                cache[cs] = (now, info)
                return dict(info)
    except Exception:
        pass
    cache[cs] = (now, None)
    return None

def _great_circle_points(lat1, lon1, lat2, lon2, steps=36):
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    x1, y1, z1 = math.cos(p1) * math.cos(l1), math.cos(p1) * math.sin(l1), math.sin(p1)
    x2, y2, z2 = math.cos(p2) * math.cos(l2), math.cos(p2) * math.sin(l2), math.sin(p2)
    dot = max(-1.0, min(1.0, x1 * x2 + y1 * y2 + z1 * z2))
    omega = math.acos(dot)
    pts = []
    for i in range(steps + 1):
        t = i / float(steps)
        if omega < 1e-6:
            pts.append((lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)))
        else:
            s_tot = math.sin(omega)
            w1 = math.sin((1.0 - t) * omega) / s_tot
            w2 = math.sin(t * omega) / s_tot
            x = w1 * x1 + w2 * x2
            y = w1 * y1 + w2 * y2
            z = w1 * z1 + w2 * z2
            pts.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return pts

def _validate_and_orient_route(fl):
    o_lat, o_lon = fl.get("orig_lat"), fl.get("orig_lon")
    d_lat, d_lon = fl.get("dst_lat"), fl.get("dst_lon")
    ac_lat, ac_lon = fl.get("lat"), fl.get("lon")
    if None in (o_lat, o_lon, d_lat, d_lon, ac_lat, ac_lon):
        fl["route_valid"] = False
        return
    tot_nm, _ = _haversine_nm_and_bearing(o_lat, o_lon, d_lat, d_lon)
    tot_km = tot_nm * 1.852
    if tot_km < 30:
        fl["route_valid"] = False
        return
    arc = _great_circle_points(o_lat, o_lon, d_lat, d_lon, steps=36)
    best_i, best_nm = 0, 1e9
    for idx, (alat, alon) in enumerate(arc):
        dnm, _ = _haversine_nm_and_bearing(ac_lat, ac_lon, alat, alon)
        if dnm < best_nm:
            best_nm, best_i = dnm, idx
    cross_km = best_nm * 1.852
    if cross_km > max(280.0, 0.28 * tot_km):
        fl["route_valid"] = False
        return
    frac = best_i / float(len(arc) - 1)
    _, fwd_brng = _haversine_nm_and_bearing(ac_lat, ac_lon, d_lat, d_lon)
    _, rev_brng = _haversine_nm_and_bearing(ac_lat, ac_lon, o_lat, o_lon)
    trk = fl.get("track")
    if trk is None:
        fl["route_valid"] = True
        fl["progress_pct"] = max(2, min(98, int(round(frac * 100))))
        return
    fwd_diff = abs((trk - fwd_brng + 180.0) % 360.0 - 180.0)
    rev_diff = abs((trk - rev_brng + 180.0) % 360.0 - 180.0)
    if fwd_diff <= 75.0:
        fl["route_valid"] = True
        fl["progress_pct"] = max(2, min(98, int(round(frac * 100))))
    elif rev_diff <= 75.0:
        # Handle reverse-sector airline callsign assignment
        for k1, k2 in [("orig_code", "dst_code"), ("orig_city", "dst_city"), ("orig_lat", "dst_lat"), ("orig_lon", "dst_lon")]:
            fl[k1], fl[k2] = fl.get(k2), fl.get(k1)
        fl["route_valid"] = True
        fl["progress_pct"] = max(2, min(98, int(round((1.0 - frac) * 100))))
    else:
        fl["route_valid"] = False

def _latlon_to_world_px(lat, lon, zoom):
    scale = 256 << zoom
    x = ((lon + 180.0) / 360.0) * scale
    s = max(-0.9999, min(0.9999, math.sin(math.radians(lat))))
    y = (0.5 - math.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)) * scale
    return x, y

def _choose_map_viewport(points_latlon, w_px, h_px):
    lats = [p[0] for p in points_latlon]
    lons = [p[1] for p in points_latlon]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    zoom = 2
    for z in range(10, 1, -1):
        x0, y0 = _latlon_to_world_px(max_lat, min_lon, z)
        x1, y1 = _latlon_to_world_px(min_lat, max_lon, z)
        if (x1 - x0) <= w_px * 0.72 and (y1 - y0) <= h_px * 0.68:
            zoom = z
            break
    x0, y0 = _latlon_to_world_px(max_lat, min_lon, zoom)
    x1, y1 = _latlon_to_world_px(min_lat, max_lon, zoom)
    cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    vx0 = cx - w_px * 0.5
    vy0 = cy - h_px * 0.5
    return zoom, vx0, vy0

def _fetch_map_mosaic(zoom, vx0, vy0, w_px, h_px):
    n_tiles = 1 << zoom
    tx0 = int(math.floor(vx0 / 256.0))
    ty0 = int(math.floor(vy0 / 256.0))
    tx1 = int(math.floor((vx0 + w_px - 1) / 256.0))
    ty1 = int(math.floor((vy0 + h_px - 1) / 256.0))
    canvas_img = Image.new("RGB", (w_px, h_px), (6, 13, 26))
    tile_headers = {"User-Agent": "HomeFlightRadar/1.0"}

    try:
        cached_files = sorted(glob.glob(os.path.join(MAP_TILE_CACHE, "*.png")), key=os.path.getmtime)
        while len(cached_files) > 180:
            os.remove(cached_files.pop(0))
    except Exception:
        pass

    for ty in range(ty0, ty1 + 1):
        if ty < 0 or ty >= n_tiles:
            continue
        for tx in range(tx0, tx1 + 1):
            wx = tx % n_tiles
            tile_path = os.path.join(MAP_TILE_CACHE, f"esri_{zoom}_{wx}_{ty}.png")
            tile_img = None
            if os.path.exists(tile_path):
                try:
                    tile_img = Image.open(tile_path).convert("RGB")
                except Exception:
                    tile_img = None
            if tile_img is None:
                try:
                    base_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{zoom}/{ty}/{wx}"
                    rb = HTTP_SESSION.get(base_url, headers=tile_headers, timeout=(4, 5))
                    if rb.status_code == 200:
                        base_l = Image.open(BytesIO(rb.content)).convert("L")
                        stretched = base_l.point(lambda p: max(0, min(255, int((p - 28) * 5.2))))
                        merged = ImageOps.colorize(stretched, black="#071020", white="#243F63").convert("RGBA")
                        try:
                            ref_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{zoom}/{ty}/{wx}"
                            rr = HTTP_SESSION.get(ref_url, headers=tile_headers, timeout=(3, 4))
                            if rr.status_code == 200:
                                ref_img = Image.open(BytesIO(rr.content)).convert("RGBA")
                                merged.paste(ref_img, (0, 0), ref_img)
                        except Exception:
                            pass
                        tile_img = merged.convert("RGB")
                        tile_img.save(tile_path)
                except Exception:
                    tile_img = None
            if tile_img is not None:
                px = int(round(tx * 256 - vx0))
                py = int(round(ty * 256 - vy0))
                canvas_img.paste(tile_img, (px, py))

    return canvas_img

def _parse_adsbfi_flights(lat, lon, radius_nm, max_alt_ft, headers):
    url = f"https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{int(radius_nm)}"
    r = HTTP_SESSION.get(url, headers=headers, timeout=(5, 8))
    if r.status_code != 200:
        raise RuntimeError(f"adsb.fi HTTP {r.status_code}")
    data = r.json()
    raw_list = data.get("aircraft") or data.get("ac") or []
    now = time.time()
    flights = []
    for a in raw_list:
        alt_raw = a.get("alt_baro")
        if alt_raw == "ground" or alt_raw is None:
            continue
        try:
            alt_ft = int(float(alt_raw))
        except Exception:
            continue
        if alt_ft < 100 or alt_ft > max_alt_ft:
            continue
        a_lat, a_lon = a.get("lat"), a.get("lon")
        if a_lat is None or a_lon is None:
            continue
        a_lat, a_lon = float(a_lat), float(a_lon)
        dst_nm, brng = _haversine_nm_and_bearing(lat, lon, a_lat, a_lon)
        if dst_nm > radius_nm:
            continue
        gs_kt = a.get("gs")
        speed_kmh = int(round(float(gs_kt) * 1.852)) if gs_kt is not None else None
        callsign = (a.get("flight") or "").strip().upper()
        reg = (a.get("r") or "").strip().upper()
        aircraft_str = _format_aircraft_type(a.get("desc"), a.get("t"))
        flights.append({
            "hex": (a.get("hex") or callsign or "UNK").upper(),
            "callsign": callsign or reg or (a.get("hex") or "UNKNOWN").upper(),
            "raw_callsign": callsign,
            "reg": reg,
            "aircraft": aircraft_str,
            "alt_ft": alt_ft,
            "speed_kmh": speed_kmh,
            "lat": a_lat,
            "lon": a_lon,
            "dst_nm": dst_nm,
            "dist_km": round(dst_nm * 1.852, 1),
            "compass": _compass_dir(brng),
            "track": int(round(float(a["track"]))) if a.get("track") is not None else None,
            "fetched_at": now,
        })
    return flights

def _parse_opensky_flights(lat, lon, radius_nm, max_alt_ft, headers):
    dlat = radius_nm / 60.0
    dlon = dlat / max(0.2, math.cos(math.radians(lat)))
    url = (
        f"https://opensky-network.org/api/states/all"
        f"?lamin={lat - dlat:.4f}&lomin={lon - dlon:.4f}"
        f"&lamax={lat + dlat:.4f}&lomax={lon + dlon:.4f}"
    )
    r = HTTP_SESSION.get(url, headers=headers, timeout=(5, 8))
    if r.status_code != 200:
        raise RuntimeError(f"opensky HTTP {r.status_code}")
    states = r.json().get("states") or []
    now = time.time()
    flights = []
    for s in states:
        if len(s) < 11 or s[8]:
            continue
        s_lon, s_lat, baro_m = s[5], s[6], s[7]
        if s_lon is None or s_lat is None or baro_m is None:
            continue
        alt_ft = int(round(float(baro_m) * 3.28084))
        if alt_ft < 100 or alt_ft > max_alt_ft:
            continue
        s_lat, s_lon = float(s_lat), float(s_lon)
        dst_nm, brng = _haversine_nm_and_bearing(lat, lon, s_lat, s_lon)
        if dst_nm > radius_nm:
            continue
        vel_ms = s[9]
        speed_kmh = int(round(float(vel_ms) * 3.6)) if vel_ms is not None else None
        callsign = (s[1] or "").strip().upper()
        flights.append({
            "hex": (s[0] or callsign or "UNK").upper(),
            "callsign": callsign or (s[0] or "UNKNOWN").upper(),
            "raw_callsign": callsign,
            "reg": "",
            "aircraft": "Aircraft",
            "alt_ft": alt_ft,
            "speed_kmh": speed_kmh,
            "lat": s_lat,
            "lon": s_lon,
            "dst_nm": dst_nm,
            "dist_km": round(dst_nm * 1.852, 1),
            "compass": _compass_dir(brng),
            "track": int(round(float(s[10]))) if s[10] is not None else None,
            "fetched_at": now,
        })
    return flights

def flights_worker():
    headers = {"User-Agent": "Mozilla/5.0"}
    route_cache = {}
    trails = {}
    last_logged_top = None
    last_ok_ts = time.time()
    fail_streak = 0
    while True:
        f_cfg = CONFIG.get("flights", {})
        if not f_cfg.get("enabled", True):
            with STATE.lock:
                STATE.flights = []
                STATE.preview_flights = []
            time.sleep(60)
            continue

        poll_sec = max(15, int(f_cfg.get("poll_seconds", 20)))
        radius_km = float(f_cfg.get("radius_km", 0) or 0)
        if radius_km > 0:
            radius_nm = radius_km / 1.852
        else:
            radius_nm = max(1.0, float(f_cfg.get("radius_nm", 5.4)))
            radius_km = round(radius_nm * 1.852, 1)
        scan_nm = max(6, int(math.ceil(radius_km / 1.852)) + 1)
        max_alt_ft = int(f_cfg.get("max_altitude_ft", 50000))
        lat = float(CONFIG.get("latitude", 13.0827))
        lon = float(CONFIG.get("longitude", 80.2707))

        try:
            try:
                all_fl = _parse_adsbfi_flights(lat, lon, scan_nm, max_alt_ft, headers)
            except Exception as e1:
                log_net_error("adsbfi_fallback", f"adsb.fi flight fetch fallback ({e1})")
                all_fl = _parse_opensky_flights(lat, lon, scan_nm, max_alt_ft, headers)

            all_fl.sort(key=lambda x: x["dist_km"])
            now = time.time()
            last_ok_ts = now
            fail_streak = 0

            # Prune old breadcrumb trails and record live positions
            for hx in [k for k, v in trails.items() if now - v[0] > 600]:
                trails.pop(hx, None)
            for fl in all_fl:
                hx = fl["hex"]
                pts = trails.get(hx, (now, []))[1]
                pt = (fl["lat"], fl["lon"])
                if not pts or abs(pts[-1][0] - pt[0]) + abs(pts[-1][1] - pt[1]) > 0.002:
                    pts.append(pt)
                    if len(pts) > 18:
                        pts.pop(0)
                trails[hx] = (now, pts)
                fl["trail"] = list(pts)

            overhead = [f for f in all_fl if f["dist_km"] <= radius_km][:5]
            regional = overhead

            enriched = set()
            for fl in overhead[:3] + regional[:5]:
                hx = fl["hex"]
                if hx in enriched:
                    continue
                enriched.add(hx)
                rt = _lookup_flight_route(fl.get("raw_callsign"), route_cache, headers)
                if rt:
                    fl.update(rt)
                _validate_and_orient_route(fl)

            with STATE.lock:
                STATE.flights = overhead
                STATE.preview_flights = regional
                if overhead:
                    last_f = dict(overhead[0])
                    last_f["seen_at"] = now
                    STATE.last_flight_seen = last_f

            top_id = overhead[0]["callsign"] if overhead else None
            if top_id != last_logged_top:
                if overhead:
                    f0 = overhead[0]
                    rt_tag = f" [{f0['orig_code']}->{f0['dst_code']}]" if f0.get("route_valid") else ""
                    log_msg(f"Flight crossing Chennai: {f0['callsign']}{rt_tag} ({f0['aircraft']}) at {f0['dist_km']} km {f0['compass']}")
                last_logged_top = top_id

        except Exception as e:
            fail_streak += 1
            log_net_error("flights_worker", f"Flights worker error: {e}")
            if time.time() - last_ok_ts > 90:
                with STATE.lock:
                    STATE.flights = []
                    STATE.preview_flights = []

        sleep_for = poll_sec if fail_streak == 0 else min(60, poll_sec * min(3, fail_streak))
        time.sleep(sleep_for)

_SPACE_MU = 398600.4418
_SPACE_RE = 6378.137
_SPACE_J2 = 1.08262668e-3
_SPACE_F = 1.0 / 298.257223563
_SPACE_E2 = _SPACE_F * (2.0 - _SPACE_F)

def _fmt_sky_countdown(sec):
    sec = max(0, int(round(sec)))
    if sec < 3600:
        return f"{max(1, sec // 60)}m"
    hrs, rem = divmod(sec // 60, 60)
    if hrs < 48:
        return f"{hrs}h {rem}m"
    return f"{hrs // 24}d {hrs % 24}h"

def _geodetic_to_ecef(lat_deg, lon_deg, alt_km=0.0):
    lr, ln = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lr), math.cos(lr)
    n_val = _SPACE_RE / math.sqrt(1.0 - _SPACE_E2 * sl * sl)
    return (
        (n_val + alt_km) * cl * math.cos(ln),
        (n_val + alt_km) * cl * math.sin(ln),
        (n_val * (1.0 - _SPACE_E2) + alt_km) * sl,
    )

def _obs_frame_sky(lat_deg, lon_deg, alt_km=0.14):
    lr, ln = math.radians(lat_deg), math.radians(lon_deg)
    ox, oy, oz = _geodetic_to_ecef(lat_deg, lon_deg, alt_km)
    return ox, oy, oz, math.sin(lr), math.cos(lr), math.sin(ln), math.cos(ln)

def _topocentric_metrics(sat_ef, sun_ef, obs):
    ox, oy, oz, sl, cl, sn, cn = obs
    dx, dy, dz = sat_ef[0] - ox, sat_ef[1] - oy, sat_ef[2] - oz
    south = sl * cn * dx + sl * sn * dy - cl * dz
    east = -sn * dx + cn * dy
    zen = cl * cn * dx + cl * sn * dy + sl * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    el = math.degrees(math.asin(max(-1.0, min(1.0, zen / max(1e-9, rng)))))
    az = (math.degrees(math.atan2(east, -south)) + 360.0) % 360.0
    if sun_ef is None:
        return el, az, None, rng
    sun_zen = cl * cn * sun_ef[0] + cl * sn * sun_ef[1] + sl * sun_ef[2]
    sun_el = math.degrees(math.asin(max(-1.0, min(1.0, sun_zen))))
    return el, az, sun_el, rng

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
    a1 = (_SPACE_MU / (n0 ** 2)) ** (1.0 / 3.0)
    cos_i = math.cos(incl)
    k_c = 0.75 * _SPACE_J2 * (3.0 * cos_i * cos_i - 1.0) / ((1.0 - ecc ** 2) ** 1.5)
    d1 = k_c * ((_SPACE_RE / a1) ** 2)
    a0 = a1 * (1.0 - d1 / 3.0 - d1 * d1 - (134.0 / 81.0) * (d1 ** 3))
    d0 = k_c * ((_SPACE_RE / a0) ** 2)
    n_b = n0 / (1.0 + d0)
    a = a0 / (1.0 - d0)
    p = a * (1.0 - ecc * ecc)
    fac = -1.5 * n_b * _SPACE_J2 * ((_SPACE_RE / p) ** 2)
    raan = raan0 + fac * cos_i * dt
    argp = argp0 + (-0.5 * fac) * (5.0 * cos_i * cos_i - 1.0) * dt
    m_anom = (m0 + n0 * dt + (ndot * 2.0 * math.pi / (86400.0 ** 2)) * dt * dt) % (2.0 * math.pi)
    e_anom = m_anom
    for _ in range(6):
        e_anom -= (e_anom - ecc * math.sin(e_anom) - m_anom) / (1.0 - ecc * math.cos(e_anom))
    x_orb = a * (math.cos(e_anom) - ecc)
    y_orb = a * math.sqrt(max(0.0, 1.0 - ecc * ecc)) * math.sin(e_anom)
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
    sunlit = True if proj >= 0 else (x_eci * x_eci + y_eci * y_eci + z_eci * z_eci - proj * proj) > (_SPACE_RE * _SPACE_RE)
    sun_ef = (s_eci[0] * cg + s_eci[1] * sg, -s_eci[0] * sg + s_eci[1] * cg, s_eci[2])
    return sat_ef, sunlit, sun_ef

def _fetch_iss_tle(headers):
    try:
        r = HTTP_SESSION.get("https://api.wheretheiss.at/v1/satellites/25544/tles", headers=headers, timeout=(5, 8))
        if r.status_code == 200:
            d = r.json()
            if d.get("line1") and d.get("line2"):
                return d["line1"].strip(), d["line2"].strip()
    except Exception:
        pass
    r = HTTP_SESSION.get("https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=tle", headers=headers, timeout=(5, 8))
    r.raise_for_status()
    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
    l1 = next(ln for ln in lines if ln.startswith("1 25544"))
    l2 = next(ln for ln in lines if ln.startswith("2 25544"))
    return l1, l2

def _sample_pass_track(raw_pts, max_t, max_el, max_az):
    """Downsample 10s sky track to <= 17 points while preserving start, peak, and end."""
    if len(raw_pts) <= 17:
        return [[el, az] for _, el, az in raw_pts]
    n = len(raw_pts)
    idxs = {0, n - 1}
    peak_i = min(range(n), key=lambda i: abs(raw_pts[i][0] - max_t))
    idxs.add(peak_i)
    for k in range(1, 14):
        idxs.add(int(round(k * (n - 1) / 14.0)))
    return [[raw_pts[i][1], raw_pts[i][2]] for i in sorted(idxs)]

def _compute_iss_passes(lat, lon, headers, days=21, force=False):
    now = time.time()
    cached = load_cache("space_iss") or {}
    loc_tag = f"{lat:.3f},{lon:.3f}"
    if (
        not force
        and cached.get("version") == 2
        and cached.get("loc") == loc_tag
        and (now - cached.get("fetched_at", 0)) < 21600
        and cached.get("scan_end", 0) > now + 86400
    ):
        return [p for p in (cached.get("passes") or []) if p.get("end", 0) > now]

    l1, l2 = _fetch_iss_tle(headers)
    tp = _parse_iss_tle(l1, l2)
    obs = _obs_frame_sky(lat, lon, 0.14)
    start_ts = int(now)
    end_ts = start_ts + days * 86400

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
        elif b_start is not None:
            brackets.append((b_start, min(end_ts, t + 60)))
            b_start = None
    if b_start is not None:
        brackets.append((b_start, end_ts))

    passes = []
    for bs, be in brackets:
        cur = None
        raw_pts = []
        for t in range(bs, be + 1, 10):
            sat_ef, lit, sun_ef = _iss_pos_sun_at(tp, t)
            el, az, sun_el, _ = _topocentric_metrics(sat_ef, sun_ef, obs)
            if el >= 10.0:
                raw_pts.append((t, round(el, 1), round(az, 1)))
                if cur is None:
                    cur = {
                        "start": t, "start_az": az, "max_el": el, "max_t": t,
                        "max_az": az, "end": t, "end_az": az, "vis_pts": 0, "sun_el": sun_el,
                    }
                if el > cur["max_el"]:
                    cur["max_el"] = round(el, 1)
                    cur["max_t"] = t
                    cur["max_az"] = round(az, 1)
                    cur["sun_el"] = round(sun_el, 1)
                cur["end"] = t
                cur["end_az"] = round(az, 1)
                if lit and sun_el <= -5.5:
                    cur["vis_pts"] += 1
            elif cur is not None:
                cur["start_az"] = round(cur["start_az"], 1)
                cur["max_el"] = round(cur["max_el"], 1)
                cur["visible"] = cur["vis_pts"] >= 2
                cur["pts"] = _sample_pass_track(raw_pts, cur["max_t"], cur["max_el"], cur["max_az"])
                passes.append(cur)
                cur = None
                raw_pts = []
        if cur is not None:
            cur["start_az"] = round(cur["start_az"], 1)
            cur["max_el"] = round(cur["max_el"], 1)
            cur["visible"] = cur["vis_pts"] >= 2
            cur["pts"] = _sample_pass_track(raw_pts, cur["max_t"], cur["max_el"], cur["max_az"])
            passes.append(cur)

    save_cache("space_iss", {"version": 2, "loc": loc_tag, "fetched_at": now, "scan_end": end_ts, "passes": passes})
    return [p for p in passes if p.get("end", 0) > now]

def _fetch_live_iss_telemetry(lat, lon, headers):
    r = HTTP_SESSION.get("https://api.wheretheiss.at/v1/satellites/25544", headers=headers, timeout=(5, 8))
    r.raise_for_status()
    d = r.json()
    i_lat = float(d["latitude"])
    i_lon = float(d["longitude"])
    i_alt = float(d["altitude"])
    vel = int(round(float(d["velocity"])))
    dist_nm, brng = _haversine_nm_and_bearing(lat, lon, i_lat, i_lon)
    obs = _obs_frame_sky(lat, lon, 0.14)
    sat_ef = _geodetic_to_ecef(i_lat, i_lon, i_alt)
    el_deg, az_deg, _, _ = _topocentric_metrics(sat_ef, None, obs)
    return {
        "lat": i_lat,
        "lon": i_lon,
        "alt_km": round(i_alt, 1),
        "speed_kmh": vel,
        "dist_km": int(round(dist_nm * 1.852)),
        "compass": _compass_dir(brng),
        "el_deg": round(el_deg, 1),
        "az_deg": round(az_deg, 1),
        "visibility": d.get("visibility") or "unknown",
        "fetched_at": time.time(),
    }

def _fetch_isro_launches(headers, force=False):
    now = time.time()
    cached = load_cache("space_isro") or {}
    if not force and cached and (now - cached.get("fetched_at", 0)) < 14400:
        return cached.get("isro") or []
    r = HTTP_SESSION.get(
        "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=4&location__ids=14",
        headers=headers,
        timeout=(5, 10),
    )
    r.raise_for_status()
    items = []
    for it in (r.json().get("results") or []):
        net_s = it.get("net") or ""
        net_ts = None
        if len(net_s) >= 19:
            try:
                net_ts = datetime.datetime.strptime(net_s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
            except Exception:
                net_ts = None
        items.append({
            "name": it.get("name") or "Unknown Mission",
            "net_ts": net_ts,
            "status": (it.get("status") or {}).get("abbrev") or "?",
        })
    save_cache("space_isro", {"fetched_at": now, "isro": items})
    return items

def space_worker():
    headers = {"User-Agent": "Mozilla/5.0 vickyhome"}
    last_pass_check = 0.0
    last_isro_check = 0.0

    # Hydrate cached sky data immediately on startup before hitting network
    try:
        now0 = time.time()
        min_el = float(CONFIG.get("sky_dome", {}).get("min_max_el_deg", 25.0))
        c_iss = load_cache("space_iss") or {}
        if c_iss.get("version") == 2:
            raw_p = [p for p in (c_iss.get("passes") or []) if p.get("end", 0) > now0]
            vis_p = [p for p in raw_p if p.get("visible") and p.get("max_el", 0) >= min_el]
            hi_p = [p for p in raw_p if p.get("max_el", 0) >= 30.0]
            with STATE.lock:
                STATE.space["passes"] = vis_p[:5]
                STATE.space["next_shadow"] = hi_p[0] if hi_p else None
        c_isro = load_cache("space_isro") or {}
        if c_isro.get("isro"):
            with STATE.lock:
                STATE.space["isro"] = c_isro["isro"][:3]
    except Exception:
        pass

    while True:
        s_cfg = CONFIG.get("sky_dome", {})
        if not s_cfg.get("enabled", True):
            time.sleep(60)
            continue

        now = time.time()
        lat = float(CONFIG.get("latitude", 13.0827))
        lon = float(CONFIG.get("longitude", 80.2707))
        min_el = float(s_cfg.get("min_max_el_deg", 25.0))

        if now - last_pass_check >= 1800:
            try:
                all_p = _compute_iss_passes(lat, lon, headers, days=21)
                vis_p = [p for p in all_p if p.get("visible") and p.get("max_el", 0) >= min_el]
                hi_p = [p for p in all_p if p.get("max_el", 0) >= 30.0]
                with STATE.lock:
                    STATE.space["passes"] = vis_p[:5]
                    STATE.space["next_shadow"] = hi_p[0] if hi_p else None
                    STATE.space["updated_at"] = now
                last_pass_check = now
            except Exception as e:
                log_net_error("sky_iss_pass", f"Sky ISS pass scan error: {e}")

        if now - last_isro_check >= 3600:
            try:
                isro_list = _fetch_isro_launches(headers)
                with STATE.lock:
                    STATE.space["isro"] = isro_list[:3]
                last_isro_check = now
            except Exception as e:
                log_net_error("sky_isro", f"ISRO launch fetch error: {e}")

        live_sleep = 60
        try:
            live = _fetch_live_iss_telemetry(lat, lon, headers)
            with STATE.lock:
                STATE.space["live"] = live
                STATE.space["updated_at"] = time.time()
            if live.get("dist_km", 99999) <= 2600:
                live_sleep = 15
        except Exception as e:
            log_net_error("sky_iss_live", f"Live ISS telemetry error: {e}")
            live_sleep = 120

        time.sleep(live_sleep)

SERVER_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")

def list_server_photos():
    """Recursively list readable image files inside the configured file-server photos folder."""
    raw_dir = CONFIG.get("slideshow", {}).get("local_folder", IMG_LOCAL_DIR)
    root_dir = os.path.expanduser(raw_dir) if raw_dir else IMG_LOCAL_DIR
    if not os.path.exists(root_dir):
        return []
    found = []
    seen_real = set()
    try:
        for cur_root, dirnames, filenames in os.walk(root_dir, followlinks=True):
            real_root = os.path.realpath(cur_root)
            if real_root in seen_real:
                dirnames[:] = []
                continue
            seen_real.add(real_root)
            dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                if os.path.splitext(fn)[1].lower() not in SERVER_IMAGE_EXTS:
                    continue
                full_p = os.path.join(cur_root, fn)
                if os.access(full_p, os.R_OK):
                    try:
                        found.append((os.path.getmtime(full_p), full_p))
                    except OSError:
                        found.append((0.0, full_p))
    except Exception as e:
        log_msg(f"Server photo scan error: {e}")
    found.sort(key=lambda item: (-item[0], item[1]))
    return [p for _, p in found]

def _load_oriented_rgb(path):
    """Open an image, apply EXIF rotation safely (Pillow 5.4+ compatible), and convert to RGB."""
    img = Image.open(path)
    try:
        if hasattr(ImageOps, "exif_transpose"):
            img = ImageOps.exif_transpose(img)
        else:
            exif = img._getexif() if hasattr(img, "_getexif") else None
            orient = exif.get(274) if isinstance(exif, dict) else None
            if orient == 3:
                img = img.rotate(180, expand=True)
            elif orient == 6:
                img = img.rotate(270, expand=True)
            elif orient == 8:
                img = img.rotate(90, expand=True)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (6, 13, 26))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img

def _compose_server_photo(path, w, h):
    """Frame any landscape or portrait photo cleanly onto a 1024x462 blurred ambient canvas."""
    src = _load_oriented_rgb(path)
    # Fast low-res ambient blur fill for 32-bit Atom performance
    bg_small = ImageOps.fit(src, (256, max(64, int(round(256 * h / float(w))))), method=RESAMPLE_MODE)
    bg_blur = bg_small.filter(ImageFilter.GaussianBlur(radius=6)).resize((w, h), Image.BILINEAR)
    canvas = ImageEnhance.Brightness(bg_blur).enhance(0.32)

    # Preserve full aspect ratio with zero cropping inside top gallery area (above 38px footer bar)
    max_w = max(120, w - 32)
    max_h = max(120, h - 58)
    fg = src.copy()
    fg.thumbnail((max_w, max_h), RESAMPLE_MODE)
    fw, fh = fg.size
    ox = (w - fw) // 2
    oy = max(10, (h - 38 - fh) // 2)

    # Subtle matte frame border around the foreground photo
    border_img = Image.new("RGB", (fw + 4, fh + 4), (30, 41, 59))
    canvas.paste(border_img, (max(0, ox - 2), max(0, oy - 2)))
    canvas.paste(fg, (ox, oy))
    return canvas, src.size

def photo_manager_worker(img_width, img_height):
    headers = {"User-Agent": "Mozilla/5.0"}
    picsum_url = f"https://picsum.photos/{img_width}/{img_height}"
    bing_api = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=en-IN"
    backoff = 15

    while True:
        sleep_sec = 60
        try:
            files = sorted(glob.glob(f"{IMG_CACHE_DIR}/*.jpg"), key=os.path.getmtime)
            max_cache = CONFIG["slideshow"].get("max_cached_files", 10)
            while len(files) > max_cache:
                try:
                    os.remove(files[0])
                    files.pop(0)
                except: break

            if len(files) < max_cache:
                downloaded = False
                try:
                    rb = HTTP_SESSION.get(bing_api, timeout=(5, 12), headers=headers)
                    if rb.status_code == 200:
                        existing = {os.path.basename(f) for f in files}
                        for item in rb.json().get("images", []):
                            sd = item.get("startdate", str(int(time.time())))
                            fname = f"bing_{sd}.jpg"
                            if fname in existing:
                                continue
                            img_url = "https://www.bing.com" + item["url"]
                            ri = HTTP_SESSION.get(img_url, timeout=(6, 15), headers=headers)
                            if ri.status_code == 200:
                                img = Image.open(BytesIO(ri.content))
                                if img.mode != "RGB":
                                    img = img.convert("RGB")
                                dst = os.path.join(IMG_CACHE_DIR, fname)
                                img.save(dst, quality=90)
                                downloaded = True
                                backoff = 15
                                sleep_sec = 5
                                break
                except Exception as be:
                    log_net_error("bing_photo", f"Bing photo fetch error: {be}")

                if not downloaded and len(files) < 2:
                    rand_id = random.randint(1000, 999999)
                    r = HTTP_SESSION.get(f"{picsum_url}?random={rand_id}", timeout=(5, 12), headers=headers, allow_redirects=True)
                    if r.status_code == 200:
                        img = Image.open(BytesIO(r.content))
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        dst = os.path.join(IMG_CACHE_DIR, f"{int(time.time())}_{rand_id}.jpg")
                        img.save(dst, quality=90)
                        backoff = 15
                        sleep_sec = 5
        except Exception as e:
            log_net_error("photo_mgr", f"Photo manager error (retry in {backoff}s): {e}")
            sleep_sec = backoff
            backoff = min(backoff * 2, 300)
        time.sleep(sleep_sec)

# ================= PHONE CONTROL HTTP SERVER =================
class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class PhoneControlHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10.0)
        super().setup()

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        token = params.get("token", [""])[0]

        expected_token = CONFIG["phone_control"].get("token", "CHANGE_ME_TOKEN")
        if token != expected_token:
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>Access Denied: Invalid or missing token</h3>")
            return

        if parsed.path == "/api/flights":
            with STATE.lock:
                payload = {
                    "location": CONFIG.get("location_name", "Chennai"),
                    "radius_km": CONFIG.get("flights", {}).get("radius_km", 10),
                    "radius_nm": CONFIG.get("flights", {}).get("radius_nm", 5.4),
                    "flights": list(STATE.flights),
                    "preview_flights": list(STATE.preview_flights),
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        if parsed.path == "/api/sky":
            with STATE.lock:
                payload = {
                    "location": CONFIG.get("location_name", "Chennai"),
                    "space": dict(STATE.space),
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        if parsed.path == "/api/status":
            page_names = ["Main", "Weather", "System", "Money", "Sky Dome", "Flight Radar", "Server Photos"]
            with STATE.lock:
                cur_p = STATE.active_page
                s = dict(STATE.system)
                w = dict(STATE.weather)
                payload = {
                    "version": DASHBOARD_VERSION,
                    "location": CONFIG.get("location_name", "Chennai"),
                    "active_page": cur_p,
                    "active_page_name": page_names[cur_p] if 0 <= cur_p < len(page_names) else "Main",
                    "rotation_paused": STATE.rotation_paused,
                    "night_mode": STATE.night_mode_active,
                    "theme": CONFIG.get("theme", "auto"),
                    "time_format_24h": CONFIG.get("time_format_24h", False),
                    "brightness": STATE.brightness_level,
                    "timer_remaining": STATE.timer_seconds_remaining,
                    "flights_count": len(STATE.flights),
                    "weather": {
                        "temp": w.get("temp", "--"),
                        "feels": w.get("feels", "--"),
                        "cond": w.get("cond", "--"),
                        "humidity": w.get("humidity", "--"),
                        "rain_chance": w.get("rain_chance", "--"),
                        "aqi": w.get("aqi", "--"),
                    },
                    "system": {
                        "cpu_percent": s.get("cpu_percent", 0.0),
                        "cpu_temp": s.get("cpu_temp", 0.0),
                        "load_avg": s.get("load_avg", "--"),
                        "ram_percent": s.get("ram_percent", 0.0),
                        "ram_used_mb": s.get("ram_used_mb", 0),
                        "ram_total_mb": s.get("ram_total_mb", 0),
                        "disk_percent": s.get("disk_percent", 0.0),
                        "disk_free_gb": s.get("disk_free_gb", 0.0),
                        "usb_status": s.get("usb_status", "Not mounted"),
                        "uptime_str": s.get("uptime_str", "--"),
                        "inet_ping": s.get("inet_ping", "--"),
                        "router_ping": s.get("router_ping", "--"),
                        "lan_ip": s.get("lan_ip", "--"),
                        "net_rx": _format_rate(s.get("net_rx_kbs", 0.0)),
                        "net_tx": _format_rate(s.get("net_tx_kbs", 0.0)),
                        "tailscale_status": s.get("tailscale_status", "--"),
                        "ads_blocked": s.get("ads_blocked", "--"),
                        "ads_percent": s.get("ads_percent", "--"),
                        "dns_queries": s.get("dns_queries", "--"),
                        "services": dict(s.get("services") or {}),
                        "security_armed": bool(s.get("security_armed", False)),
                        "security_events": int(s.get("security_events", 0)),
                        "security_last": s.get("security_last", ""),
                    },
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wall Dashboard Control v{DASHBOARD_VERSION}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ background: #070C16; color: #F8FAFC; padding: 14px 14px 72px; max-width: 540px; margin: 0 auto; }}
  .top-bar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
  h1 {{ font-size: 1.25rem; color: #38BDF8; letter-spacing: 0.2px; }}
  .ver-pill {{ background: #172554; color: #7DD3FC; border: 1px solid #1D4ED8; border-radius: 999px; padding: 3px 10px; font-size: 0.75rem; font-weight: 700; }}
  .status-strip {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }}
  .pill {{ display: inline-flex; align-items: center; gap: 5px; padding: 4px 9px; border-radius: 999px; font-size: 0.76rem; font-weight: 600; background: #151F32; border: 1px solid #22324F; color: #CBD5E1; }}
  .pill-ok {{ border-color: #065F46; color: #34D399; background: #06281E; }}
  .pill-warn {{ border-color: #92400E; color: #FBBF24; background: #2A1906; }}
  .pill-err {{ border-color: #991B1B; color: #FCA5A5; background: #2A0A0A; }}
  .card {{ background: #111A2C; border-radius: 14px; padding: 14px; margin-bottom: 12px; border: 1px solid #1E2E4A; }}
  .card h2 {{ font-size: 0.78rem; margin-bottom: 10px; color: #94A3B8; text-transform: uppercase; letter-spacing: 0.8px; display: flex; justify-content: space-between; align-items: center; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }}
  .stat-box {{ background: #0B1220; border: 1px solid #1C2A44; border-radius: 10px; padding: 8px 10px; }}
  .stat-k {{ font-size: 0.68rem; color: #64748B; text-transform: uppercase; font-weight: 700; }}
  .stat-v {{ font-size: 1.02rem; font-weight: 700; color: #F8FAFC; margin-top: 2px; }}
  .stat-s {{ font-size: 0.7rem; color: #94A3B8; margin-top: 1px; }}
  .svc-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
  .svc-chip {{ padding: 3px 8px; border-radius: 6px; font-size: 0.72rem; font-weight: 700; background: #0B1220; border: 1px solid #1E293B; color: #94A3B8; }}
  .svc-on {{ color: #34D399; border-color: #065F46; background: #072219; }}
  .svc-off {{ color: #FCA5A5; border-color: #991B1B; background: #260B0B; }}
  button, input, select {{ width: 100%; padding: 10px 12px; border-radius: 9px; border: none; font-size: 0.92rem; margin-top: 6px; }}
  input, select {{ background: #09111E; color: #F8FAFC; border: 1px solid #334155; }}
  button {{ background: #0284C7; color: #FFFFFF; font-weight: 600; cursor: pointer; transition: transform 0.1s, opacity 0.15s; }}
  button:active {{ transform: scale(0.97); }}
  .btn-row {{ display: flex; gap: 8px; margin-top: 6px; }}
  .btn-row button, .btn-row select, .btn-row input {{ flex: 1; margin-top: 0; }}
  .page-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
  .page-grid button {{ margin-top: 0; background: #16243D; border: 1px solid #263B61; color: #E2E8F0; }}
  .page-grid button.active {{ background: #0284C7; border-color: #38BDF8; color: #FFFFFF; }}
  .btn-amber {{ background: #D97706 !important; border-color: #F59E0B !important; }}
  .btn-green {{ background: #059669 !important; border-color: #10B981 !important; }}
  .btn-red {{ background: #DC2626 !important; border-color: #EF4444 !important; }}
  .btn-muted {{ background: #1E293B !important; color: #CBD5E1 !important; }}
  .range-wrap {{ display: flex; align-items: center; gap: 10px; margin-top: 6px; }}
  input[type=range] {{ flex: 1; padding: 0; height: 8px; accent-color: #38BDF8; background: transparent; border: none; }}
  .links-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
  .link-btn {{ display: block; text-align: center; text-decoration: none; padding: 10px; border-radius: 9px; background: #0B1220; border: 1px solid #22324F; color: #7DD3FC; font-weight: 600; font-size: 0.86rem; }}
  #toast {{ position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); background: #0F172A; color: #38BDF8; border: 1px solid #38BDF8; padding: 9px 16px; border-radius: 999px; font-size: 0.85rem; font-weight: 600; opacity: 0; pointer-events: none; transition: opacity 0.2s; z-index: 99; max-width: 90%; text-align: center; }}
  #toast.show {{ opacity: 1; }}
</style>
</head>
<body>
  <div class="top-bar">
    <h1>Wall Dashboard Remote</h1>
    <span class="ver-pill">v{DASHBOARD_VERSION}</span>
  </div>
  <div class="status-strip">
    <span id="pSlide" class="pill pill-ok">Slide: --</span>
    <span id="pSec" class="pill">Sec Cam: --</span>
    <span id="pNet" class="pill">Net: --</span>
  </div>

  <div class="card">
    <h2>Live Telemetry <span id="stUpdated" style="color:#38BDF8;font-size:0.72rem;">Syncing...</span></h2>
    <div class="stat-grid">
      <div class="stat-box"><div class="stat-k">Weather</div><div class="stat-v" id="stTemp">--</div><div class="stat-s" id="stCond">--</div></div>
      <div class="stat-box"><div class="stat-k">CPU / Temp</div><div class="stat-v" id="stCpu">--</div><div class="stat-s" id="stTempC">--</div></div>
      <div class="stat-box"><div class="stat-k">RAM / Disk</div><div class="stat-v" id="stRam">--</div><div class="stat-s" id="stDisk">--</div></div>
      <div class="stat-box"><div class="stat-k">Bandwidth</div><div class="stat-v" id="stRx">--</div><div class="stat-s" id="stTx">--</div></div>
      <div class="stat-box"><div class="stat-k">AdGuard</div><div class="stat-v" id="stAds">--</div><div class="stat-s" id="stAdsPct">--</div></div>
      <div class="stat-box"><div class="stat-k">Uptime</div><div class="stat-v" id="stUp">--</div><div class="stat-s" id="stUsb">--</div></div>
    </div>
    <div class="svc-row" id="svcRow"></div>
  </div>

  <div class="card">
    <h2>Slide Switcher <span id="rotState" style="color:#FBBF24;font-size:0.72rem;"></span></h2>
    <div class="page-grid">
      <button id="pg0" onclick="setPage(0)">1. Main</button>
      <button id="pg1" onclick="setPage(1)">2. Weather</button>
      <button id="pg2" onclick="setPage(2)">3. System</button>
      <button id="pg3" onclick="setPage(3)">4. Money</button>
      <button id="pg4" onclick="setPage(4)">5. Sky Dome</button>
      <button id="pg5" onclick="setPage(5)">6. Flight Radar</button>
      <button id="pg6" style="grid-column: span 2;" onclick="setPage(6)">7. Server Photos Gallery</button>
    </div>
    <div class="btn-row" style="margin-top:8px;">
      <button class="btn-muted" onclick="stepPage(-1)">&lt; Prev</button>
      <button class="btn-amber" onclick="skipPhoto()">Next Photo</button>
      <button id="btnPause" onclick="togglePause()">Pause</button>
      <button class="btn-muted" onclick="stepPage(1)">Next &gt;</button>
    </div>
  </div>

  <div class="card">
    <h2>Banner Message</h2>
    <input type="text" id="msgText" placeholder="e.g. Dinner is ready!" />
    <div class="btn-row" style="margin-top:8px;">
      <select id="msgDur">
        <option value="30">30 sec</option>
        <option value="60" selected>1 min</option>
        <option value="300">5 mins</option>
        <option value="900">15 mins</option>
      </select>
      <button onclick="sendMsg()">Show</button>
      <button class="btn-muted" onclick="clearMsg()">Clear</button>
    </div>
  </div>

  <div class="card">
    <h2>Display, Theme &amp; Brightness <span id="briLbl" style="color:#38BDF8;">100%</span></h2>
    <div class="range-wrap">
      <span style="font-size:0.8rem;color:#94A3B8;">20%</span>
      <input type="range" id="briSlider" min="20" max="100" step="5" value="100" oninput="document.getElementById('briLbl').textContent=this.value+'%'" onchange="setBrightness(this.value/100)" />
      <span style="font-size:0.8rem;color:#94A3B8;">100%</span>
    </div>
    <div class="btn-row" style="margin-top:10px;">
      <button class="btn-muted" onclick="setTheme('auto')">Auto</button>
      <button class="btn-muted" onclick="setTheme('day')">Day</button>
      <button class="btn-muted" onclick="setTheme('evening')">Evening</button>
      <button class="btn-muted" onclick="setTheme('night')">Night</button>
      <button class="btn-muted" onclick="setTheme('oled')">OLED</button>
    </div>
    <div class="btn-row" style="margin-top:8px;">
      <button class="btn-muted" onclick="toggleTimeFmt()">Toggle 12h / 24h</button>
      <button class="btn-green" onclick="toggleMap()">Flight Map</button>
      <button class="btn-amber" onclick="toggleSky()">Sky Dome</button>
    </div>
  </div>

  <div class="card">
    <h2>Countdown Timer <span id="tmrLbl" style="color:#FBBF24;"></span></h2>
    <div class="btn-row">
      <button onclick="setTimer(60)">1m</button>
      <button onclick="setTimer(300)">5m</button>
      <button onclick="setTimer(600)">10m</button>
      <button onclick="setTimer(900)">15m</button>
      <button onclick="setTimer(1800)">30m</button>
    </div>
    <div class="btn-row" style="margin-top:8px;">
      <input type="number" id="customMin" min="1" max="180" placeholder="Custom mins" />
      <button onclick="setCustomTimer()">Start</button>
      <button class="btn-red" onclick="cancelTimer()">Cancel</button>
    </div>
  </div>

  <div class="card">
    <h2>Server Apps</h2>
    <div class="links-grid">
      <a class="link-btn" id="lnkFb" target="_blank">FileBrowser (:8080)</a>
      <a class="link-btn" id="lnkCp" target="_blank">Copyparty (:3923)</a>
      <a class="link-btn" id="lnkAg" target="_blank">AdGuard Home (:8081)</a>
      <a class="link-btn" id="lnkCk" target="_blank">Cockpit HUD (:9090)</a>
    </div>
  </div>

  <div id="toast"></div>

<script>
  const token = "{token}";
  const host = window.location.hostname || "192.168.1.100";
  document.getElementById("lnkFb").href = "http://" + host + ":8080/";
  document.getElementById("lnkCp").href = "http://" + host + ":3923/";
  document.getElementById("lnkAg").href = "http://" + host + ":8081/";
  document.getElementById("lnkCk").href = "https://" + host + ":9090/";

  let toastT = null;
  function showToast(msg) {{
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastT);
    toastT = setTimeout(() => el.classList.remove("show"), 2200);
  }}
  function postApi(endpoint, data) {{
    fetch("/api/" + endpoint + "?token=" + encodeURIComponent(token), {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(data || {{}})
    }}).then(r => r.json()).then(res => {{
      showToast(res.status || res.error || "Done");
      setTimeout(refreshStatus, 350);
    }}).catch(() => showToast("Connection error"));
  }}
  function sendMsg() {{
    const val = document.getElementById("msgText").value.trim();
    const dur = parseInt(document.getElementById("msgDur").value || "60", 10);
    if (val) postApi("message", {{text: val, duration: dur}});
  }}
  function clearMsg() {{ postApi("message/clear", {{}}); }}
  function setPage(n) {{ postApi("page", {{page: n}}); }}
  function stepPage(d) {{ postApi("page/step", {{delta: d}}); }}
  function skipPhoto() {{ postApi("photo/skip", {{}}); }}
  function togglePause() {{ postApi("rotate/toggle", {{}}); }}
  function toggleMap() {{ postApi("flights/toggle", {{}}); }}
  function toggleSky() {{ postApi("sky/toggle", {{}}); }}
  function toggleTimeFmt() {{ postApi("time_format", {{}}); }}
  function setTheme(t) {{ postApi("theme", {{theme: t}}); }}
  function setTimer(secs) {{ postApi("timer", {{seconds: secs}}); }}
  function setCustomTimer() {{
    const m = parseInt(document.getElementById("customMin").value || "0", 10);
    if (m > 0) setTimer(m * 60);
  }}
  function cancelTimer() {{ postApi("timer/cancel", {{}}); }}
  function setBrightness(b) {{ postApi("brightness", {{level: b}}); }}

  function refreshStatus() {{
    fetch("/api/status?token=" + encodeURIComponent(token))
      .then(r => r.json())
      .then(d => {{
        const s = d.system || {{}};
        const w = d.weather || {{}};
        document.getElementById("pSlide").textContent = "Slide: " + d.active_page_name + (d.rotation_paused ? " [PAUSED]" : "");
        const pSec = document.getElementById("pSec");
        pSec.textContent = s.security_armed ? "Sec Cam: ARMED" : ("Sec Cam: Standby (" + (s.security_events || 0) + ")");
        pSec.className = "pill " + (s.security_armed ? "pill-err" : "pill-ok");
        const pNet = document.getElementById("pNet");
        pNet.textContent = "Inet: " + (s.inet_ping || "--") + " | " + (s.lan_ip || "");
        pNet.className = "pill " + (s.inet_ping === "Offline" ? "pill-err" : "pill-ok");

        document.getElementById("stTemp").textContent = w.temp || "--";
        document.getElementById("stCond").textContent = (w.cond || "--") + " | Rain " + (w.rain_chance || "--");
        document.getElementById("stCpu").textContent = (s.cpu_percent != null ? s.cpu_percent : "--") + "%";
        document.getElementById("stTempC").textContent = (s.cpu_temp != null ? s.cpu_temp : "--") + " C | Load " + (s.load_avg || "--");
        document.getElementById("stRam").textContent = (s.ram_percent != null ? s.ram_percent : "--") + "% RAM";
        document.getElementById("stDisk").textContent = (s.disk_free_gb != null ? s.disk_free_gb : "--") + " GB free";
        document.getElementById("stRx").textContent = "DN " + (s.net_rx || "--");
        document.getElementById("stTx").textContent = "UP " + (s.net_tx || "--");
        document.getElementById("stAds").textContent = s.ads_blocked || "--";
        document.getElementById("stAdsPct").textContent = (s.ads_percent || "--") + " of " + (s.dns_queries || "--");
        document.getElementById("stUp").textContent = s.uptime_str || "--";
        document.getElementById("stUsb").textContent = s.usb_status || "USB: --";
        document.getElementById("stUpdated").textContent = "Live " + new Date().toLocaleTimeString();

        for (let i = 0; i < 7; i++) {{
          const btn = document.getElementById("pg" + i);
          if (btn) btn.classList.toggle("active", i === d.active_page);
        }}
        document.getElementById("rotState").textContent = d.rotation_paused ? "PAUSED" : "AUTO";
        document.getElementById("btnPause").textContent = d.rotation_paused ? "Resume" : "Pause";
        const briPct = Math.round((d.brightness || 1.0) * 100);
        document.getElementById("briSlider").value = briPct;
        document.getElementById("briLbl").textContent = briPct + "%";
        if (d.timer_remaining !== null && d.timer_remaining !== undefined) {{
          const mm = Math.floor(d.timer_remaining / 60);
          const ss = d.timer_remaining % 60;
          document.getElementById("tmrLbl").textContent = String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
        }} else {{
          document.getElementById("tmrLbl").textContent = "";
        }}

        const row = document.getElementById("svcRow");
        const svcs = s.services || {{}};
        row.innerHTML = Object.keys(svcs).map(k => {{
          const ok = svcs[k];
          return '<span class="svc-chip ' + (ok ? 'svc-on' : 'svc-off') + '">' + k + ' ' + (ok ? 'OK' : 'DOWN') + '</span>';
        }}).join("");
      }}).catch(() => {{}});
  }}
  refreshStatus();
  setInterval(refreshStatus, 5000);
</script>
</body>
</html>
"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        token = params.get("token", [""])[0]

        expected_token = CONFIG["phone_control"].get("token", "CHANGE_ME_TOKEN")
        if token != expected_token:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        data = {}
        if body:
            try:
                data = json.loads(body.decode("utf-8"))
            except: pass

        endpoint = parsed.path.replace("/api/", "")

        with STATE.lock:
            if endpoint == "message":
                text = safe_tcl(data.get("text", ""))
                dur = max(5, min(3600, int(data.get("duration", 60))))
                STATE.user_message = text
                STATE.user_message_until = time.time() + dur
                resp = {"status": f"Banner displayed for {dur}s"}
            elif endpoint == "message/clear":
                STATE.user_message = None
                STATE.user_message_until = 0
                resp = {"status": "Banner cleared"}
            elif endpoint == "page":
                p = max(0, min(6, int(data.get("page", 0))))
                STATE.page_switch_req = p
                resp = {"status": f"Switched to slide {p + 1}"}
            elif endpoint == "page/step":
                delta = int(data.get("delta", 1))
                p = (STATE.active_page + delta) % 7
                STATE.page_switch_req = p
                resp = {"status": f"Switched to slide {p + 1}"}
            elif endpoint == "theme":
                th = str(data.get("theme", "auto")).lower()
                if th in ("auto", "day", "evening", "night", "oled"):
                    STATE.theme_switch_req = th
                    resp = {"status": f"Theme set to {th.upper()}"}
                else:
                    resp = {"error": "Invalid theme"}
            elif endpoint == "time_format":
                STATE.time_fmt_toggle_req = True
                resp = {"status": "Toggled 12h/24h format"}
            elif endpoint == "photo/skip":
                STATE.skip_photo_requested = True
                resp = {"status": "Photo skipped"}
            elif endpoint == "rotate/toggle":
                STATE.rotation_paused = not STATE.rotation_paused
                resp = {"status": "Rotation paused" if STATE.rotation_paused else "Rotation resumed"}
            elif endpoint == "flights/toggle":
                STATE.flight_map_toggle_req = True
                resp = {"status": "Toggled flight map"}
            elif endpoint == "sky/toggle":
                STATE.sky_dome_toggle_req = True
                resp = {"status": "Toggled Sky & ISS Dome"}
            elif endpoint == "sky/next":
                STATE.sky_pass_cycle_req = True
                resp = {"status": "Cycled ISS pass"}
            elif endpoint == "timer":
                secs = max(5, min(10800, int(data.get("seconds", 300))))
                STATE.timer_seconds_remaining = secs
                STATE.timer_is_ringing = False
                resp = {"status": f"Timer started ({secs // 60}m {secs % 60}s)"}
            elif endpoint == "timer/cancel":
                STATE.timer_seconds_remaining = None
                STATE.timer_is_ringing = False
                resp = {"status": "Timer canceled"}
            elif endpoint == "brightness":
                lvl = float(data.get("level", 1.0))
                set_screen_brightness(lvl)
                resp = {"status": f"Brightness set to {lvl}"}
            else:
                resp = {"error": "Unknown endpoint"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode("utf-8"))

def start_phone_server():
    port = CONFIG["phone_control"].get("port", 8765)
    try:
        server = _ThreadingHTTPServer(("0.0.0.0", port), PhoneControlHandler)
        log_msg(f"Phone control server running on port {port}")
        server.serve_forever()
    except Exception as e:
        log_msg(f"Phone control server error: {e}")

# ================= SMOOTH NEWS TICKER =================
class ModernNewsBar(tk.Canvas):
    def __init__(self, master, width=1024, height=90, theme=None):
        bg = theme["news_bg"] if theme else "#000000"
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0)
        self.width = width
        self.height = height
        self.news_idx = 0
        self.theme = theme
        self.show_next()

    def update_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme["news_bg"])

    def show_next(self):
        with STATE.lock:
            items = list(STATE.news)

        if not items:
            source = "BREAKING"
            title = "Loading live news feeds..."
            is_new = False
            highlight = False
        else:
            if self.news_idx >= len(items):
                self.news_idx = 0
            item = items[self.news_idx]
            self.news_idx += 1
            source = item["source"]
            title = safe_tcl(item["title"])
            is_new = item.get("is_new", False)
            highlight = item.get("highlight", False)

        self.delete("all")

        source_colors = {
            "BBC": ("#B91C1C", "#FFFFFF"),
            "TOI": ("#1D4ED8", "#FFFFFF"),
            "DT NEXT": ("#047857", "#FFFFFF"),
            "BREAKING": ("#DC2626", "#FFFFFF")
        }
        chip_bg, chip_fg = source_colors.get(source, ("#374151", "#FFFFFF"))

        chip_w = 84
        chip_h = 28
        chip_x = 20
        chip_y = (self.height - chip_h) / 2
        self.create_rectangle(chip_x, chip_y, chip_x + chip_w, chip_y + chip_h, fill=chip_bg, outline="", width=0)
        self.create_text(chip_x + chip_w / 2, chip_y + chip_h / 2, text=source, font=("DejaVu Sans", 11, "bold"), fill=chip_fg)

        text_x = chip_x + chip_w + 16

        if is_new:
            badge_w = 46
            badge_h = 24
            badge_y = (self.height - badge_h) / 2
            self.create_rectangle(text_x, badge_y, text_x + badge_w, badge_y + badge_h, fill="#EF4444", outline="")
            self.create_text(text_x + badge_w / 2, badge_y + badge_h / 2, text="NEW", font=("DejaVu Sans", 9, "bold"), fill="white")
            text_x += badge_w + 12

        max_wrap = self.width - text_x - 30
        head_color = "#FEF08A" if highlight else (self.theme["fg"] if self.theme else "#FFFFFF")
        self.create_text(
            text_x,
            self.height / 2,
            text=title,
            font=("DejaVu Sans", 16, "bold"),
            fill=head_color,
            anchor="w",
            width=max_wrap
        )

        read_time = CONFIG["news"].get("read_time_seconds", 10)
        self.after(read_time * 1000, self.show_next)

# ================= CANVAS HOURLY CHART (NO MATPLOTLIB) =================
class WeatherChartCanvas(tk.Canvas):
    def __init__(self, master, width=540, height=190, bg="#151F32"):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0)
        self.width = width
        self.height = height

    def draw_chart(self, hourly_data, theme):
        self.delete("all")
        self.configure(bg=theme["card_bg"])

        if not hourly_data or len(hourly_data) < 2:
            self.create_text(self.width/2, self.height/2, text="Loading hourly forecast...", fill=theme["fg_sub"], font=("DejaVu Sans", 12))
            return

        points = hourly_data[:12]
        n = len(points)

        temps = [p["temp"] for p in points if isinstance(p["temp"], (int, float))]
        if not temps:
            return
        min_t = min(temps) - 1
        max_t = max(temps) + 1
        t_range = max(1, max_t - min_t)

        pad_x = 35
        pad_top = 28
        pad_bottom = 35
        chart_h = self.height - pad_top - pad_bottom
        step_x = (self.width - 2 * pad_x) / (n - 1)

        coords = []
        for i, p in enumerate(points):
            x = pad_x + i * step_x
            t = p["temp"] if isinstance(p["temp"], (int, float)) else min_t
            y = pad_top + chart_h - ((t - min_t) / t_range) * chart_h
            coords.append((x, y, p))

        # Rain bars at bottom
        for x, y, p in coords:
            prob = p.get("rain_prob", 0)
            if prob > 0:
                bar_h = (prob / 100.0) * 24
                bar_y1 = self.height - pad_bottom
                bar_y0 = bar_y1 - bar_h
                self.create_rectangle(x - 8, bar_y0, x + 8, bar_y1, fill="#0284C7", outline="")

        # Draw temperature smooth line
        line_pts = []
        for x, y, _ in coords:
            line_pts.extend([x, y])
        self.create_line(line_pts, fill=theme["accent"], width=3, smooth=True)

        # Draw dots, temperatures, hour labels
        for x, y, p in coords:
            self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=theme["accent"], outline=theme["card_bg"], width=2)
            self.create_text(x, y - 12, text=f"{p['temp']}°", fill=theme["fg"], font=("DejaVu Sans", 10, "bold"))
            self.create_text(x, self.height - 14, text=p["hour"], fill=theme["fg_sub"], font=("DejaVu Sans", 9))

# ================= MAIN GUI APPLICATION =================
class AnimatedWeatherIcon:
    """Lightweight Tkinter Canvas animated weather icon (~7 FPS) for low-CPU systems."""

    def __init__(self, parent, size=100, bg="#1E293B"):
        self.size = size
        self.bg = bg
        self.condition = "cloud"
        self.frame = 0
        self.canvas = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self._tick()

    def place(self, **kwargs):
        self.canvas.place(**kwargs)

    def set_bg(self, bg):
        if bg != self.bg:
            self.bg = bg
            self.canvas.config(bg=bg)

    def set_condition(self, condition):
        if condition != self.condition:
            self.condition = condition or "cloud"
            self.frame = 0

    def _draw_cloud(self, cx, cy, color="#E2E8F0", shade="#CBD5E1"):
        self.canvas.create_oval(cx - 26, cy - 6, cx - 2, cy + 16, fill=shade, outline="")
        self.canvas.create_oval(cx - 12, cy - 18, cx + 16, cy + 10, fill=color, outline="")
        self.canvas.create_oval(cx + 4, cy - 10, cx + 28, cy + 14, fill=color, outline="")
        self.canvas.create_rectangle(cx - 16, cy, cx + 18, cy + 16, fill=color, outline="")

    def _tick(self):
        c = self.canvas
        c.delete("all")
        f = self.frame
        cond = self.condition

        if cond == "clear_day":
            cx, cy, r = 50, 50, 18 + math.sin(f * 0.25) * 1.5
            ang0 = f * 0.12
            for i in range(8):
                a = ang0 + i * (math.pi / 4)
                x1 = cx + math.cos(a) * (r + 5)
                y1 = cy + math.sin(a) * (r + 5)
                x2 = cx + math.cos(a) * (r + 14)
                y2 = cy + math.sin(a) * (r + 14)
                c.create_line(x1, y1, x2, y2, fill="#FBBF24", width=3, capstyle=tk.ROUND)
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#F59E0B", outline="#FDE68A", width=2)

        elif cond == "clear_night":
            c.create_oval(28, 24, 72, 68, fill="#FDE68A", outline="")
            c.create_oval(40, 20, 80, 60, fill=self.bg, outline="")
            stars = [(22, 24, 0), (76, 30, 2), (72, 72, 4), (28, 74, 1)]
            for sx, sy, ph in stars:
                if (f + ph) % 8 < 5:
                    c.create_oval(sx - 2, sy - 2, sx + 2, sy + 2, fill="#F8FAFC", outline="")

        elif cond in ("cloud", "cloud_day"):
            dx = math.sin(f * 0.22) * 8
            dy = math.cos(f * 0.16) * 2
            sun_r = 12 + math.sin(f * 0.25) * 1.2
            c.create_oval(70 - sun_r + dx * 0.3, 30 - sun_r, 70 + sun_r + dx * 0.3, 30 + sun_r, fill="#F59E0B", outline="#FDE68A", width=1.5)
            self._draw_cloud(48 + dx, 54 + dy)

        elif cond == "cloud_night":
            dx = math.sin(f * 0.22) * 8
            dy = math.cos(f * 0.16) * 2
            c.create_oval(54, 14, 82, 42, fill="#FDE68A", outline="")
            c.create_oval(62, 11, 88, 37, fill=self.bg, outline="")
            for sx, sy, ph in [(24, 22, 0), (86, 44, 3), (36, 16, 5)]:
                if (f + ph) % 8 < 5:
                    c.create_oval(sx - 2, sy - 2, sx + 2, sy + 2, fill="#F8FAFC", outline="")
            self._draw_cloud(48 + dx, 56 + dy, color="#CBD5E1", shade="#94A3B8")

        elif cond == "rain":
            dx = math.sin(f * 0.15) * 3
            self._draw_cloud(50 + dx, 42, color="#CBD5E1", shade="#94A3B8")
            for i, rx in enumerate([32, 44, 56, 68]):
                ry = 62 + ((f * 5 + i * 9) % 28)
                c.create_line(rx, ry, rx - 3, ry + 9, fill="#38BDF8", width=2.5, capstyle=tk.ROUND)

        elif cond == "storm":
            dx = math.sin(f * 0.25) * 2
            self._draw_cloud(50 + dx, 38, color="#94A3B8", shade="#64748B")
            for i, rx in enumerate([30, 42, 64]):
                ry = 58 + ((f * 6 + i * 11) % 28)
                c.create_line(rx, ry, rx - 3, ry + 8, fill="#38BDF8", width=2, capstyle=tk.ROUND)
            if f % 14 in (2, 3, 5):
                c.create_polygon(52, 52, 43, 68, 51, 68, 46, 86, 61, 64, 53, 64, fill="#FACC15", outline="")

        elif cond == "mist":
            for i, y in enumerate([34, 48, 62, 74]):
                ox = math.sin(f * 0.2 + i * 1.1) * 7
                c.create_line(22 + ox, y, 78 + ox, y, fill="#CBD5E1", width=4, capstyle=tk.ROUND)

        elif cond == "snow":
            dx = math.sin(f * 0.15) * 3
            self._draw_cloud(50 + dx, 40)
            for i, sx in enumerate([32, 45, 58, 70]):
                sy = 60 + ((f * 3 + i * 8) % 28)
                sway = math.sin(f * 0.3 + i) * 3
                c.create_oval(sx + sway - 3, sy - 3, sx + sway + 3, sy + 3, fill="#F8FAFC", outline="")

        else:
            self._draw_cloud(50, 52)

        self.frame = (self.frame + 1) % 10000
        c.after(140, self._tick)

class DashboardApp:
    def __init__(self, root):
        self.root = root

        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        self.screen_w = screen_w
        self.screen_h = screen_h

        root.geometry(f"{screen_w}x{screen_h}+0+0")
        root.attributes("-fullscreen", True)
        root.configure(bg="#000000", cursor="none")

        self.news_h = 86
        self.header_h = 30
        self.top_h = screen_h - self.news_h - self.header_h
        self.sidebar_w = 440
        self.right_w = screen_w - self.sidebar_w

        self.current_theme_name = "day"
        self.theme = THEMES["day"]
        self.key_hints_visible = False
        self.burn_shift_x = 0
        self.burn_shift_y = 0
        self._themed_cards = []
        self._themed_titles = []

        self.build_frames()

        root.bind("<Escape>", lambda e: self.quit_app())
        root.bind("<Left>", lambda e: self.change_page(-1))
        root.bind("<Right>", lambda e: self.change_page(1))
        root.bind("<space>", lambda e: self.toggle_pause_rotation())
        root.bind("<n>", lambda e: self.skip_photo())
        root.bind("<N>", lambda e: self.skip_photo())
        root.bind("<b>", lambda e: self.toggle_night_mode())
        root.bind("<B>", lambda e: self.toggle_night_mode())
        root.bind("<t>", lambda e: self.cycle_theme())
        root.bind("<T>", lambda e: self.cycle_theme())
        root.bind("<f>", lambda e: self.toggle_12_24h())
        root.bind("<F>", lambda e: self.toggle_12_24h())
        root.bind("<h>", lambda e: self.toggle_key_hints())
        root.bind("<H>", lambda e: self.toggle_key_hints())
        root.bind("<m>", lambda e: self.toggle_flight_map())
        root.bind("<M>", lambda e: self.toggle_flight_map())
        root.bind("<s>", lambda e: self.toggle_sky_dome())
        root.bind("<S>", lambda e: self.toggle_sky_dome())
        root.bind("<p>", lambda e: self.toggle_server_photos())
        root.bind("<P>", lambda e: self.toggle_server_photos())
        root.bind("<KeyPress-1>", lambda e: self.select_sky_pass(0))
        root.bind("<KeyPress-2>", lambda e: self.select_sky_pass(1))
        root.bind("<KeyPress-3>", lambda e: self.select_sky_pass(2))
        root.bind("<Key>", lambda e: self.on_any_keypress())

        threading.Thread(target=weather_worker, daemon=True).start()
        threading.Thread(target=news_worker, daemon=True).start()
        threading.Thread(target=rates_and_quotes_worker, daemon=True).start()
        threading.Thread(target=system_stats_worker, daemon=True).start()
        threading.Thread(target=events_worker, daemon=True).start()
        threading.Thread(target=flights_worker, daemon=True).start()
        threading.Thread(target=space_worker, daemon=True).start()
        threading.Thread(target=photo_manager_worker, args=(self.right_w, self.top_h), daemon=True).start()
        if CONFIG["phone_control"].get("enabled", True):
            threading.Thread(target=start_phone_server, daemon=True).start()

        self.last_photo_change = 0.0
        self.last_page_change = time.time()
        self.active_photo_path = None
        self.tk_photo_cache = None
        self.flight_map_dismissed_hexes = set()
        self._seen_overhead_hexes = set()
        self._flight_auto_active = False
        self._flight_auto_return_page = 0
        self._active_map_flight = None
        self._active_map_is_live = False
        self._map_anim_after = None
        self._map_anim_frame = 0
        self._map_tile_key = None
        self._map_loading_key = None
        self._map_pending_mosaic = None
        self._map_bg_photo = None
        self._map_bg_key = None
        self._flight_toggle_box = None
        self._sky_pinned_idx = 0
        self._sky_pinned_until = 0.0
        self._sky_anim_after = None
        self._sky_anim_frame = 0
        self._sky_card_boxes = []
        self._sky_toggle_box = None
        self._server_photo_idx = -1
        self._server_photo_after = None
        self._server_photo_tk = None
        self._server_photo_path = None
        self._photos_toggle_box = None
        self._rot_cursor = 0

        self.update_clock()
        self.update_weather_display()
        self.update_slideshow()
        self.update_page_rotation()
        self.update_burn_in_protection()
        self.update_theme_and_brightness()

    def build_frames(self):
        # 1. Header + 2px Slide Cycle Progress Bar
        self.header_frame = tk.Frame(self.root, bg="#070C16", width=self.screen_w, height=self.header_h - 2)
        self.header_frame.place(x=0, y=0)
        self.header_frame.pack_propagate(False)

        self.lbl_sec_pill = tk.Label(
            self.header_frame, text="", font=("DejaVu Sans", 8, "bold"),
            bg="#991B1B", fg="#FFFFFF", padx=6, pady=0
        )

        self.lbl_alert_banner = tk.Label(
            self.header_frame, text="", font=("DejaVu Sans", 10, "bold"),
            bg="#070C16", fg="#F87171", anchor="w"
        )
        self.lbl_alert_banner.pack(side=tk.LEFT, padx=(12, 8))

        self.lbl_ver_pill = tk.Label(
            self.header_frame, text=f"v{DASHBOARD_VERSION}", font=("DejaVu Sans", 8, "bold"),
            bg="#172554", fg="#7DD3FC", padx=6, pady=0
        )
        self.lbl_ver_pill.pack(side=tk.RIGHT, padx=(6, 10), pady=4)

        self.page_dots_frame = tk.Frame(self.header_frame, bg="#070C16")
        self.page_dots_frame.pack(side=tk.RIGHT, padx=(6, 4))
        self.page_dot_labels = []
        for idx_dot in range(7):
            dl = tk.Label(
                self.page_dots_frame, text="\u25cf", font=("DejaVu Sans", 8),
                bg="#070C16", fg="#334155", padx=2, cursor="hand2"
            )
            dl.pack(side=tk.LEFT)
            dl.bind("<Button-1>", lambda e, p=idx_dot: self._on_dot_click(p))
            self.page_dot_labels.append(dl)

        self.lbl_header_status = tk.Label(
            self.header_frame, text="", font=("DejaVu Sans", 9),
            bg="#070C16", fg="#94A3B8"
        )
        self.lbl_header_status.pack(side=tk.RIGHT, padx=(8, 4))

        self.cycle_progress_canvas = tk.Canvas(
            self.root, width=self.screen_w, height=2,
            bg="#0F172A", highlightthickness=0
        )
        self.cycle_progress_canvas.place(x=0, y=self.header_h - 2)
        self._progress_bar_id = self.cycle_progress_canvas.create_rectangle(
            0, 0, 0, 2, fill="#38BDF8", outline=""
        )

        # 2. Main content container
        self.content_frame = tk.Frame(
            self.root, bg=self.theme["bg"], width=self.screen_w, height=self.top_h
        )
        self.content_frame.place(x=0, y=self.header_h)

        # PAGE 1: MAIN
        self.page_main = tk.Frame(self.content_frame, bg=self.theme["bg"], width=self.screen_w, height=self.top_h)
        self.page_main.place(x=0, y=0)

        self.sidebar_frame = tk.Frame(self.page_main, bg=self.theme["bg"], width=self.sidebar_w, height=self.top_h)
        self.sidebar_frame.place(x=0, y=0)

        self.slideshow_canvas = tk.Canvas(
            self.page_main, width=self.right_w, height=self.top_h,
            bg="#000000", highlightthickness=0
        )
        self.slideshow_canvas.place(x=self.sidebar_w, y=0)

        self.photo_caption_bg_id = self.slideshow_canvas.create_rectangle(
            10, self.top_h - 32, 170, self.top_h - 8,
            fill="#070C16", outline="#1E293B", width=1
        )
        self.photo_caption_id = self.slideshow_canvas.create_text(
            18, self.top_h - 20, text="Random Photo", anchor="w",
            fill="#E2E8F0", font=("DejaVu Sans", 9, "bold")
        )

        self.build_clock_card()
        self.build_weather_card()
        self.build_event_card()

        # Extra Pages (Full-Screen Slides)
        self.page_weather = tk.Frame(self.content_frame, bg=self.theme["bg"], width=self.screen_w, height=self.top_h)
        self.build_page_weather()

        self.page_system = tk.Frame(self.content_frame, bg=self.theme["bg"], width=self.screen_w, height=self.top_h)
        self.build_page_system()

        self.page_money = tk.Frame(self.content_frame, bg=self.theme["bg"], width=self.screen_w, height=self.top_h)
        self.build_page_money()

        self.page_sky = tk.Frame(self.content_frame, bg="#060D1A", width=self.screen_w, height=self.top_h)
        self.build_page_sky()

        self.page_flight = tk.Frame(self.content_frame, bg="#060D1A", width=self.screen_w, height=self.top_h)
        self.build_page_flight()

        self.page_photos = tk.Frame(self.content_frame, bg="#060D1A", width=self.screen_w, height=self.top_h)
        self.build_page_photos()

        # 3. Bottom News
        self.news_bar = ModernNewsBar(self.root, width=self.screen_w, height=self.news_h, theme=self.theme)
        self.news_bar.place(x=0, y=self.screen_h - self.news_h)

        # 4. Key Hints
        self.hint_bar = tk.Label(
            self.root, text="[<-/->] Slide  [Space] Pause  [N] Next Photo  [P] Photos  [S] Sky  [M] Flight  [T] Theme  [B] Night  [F] 12/24h  [Esc] Exit",
            font=("DejaVu Sans", 9), bg="#1E293B", fg="#E2E8F0"
        )

        # 5. Timer Modal
        self.timer_overlay = tk.Frame(self.root, bg="#0F172A", bd=4, relief="ridge")
        self.lbl_timer_title = tk.Label(self.timer_overlay, text="COUNTDOWN TIMER", font=("DejaVu Sans", 14, "bold"), fg="#38BDF8", bg="#0F172A")
        self.lbl_timer_title.pack(pady=(12, 4))
        self.lbl_timer_digits = tk.Label(self.timer_overlay, text="00:00", font=("DejaVu Sans", 48, "bold"), fg="#F8FAFC", bg="#0F172A")
        self.lbl_timer_digits.pack(pady=4)

    def build_clock_card(self):
        self.card_clock = tk.Frame(self.sidebar_frame, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_clock.place(x=14, y=8, width=self.sidebar_w - 28, height=138)

        self.lbl_time = tk.Label(self.card_clock, text="00:00", font=("DejaVu Sans", 48, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_time.place(x=14, y=10)

        self.lbl_ampm = tk.Label(self.card_clock, text="AM", font=("DejaVu Sans", 18, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        self.lbl_ampm.place(x=230, y=36)

        self.lbl_date = tk.Label(self.card_clock, text="Saturday, 24 October", font=("DejaVu Sans", 15), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_date.place(x=16, y=78)

        loc_text = safe_tcl(f"LOCATION: {CONFIG['location_name']}")
        self.lbl_location = tk.Label(self.card_clock, text=loc_text, font=("DejaVu Sans", 12, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_location.place(x=16, y=108)

    def build_weather_card(self):
        self.card_weather = tk.Frame(self.sidebar_frame, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_weather.place(x=14, y=154, width=self.sidebar_w - 28, height=172)

        self.lbl_weather_temp = tk.Label(self.card_weather, text="--°C", font=("DejaVu Sans", 42, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_weather_temp.place(x=14, y=12)

        self.weather_icon = AnimatedWeatherIcon(self.card_weather, size=100, bg=self.theme["card_bg"])
        self.weather_icon.place(x=260, y=10)

        self.lbl_weather_desc = tk.Label(self.card_weather, text="Loading...", font=("DejaVu Sans", 15, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        self.lbl_weather_desc.place(x=16, y=72)

        self.lbl_weather_feels = tk.Label(self.card_weather, text="Feels: --  •  H: --  L: --", font=("DejaVu Sans", 11), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_weather_feels.place(x=16, y=104)

        self.lbl_weather_extras = tk.Label(self.card_weather, text="Rain: --%  •  Humidity: --%  •  Wind: --", font=("DejaVu Sans", 10), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_weather_extras.place(x=16, y=134)

    def build_event_card(self):
        self.card_event = tk.Frame(self.sidebar_frame, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_event.place(x=14, y=334, width=self.sidebar_w - 28, height=118)

        t_ev_main = tk.Label(self.card_event, text="UPCOMING EVENT & REMINDERS", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_ev_main.place(x=14, y=8)
        self._themed_titles.append(t_ev_main)

        self.lbl_event_title = tk.Label(self.card_event, text="Checking calendar...", font=("DejaVu Sans", 13, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_event_title.place(x=14, y=36)

        self.lbl_event_badge = tk.Label(self.card_event, text="--", font=("DejaVu Sans", 11, "bold"), bg="#0284C7", fg="white", padx=8, pady=2)
        self.lbl_event_badge.place(x=14, y=72)

        self.lbl_event_sub = tk.Label(self.card_event, text="", font=("DejaVu Sans", 10), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_event_sub.place(x=120, y=74)

    # ---------------- PAGE 5: FULL-SCREEN SKY & ISS DOME ----------------
    def build_page_sky(self):
        self.sky_dome_h = self.top_h - 96
        self.sky_canvas = tk.Canvas(
            self.page_sky, width=self.screen_w, height=self.sky_dome_h,
            bg="#060D1A", highlightthickness=0
        )
        self.sky_canvas.place(x=0, y=0)
        self.sky_canvas.bind("<Button-1>", self._on_sky_canvas_click)

        self.sky_hud_accent = tk.Frame(self.page_sky, bg=self.theme["accent"], width=self.screen_w, height=2)
        self.sky_hud_accent.place(x=0, y=self.sky_dome_h)

        self.sky_hud = tk.Frame(
            self.page_sky, bg="#0B1324", width=self.screen_w, height=94
        )
        self.sky_hud.place(x=0, y=self.sky_dome_h + 2)

        self.lbl_sky_badge = tk.Label(
            self.sky_hud, text=safe_tcl("LOCAL SKY"),
            font=("DejaVu Sans", 9, "bold"), bg="#D97706", fg="#FFFFFF", padx=8, pady=1
        )
        self.lbl_sky_badge.place(x=14, y=8)

        self.lbl_sky_title = tk.Label(
            self.sky_hud, text="", font=("DejaVu Sans", 11, "bold"),
            bg="#0B1324", fg="#F8FAFC", anchor="w"
        )
        self.lbl_sky_title.place(x=142, y=8)

        self.lbl_sky_peak = tk.Label(
            self.sky_hud, text="", font=("DejaVu Sans", 10, "bold"),
            bg="#172554", fg="#38BDF8", padx=9, pady=1
        )
        self.lbl_sky_peak.place(relx=1.0, x=-14, y=8, anchor="ne")

        self.lbl_sky_look = tk.Label(
            self.sky_hud, text="", font=("DejaVu Sans", 12, "bold"),
            bg="#0B1324", fg="#FBBF24", anchor="w"
        )
        self.lbl_sky_look.place(x=14, y=36)

        self.lbl_sky_meta = tk.Label(
            self.sky_hud, text="", font=("DejaVu Sans", 10),
            bg="#0B1324", fg="#94A3B8", anchor="w"
        )
        self.lbl_sky_meta.place(x=14, y=64)

    # ---------------- PAGE 6: FULL-SCREEN FLIGHT TRACK RADAR ----------------
    def build_page_flight(self):
        self.flight_map_h = self.top_h - 96
        self.flight_map_canvas = tk.Canvas(
            self.page_flight, width=self.screen_w, height=self.flight_map_h,
            bg="#060D1A", highlightthickness=0
        )
        self.flight_map_canvas.place(x=0, y=0)
        self.flight_map_canvas.bind("<Button-1>", self._on_flight_canvas_click)

        self.flight_hud_accent = tk.Frame(self.page_flight, bg=self.theme["accent"], width=self.screen_w, height=2)
        self.flight_hud_accent.place(x=0, y=self.flight_map_h)

        self.flight_hud = tk.Frame(
            self.page_flight, bg="#0B1324", width=self.screen_w, height=94
        )
        self.flight_hud.place(x=0, y=self.flight_map_h + 2)

        self.lbl_flight_badge = tk.Label(
            self.flight_hud, text=safe_tcl("\u2708 OVERHEAD HOME"),
            font=("DejaVu Sans", 9, "bold"), bg="#DC2626", fg="#FFFFFF", padx=8, pady=1
        )
        self.lbl_flight_badge.place(x=14, y=8)

        self.lbl_flight_title = tk.Label(
            self.flight_hud, text="", font=("DejaVu Sans", 11, "bold"),
            bg="#0B1324", fg="#F8FAFC", anchor="w"
        )
        self.lbl_flight_title.place(x=156, y=8)

        self.lbl_flight_dist = tk.Label(
            self.flight_hud, text="", font=("DejaVu Sans", 10, "bold"),
            bg="#172554", fg="#38BDF8", padx=9, pady=1
        )
        self.lbl_flight_dist.place(relx=1.0, x=-14, y=8, anchor="ne")

        self.lbl_map_orig = tk.Label(
            self.flight_hud, text="", font=("DejaVu Sans", 12, "bold"),
            bg="#0B1324", fg="#10B981", anchor="w"
        )
        self.lbl_map_dst = tk.Label(
            self.flight_hud, text="", font=("DejaVu Sans", 12, "bold"),
            bg="#0B1324", fg="#F59E0B", anchor="e"
        )
        self.flight_prog_canvas = tk.Canvas(
            self.flight_hud, width=420, height=24, bg="#0B1324", highlightthickness=0
        )
        self.lbl_flight_route = tk.Label(
            self.flight_hud, text="", font=("DejaVu Sans", 12, "bold"),
            bg="#0B1324", fg=self.theme["accent"], anchor="w"
        )

        self.lbl_flight_meta = tk.Label(
            self.flight_hud, text="", font=("DejaVu Sans", 10),
            bg="#0B1324", fg="#94A3B8", anchor="w"
        )
        self.lbl_flight_meta.place(x=14, y=64)

    # ---------------- PAGE 7: FULL-SCREEN SERVER PHOTOS GALLERY ----------------
    def build_page_photos(self):
        self.photos_canvas = tk.Canvas(
            self.page_photos, width=self.screen_w, height=self.top_h,
            bg="#060D1A", highlightthickness=0
        )
        self.photos_canvas.place(x=0, y=0)
        self.photos_canvas.bind("<Button-1>", self._on_photos_canvas_click)

    # ---------------- PAGE 2: WEATHER DETAIL ----------------
    def build_page_weather(self):
        self.card_chart = tk.Frame(self.page_weather, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_chart.place(x=14, y=8, width=self.screen_w - 28, height=220)
        self._themed_cards.append(self.card_chart)

        t_ch = tk.Label(self.card_chart, text="12-HOUR FORECAST & PRECIPITATION CHANCE", font=("DejaVu Sans", 11, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_ch.pack(anchor="w", padx=16, pady=(8, 0))
        self._themed_titles.append(t_ch)

        self.hourly_canvas = WeatherChartCanvas(self.card_chart, width=self.screen_w - 60, height=180, bg=self.theme["card_bg"])
        self.hourly_canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        self.card_5day = tk.Frame(self.page_weather, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_5day.place(x=14, y=236, width=540, height=216)
        self._themed_cards.append(self.card_5day)
        t_5d = tk.Label(self.card_5day, text="5-DAY OUTLOOK", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_5d.pack(anchor="w", padx=14, pady=8)
        self._themed_titles.append(t_5d)

        self.daily_labels = []
        for i in range(5):
            row = tk.Frame(self.card_5day, bg=self.theme["card_bg"])
            row.pack(fill=tk.X, padx=14, pady=3)
            lbl_d = tk.Label(row, text="--", font=("DejaVu Sans", 11, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"], width=8, anchor="w")
            lbl_d.pack(side=tk.LEFT)
            lbl_t = tk.Label(row, text="--° / --°", font=("DejaVu Sans", 11), bg=self.theme["card_bg"], fg=self.theme["accent"], anchor="e")
            lbl_t.pack(side=tk.RIGHT)
            self.daily_labels.append((lbl_d, lbl_t))

        self.card_metrics = tk.Frame(self.page_weather, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_metrics.place(x=568, y=236, width=self.screen_w - 582, height=216)
        self._themed_cards.append(self.card_metrics)
        t_mt = tk.Label(self.card_metrics, text="CONDITIONS & ASTRONOMY", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_mt.pack(anchor="w", padx=14, pady=8)
        self._themed_titles.append(t_mt)

        self.lbl_p2_uv_aqi = tk.Label(self.card_metrics, text="UV Index: --  •  AQI: --", font=("DejaVu Sans", 12), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_p2_uv_aqi.pack(anchor="w", padx=14, pady=4)

        self.lbl_p2_sun = tk.Label(self.card_metrics, text="Sunrise: --  •  Sunset: --", font=("DejaVu Sans", 12), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_p2_sun.pack(anchor="w", padx=14, pady=4)

        self.lbl_p2_moon = tk.Label(self.card_metrics, text="Moon: Calculating...", font=("DejaVu Sans", 12), bg=self.theme["card_bg"], fg=self.theme["accent"])
        self.lbl_p2_moon.pack(anchor="w", padx=14, pady=4)

        self.lbl_p2_wind_hum = tk.Label(self.card_metrics, text="Wind: --  •  Humidity: --", font=("DejaVu Sans", 11), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_p2_wind_hum.pack(anchor="w", padx=14, pady=4)

    # ---------------- PAGE 3: HOME & SYSTEM ----------------
    def build_page_system(self):
        cw = (self.screen_w - 42) // 3
        ch = 140

        self.c_cpu = tk.Frame(self.page_system, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.c_cpu.place(x=14, y=8, width=cw, height=ch)
        self._themed_cards.append(self.c_cpu)
        t_cpu = tk.Label(self.c_cpu, text="CPU LOAD & TEMP", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_cpu.pack(anchor="w", padx=12, pady=(6, 2))
        self._themed_titles.append(t_cpu)
        self.lbl_sys_cpu = tk.Label(self.c_cpu, text="--%", font=("DejaVu Sans", 24, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_sys_cpu.pack(anchor="w", padx=12)
        self.lbl_sys_temp = tk.Label(self.c_cpu, text="Temp: --\u00b0C  \u2022  Load: --", font=("DejaVu Sans", 10), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_sys_temp.pack(anchor="w", padx=12, pady=2)

        self.c_ram = tk.Frame(self.page_system, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.c_ram.place(x=14 + cw + 7, y=8, width=cw, height=ch)
        self._themed_cards.append(self.c_ram)
        t_ram = tk.Label(self.c_ram, text="MEMORY (RAM)", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_ram.pack(anchor="w", padx=12, pady=(6, 2))
        self._themed_titles.append(t_ram)
        self.lbl_sys_ram = tk.Label(self.c_ram, text="--%", font=("DejaVu Sans", 24, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_sys_ram.pack(anchor="w", padx=12)
        self.lbl_sys_ram_sub = tk.Label(self.c_ram, text="-- MB used / -- MB", font=("DejaVu Sans", 10), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_sys_ram_sub.pack(anchor="w", padx=12, pady=2)

        self.c_disk = tk.Frame(self.page_system, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.c_disk.place(x=14 + 2 * (cw + 7), y=8, width=cw, height=ch)
        self._themed_cards.append(self.c_disk)
        t_dsk = tk.Label(self.c_disk, text="DISK & USB STORAGE", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_dsk.pack(anchor="w", padx=12, pady=(6, 2))
        self._themed_titles.append(t_dsk)
        self.lbl_sys_disk = tk.Label(self.c_disk, text="--%", font=("DejaVu Sans", 22, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_sys_disk.pack(anchor="w", padx=12)
        self.lbl_sys_disk_sub = tk.Label(self.c_disk, text="-- GB Free", font=("DejaVu Sans", 10), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_sys_disk_sub.pack(anchor="w", padx=12)
        self.lbl_sys_usb = tk.Label(self.c_disk, text="USB: Not mounted", font=("DejaVu Sans", 9, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_sys_usb.pack(anchor="w", padx=12, pady=1)

        self.c_net = tk.Frame(self.page_system, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.c_net.place(x=14, y=156, width=cw, height=ch)
        self._themed_cards.append(self.c_net)
        t_net = tk.Label(self.c_net, text="NETWORK & BANDWIDTH", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_net.pack(anchor="w", padx=12, pady=(6, 2))
        self._themed_titles.append(t_net)
        self.lbl_sys_net = tk.Label(self.c_net, text="Inet: --", font=("DejaVu Sans", 14, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_sys_net.pack(anchor="w", padx=12)
        self.lbl_sys_net_sub = tk.Label(self.c_net, text="Router: --  \u2022  IP: --", font=("DejaVu Sans", 10), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_sys_net_sub.pack(anchor="w", padx=12, pady=1)
        self.lbl_sys_bw = tk.Label(self.c_net, text="\u2193 0.0 KB/s   \u2191 0.0 KB/s", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg="#38BDF8")
        self.lbl_sys_bw.pack(anchor="w", padx=12, pady=2)

        self.c_srv = tk.Frame(self.page_system, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.c_srv.place(x=14 + cw + 7, y=156, width=cw, height=ch)
        self._themed_cards.append(self.c_srv)
        t_srv = tk.Label(self.c_srv, text="SERVICES & SECURITY", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_srv.pack(anchor="w", padx=12, pady=(6, 2))
        self._themed_titles.append(t_srv)
        self.svc_dots_frame = tk.Frame(self.c_srv, bg=self.theme["card_bg"])
        self.svc_dots_frame.pack(anchor="w", padx=10, pady=2)
        self.svc_chip_labels = {}
        for lbl, _ in CORE_SERVICE_SPECS:
            chip = tk.Label(
                self.svc_dots_frame, text=f"\u25cf{lbl}", font=("DejaVu Sans", 8, "bold"),
                bg=self.theme["card_bg"], fg="#64748B", padx=3
            )
            chip.pack(side=tk.LEFT)
            self.svc_chip_labels[lbl] = chip
        self.lbl_sys_adguard = tk.Label(self.c_srv, text="Ads Blocked: --", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg="#10B981")
        self.lbl_sys_adguard.pack(anchor="w", padx=12, pady=2)
        self.lbl_sys_sec = tk.Label(self.c_srv, text="Sec: Standby", font=("DejaVu Sans", 9, "bold"), bg=self.theme["card_bg"], fg="#94A3B8", anchor="w")
        self.lbl_sys_sec.pack(anchor="w", padx=12, pady=1)

        self.c_pwr = tk.Frame(self.page_system, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.c_pwr.place(x=14 + 2 * (cw + 7), y=156, width=cw, height=ch)
        self._themed_cards.append(self.c_pwr)
        t_pwr = tk.Label(self.c_pwr, text="POWER & TAILSCALE", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_pwr.pack(anchor="w", padx=12, pady=(6, 2))
        self._themed_titles.append(t_pwr)
        self.lbl_sys_pwr = tk.Label(self.c_pwr, text="Power: --", font=("DejaVu Sans", 13, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_sys_pwr.pack(anchor="w", padx=12)
        self.lbl_sys_uptime = tk.Label(self.c_pwr, text="Uptime: --", font=("DejaVu Sans", 10), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_sys_uptime.pack(anchor="w", padx=12, pady=1)
        self.lbl_sys_ts = tk.Label(self.c_pwr, text="Tailscale: --", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_sys_ts.pack(anchor="w", padx=12, pady=2)

        self.card_hist = tk.Frame(self.page_system, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_hist.place(x=14, y=304, width=self.screen_w - 28, height=148)
        self._themed_cards.append(self.card_hist)
        t_hist = tk.Label(self.card_hist, text="RESOURCE HISTORY  (CYAN: CPU  \u2022  GREEN: RAM  \u2022  AMBER: NET)", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_hist.pack(anchor="w", padx=12, pady=4)
        self._themed_titles.append(t_hist)

        self.sys_sparkline = tk.Canvas(self.card_hist, width=self.screen_w - 60, height=110, bg=self.theme["card_bg"], highlightthickness=0)
        self.sys_sparkline.pack(fill=tk.BOTH, expand=True, padx=8, pady=2)

    # ---------------- PAGE 4: MONEY & DAY ----------------
    def build_page_money(self):
        cw = (self.screen_w - 35) // 2
        ch = 210

        self.card_rates = tk.Frame(self.page_money, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_rates.place(x=14, y=8, width=cw, height=ch)
        self._themed_cards.append(self.card_rates)
        t_rt = tk.Label(self.card_rates, text="LIVE CURRENCY EXCHANGE (INR)", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_rt.pack(anchor="w", padx=14, pady=8)
        self._themed_titles.append(t_rt)

        self.lbl_rate_usd = tk.Label(self.card_rates, text="1 USD = Rs.--", font=("DejaVu Sans", 16, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_rate_usd.pack(anchor="w", padx=14, pady=4)
        self.lbl_rate_eur = tk.Label(self.card_rates, text="1 EUR = Rs.--", font=("DejaVu Sans", 14), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_rate_eur.pack(anchor="w", padx=14, pady=4)
        self.lbl_rate_gbp = tk.Label(self.card_rates, text="1 GBP = Rs.--", font=("DejaVu Sans", 14), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_rate_gbp.pack(anchor="w", padx=14, pady=4)
        self.lbl_rate_sub = tk.Label(self.card_rates, text="Source: open.er-api.com", font=("DejaVu Sans", 9), bg=self.theme["card_bg"], fg=self.theme["fg_sub"])
        self.lbl_rate_sub.pack(anchor="w", padx=14, pady=4)

        self.card_day = tk.Frame(self.page_money, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_day.place(x=14 + cw + 7, y=8, width=cw, height=ch)
        self._themed_cards.append(self.card_day)
        t_dy = tk.Label(self.card_day, text="DAY & ASTRONOMY", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_dy.pack(anchor="w", padx=14, pady=8)
        self._themed_titles.append(t_dy)

        self.lbl_day_of_year = tk.Label(self.card_day, text="Day -- of 365", font=("DejaVu Sans", 16, "bold"), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_day_of_year.pack(anchor="w", padx=14, pady=4)
        self.lbl_week_num = tk.Label(self.card_day, text="Week --", font=("DejaVu Sans", 14), bg=self.theme["card_bg"], fg=self.theme["fg"])
        self.lbl_week_num.pack(anchor="w", padx=14, pady=4)
        self.lbl_moon_info = tk.Label(self.card_day, text="Moon: --", font=("DejaVu Sans", 14, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        self.lbl_moon_info.pack(anchor="w", padx=14, pady=4)

        self.card_quote = tk.Frame(self.page_money, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_quote.place(x=14, y=226, width=cw, height=ch)
        self._themed_cards.append(self.card_quote)
        t_qt = tk.Label(self.card_quote, text="THOUGHT FOR TODAY", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_qt.pack(anchor="w", padx=14, pady=8)
        self._themed_titles.append(t_qt)

        self.lbl_quote_text = tk.Label(
            self.card_quote, text="Loading daily quote...", font=("DejaVu Sans", 12, "italic"),
            bg=self.theme["card_bg"], fg=self.theme["fg"], wraplength=cw - 30, justify="left"
        )
        self.lbl_quote_text.pack(anchor="w", padx=14, pady=6)
        self.lbl_quote_author = tk.Label(self.card_quote, text="— Author", font=("DejaVu Sans", 11, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        self.lbl_quote_author.pack(anchor="e", padx=14, pady=4)

        self.card_ev_p4 = tk.Frame(self.page_money, bg=self.theme["card_bg"], bd=1, highlightbackground=self.theme["card_border"], highlightthickness=1)
        self.card_ev_p4.place(x=14 + cw + 7, y=226, width=cw, height=ch)
        self._themed_cards.append(self.card_ev_p4)
        t_ev = tk.Label(self.card_ev_p4, text="TAMIL NADU & NATIONAL FESTIVALS", font=("DejaVu Sans", 10, "bold"), bg=self.theme["card_bg"], fg=self.theme["accent"])
        t_ev.pack(anchor="w", padx=14, pady=8)
        self._themed_titles.append(t_ev)

        self.p4_ev_container = tk.Frame(self.card_ev_p4, bg=self.theme["card_bg"])
        self.p4_ev_container.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)

    # ================= PERIODIC GUI REFRESH METHODS =================
    def update_clock(self):
        STATE.last_ui_tick = time.time()
        now = datetime.datetime.now()

        is_24h = CONFIG.get("time_format_24h", False)
        if is_24h:
            t_str = now.strftime("%H:%M")
            self.lbl_time.config(text=t_str)
            self.lbl_ampm.config(text="")
        else:
            t_str = now.strftime("%I:%M")
            self.lbl_time.config(text=t_str)
            self.lbl_ampm.config(text=now.strftime("%p"))

        self.lbl_date.config(text=now.strftime("%A, %d %B"))

        d_num = now.timetuple().tm_yday
        is_leap = (now.year % 4 == 0 and now.year % 100 != 0) or (now.year % 400 == 0)
        tot_days = 366 if is_leap else 365
        w_num = now.isocalendar()[1]
        self.lbl_day_of_year.config(text=f"Day {d_num} of {tot_days}")
        self.lbl_week_num.config(text=f"Week {w_num} of {now.year}")

        m_name, m_icon = get_moon_phase(now)
        self.lbl_moon_info.config(text=safe_tcl(f"Moon: {m_name} {m_icon}"))
        self.lbl_p2_moon.config(text=safe_tcl(f"Moon Phase: {m_name} {m_icon}"))

        toggle_map = False
        toggle_sky = False
        cycle_sky = False
        req_page = None
        req_theme = None
        req_time_fmt = False
        with STATE.lock:
            if STATE.flight_map_toggle_req:
                toggle_map = True
                STATE.flight_map_toggle_req = False
            if STATE.sky_dome_toggle_req:
                toggle_sky = True
                STATE.sky_dome_toggle_req = False
            if STATE.sky_pass_cycle_req:
                cycle_sky = True
                STATE.sky_pass_cycle_req = False
            if STATE.page_switch_req is not None:
                req_page = STATE.page_switch_req
                STATE.page_switch_req = None
            if STATE.theme_switch_req is not None:
                req_theme = STATE.theme_switch_req
                STATE.theme_switch_req = None
            if STATE.time_fmt_toggle_req:
                req_time_fmt = True
                STATE.time_fmt_toggle_req = False
            active_msg = None
            flight_hdr = None
            sec_armed = bool(STATE.system.get("security_armed", False))
            if STATE.user_message and time.time() < STATE.user_message_until:
                active_msg = safe_tcl(f"MESSAGE: {STATE.user_message}")
            elif STATE.alerts:
                active_msg = safe_tcl("  |  ".join(STATE.alerts))
            elif STATE.health_warning:
                active_msg = safe_tcl(f"ALERT: {STATE.health_warning}")
            elif STATE.flights and STATE.active_page != 5:
                fl = STATE.flights[0]
                code = fl.get("iata") or fl["callsign"]
                rt = ""
                if fl.get("route_valid") and fl.get("orig_code") and fl.get("dst_code"):
                    rt = f" ({fl['orig_code']}\u2192{fl['dst_code']})"
                flight_hdr = safe_tcl(f"\u2708 {code}{rt}  \u2022  {fl['dist_km']} km {fl['compass']}  \u2022  {fl['alt_ft']:,} ft")

        if req_time_fmt:
            self.toggle_12_24h()
        if req_theme is not None:
            CONFIG["theme"] = req_theme
            if req_theme == "auto":
                STATE.night_mode_active = False
                self._apply_auto_theme_now()
            else:
                STATE.night_mode_active = (req_theme == "night")
                self.apply_theme(req_theme)
                if req_theme == "night":
                    set_screen_brightness(CONFIG["night_mode"].get("brightness_night", 0.5))
        if req_page is not None:
            self.last_page_change = time.time()
            self._flight_auto_active = False
            if req_page == 5:
                self.flight_map_dismissed_hexes.clear()
            self.set_page(req_page)
        if toggle_map:
            self.toggle_flight_map()
        if toggle_sky:
            self.toggle_sky_dome()
        if cycle_sky:
            passes = STATE.space.get("passes") or []
            nxt = (self._sky_pinned_idx + 1) % max(1, len(passes[:3]))
            self.select_sky_pass(nxt)

        if sec_armed:
            self.lbl_sec_pill.config(text="\u25cf SEC ARMED", bg="#DC2626", fg="#FFFFFF")
            if not self.lbl_sec_pill.winfo_ismapped():
                self.lbl_sec_pill.pack(side=tk.LEFT, padx=(10, 0), pady=4, before=self.lbl_alert_banner)
        else:
            if self.lbl_sec_pill.winfo_ismapped():
                self.lbl_sec_pill.pack_forget()

        hdr_bg = "#070C16"
        if active_msg:
            hdr_bg = "#991B1B" if "ALERT" in active_msg else "#0284C7"
            self.lbl_alert_banner.config(text=active_msg, bg=hdr_bg, fg="#FFFFFF")
        elif flight_hdr:
            self.lbl_alert_banner.config(text=flight_hdr, bg=hdr_bg, fg="#38BDF8")
        else:
            self.lbl_alert_banner.config(text="", bg=hdr_bg)

        self.header_frame.config(bg=hdr_bg)
        self.page_dots_frame.config(bg=hdr_bg)
        self.lbl_header_status.config(bg=hdr_bg, fg="#FFFFFF" if active_msg else "#94A3B8")

        page_names = ["Main", "Weather", "System", "Money", "Sky Dome", "Flight Radar", "Server Photos"]
        cur_p = page_names[STATE.active_page] if 0 <= STATE.active_page < len(page_names) else "Main"
        paused_tag = " [PAUSED]" if STATE.rotation_paused else ""
        with STATE.lock:
            ads_b = STATE.system.get("ads_blocked", "--")
            ads_p = STATE.system.get("ads_percent", "--")
            ap_idx = STATE.active_page
            is_paused = STATE.rotation_paused
        for i_dot, dl in enumerate(self.page_dot_labels):
            if active_msg:
                inactive_col = "#5C1919" if "ALERT" in active_msg else "#075985"
                dot_fg = "#FFFFFF" if i_dot == ap_idx else inactive_col
            else:
                dot_fg = self.theme["accent"] if i_dot == ap_idx else "#334155"
            dl.config(bg=hdr_bg, fg=dot_fg)
        ag_tag = f"Ads: {ads_b} ({ads_p})  \u2022  " if ads_b != "--" else ""
        self.lbl_header_status.config(text=safe_tcl(f"{ag_tag}{cur_p}{paused_tag}"))

        # Update 2px slide cycle progress bar
        cycle_total = CONFIG.get("page_cycle_seconds", 180) if ap_idx == 0 else CONFIG.get("extra_page_display_seconds", 15)
        if self._flight_auto_active:
            bar_w = self.screen_w
            bar_col = "#EF4444"
        elif is_paused:
            bar_w = self.screen_w
            bar_col = "#F59E0B"
        else:
            frac = max(0.0, min(1.0, (time.time() - self.last_page_change) / float(max(1, cycle_total))))
            bar_w = int(round(self.screen_w * frac))
            bar_col = self.theme["accent"]
        self.cycle_progress_canvas.coords(self._progress_bar_id, 0, 0, bar_w, 2)
        self.cycle_progress_canvas.itemconfig(self._progress_bar_id, fill=bar_col)

        if STATE.timer_seconds_remaining is not None:
            if STATE.timer_seconds_remaining > 0:
                STATE.timer_seconds_remaining -= 1
                m = STATE.timer_seconds_remaining // 60
                s = STATE.timer_seconds_remaining % 60
                self.lbl_timer_digits.config(text=f"{m:02d}:{s:02d}", fg="#F8FAFC")
                self.timer_overlay.place(relx=0.5, rely=0.45, anchor="center", width=340, height=180)
                self.timer_overlay.lift()
            else:
                self.lbl_timer_digits.config(text="TIME'S UP!", fg="#EF4444")
                self.timer_overlay.place(relx=0.5, rely=0.45, anchor="center", width=340, height=180)
                self.timer_overlay.lift()
                try:
                    self.root.bell()
                except: pass
                if not STATE.timer_is_ringing:
                    STATE.timer_is_ringing = True
                    self.root.after(10000, self.dismiss_timer)

        self.root.after(1000, self.update_clock)

    def dismiss_timer(self):
        with STATE.lock:
            STATE.timer_seconds_remaining = None
            STATE.timer_is_ringing = False
        self.timer_overlay.place_forget()

    def update_weather_display(self):
        with STATE.lock:
            w = dict(STATE.weather)

        self.lbl_weather_temp.config(text=safe_tcl(w["temp"]))
        self.lbl_weather_desc.config(text=safe_tcl(w["cond"]))
        self.lbl_weather_feels.config(text=safe_tcl(f"Feels: {w['feels']}  •  H: {w['temp_max']}  L: {w['temp_min']}"))
        self.lbl_weather_extras.config(text=safe_tcl(f"Rain: {w['rain_chance']}  •  Hum: {w['humidity']}  •  Wind: {w['wind']}"))

        self.weather_icon.set_condition(w["icon_name"])

        self.lbl_p2_uv_aqi.config(text=safe_tcl(f"UV Index: {w['uv']}  •  AQI: {w['aqi']}"))
        self.lbl_p2_sun.config(text=safe_tcl(f"Sunrise: {w['sunrise']}  •  Sunset: {w['sunset']}"))
        self.lbl_p2_wind_hum.config(text=safe_tcl(f"Wind Speed: {w['wind']}  •  Humidity: {w['humidity']}"))

        daily = w.get("daily", [])
        for idx, (lbl_d, lbl_t) in enumerate(self.daily_labels):
            if idx < len(daily):
                d_item = daily[idx]
                lbl_d.config(text=safe_tcl(d_item["day"]))
                lbl_t.config(text=safe_tcl(f"{d_item['max']}° / {d_item['min']}°"))
            else:
                lbl_d.config(text="--")
                lbl_t.config(text="--° / --°")

        if STATE.active_page == 1:
            self.hourly_canvas.draw_chart(w.get("hourly", []), self.theme)

        with STATE.lock:
            events = list(STATE.events)
        if events:
            top_ev = events[0]
            self.lbl_event_title.config(text=safe_tcl(top_ev["title"]))
            self.lbl_event_badge.config(text=safe_tcl(top_ev["badge"]))
            if len(events) > 1:
                self.lbl_event_sub.config(text=safe_tcl(f"Next: {events[1]['title']} ({events[1]['badge']})"))
            else:
                self.lbl_event_sub.config(text="")

        for widget in self.p4_ev_container.winfo_children():
            widget.destroy()
        for ev in events[:4]:
            row = tk.Frame(self.p4_ev_container, bg=self.theme["card_bg"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=safe_tcl(ev["badge"]), font=("DejaVu Sans", 10, "bold"), bg="#0284C7", fg="white", padx=6).pack(side=tk.LEFT)
            tk.Label(row, text=safe_tcl(f" {ev['title']}"), font=("DejaVu Sans", 11), bg=self.theme["card_bg"], fg=self.theme["fg"]).pack(side=tk.LEFT)

        with STATE.lock:
            fin = dict(STATE.finance)
            q = dict(STATE.quote)

        self.lbl_rate_usd.config(text=safe_tcl(f"1 USD = {fin['usd_inr']}"))
        self.lbl_rate_eur.config(text=safe_tcl(f"1 EUR = {fin['eur_inr']}"))
        self.lbl_rate_gbp.config(text=safe_tcl(f"1 GBP = {fin['gbp_inr']}"))
        self.lbl_rate_sub.config(text=safe_tcl(f"Rates as of {fin['updated_time']} • open.er-api.com"))

        self.lbl_quote_text.config(text=safe_tcl(f"“{q['text']}”"))
        self.lbl_quote_author.config(text=safe_tcl(f"— {q['author']}"))

        self.update_system_page_ui()
        self.update_flight_display()
        if STATE.active_page == 4:
            self.update_sky_display()

        self.root.after(3000, self.update_weather_display)

    # ---------------- PAGE 6: FULL-SCREEN FLIGHT TRACK RADAR ----------------
    def toggle_flight_map(self):
        with STATE.lock:
            overhead = [dict(f) for f in STATE.flights]
            cur_p = STATE.active_page
        if cur_p == 5:
            if overhead:
                for f in overhead:
                    self.flight_map_dismissed_hexes.add(f["hex"])
            ret = self._flight_auto_return_page if (self._flight_auto_active and self._flight_auto_return_page != 5) else 0
            self._flight_auto_active = False
            self.last_page_change = time.time()
            self.set_page(ret)
        else:
            self.flight_map_dismissed_hexes.clear()
            self._flight_auto_active = False
            self.last_page_change = time.time()
            self.set_page(5)

    def _on_flight_canvas_click(self, event):
        if self._flight_toggle_box:
            x0, y0, x1, y1 = self._flight_toggle_box
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self.toggle_flight_map()
                return

    def update_flight_display(self):
        with STATE.lock:
            overhead = [dict(f) for f in STATE.flights]
            last_fl = dict(STATE.last_flight_seen) if STATE.last_flight_seen else None
            cur_p = STATE.active_page

        self._last_flight_seen_ui = last_fl
        active_ov_hexes = {f["hex"] for f in overhead}
        self.flight_map_dismissed_hexes &= active_ov_hexes
        new_overhead = active_ov_hexes - self._seen_overhead_hexes
        self._seen_overhead_hexes = active_ov_hexes

        # Auto-switch full screen to Page 5 whenever an aircraft enters 10 km Local radius
        if new_overhead and not (new_overhead <= self.flight_map_dismissed_hexes):
            if cur_p != 5:
                self._flight_auto_return_page = cur_p
            self._flight_auto_active = True
            self.last_page_change = time.time()
            if cur_p != 5:
                self.set_page(5)
                cur_p = 5

        if cur_p == 5:
            active_ov = overhead
        else:
            active_ov = [f for f in overhead if f["hex"] not in self.flight_map_dismissed_hexes]

        if not active_ov:
            self._active_map_flight = None
            self._active_map_is_live = False
            if self._flight_auto_active:
                self._flight_auto_active = False
                ret = self._flight_auto_return_page if self._flight_auto_return_page != 5 else 0
                self.last_page_change = time.time()
                if cur_p == 5:
                    self.set_page(ret)
                    return
            if cur_p == 5:
                self._update_flight_clear_hud(last_fl)
                if self._map_anim_after is None:
                    self._animate_flight_map()
                else:
                    self._draw_flight_map_canvas()
            return

        idx = (int(time.time()) // 10) % len(active_ov)
        fl = active_ov[idx]
        self._active_map_flight = fl
        self._active_map_is_live = True

        if cur_p == 5:
            self.last_page_change = time.time()
            self._update_flight_hud(fl, idx, len(active_ov), True)
            if self._map_anim_after is None:
                self._animate_flight_map()
            else:
                self._draw_flight_map_canvas()

    # ---------------- PAGE 5: FULL-SCREEN LOCAL SKY & ISS DOME ----------------
    def toggle_sky_dome(self):
        with STATE.lock:
            cur_p = STATE.active_page
        self.last_page_change = time.time()
        if cur_p == 4:
            self.set_page(0)
        else:
            self._flight_auto_active = False
            self.set_page(4)

    def select_sky_pass(self, idx):
        if STATE.active_page != 4:
            return
        with STATE.lock:
            passes = list(STATE.space.get("passes") or [])[:3]
        if idx < len(passes):
            self._sky_pinned_idx = idx
            self._sky_pinned_until = time.time() + 45.0
            self._sky_anim_frame = 0
            self.update_sky_display()

    def _on_sky_canvas_click(self, event):
        if self._sky_toggle_box:
            x0, y0, x1, y1 = self._sky_toggle_box
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self.toggle_sky_dome()
                return
        for idx, (x0, y0, x1, y1) in enumerate(self._sky_card_boxes):
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self.select_sky_pass(idx)
                return

    def _sky_pass_xy(self, el, az, cx, cy, R):
        r = max(0.0, min(1.0, (90.0 - float(el)) / 80.0)) * R
        rad = math.radians(float(az) - 90.0)
        return cx + r * math.cos(rad), cy + r * math.sin(rad)

    def _interp_sky_pass_pt(self, p, prog):
        pts = p.get("pts")
        if pts and len(pts) >= 2:
            scaled = max(0.0, min(1.0, prog)) * (len(pts) - 1)
            i0 = min(len(pts) - 2, int(scaled))
            frac = scaled - i0
            el0, az0 = pts[i0]
            el1, az1 = pts[i0 + 1]
            d_az = ((az1 - az0 + 540.0) % 360.0) - 180.0
            return el0 + (el1 - el0) * frac, (az0 + d_az * frac + 360.0) % 360.0
        s_az = p.get("start_az", 315.0)
        m_az = p.get("max_az", 235.0)
        e_az = p.get("end_az", 150.0)
        m_el = p.get("max_el", 45.0)
        el = 10.0 + (m_el - 10.0) * math.sin(math.pi * prog)
        if prog < 0.5:
            d_az = ((m_az - s_az + 540.0) % 360.0) - 180.0
            az = (s_az + d_az * (prog * 2.0) + 360.0) % 360.0
        else:
            d_az = ((e_az - m_az + 540.0) % 360.0) - 180.0
            az = (m_az + d_az * ((prog - 0.5) * 2.0) + 360.0) % 360.0
        return el, az

    def update_sky_display(self):
        if STATE.active_page != 4:
            return

        now = time.time()
        with STATE.lock:
            sp = {
                "live": dict(STATE.space["live"]) if STATE.space.get("live") else None,
                "passes": [dict(x) for x in (STATE.space.get("passes") or [])],
                "next_shadow": dict(STATE.space["next_shadow"]) if STATE.space.get("next_shadow") else None,
                "isro": [dict(x) for x in (STATE.space.get("isro") or [])],
            }
            w = dict(STATE.weather)
            ov_count = len(STATE.flights)

        vis_passes = [p for p in sp["passes"] if p.get("end", 0) > now][:3]
        live = sp["live"]
        live_km = float(CONFIG.get("sky_dome", {}).get("live_overhead_km", 1600))
        is_iss_overhead = bool(live and live.get("dist_km", 99999) <= live_km)

        if vis_passes:
            if now < self._sky_pinned_until:
                sel_idx = self._sky_pinned_idx % len(vis_passes)
            else:
                sel_idx = (int(now) // 12) % len(vis_passes)
            active_pass = vis_passes[sel_idx]
        else:
            sel_idx = 0
            active_pass = sp["next_shadow"]

        self._sky_state = {
            "live": live,
            "passes": vis_passes,
            "active_pass": active_pass,
            "next_shadow": sp["next_shadow"],
            "sel_idx": sel_idx,
            "isro": sp["isro"],
            "is_live_overhead": is_iss_overhead,
            "sunrise": w.get("sunrise", "--:--"),
            "sunset": w.get("sunset", "--:--"),
            "ov_count": ov_count,
        }
        self._update_sky_hud()

        if self._sky_anim_after is None:
            self._animate_sky_dome()
        else:
            self._draw_sky_dome_canvas()

    def _update_sky_hud(self):
        st = getattr(self, "_sky_state", None)
        if not st:
            return
        live = st["live"]
        p = st["active_pass"]
        vis_passes = st["passes"]

        if st["is_live_overhead"]:
            self.lbl_sky_badge.config(text=safe_tcl("ISS OVERHEAD"), bg="#DC2626")
        elif p and p.get("visible"):
            self.lbl_sky_badge.config(text=safe_tcl("VISIBLE PASS"), bg="#D97706")
        else:
            self.lbl_sky_badge.config(text=safe_tcl("LOCAL SKY"), bg="#0284C7")

        if p:
            dt_st = datetime.datetime.fromtimestamp(p["start"])
            dt_en = datetime.datetime.fromtimestamp(p["end"])
            az_path = f"{_compass_dir(p['start_az'])} \u2192 {_compass_dir(p['end_az'])}"
            if vis_passes:
                title_txt = "Next Visible ISS Pass: " + dt_st.strftime("%a %d %b, %I:%M") + " - " + dt_en.strftime("%I:%M %p")
            else:
                title_txt = "Next Orbit Shadow Pass: " + dt_st.strftime("%a %d %b %I:%M %p")
            peak_txt = f"Peak {p['max_el']:.0f}\u00b0  \u2022  {az_path}"
            if len(vis_passes) > 1:
                peak_txt += f" ({st['sel_idx'] + 1}/{len(vis_passes)})"
        else:
            title_txt = "Scanning ISS visible passes over Chennai..."
            peak_txt = "Min Peak >= 25\u00b0"

        self.lbl_sky_title.config(text=safe_tcl(title_txt))
        self.lbl_sky_peak.config(text=safe_tcl(peak_txt))

        m_name, _ = get_moon_phase()
        isro_tag = ""
        if st["isro"]:
            first_m = st["isro"][0]["name"].split("|")[-1].strip()
            if len(first_m) > 28:
                first_m = first_m[:27] + "."
            isro_tag = f"  \u2022  ISRO: {first_m}"
        meta_txt = f"Sun: {st['sunrise']} - {st['sunset']}  \u2022  Moon: {m_name}  \u2022  10 km Local Radar{isro_tag}"
        self.lbl_sky_meta.config(text=safe_tcl(meta_txt))

    def _animate_sky_dome(self):
        if STATE.active_page != 4:
            self._sky_anim_after = None
            return
        self._draw_sky_dome_canvas()
        self._sky_anim_frame = (self._sky_anim_frame + 1) % 10000
        self._sky_anim_after = self.root.after(140, self._animate_sky_dome)

    def _draw_sky_dome_canvas(self):
        st = getattr(self, "_sky_state", None)
        if not st:
            return
        c = self.sky_canvas
        mw = self.screen_w
        mh = self.sky_dome_h
        c.delete("all")
        self._sky_card_boxes = []

        now = time.time()
        frame = self._sky_anim_frame
        live = st["live"]
        p = st["active_pass"]
        vis_passes = st["passes"]
        sel_idx = st["sel_idx"]

        # Column 1 (left): Large All-Sky Stereographic Dome over Home
        cx, cy, R = 196, 194, 132
        c.create_oval(cx - R - 20, cy - R - 20, cx + R + 20, cy + R + 20, fill="#091326", outline="#1E3A5F", width=2)

        stars = [
            (0.22, -0.52), (-0.48, -0.36), (0.56, 0.30), (-0.31, 0.62), (0.12, 0.74),
            (-0.74, 0.14), (0.72, -0.22), (-0.16, -0.78), (0.44, -0.58), (-0.56, 0.44),
            (0.32, 0.48), (-0.65, -0.15), (0.04, -0.44), (-0.28, -0.18), (0.64, 0.08),
            (-0.12, 0.38), (0.52, -0.12), (-0.68, -0.48),
        ]
        for sx, sy in stars:
            px, py = cx + sx * R, cy + sy * R
            c.create_oval(px - 1.2, py - 1.2, px + 1.2, py + 1.2, fill="#CBD5E1", outline="")

        c.create_oval(cx - R, cy - R, cx + R, cy + R, outline="#334155", width=1.5)
        for scale, el_lbl in [(0.67, "30\u00b0"), (0.33, "60\u00b0")]:
            rr = R * scale
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline="#1E3A5F", dash=(3, 4), width=1)
            c.create_text(cx + 4, cy + rr - 7, text=safe_tcl(el_lbl), fill="#64748B", font=("DejaVu Sans", 8), anchor="w")
        c.create_line(cx - R, cy, cx + R, cy, fill="#162844", width=1)
        c.create_line(cx, cy - R, cx, cy + R, fill="#162844", width=1)

        # Center 10 km Chennai flight radar circle
        c.create_oval(cx - 24, cy - 24, cx + 24, cy + 24, outline="#EF4444", dash=(3, 3), width=1.5)
        c.create_text(cx, cy + 15, text="10 km Radar", fill="#FCA5A5", font=("DejaVu Sans", 7, "bold"))
        c.create_polygon(cx, cy - 5, cx + 5, cy, cx, cy + 5, cx - 5, cy, fill="#EF4444", outline="#FEF08A")

        # Cardinal + intercardinal markers
        c.create_text(cx, cy - R - 11, text="N", fill="#38BDF8", font=("DejaVu Sans", 10, "bold"))
        c.create_text(cx, cy + R + 11, text="S", fill="#94A3B8", font=("DejaVu Sans", 10, "bold"))
        c.create_text(cx + R + 12, cy, text="E", fill="#94A3B8", font=("DejaVu Sans", 10, "bold"))
        c.create_text(cx - R - 12, cy, text="W", fill="#94A3B8", font=("DejaVu Sans", 10, "bold"))
        for lbl_ic, ang_deg in [("NE", 45), ("SE", 135), ("SW", 225), ("NW", 315)]:
            arad = math.radians(ang_deg - 90.0)
            c.create_text(cx + (R + 13) * math.cos(arad), cy + (R + 13) * math.sin(arad), text=lbl_ic, fill="#475569", font=("DejaVu Sans", 7, "bold"))

        # Sriharikota ISRO direction beacon at 42 deg NE
        sri_rad = math.radians(42.0 - 90.0)
        sx_i = cx + R * math.cos(sri_rad)
        sy_i = cy + R * math.sin(sri_rad)
        c.create_oval(sx_i - 5, sy_i - 5, sx_i + 5, sy_i + 5, fill="#38BDF8", outline="#E0F2FE", width=1.5)
        c.create_text(sx_i - 10, sy_i + 12, text="ISRO 42\u00b0", fill="#38BDF8", font=("DejaVu Sans", 7, "bold"), anchor="e")

        # Animated ISS pass arc across dome
        look_txt = "No bright dark-sky ISS pass in next 21 days (orbits cross in daylight/shadow)"
        if p:
            pts = p.get("pts")
            if not pts or len(pts) < 2:
                pts = [list(self._interp_sky_pass_pt(p, i / 16.0)) for i in range(17)]
            full_xy = [self._sky_pass_xy(el, az, cx, cy, R) for el, az in pts]
            full_flat = [coord for pt in full_xy for coord in pt]
            if len(full_flat) >= 4:
                c.create_line(full_flat, fill="#78350F", width=5, smooth=True, capstyle=tk.ROUND)
                c.create_line(full_flat, fill="#F59E0B", width=2, dash=(5, 4), smooth=True)

            prog = (frame % 72) / 71.0
            trail_pts = []
            trail_steps = max(2, int(round(prog * 24)))
            for i in range(trail_steps + 1):
                t_el, t_az = self._interp_sky_pass_pt(p, (i / trail_steps) * prog)
                trail_pts.extend(self._sky_pass_xy(t_el, t_az, cx, cy, R))
            if len(trail_pts) >= 4:
                c.create_line(trail_pts, fill="#FBBF24", width=3.5, smooth=True, capstyle=tk.ROUND)

            st_x, st_y = full_xy[0]
            en_x, en_y = full_xy[-1]
            c.create_oval(st_x - 4, st_y - 4, st_x + 4, st_y + 4, fill="#10B981", outline="#FFFFFF")
            c.create_oval(en_x - 4, en_y - 4, en_x + 4, en_y + 4, fill="#F59E0B", outline="#FFFFFF")

            cur_el, cur_az = self._interp_sky_pass_pt(p, prog)
            ix, iy = self._sky_pass_xy(cur_el, cur_az, cx, cy, R)
            halo_r = 10 + (frame % 8) * 0.8
            c.create_oval(ix - halo_r, iy - halo_r, ix + halo_r, iy + halo_r, outline="#FBBF24", width=1.4)
            c.create_oval(ix - 7, iy - 7, ix + 7, iy + 7, fill="#78350F", outline="#FBBF24", width=1.5)
            c.create_oval(ix - 3.5, iy - 3.5, ix + 3.5, iy + 3.5, fill="#FFFFFF", outline="")

            cd_str = _fmt_sky_countdown(max(0, p["start"] - now))
            vis_tag = "Dark-sky sunlit" if p.get("visible") else ("Daytime" if p.get("sun_el", 0) > -5.5 else "Earth shadow")
            look_txt = f"Arc Pos: {cur_el:.0f}\u00b0 Elev @ {cur_az:.0f}\u00b0 {_compass_dir(cur_az)}  \u2022  Starts in {cd_str}  \u2022  {vis_tag}"

        if live and live.get("el_deg", -90) > 0:
            lx, ly = self._sky_pass_xy(max(2.0, live["el_deg"]), live["az_deg"], cx, cy, R)
            r_pulse = 11 + (frame % 10) * 0.9
            c.create_oval(lx - r_pulse, ly - r_pulse, lx + r_pulse, ly + r_pulse, outline="#EF4444", width=1.8)
            c.create_oval(lx - 4.5, ly - 4.5, lx + 4.5, ly + 4.5, fill="#EF4444", outline="#FFFFFF", width=1)
            self._draw_pill_label(c, lx, max(36, ly - 18), f"ISS LIVE {live['el_deg']:.0f}\u00b0", "#FCA5A5", bg="#1E1016", border="#991B1B", font=("DejaVu Sans", 8, "bold"))
            look_txt = f"ISS LIVE SKY: {live['dist_km']:,} km {live['compass']}  \u2022  Elev {live['el_deg']:.0f}\u00b0 @ {live['az_deg']:.0f}\u00b0  \u2022  {live['alt_km']} km alt"

        if getattr(self, "_last_sky_look_txt", None) != look_txt:
            self.lbl_sky_look.config(text=safe_tcl(look_txt))
            self._last_sky_look_txt = look_txt

        # Top banner pills
        top_txt = "\u25cf ISS OVERHEAD HOME (<= 1600 KM)" if st["is_live_overhead"] else "\u25cf LOCAL ALL-SKY & ISS DOME (13.08\u00b0N, 80.27\u00b0E)"
        top_fg = "#FCA5A5" if st["is_live_overhead"] else "#FBBF24"
        self._draw_pill_label(c, 12, 14, top_txt, top_fg, bg="#09111E", border="#1E293B", anchor="w", font=("DejaVu Sans", 8, "bold"))

        tog_id = c.create_text(mw - 14, 14, text=safe_tcl("[S] Back to Main"), fill="#94A3B8", font=("DejaVu Sans", 8, "bold"), anchor="e")
        tb = c.bbox(tog_id)
        if tb:
            box = (tb[0] - 6, tb[1] - 3, tb[2] + 6, tb[3] + 3)
            tr = c.create_rectangle(*box, fill="#09111E", outline="#1E293B", width=1)
            c.tag_lower(tr, tog_id)
            self._sky_toggle_box = box

        # Column 2 (x=384..694): Live ISS Telemetry + Chennai Sun/Moon Almanac + ISRO Manifest
        mx0, mx1 = 384, 694
        c.create_rectangle(mx0, 36, mx1, 128, fill="#0B1324", outline="#1E293B", width=1)
        c.create_text(mx0 + 12, 50, text="ISS LIVE TELEMETRY", fill="#38BDF8", font=("DejaVu Sans", 9, "bold"), anchor="w")
        if live:
            st_lbl = "OVERHEAD HOME NOW" if st["is_live_overhead"] else f"{live['dist_km']:,} km {live['compass']} of Home"
            st_col = "#FCA5A5" if st["is_live_overhead"] else "#F8FAFC"
            c.create_text(mx0 + 12, 72, text=safe_tcl(st_lbl), fill=st_col, font=("DejaVu Sans", 12, "bold"), anchor="w")
            sub1 = f"Elev {live['el_deg']:.0f}\u00b0  \u2022  Az {live['az_deg']:.0f}\u00b0  \u2022  {live['alt_km']} km alt"
            sub2 = f"{live['speed_kmh']:,} km/h  \u2022  {live['visibility']}  \u2022  ({live['lat']:.1f}, {live['lon']:.1f})"
            c.create_text(mx0 + 12, 95, text=safe_tcl(sub1), fill="#CBD5E1", font=("DejaVu Sans", 8), anchor="w")
            c.create_text(mx0 + 12, 113, text=safe_tcl(sub2), fill="#94A3B8", font=("DejaVu Sans", 8), anchor="w")
        else:
            c.create_text(mx0 + 12, 82, text="Syncing live ISS position...", fill="#94A3B8", font=("DejaVu Sans", 9), anchor="w")

        c.create_rectangle(mx0, 136, mx1, 232, fill="#0B1324", outline="#1E293B", width=1)
        c.create_text(mx0 + 12, 150, text="LOCAL ALMANAC & RADAR", fill="#FBBF24", font=("DejaVu Sans", 9, "bold"), anchor="w")
        m_name, m_icon = get_moon_phase()
        c.create_text(mx0 + 12, 172, text=safe_tcl(f"Sunrise {st['sunrise']}   \u2022   Sunset {st['sunset']}"), fill="#F8FAFC", font=("DejaVu Sans", 9, "bold"), anchor="w")
        c.create_text(mx0 + 12, 193, text=safe_tcl(f"Moon: {m_name} {m_icon}"), fill="#CBD5E1", font=("DejaVu Sans", 8), anchor="w")
        if st.get("ov_count", 0) > 0:
            radar_st = f"10 km Airspace: {st['ov_count']} overhead"
            radar_fg = "#FCA5A5"
        else:
            radar_st = "10 km Airspace: Clear  \u2022  Alert <= 10 km"
            radar_fg = "#34D399"
        c.create_text(mx0 + 12, 213, text=safe_tcl(radar_st), fill=radar_fg, font=("DejaVu Sans", 8, "bold"), anchor="w")

        c.create_rectangle(mx0, 240, mx1, mh - 10, fill="#0B1324", outline="#1E293B", width=1)
        c.create_text(mx0 + 12, 254, text="ISRO SRIHARIKOTA (NE 42\u00b0)", fill="#38BDF8", font=("DejaVu Sans", 9, "bold"), anchor="w")
        isro_items = st["isro"]
        if isro_items:
            ly = 278
            for it in isro_items[:3]:
                nt = it.get("net_ts")
                t_tag = datetime.datetime.fromtimestamp(nt).strftime("%d %b %y") if nt else "TBD"
                name_s = it["name"].replace(" | ", " ").strip()
                if len(name_s) > 29:
                    name_s = name_s[:28] + "."
                line_s = f"\u2022 {name_s} ({t_tag})"
                c.create_text(mx0 + 12, ly, text=safe_tcl(line_s), fill="#E2E8F0", font=("DejaVu Sans", 8), anchor="w")
                ly += 25
        else:
            c.create_text(mx0 + 12, 286, text="Loading upcoming ISRO launches...", fill="#94A3B8", font=("DejaVu Sans", 8), anchor="w")

        # Column 3 (x=706..1012): Next 3 Visible ISS Passes over Home
        rx0, rx1 = 706, mw - 12
        c.create_text(rx0 + 2, 42, text="VISIBLE ISS PASSES (MAX >= 25\u00b0)", fill="#FBBF24", font=("DejaVu Sans", 9, "bold"), anchor="w")
        cy_card = 56
        if vis_passes:
            for idx, vp in enumerate(vis_passes[:3]):
                y0, y1 = cy_card, cy_card + 92
                is_sel = (idx == sel_idx)
                bg_c = "#13203B" if is_sel else "#0B1324"
                bd_c = "#F59E0B" if is_sel else "#1E293B"
                c.create_rectangle(rx0, y0, rx1, y1, fill=bg_c, outline=bd_c, width=2 if is_sel else 1)
                dt_s = datetime.datetime.fromtimestamp(vp["start"])
                dt_e = datetime.datetime.fromtimestamp(vp["end"])
                dur = max(10, int(vp["end"] - vp["start"]))
                l1 = dt_s.strftime("%a %d %b") + "  \u2022  " + dt_s.strftime("%I:%M") + " - " + dt_e.strftime("%I:%M %p")
                l2 = f"Peak {vp['max_el']:.0f}\u00b0 Elev  \u2022  Duration {dur // 60}m {dur % 60:02d}s"
                l3 = f"Path: {_compass_dir(vp['start_az'])} ({vp['start_az']:.0f}\u00b0) \u2192 {_compass_dir(vp['end_az'])} ({vp['end_az']:.0f}\u00b0)"
                l4 = f"Starts in {_fmt_sky_countdown(max(0, vp['start'] - now))}"
                c.create_text(rx0 + 12, y0 + 16, text=safe_tcl(l1), fill="#FBBF24" if is_sel else "#F8FAFC", font=("DejaVu Sans", 9, "bold"), anchor="w")
                c.create_text(rx0 + 12, y0 + 38, text=safe_tcl(l2), fill="#E2E8F0", font=("DejaVu Sans", 8), anchor="w")
                c.create_text(rx0 + 12, y0 + 56, text=safe_tcl(l3), fill="#CBD5E1", font=("DejaVu Sans", 8), anchor="w")
                c.create_text(rx0 + 12, y0 + 75, text=safe_tcl(l4), fill="#38BDF8", font=("DejaVu Sans", 8, "bold"), anchor="w")
                self._sky_card_boxes.append((rx0, y0, rx1, y1))
                cy_card = y1 + 8
        else:
            c.create_rectangle(rx0, cy_card, rx1, cy_card + 130, fill="#0B1324", outline="#1E293B", width=1)
            msg_none = "No dark-sky visible passes (next 21d)\nOrbits cross Home in daylight or shadow."
            c.create_text(rx0 + 12, cy_card + 36, text=safe_tcl(msg_none), fill="#CBD5E1", font=("DejaVu Sans", 9), anchor="w")
            nx = st.get("next_shadow")
            if nx:
                dt_nx = datetime.datetime.fromtimestamp(nx["start"])
                nx_s = "Next orbit: " + dt_nx.strftime("%a %d %b %I:%M %p") + f" ({nx['max_el']:.0f}\u00b0)"
                c.create_text(rx0 + 12, cy_card + 86, text=safe_tcl(nx_s), fill="#38BDF8", font=("DejaVu Sans", 8, "bold"), anchor="w")

    def _update_flight_hud(self, fl, idx, total, is_live_crossing):
        self.lbl_flight_badge.config(text=safe_tcl("\u2708 OVERHEAD 10KM"), bg="#DC2626")

        cs_disp = fl["callsign"]
        if fl.get("iata") and fl["iata"] != fl["callsign"]:
            cs_disp = f"{fl['iata']} / {fl['callsign']}"
        airline = fl.get("airline") or ""
        title_str = f"{cs_disp}  \u2022  {airline}" if airline else cs_disp
        if len(title_str) > 48:
            title_str = title_str[:47] + "..."

        hdg_str = f"  \u2022  HDG {fl['track']}\u00b0" if fl.get("track") is not None else ""
        cnt_str = f" ({idx + 1}/{total})" if total > 1 else ""
        dist_str = f"{fl['dist_km']} km {fl.get('compass', '')}{hdg_str}{cnt_str}"

        self.lbl_flight_title.config(text=safe_tcl(title_str))
        self.lbl_flight_dist.config(text=safe_tcl(dist_str))

        if fl.get("route_valid") and fl.get("orig_code") and fl.get("dst_code"):
            self.lbl_flight_route.place_forget()
            o_city = fl.get("orig_city") or ""
            d_city = fl.get("dst_city") or ""
            o_txt = f"{fl['orig_code']}  {o_city}" if o_city else fl["orig_code"]
            d_txt = f"{d_city}  {fl['dst_code']}" if d_city else fl["dst_code"]
            if len(o_txt) > 22:
                o_txt = o_txt[:21] + "."
            if len(d_txt) > 22:
                d_txt = d_txt[:21] + "."

            self.lbl_map_orig.config(text=safe_tcl(o_txt))
            self.lbl_map_dst.config(text=safe_tcl(d_txt))
            self.lbl_map_orig.place(x=14, y=36)
            self.flight_prog_canvas.place(x=302, y=34)
            self.lbl_map_dst.place(relx=1.0, x=-14, y=36, anchor="ne")

            pc = self.flight_prog_canvas
            pc.delete("all")
            pw = 420
            pct = max(0.02, min(0.98, fl.get("progress_pct", 50) / 100.0))
            px = 18 + (pw - 36) * pct
            cy = 16
            pc.create_line(18, cy, pw - 18, cy, fill="#1E293B", width=5, capstyle=tk.ROUND)
            pc.create_line(18, cy, px, cy, fill="#F59E0B", width=5, capstyle=tk.ROUND)
            pc.create_oval(12, cy - 5, 22, cy + 5, fill="#10B981", outline="")
            pc.create_oval(pw - 22, cy - 5, pw - 12, cy + 5, fill="#F59E0B", outline="")
            pc.create_polygon(px + 7, cy, px - 5, cy - 6, px - 2, cy, px - 5, cy + 6, fill="#FFFFFF", outline="#38BDF8")
            pc.create_text(pw // 2, 5, text=f"{fl.get('progress_pct', 50)}%", fill="#94A3B8", font=("DejaVu Sans", 8, "bold"))
        else:
            self.lbl_map_orig.place_forget()
            self.lbl_map_dst.place_forget()
            self.flight_prog_canvas.place_forget()
            brng_lbl = f"Heading {fl['track']}\u00b0 ({_compass_dir(fl['track'])})" if fl.get("track") is not None else "Live Track"
            rt_txt = f"Crossing Chennai 10 km Zone  \u2022  {brng_lbl}"
            self.lbl_flight_route.config(text=safe_tcl(rt_txt), fg=self.theme["accent"])
            self.lbl_flight_route.place(x=14, y=34)

        parts = [fl["aircraft"], f"Alt: {fl['alt_ft']:,} ft"]
        if fl.get("speed_kmh") is not None:
            parts.append(f"Speed: {fl['speed_kmh']} km/h")
        if fl.get("vspeed_fpm") not in (None, 0):
            sign = "+" if fl["vspeed_fpm"] > 0 else ""
            parts.append(f"V/S: {sign}{fl['vspeed_fpm']} fpm")
        if fl.get("squawk"):
            parts.append(f"Squawk: {fl['squawk']}")
        if fl.get("reg"):
            parts.append(f"Reg: {fl['reg']}")
        parts.append("Local 10 km Radar")
        self.lbl_flight_meta.config(text=safe_tcl("  \u2022  ".join(parts)))

    def _update_flight_clear_hud(self, last_fl):
        self.lbl_flight_badge.config(text=safe_tcl("\u2708 10 KM RADAR"), bg="#0284C7")
        self.lbl_flight_title.config(text=safe_tcl("Chennai Airspace Clear (0 within 10 km)"))
        self.lbl_flight_dist.config(text=safe_tcl("Auto-Popup: Active (10 km)"))

        self.lbl_map_orig.place_forget()
        self.lbl_map_dst.place_forget()
        self.flight_prog_canvas.place_forget()

        if last_fl:
            code = last_fl.get("iata") or last_fl["callsign"]
            airline = last_fl.get("airline") or ""
            rt_part = ""
            if last_fl.get("route_valid") and last_fl.get("orig_code") and last_fl.get("dst_code"):
                rt_part = f" ({last_fl['orig_code']} \u2192 {last_fl['dst_code']})"
            ago_s = max(0, int(time.time() - last_fl.get("seen_at", time.time())))
            ago_txt = f"{ago_s}s ago" if ago_s < 60 else (f"{ago_s // 60}m ago" if ago_s < 3600 else f"{ago_s // 3600}h {(ago_s % 3600) // 60}m ago")
            air_part = f" - {airline}" if airline else ""
            rt_txt = f"Last 10 km Crossing: {code}{air_part}{rt_part}  \u2022  {ago_txt}"
            self.lbl_flight_route.config(text=safe_tcl(rt_txt), fg="#FBBF24")
            self.lbl_flight_route.place(x=14, y=34)

            parts = [last_fl.get("aircraft", "Aircraft"), f"Closest: {last_fl['dist_km']} km {last_fl.get('compass', '')}", f"Alt: {last_fl['alt_ft']:,} ft"]
            if last_fl.get("speed_kmh") is not None:
                parts.append(f"Speed: {last_fl['speed_kmh']} km/h")
            if last_fl.get("reg"):
                parts.append(f"Reg: {last_fl['reg']}")
            self.lbl_flight_meta.config(text=safe_tcl("  \u2022  ".join(parts)))
        else:
            self.lbl_flight_route.config(text=safe_tcl("Scanning 10 km radius around Chennai (13.0827\u00b0N, 80.2707\u00b0E) every 15s"), fg=self.theme["accent"])
            self.lbl_flight_route.place(x=14, y=34)
            self.lbl_flight_meta.config(text=safe_tcl("Automatically switches to full-screen live track whenever any aircraft enters 10 km of Home"))

    def _request_map_tiles(self, tile_key, zoom, vx0, vy0, mw, mh):
        if self._map_loading_key == tile_key:
            return
        self._map_loading_key = tile_key
        def _loader():
            try:
                img = _fetch_map_mosaic(zoom, vx0, vy0, mw, mh)
                self._map_pending_mosaic = (tile_key, img)
            except Exception as e:
                log_msg(f"Map tile fetch error: {e}")
            finally:
                if self._map_loading_key == tile_key:
                    self._map_loading_key = None
        threading.Thread(target=_loader, daemon=True).start()

    def _animate_flight_map(self):
        if STATE.active_page != 5:
            self._map_anim_after = None
            return
        self._draw_flight_map_canvas()
        self._map_anim_frame = (self._map_anim_frame + 1) % 10000
        self._map_anim_after = self.root.after(125, self._animate_flight_map)

    def _draw_pill_label(self, c, x, y, text, fg, bg="#09111E", border="#1E293B", anchor="center", font=("DejaVu Sans", 8, "bold")):
        txt = safe_tcl(text)
        tid = c.create_text(x, y, text=txt, fill=fg, font=font, anchor=anchor)
        bbox = c.bbox(tid)
        if bbox:
            pad_x, pad_y = 6, 3
            mw = self.screen_w
            mh = self.flight_map_h
            dx = 0
            dy = 0
            if bbox[0] - pad_x < 4:
                dx = 4 - (bbox[0] - pad_x)
            elif bbox[2] + pad_x > mw - 4:
                dx = (mw - 4) - (bbox[2] + pad_x)
            if bbox[1] - pad_y < 4:
                dy = 4 - (bbox[1] - pad_y)
            elif bbox[3] + pad_y > mh - 4:
                dy = (mh - 4) - (bbox[3] + pad_y)
            if dx or dy:
                c.move(tid, dx, dy)
                bbox = c.bbox(tid)
            rid = c.create_rectangle(
                bbox[0] - pad_x, bbox[1] - pad_y, bbox[2] + pad_x, bbox[3] + pad_y,
                fill=bg, outline=border, width=1
            )
            c.tag_lower(rid, tid)

    def _draw_flight_map_canvas(self):
        fl = self._active_map_flight
        c = self.flight_map_canvas
        mw = self.screen_w
        mh = self.flight_map_h
        c.delete("all")

        home_lat = float(CONFIG.get("latitude", 13.0827))
        home_lon = float(CONFIG.get("longitude", 80.2707))

        if fl is not None:
            dt = max(0.0, min(25.0, time.time() - fl.get("fetched_at", time.time())))
            ac_lat, ac_lon = fl["lat"], fl["lon"]
            trk = fl.get("track")
            if trk is not None and fl.get("speed_kmh"):
                spd_mps = fl["speed_kmh"] / 3.6
                tr_rad = math.radians(trk)
                ac_lat += (spd_mps * dt * math.cos(tr_rad)) / 111320.0
                ac_lon += (spd_mps * dt * math.sin(tr_rad)) / (111320.0 * max(0.2, math.cos(math.radians(ac_lat))))

            has_route = bool(fl.get("route_valid") and fl.get("orig_lat") is not None and fl.get("dst_lat") is not None)
            if has_route:
                vp_pts = [(home_lat, home_lon), (ac_lat, ac_lon), (fl["orig_lat"], fl["orig_lon"]), (fl["dst_lat"], fl["dst_lon"])]
            else:
                dlat, dlon = 0.11, 0.18
                min_lat = min(home_lat - dlat, ac_lat - 0.03)
                max_lat = max(home_lat + dlat, ac_lat + 0.03)
                min_lon = min(home_lon - dlon, ac_lon - 0.04)
                max_lon = max(home_lon + dlon, ac_lon + 0.04)
                vp_pts = [(min_lat, min_lon), (max_lat, max_lon)]
        else:
            has_route = False
            vp_pts = [(home_lat - 0.105, home_lon - 0.18), (home_lat + 0.105, home_lon + 0.18)]

        zoom, vx0, vy0 = _choose_map_viewport(vp_pts, mw, mh)
        tile_key = (zoom, int(round(vx0 / 40.0)), int(round(vy0 / 40.0)))

        if self._map_pending_mosaic is not None:
            p_key, p_img = self._map_pending_mosaic
            self._map_pending_mosaic = None
            try:
                self._map_bg_photo = ImageTk.PhotoImage(p_img)
                self._map_bg_key = p_key
            except Exception:
                pass

        if self._map_bg_key != tile_key:
            self._request_map_tiles(tile_key, zoom, vx0, vy0, mw, mh)

        if self._map_bg_photo is not None and self._map_bg_key == tile_key:
            c.create_image(0, 0, image=self._map_bg_photo, anchor="nw")
        else:
            for gx in range(48, mw, 64):
                c.create_line(gx, 0, gx, mh, fill="#0F213B", width=1)
            for gy in range(36, mh, 64):
                c.create_line(0, gy, mw, gy, fill="#0F213B", width=1)

        def to_xy(lt, ln):
            wx, wy = _latlon_to_world_px(lt, ln, zoom)
            return wx - vx0, wy - vy0

        hx, hy = to_xy(home_lat, home_lon)
        frame = self._map_anim_frame

        # Draw local 5 km and 10 km radar rings around Chennai whenever scale permits
        px_5km_x, _ = to_xy(home_lat, home_lon + (5.0 / 111.32) / max(0.2, math.cos(math.radians(home_lat))))
        r5 = abs(px_5km_x - hx)
        if r5 >= 8:
            for mult, lbl, col in [(1, "5 km", "#1E3A5F"), (2, "10 km", "#0284C7")]:
                rr = r5 * mult
                c.create_oval(hx - rr, hy - rr, hx + rr, hy + rr, outline=col, dash=(5, 4), width=1.4 if mult == 2 else 1)
                c.create_text(hx + 6, hy - rr + 10, text=lbl, fill="#7DD3FC" if mult == 2 else "#64748B", font=("DejaVu Sans", 8, "bold"), anchor="w")

        if fl is None:
            # Clear 10 km Radar Mode: crosshairs + animated rotating radar sweep inside 10 km ring
            r10 = max(60, r5 * 2)
            c.create_line(hx - r10, hy, hx + r10, hy, fill="#162D4C", width=1)
            c.create_line(hx, hy - r10, hx, hy + r10, fill="#162D4C", width=1)
            sweep_deg = (frame * 5) % 360
            for trail_idx, s_col, w_px in [(2, "#0E3A5B", 1), (1, "#0284C7", 1.5), (0, "#38BDF8", 2.5)]:
                ang = math.radians(((sweep_deg - trail_idx * 8) % 360) - 90.0)
                c.create_line(hx, hy, hx + r10 * math.cos(ang), hy + r10 * math.sin(ang), fill=s_col, width=w_px)

            # Pulsing Chennai center beacon
            for wave_off in (0, 14):
                phase = ((frame + wave_off) % 28) / 28.0
                pr = 6 + phase * 34
                pcol = "#EF4444" if phase < 0.4 else ("#B91C1C" if phase < 0.72 else "#1E3A5F")
                c.create_oval(hx - pr, hy - pr, hx + pr, hy + pr, outline=pcol, width=2 if phase < 0.45 else 1)
            c.create_polygon(hx, hy - 6, hx + 6, hy, hx, hy + 6, hx - 6, hy, fill="#EF4444", outline="#FEF08A", width=1.5)
            self._draw_pill_label(c, hx, hy + 20, "HOME RADAR", "#FCA5A5", bg="#1E1016", border="#991B1B")

            # Info card bottom-left on Canvas
            last_fl = getattr(self, "_last_flight_seen_ui", None)
            bx0, by0, bx1, by1 = 14, mh - 108, 368, mh - 12
            c.create_rectangle(bx0, by0, bx1, by1, fill="#09111E", outline="#1E3A5F", width=1.5)
            if last_fl:
                code = last_fl.get("iata") or last_fl["callsign"]
                ago_s = max(0, int(time.time() - last_fl.get("seen_at", time.time())))
                ago_txt = f"{ago_s}s ago" if ago_s < 60 else (f"{ago_s // 60}m ago" if ago_s < 3600 else f"{ago_s // 3600}h {(ago_s % 3600) // 60}m ago")
                c.create_text(bx0 + 12, by0 + 14, text="LAST 10 KM HOME CROSSING", fill="#FBBF24", font=("DejaVu Sans", 8, "bold"), anchor="w")
                rt_str = f"  ({last_fl['orig_code']} \u2192 {last_fl['dst_code']})" if (last_fl.get("route_valid") and last_fl.get("orig_code") and last_fl.get("dst_code")) else ""
                c.create_text(bx0 + 12, by0 + 36, text=safe_tcl(f"{code}{rt_str}  \u2022  {ago_txt}"), fill="#F8FAFC", font=("DejaVu Sans", 10, "bold"), anchor="w")
                c.create_text(bx0 + 12, by0 + 57, text=safe_tcl(f"{last_fl.get('aircraft', 'Aircraft')}  \u2022  {last_fl['dist_km']} km {last_fl.get('compass', '')}"), fill="#CBD5E1", font=("DejaVu Sans", 8), anchor="w")
                c.create_text(bx0 + 12, by0 + 77, text=safe_tcl(f"Alt: {last_fl['alt_ft']:,} ft  \u2022  Speed: {last_fl.get('speed_kmh', '--')} km/h"), fill="#38BDF8", font=("DejaVu Sans", 8, "bold"), anchor="w")
            else:
                c.create_text(bx0 + 12, by0 + 14, text="HOME 10 KM RADAR ACTIVE", fill="#38BDF8", font=("DejaVu Sans", 8, "bold"), anchor="w")
                c.create_text(bx0 + 12, by0 + 38, text="Monitoring 10 km circle around Chennai", fill="#F8FAFC", font=("DejaVu Sans", 9, "bold"), anchor="w")
                c.create_text(bx0 + 12, by0 + 59, text="Center: 13.0827\u00b0N, 80.2707\u00b0E", fill="#CBD5E1", font=("DejaVu Sans", 8), anchor="w")
                c.create_text(bx0 + 12, by0 + 78, text="Full-screen popup activates on crossing", fill="#34D399", font=("DejaVu Sans", 8, "bold"), anchor="w")

            self._draw_pill_label(c, 12, 16, "\u25cf 10 KM HOME RADAR  \u2022  CLEAR", "#7DD3FC", bg="#09111E", border="#1E293B", anchor="w")
        else:
            ax, ay = to_xy(ac_lat, ac_lon)
            if has_route:
                arc = _great_circle_points(fl["orig_lat"], fl["orig_lon"], fl["dst_lat"], fl["dst_lon"], steps=44)
                arc_xy = [to_xy(lt, ln) for lt, ln in arc]
                best_i = min(range(len(arc_xy)), key=lambda i: (arc_xy[i][0] - ax) ** 2 + (arc_xy[i][1] - ay) ** 2)

                flown_flat = []
                for pt in arc_xy[:best_i + 1]:
                    flown_flat.extend(pt)
                if len(flown_flat) >= 4:
                    c.create_line(flown_flat, fill="#92400E", width=6, smooth=True, capstyle=tk.ROUND)
                    c.create_line(flown_flat, fill="#F59E0B", width=3, smooth=True, capstyle=tk.ROUND)

                rem_pts = [(ax, ay)] + arc_xy[best_i:]
                rem_flat = []
                for pt in rem_pts:
                    rem_flat.extend(pt)
                if len(rem_flat) >= 4:
                    c.create_line(rem_flat, fill="#0284C7", width=3, dash=(8, 6), smooth=True)
                    c.create_line(rem_flat, fill="#7DD3FC", width=1, dash=(8, 6), smooth=True)

                if len(rem_pts) >= 2:
                    prog_i = int((frame * 0.6) % len(rem_pts))
                    bx, by = rem_pts[prog_i]
                    c.create_oval(bx - 4, by - 4, bx + 4, by + 4, fill="#38BDF8", outline="#FFFFFF")

                ox, oy = arc_xy[0]
                dx, dy = arc_xy[-1]
                ox_c, oy_c = max(56, min(mw - 56, ox)), max(30, min(mh - 22, oy))
                dx_c, dy_c = max(56, min(mw - 56, dx)), max(30, min(mh - 22, dy))
                c.create_oval(ox - 7, oy - 7, ox + 7, oy + 7, outline="#10B981", width=2)
                c.create_oval(ox - 3, oy - 3, ox + 3, oy + 3, fill="#10B981", outline="")
                self._draw_pill_label(c, ox_c, oy_c + (16 if oy_c < mh - 38 else -16), f"{fl['orig_code']}  {fl.get('orig_city', '')}".strip(), "#34D399", border="#065F46")

                c.create_oval(dx - 8, dy - 8, dx + 8, dy + 8, outline="#F59E0B", width=2)
                c.create_oval(dx - 3, dy - 3, dx + 3, dy + 3, fill="#F59E0B", outline="")
                self._draw_pill_label(c, dx_c, dy_c + (16 if dy_c < mh - 38 else -16), f"{fl['dst_code']}  {fl.get('dst_city', '')}".strip(), "#FBBF24", border="#92400E")
            elif trk is not None:
                tr_rad = math.radians(trk)
                proj_lat = ac_lat + 0.42 * math.cos(tr_rad)
                proj_lon = ac_lon + 0.42 * math.sin(tr_rad) / max(0.2, math.cos(math.radians(ac_lat)))
                px2, py2 = to_xy(proj_lat, proj_lon)
                c.create_line(ax, ay, px2, py2, fill="#38BDF8", width=2, dash=(6, 5))

            trail = fl.get("trail") or []
            if len(trail) >= 2:
                t_flat = []
                for tlat, tlon in trail:
                    tx, ty = to_xy(tlat, tlon)
                    t_flat.extend([tx, ty])
                t_flat.extend([ax, ay])
                c.create_line(t_flat, fill="#F59E0B", width=2.5, dash=(3, 3))

            for wave_off in (0, 14):
                phase = ((frame + wave_off) % 28) / 28.0
                pr = 6 + phase * 34
                pcol = "#EF4444" if phase < 0.4 else ("#B91C1C" if phase < 0.72 else "#1E3A5F")
                c.create_oval(hx - pr, hy - pr, hx + pr, hy + pr, outline=pcol, width=2 if phase < 0.45 else 1)
            c.create_polygon(hx, hy - 6, hx + 6, hy, hx, hy + 6, hx - 6, hy, fill="#EF4444", outline="#FEF08A", width=1.5)
            hx_c = max(84, min(mw - 84, hx))
            hy_off = 18 if hy < mh - 36 else -18
            self._draw_pill_label(c, hx_c, hy + hy_off, "HOME", "#FCA5A5", bg="#1E1016", border="#991B1B")

            halo_r = 14 + (frame % 10) * 0.9
            c.create_oval(ax - halo_r, ay - halo_r, ax + halo_r, ay + halo_r, outline="#38BDF8", width=1)
            c.create_oval(ax - 11, ay - 11, ax + 11, ay + 11, outline="#F59E0B", width=1.5)

            theta = math.radians(trk if trk is not None else 0.0)
            sn, cs = math.sin(theta), math.cos(theta)
            jet_shape = [
                (0, 14), (2.4, 5), (12.5, -3), (12.5, -6), (2.4, -3),
                (1.9, -10), (6.0, -13), (6.0, -15), (0, -12),
                (-6.0, -15), (-6.0, -13), (-1.9, -10),
                (-2.4, -3), (-12.5, -6), (-12.5, -3), (-2.4, 5)
            ]
            jet_poly = []
            for rr, ff in jet_shape:
                jet_poly.extend([ax + rr * cs + ff * sn, ay + rr * sn - ff * cs])
            c.create_polygon(jet_poly, fill="#FFFFFF", outline="#0284C7", width=1.2)

            tag_txt = f"{fl.get('iata') or fl['callsign']}  \u2022  {fl['alt_ft']:,} ft"
            tag_x = max(72, min(mw - 72, ax))
            tag_y = ay - 22 if ay > 48 else ay + 24
            self._draw_pill_label(c, tag_x, tag_y, tag_txt, "#FFFFFF", bg="#0F172A", border="#38BDF8")

            self._draw_pill_label(c, 12, 16, "\u25cf LIVE OVERHEAD HOME (<= 10 KM)", "#FCA5A5", bg="#09111E", border="#991B1B", anchor="w")

        tog_id = c.create_text(mw - 14, 16, text=safe_tcl("[M] Back to Main"), fill="#94A3B8", font=("DejaVu Sans", 8, "bold"), anchor="e")
        tb = c.bbox(tog_id)
        if tb:
            box = (tb[0] - 6, tb[1] - 3, tb[2] + 6, tb[3] + 3)
            tr = c.create_rectangle(*box, fill="#09111E", outline="#1E293B", width=1)
            c.tag_lower(tr, tog_id)
            self._flight_toggle_box = box

    def update_system_page_ui(self):
        with STATE.lock:
            s = dict(STATE.system)

        cpu_p = s["cpu_percent"]
        cpu_t = s["cpu_temp"]
        ram_p = s["ram_percent"]
        disk_p = s["disk_percent"]

        cpu_col = "#EF4444" if cpu_p > 85 or cpu_t > 75 else ("#F59E0B" if cpu_p > 70 or cpu_t > 68 else self.theme["fg"])
        ram_col = "#EF4444" if ram_p > 85 else ("#F59E0B" if ram_p > 70 else self.theme["fg"])
        disk_col = "#EF4444" if disk_p > 90 else ("#F59E0B" if disk_p > 80 else self.theme["fg"])

        self.lbl_sys_cpu.config(text=f"{cpu_p}%", fg=cpu_col)
        self.lbl_sys_temp.config(text=safe_tcl(f"Temp: {cpu_t}\u00b0C  \u2022  Load: {s.get('load_avg', '--')}"), fg=cpu_col)

        self.lbl_sys_ram.config(text=f"{ram_p}%", fg=ram_col)
        self.lbl_sys_ram_sub.config(text=f"{s['ram_used_mb']} MB used / {s['ram_total_mb']} MB")

        self.lbl_sys_disk.config(text=f"{disk_p}%", fg=disk_col)
        self.lbl_sys_disk_sub.config(text=f"{s['disk_free_gb']} GB Free on /")
        usb_txt = s.get("usb_status", "USB: Not mounted")
        self.lbl_sys_usb.config(
            text=safe_tcl(usb_txt),
            fg="#10B981" if "free" in usb_txt else self.theme["fg_sub"]
        )

        self.lbl_sys_net.config(text=f"Internet: {s['inet_ping']}", fg="#10B981" if "ms" in s["inet_ping"] else "#EF4444")
        self.lbl_sys_net_sub.config(text=safe_tcl(f"Router: {s['router_ping']}  \u2022  {s.get('lan_ip', '--')}"))
        rx_s = _format_rate(s.get("net_rx_kbs", 0.0))
        tx_s = _format_rate(s.get("net_tx_kbs", 0.0))
        self.lbl_sys_bw.config(text=safe_tcl(f"\u2193 {rx_s}   \u2191 {tx_s}"))

        svcs = s.get("services") or {}
        for lbl, chip in self.svc_chip_labels.items():
            ok = bool(svcs.get(lbl, False))
            chip.config(fg="#10B981" if ok else "#EF4444")

        ads_b = s.get("ads_blocked", "--")
        ads_p = s.get("ads_percent", "--")
        dns_q = s.get("dns_queries", "--")
        self.lbl_sys_adguard.config(text=safe_tcl(f"Ads Blocked: {ads_b} / {dns_q} ({ads_p})"))

        sec_armed = bool(s.get("security_armed", False))
        sec_cnt = int(s.get("security_events", 0))
        sec_last = s.get("security_last", "")
        if sec_armed:
            sec_txt = f"Sec: ARMED ({sec_cnt} ev)"
            sec_col = "#EF4444"
        else:
            last_hm = sec_last.split()[-1] if sec_last else ""
            tail = f" \u2022 last {last_hm}" if last_hm else ""
            sec_txt = f"Sec: Standby ({sec_cnt} ev{tail})"
            sec_col = "#94A3B8"
        self.lbl_sys_sec.config(text=safe_tcl(sec_txt), fg=sec_col)

        self.lbl_sys_pwr.config(text=f"Power: {s['battery_status']}")
        self.lbl_sys_uptime.config(text=f"Uptime: {s['uptime_str']}")
        self.lbl_sys_ts.config(
            text=f"Tailscale: {s['tailscale_status']}",
            fg="#10B981" if s["tailscale_status"] == "Connected" else self.theme["fg_sub"]
        )

        if STATE.active_page == 2:
            self.draw_sparklines(s["cpu_history"], s["ram_history"], s.get("net_history"))

    def draw_sparklines(self, cpu_hist, ram_hist, net_hist=None):
        c = self.sys_sparkline
        c.delete("all")
        w = c.winfo_width() or (self.screen_w - 60)
        h = c.winfo_height() or 110

        pad_l = 36
        pad_r = 16
        pad_y = 12
        cw = max(40, w - pad_l - pad_r)
        ch = max(30, h - 2 * pad_y)
        n = len(cpu_hist)
        if n < 2:
            return
        step_x = cw / float(n - 1)

        for pct_g, lbl_g in ((100, "100%"), (50, "50%"), (0, "0%")):
            gy = pad_y + ch - (pct_g / 100.0) * ch
            c.create_line(pad_l, gy, pad_l + cw, gy, fill="#334155", width=1, dash=(2, 3) if pct_g else None)
            c.create_text(pad_l - 6, gy, text=lbl_g, fill="#64748B", font=("DejaVu Sans", 7), anchor="e")

        def _plot(series, color, width=2):
            pts = []
            for i, val in enumerate(series):
                x = pad_l + i * step_x
                y = pad_y + ch - (max(0.0, min(100.0, float(val))) / 100.0) * ch
                pts.extend([x, y])
            if len(pts) >= 4:
                c.create_line(pts, fill=color, width=width, smooth=True)

        if net_hist and len(net_hist) == n:
            _plot(net_hist, "#F59E0B", width=1.8)
        _plot(ram_hist, "#10B981", width=2.2)
        _plot(cpu_hist, "#06B6D4", width=2.2)

    def update_slideshow(self):
        skip = False
        with STATE.lock:
            cur_p = STATE.active_page
            if STATE.skip_photo_requested:
                skip = True
                STATE.skip_photo_requested = False

        if skip and cur_p == 6:
            self.update_server_photos_slide(advance=True)
            self.root.after(1000, self.update_slideshow)
            return

        interval = CONFIG["slideshow"].get("interval_seconds", 15)
        now = time.time()

        if skip or (now - self.last_photo_change >= interval):
            self.last_photo_change = now
            cache_files = glob.glob(f"{IMG_CACHE_DIR}/*.jpg")

            target_img_path = None
            is_local = False

            if cache_files:
                prev = getattr(self, "last_photo_path", None)
                choices = [f for f in cache_files if f != prev] if len(cache_files) > 1 else cache_files
                target_img_path = random.choice(choices)
                is_local = False
            else:
                fallback_srv = list_server_photos()
                if fallback_srv:
                    target_img_path = random.choice(fallback_srv)
                    is_local = True

            if target_img_path and os.path.exists(target_img_path):
                try:
                    pil_img = _load_oriented_rgb(target_img_path)
                    pil_fit = ImageOps.fit(pil_img, (self.right_w, self.top_h), method=RESAMPLE_MODE)
                    tk_img = ImageTk.PhotoImage(pil_fit)

                    self.slideshow_canvas.delete("img")
                    self.slideshow_canvas.create_image(0, 0, image=tk_img, anchor="nw", tags="img")
                    self.slideshow_canvas.image = tk_img
                    self.last_photo_path = target_img_path

                    cap_text = os.path.basename(target_img_path) if is_local else "Bing Daily / Gallery"
                    self.slideshow_canvas.itemconfig(self.photo_caption_id, text=safe_tcl(cap_text))
                    bbox = self.slideshow_canvas.bbox(self.photo_caption_id)
                    if bbox:
                        self.slideshow_canvas.coords(
                            self.photo_caption_bg_id,
                            bbox[0] - 8, bbox[1] - 4, bbox[2] + 8, bbox[3] + 4
                        )
                    self.slideshow_canvas.lift(self.photo_caption_bg_id)
                    self.slideshow_canvas.lift(self.photo_caption_id)

                except Exception as e:
                    log_msg(f"Corrupted image skip: {e}")
                    if not is_local:
                        try: os.remove(target_img_path)
                        except: pass

        self.root.after(1000, self.update_slideshow)

    def toggle_server_photos(self):
        with STATE.lock:
            cur_p = STATE.active_page
        self.last_page_change = time.time()
        if cur_p == 6:
            self.set_page(0)
        else:
            self._flight_auto_active = False
            self.set_page(6)

    def _on_photos_canvas_click(self, event):
        if self._photos_toggle_box:
            x0, y0, x1, y1 = self._photos_toggle_box
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self.toggle_server_photos()
                return
        self.update_server_photos_slide(advance=True)

    def _schedule_server_photo_next(self):
        if self._server_photo_after is not None:
            try:
                self.root.after_cancel(self._server_photo_after)
            except Exception:
                pass
            self._server_photo_after = None
        if STATE.active_page != 6:
            return
        sec = max(3, int(CONFIG.get("slideshow", {}).get("server_photo_interval_seconds", 8)))
        self._server_photo_after = self.root.after(sec * 1000, lambda: self.update_server_photos_slide(advance=True))

    def update_server_photos_slide(self, advance=True):
        if STATE.active_page != 6:
            return
        c = self.photos_canvas
        mw = self.screen_w
        mh = self.top_h
        c.delete("all")
        self._photos_toggle_box = None

        files = list_server_photos()
        if not files:
            c.create_rectangle(140, 108, mw - 140, mh - 108, fill="#0B1324", outline="#1E3A5F", width=2)
            c.create_text(mw // 2, 166, text="SERVER PHOTOS SLIDESHOW", fill="#38BDF8", font=("DejaVu Sans", 16, "bold"))
            c.create_text(mw // 2, 212, text="No photos found in /home/shared/dashboard_photos", fill="#F8FAFC", font=("DejaVu Sans", 12, "bold"))
            c.create_text(
                mw // 2, 250,
                text="Upload JPG, JPEG, PNG, WEBP, BMP, or GIF via FileBrowser (:8080), Copyparty (:3923), or Samba",
                fill="#94A3B8", font=("DejaVu Sans", 10)
            )
            c.create_text(mw // 2, 290, text="Newly uploaded photos appear automatically here", fill="#34D399", font=("DejaVu Sans", 10, "bold"))
        else:
            rendered = False
            total = len(files)
            if advance or self._server_photo_idx < 0:
                base_idx = (self._server_photo_idx + 1) % total
            else:
                base_idx = self._server_photo_idx % total

            for offset in range(total):
                idx = (base_idx + offset) % total
                target_path = files[idx]
                try:
                    composed, orig_size = _compose_server_photo(target_path, mw, mh)
                    tk_img = ImageTk.PhotoImage(composed)
                    c.create_image(0, 0, image=tk_img, anchor="nw")
                    self._server_photo_tk = tk_img
                    self._server_photo_idx = idx
                    self._server_photo_path = target_path

                    # Bottom caption bar
                    c.create_rectangle(0, mh - 36, mw, mh, fill="#070C16", outline="")
                    c.create_line(0, mh - 36, mw, mh - 36, fill="#1E293B", width=1)
                    base_name = os.path.basename(target_path)
                    stem, ext = os.path.splitext(base_name)
                    if len(stem) > 52:
                        stem = stem[:51] + "..."
                    ext_tag = ext.lstrip(".").upper() or "IMG"
                    left_txt = f"{stem}  \u2022  {orig_size[0]}x{orig_size[1]} {ext_tag}"
                    right_txt = f"Photo {idx + 1} / {total}  \u2022  dashboard_photos  \u2022  [N] Next"
                    c.create_text(16, mh - 18, text=safe_tcl(left_txt), fill="#F8FAFC", font=("DejaVu Sans", 10, "bold"), anchor="w")
                    c.create_text(mw - 16, mh - 18, text=safe_tcl(right_txt), fill="#38BDF8", font=("DejaVu Sans", 9, "bold"), anchor="e")
                    rendered = True
                    break
                except Exception as e:
                    log_msg(f"Server photo load error ({os.path.basename(target_path)}): {e}")

            if not rendered:
                c.create_text(mw // 2, mh // 2, text="Unable to decode server photos", fill="#F87171", font=("DejaVu Sans", 13, "bold"))

        # Top-left & top-right corner badges
        self._draw_pill_label(c, 12, 16, "\u25cf SERVER PHOTOS", "#38BDF8", bg="#09111E", border="#1E293B", anchor="w")
        tog_id = c.create_text(mw - 14, 16, text=safe_tcl("[P] Back to Main"), fill="#94A3B8", font=("DejaVu Sans", 8, "bold"), anchor="e")
        tb = c.bbox(tog_id)
        if tb:
            box = (tb[0] - 6, tb[1] - 3, tb[2] + 6, tb[3] + 3)
            tr = c.create_rectangle(*box, fill="#09111E", outline="#1E293B", width=1)
            c.tag_lower(tr, tog_id)
            self._photos_toggle_box = box

        self._schedule_server_photo_next()

    def update_page_rotation(self):
        cycle_sec = CONFIG.get("page_cycle_seconds", 180)
        disp_sec = CONFIG.get("extra_page_display_seconds", 15)
        page_map = {"main": 0, "weather": 1, "system": 2, "money": 3, "sky": 4, "flight": 5, "photos": 6}
        enabled = [page_map[k] for k in CONFIG.get("pages_enabled", ["main", "weather", "system", "money", "sky", "flight", "photos"]) if page_map.get(k, 0) != 0]
        if not enabled:
            enabled = [1, 2, 3, 4, 5, 6]

        with STATE.lock:
            paused = STATE.rotation_paused
            cur_p = STATE.active_page

        if self._flight_auto_active:
            self.last_page_change = time.time()
        elif not paused:
            now = time.time()
            if cur_p == 0:
                if now - self.last_page_change >= cycle_sec:
                    self.last_page_change = now
                    next_p = enabled[self._rot_cursor % len(enabled)]
                    self._rot_cursor = (self._rot_cursor + 1) % len(enabled)
                    self.set_page(next_p)
            else:
                if now - self.last_page_change >= disp_sec:
                    self.last_page_change = now
                    self.set_page(0)

        self.root.after(1000, self.update_page_rotation)

    def set_page(self, page_num):
        page_num = int(page_num) % 7
        with STATE.lock:
            prev_p = STATE.active_page
            if prev_p == 5 and page_num != 5 and self._flight_auto_active:
                for fl in STATE.flights:
                    self.flight_map_dismissed_hexes.add(fl["hex"])
                self._flight_auto_active = False
            STATE.active_page = page_num

        if page_num != 4 and self._sky_anim_after is not None:
            try:
                self.root.after_cancel(self._sky_anim_after)
            except Exception:
                pass
            self._sky_anim_after = None
        if page_num != 5 and self._map_anim_after is not None:
            try:
                self.root.after_cancel(self._map_anim_after)
            except Exception:
                pass
            self._map_anim_after = None
        if page_num != 6 and self._server_photo_after is not None:
            try:
                self.root.after_cancel(self._server_photo_after)
            except Exception:
                pass
            self._server_photo_after = None

        self.page_main.place_forget()
        self.page_weather.place_forget()
        self.page_system.place_forget()
        self.page_money.place_forget()
        self.page_sky.place_forget()
        self.page_flight.place_forget()
        self.page_photos.place_forget()

        if page_num == 0:
            self.page_main.place(x=0, y=0)
        elif page_num == 1:
            self.page_weather.place(x=0, y=0)
            with STATE.lock:
                h_data = list(STATE.weather.get("hourly", []))
            self.hourly_canvas.draw_chart(h_data, self.theme)
        elif page_num == 2:
            self.page_system.place(x=0, y=0)
            self.update_system_page_ui()
        elif page_num == 3:
            self.page_money.place(x=0, y=0)
        elif page_num == 4:
            self.page_sky.place(x=0, y=0)
            self.update_sky_display()
        elif page_num == 5:
            self.page_flight.place(x=0, y=0)
            self.update_flight_display()
        elif page_num == 6:
            self.page_photos.place(x=0, y=0)
            self.update_server_photos_slide(advance=True)

    def change_page(self, direction):
        with STATE.lock:
            p = (STATE.active_page + direction) % 7
        self.last_page_change = time.time()
        self._flight_auto_active = False
        if p == 5:
            self.flight_map_dismissed_hexes.clear()
        self.set_page(p)

    def toggle_pause_rotation(self):
        with STATE.lock:
            STATE.rotation_paused = not STATE.rotation_paused
        log_msg(f"Rotation paused: {STATE.rotation_paused}")

    def skip_photo(self):
        if STATE.active_page == 6:
            self.update_server_photos_slide(advance=True)
            return
        with STATE.lock:
            STATE.skip_photo_requested = True

    def toggle_12_24h(self):
        CONFIG["time_format_24h"] = not CONFIG.get("time_format_24h", False)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(CONFIG, f, indent=2)
        except: pass

    def toggle_key_hints(self):
        self.key_hints_visible = not self.key_hints_visible
        if self.key_hints_visible:
            self.hint_bar.place(x=0, y=self.screen_h - self.news_h - 22, width=self.screen_w, height=22)
            self.hint_bar.lift()
        else:
            self.hint_bar.place_forget()

    def on_any_keypress(self):
        if STATE.night_mode_active:
            STATE.night_mode_active = False
            self.apply_theme("evening" if 17 <= datetime.datetime.now().hour < 23 else "day")
            set_screen_brightness(CONFIG["night_mode"]["brightness_day"])

    def toggle_night_mode(self):
        STATE.night_mode_active = not STATE.night_mode_active
        if STATE.night_mode_active:
            self.apply_theme("night")
            set_screen_brightness(CONFIG["night_mode"]["brightness_night"])
        else:
            self.apply_theme("day")
            set_screen_brightness(CONFIG["night_mode"]["brightness_day"])

    def update_burn_in_protection(self):
        self.burn_shift_x = random.choice([-2, 0, 2])
        self.burn_shift_y = random.choice([-2, 0, 2])
        self.sidebar_frame.place(x=self.burn_shift_x, y=self.burn_shift_y)
        self.root.after(300000, self.update_burn_in_protection)

    def _on_dot_click(self, page_idx):
        self.last_page_change = time.time()
        self._flight_auto_active = False
        if page_idx == 5:
            self.flight_map_dismissed_hexes.clear()
        self.set_page(page_idx)

    def cycle_theme(self):
        order = ["day", "evening", "night", "oled"]
        cur = self.current_theme_name if self.current_theme_name in order else "day"
        nxt = order[(order.index(cur) + 1) % len(order)]
        CONFIG["theme"] = nxt
        STATE.night_mode_active = (nxt == "night")
        self.apply_theme(nxt)

    def _apply_auto_theme_now(self):
        hour = datetime.datetime.now().hour
        n_cfg = CONFIG.get("night_mode", {})
        n_start = n_cfg.get("start_hour", 23)
        n_end = n_cfg.get("end_hour", 6)
        is_night_time = (hour >= n_start or hour < n_end)
        if is_night_time and n_cfg.get("enabled", True):
            self.apply_theme("night")
            set_screen_brightness(n_cfg.get("brightness_night", 0.5))
        elif 17 <= hour < n_start:
            self.apply_theme("evening")
            set_screen_brightness(n_cfg.get("brightness_evening", 0.85))
        else:
            self.apply_theme("day")
            set_screen_brightness(n_cfg.get("brightness_day", 1.0))

    def update_theme_and_brightness(self):
        pref = str(CONFIG.get("theme", "auto")).lower()
        if pref in ("day", "evening", "night", "oled"):
            self.apply_theme(pref)
        elif not STATE.night_mode_active:
            self._apply_auto_theme_now()
        self.root.after(600000, self.update_theme_and_brightness)

    def _retheme_descendants(self, widget, card_bg):
        for child in widget.winfo_children():
            if child in (getattr(self, "lbl_event_badge", None),):
                continue
            try:
                child.config(bg=card_bg)
            except Exception:
                pass
            if isinstance(child, tk.Frame):
                self._retheme_descendants(child, card_bg)

    def apply_theme(self, theme_name):
        self.current_theme_name = theme_name
        self.theme = THEMES.get(theme_name, THEMES["day"])

        bg = self.theme["bg"]
        card_bg = self.theme["card_bg"]
        card_border = self.theme["card_border"]
        fg = self.theme["fg"]
        fg_sub = self.theme["fg_sub"]
        accent = self.theme["accent"]

        self.content_frame.config(bg=bg)
        self.sidebar_frame.config(bg=bg)
        self.page_main.config(bg=bg)
        self.page_weather.config(bg=bg)
        self.page_system.config(bg=bg)
        self.page_money.config(bg=bg)

        all_cards = [self.card_clock, self.card_weather, self.card_event] + list(self._themed_cards)
        for c in all_cards:
            try:
                c.config(bg=card_bg, highlightbackground=card_border)
                self._retheme_descendants(c, card_bg)
            except Exception:
                pass

        for t_lbl in self._themed_titles:
            try:
                t_lbl.config(fg=accent, bg=card_bg)
            except Exception:
                pass

        self.lbl_time.config(fg=fg)
        self.lbl_ampm.config(fg=accent)
        self.lbl_date.config(fg=fg)
        self.lbl_location.config(fg=fg_sub)

        self.lbl_weather_temp.config(fg=fg)
        self.lbl_weather_desc.config(fg=accent)
        self.lbl_weather_feels.config(fg=fg_sub)
        self.lbl_weather_extras.config(fg=fg_sub)

        self.lbl_event_title.config(fg=fg)
        self.lbl_event_sub.config(fg=fg_sub)

        for lbl_d, lbl_t in getattr(self, "daily_labels", []):
            lbl_d.config(fg=fg)
            lbl_t.config(fg=accent)
        for lbl_w in (self.lbl_p2_uv_aqi, self.lbl_p2_sun, self.lbl_rate_usd, self.lbl_rate_eur, self.lbl_rate_gbp, self.lbl_day_of_year, self.lbl_week_num, self.lbl_quote_text, self.lbl_sys_pwr):
            try:
                lbl_w.config(fg=fg)
            except Exception:
                pass
        for lbl_s in (self.lbl_p2_wind_hum, self.lbl_rate_sub, self.lbl_sys_ram_sub, self.lbl_sys_disk_sub, self.lbl_sys_net_sub, self.lbl_sys_uptime):
            try:
                lbl_s.config(fg=fg_sub)
            except Exception:
                pass
        for lbl_a in (self.lbl_p2_moon, self.lbl_moon_info, self.lbl_quote_author):
            try:
                lbl_a.config(fg=accent)
            except Exception:
                pass

        self.hourly_canvas.config(bg=card_bg)
        self.sys_sparkline.config(bg=card_bg)
        self.flight_hud_accent.config(bg=accent)
        self.sky_hud_accent.config(bg=accent)
        if not getattr(self, "_active_map_flight", None) or not self._active_map_flight.get("route_valid"):
            self.lbl_flight_route.config(fg=accent)

        self.weather_icon.set_bg(card_bg)
        self.news_bar.update_theme(self.theme)

    def quit_app(self):
        log_msg("Dashboard exited cleanly by user (Esc)")
        self.root.destroy()
        sys.exit(0)

# ================= APPLICATION ENTRYPOINT =================
def acquire_single_instance_lock():
    """Acquire an exclusive non-blocking file lock so only one dashboard instance runs."""
    try:
        import fcntl
    except ImportError:
        return None
    lock_path = "/tmp/newdash25.lock"
    fp = open(lock_path, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fp.write(str(os.getpid()))
        fp.flush()
        return fp
    except BlockingIOError:
        fp.close()
        return False

if __name__ == "__main__":
    _instance_lock = acquire_single_instance_lock()
    if _instance_lock is False:
        log_msg("Another instance of newdash25.py is already running; exiting duplicate.")
        sys.exit(0)

    os.environ["DISPLAY"] = ":0"
    log_msg(f"Starting newdash v{DASHBOARD_VERSION} dashboard application")

    root = tk.Tk()
    app = DashboardApp(root)
    root.mainloop()
