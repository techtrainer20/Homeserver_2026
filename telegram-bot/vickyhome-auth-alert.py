#!/usr/bin/env python3
"""Lightweight real-time SSH & sudo intrusion alert monitor for Linux home servers."""

import ipaddress
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

CONF_PATH = "/etc/vickyhome-alerts.conf"
AUTH_LOG = "/var/log/auth.log"

RE_SSH_OK = re.compile(r"sshd\[\d+\]:\s+Accepted\s+(\S+)\s+for\s+(\S+)\s+from\s+([\da-fA-F.:]+)")
RE_SSH_FAIL = re.compile(r"sshd\[\d+\]:\s+(?:Failed\s+\S+\s+for(?:\s+invalid user)?\s+|Invalid user\s+)(\S+)\s+from\s+([\da-fA-F.:]+)")
RE_SUDO_CMD = re.compile(r"sudo:\s+(\S+)\s*:.*USER=(\S+)\s*;\s*COMMAND=(.+)$")
RE_SUDO_FAIL = re.compile(r"sudo:\s+(\S+)\s*:.*incorrect password attempts")


def load_conf():
    cfg = {
        "NTFY_SERVER": "https://ntfy.sh",
        "NTFY_TOPIC": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "ENABLE_SSH_ALERTS": "1",
        "ENABLE_SUDO_ALERTS": "1",
    }
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def classify_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_loopback:
            return "Localhost"
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return "Tailscale VPN"
        if ip.is_private:
            return "Home LAN"
        return "EXTERNAL INTERNET"
    except Exception:
        return "Unknown Network"


def send_alert(cfg, title, message, priority="high", tags="warning"):
    ntfy_topic = cfg.get("NTFY_TOPIC", "")
    ntfy_srv = cfg.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    tg_tok = cfg.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = cfg.get("TELEGRAM_CHAT_ID", "")

    def _post():
        if ntfy_topic:
            try:
                req = urllib.request.Request(
                    f"{ntfy_srv}/{ntfy_topic}",
                    data=message.encode("utf-8"),
                    headers={
                        "Title": title,
                        "Priority": priority,
                        "Tags": tags,
                        "Content-Type": "text/plain; charset=utf-8",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=6):
                    pass
            except Exception as e:
                print(f"[ntfy error] {e}", file=sys.stderr, flush=True)

        if tg_tok and tg_chat:
            try:
                payload = urllib.parse.urlencode({
                    "chat_id": tg_chat,
                    "text": f"{title}\n{message}",
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{tg_tok}/sendMessage",
                    data=payload,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=6):
                    pass
            except Exception as e:
                print(f"[telegram error] {e}", file=sys.stderr, flush=True)

    threading.Thread(target=_post, daemon=True).start()


def main():
    cfg = load_conf()
    ssh_on = cfg.get("ENABLE_SSH_ALERTS", "1") == "1"
    sudo_on = cfg.get("ENABLE_SUDO_ALERTS", "1") == "1"

    fail_history = {}
    cooldowns = {}

    proc = subprocess.Popen(
        ["tail", "-Fn0", AUTH_LOG],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        now = time.time()
        if len(cooldowns) > 200:
            cooldowns = {k: v for k, v in cooldowns.items() if now - v < 120}
        if len(fail_history) > 200:
            fail_history = {k: v for k, v in fail_history.items() if v and now - v[-1] < 120}

        if ssh_on:
            m = RE_SSH_OK.search(line)
            if m:
                method, user, ip = m.group(1), m.group(2), m.group(3)
                net = classify_ip(ip)
                prio = "urgent" if net == "EXTERNAL INTERNET" else "high"
                tags = "rotating_light" if net == "EXTERNAL INTERNET" else "lock,computer"
                ts = time.strftime("%H:%M:%S")
                send_alert(
                    cfg,
                    f"SSH Login: {user} ({net})",
                    f"Server: vickyhome\nUser: {user}\nSource IP: {ip} [{net}]\nMethod: {method}\nTime: {ts}",
                    priority=prio,
                    tags=tags,
                )
                continue

            m = RE_SSH_FAIL.search(line)
            if m:
                user, ip = m.group(1), m.group(2)
                net = classify_ip(ip)
                hist = [t for t in fail_history.get(ip, []) if now - t < 60]
                hist.append(now)
                fail_history[ip] = hist
                threshold = 1 if net == "EXTERNAL INTERNET" else 3
                last_cd = cooldowns.get(f"fail:{ip}", 0)
                if len(hist) >= threshold and (now - last_cd) > 45:
                    cooldowns[f"fail:{ip}"] = now
                    ts = time.strftime("%H:%M:%S")
                    send_alert(
                        cfg,
                        f"SSH Intrusion Probe! ({net})",
                        f"Server: vickyhome\nTarget user: {user}\nAttacker IP: {ip} [{net}]\nFailures in 60s: {len(hist)}\nTime: {ts}",
                        priority="urgent",
                        tags="rotating_light,warning",
                    )
                continue

        if sudo_on and "sudo:" in line:
            if "/var/log/usb_auto_mount.log" in line and "root : TTY=unknown" in line:
                continue

            mf = RE_SUDO_FAIL.search(line)
            if mf:
                invoker = mf.group(1)
                if now - cooldowns.get(f"sudofail:{invoker}", 0) > 30:
                    cooldowns[f"sudofail:{invoker}"] = now
                    send_alert(
                        cfg,
                        "Failed sudo Password on vickyhome!",
                        f"User: {invoker}\nEvent: {line[-140:]}",
                        priority="urgent",
                        tags="rotating_light,warning",
                    )
                continue

            ms = RE_SUDO_CMD.search(line)
            if ms:
                invoker, target, cmd = ms.group(1), ms.group(2), ms.group(3).strip()
                key = f"sudo:{invoker}:{cmd[:60]}"
                if now - cooldowns.get(key, 0) > 15:
                    cooldowns[key] = now
                    ts = time.strftime("%H:%M:%S")
                    send_alert(
                        cfg,
                        f"sudo Root Access ({invoker} -> {target})",
                        f"Server: vickyhome\nCommand: {cmd[:180]}\nTime: {ts}",
                        priority="default",
                        tags="key,shield",
                    )


if __name__ == "__main__":
    main()
