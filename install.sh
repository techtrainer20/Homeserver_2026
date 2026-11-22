#!/bin/bash
# ==============================================================================
#  homeserver_2026 - Complete Low-RAM Linux Home Server Installer
#  Installs: Wall Dashboard, FileBrowser, Copyparty, Samba, AdGuard Home,
#            Tailscale VPN, Cockpit HUD, Safe USB Auto-Mount, UFW Firewall,
#            Hardened Telegram Bot, OpenCV USB Security Cam & SSH/sudo Alerts.
#  Tested on Debian / Ubuntu / Raspberry Pi OS (i386, amd64, armhf, arm64).
# ==============================================================================

set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "Please run as root: sudo bash install.sh"
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-${USER}}"
if [ "$TARGET_USER" = "root" ]; then
    TARGET_HOME="/root"
else
    TARGET_HOME="$(eval echo "~${TARGET_USER}")"
fi

AUTO_ALL=0
if [ "${1:-}" = "--all" ] || [ "${1:-}" = "-y" ]; then
    AUTO_ALL=1
fi

ask_yes() {
    local prompt="$1"
    if [ "$AUTO_ALL" -eq 1 ]; then
        return 0
    fi
    local ans=""
    read -r -p "${prompt} [Y/n]: " ans || true
    case "${ans}" in
        [nN]|[nN][oO]) return 1 ;;
        *) return 0 ;;
    esac
}

echo "============================================================"
echo "  homeserver_2026 Full-Stack Home Server Installer"
echo "  Target Desktop User: ${TARGET_USER} (${TARGET_HOME})"
echo "============================================================"
echo ""

echo "[1/11] Installing base system & Python packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release \
    python3 python3-tk python3-pil python3-pil.imagetk \
    python3-requests python3-feedparser python3-psutil \
    python3-opencv python3-numpy \
    ffmpeg fswebcam v4l-utils x11-xserver-utils \
    ufw samba || true

echo "[2/11] Creating shared storage & dashboard photo directories..."
mkdir -p /home/shared/dashboard_photos /var/lib/copyparty
chmod 0775 /home/shared /home/shared/dashboard_photos
if [ "$TARGET_USER" != "root" ]; then
    chown "${TARGET_USER}:${TARGET_USER}" /home/shared/dashboard_photos || true
fi
ln -sfn /home/shared/dashboard_photos "${TARGET_HOME}/slides_local"
if [ "$TARGET_USER" != "root" ]; then
    chown -h "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}/slides_local" || true
fi

if ask_yes "[3/11] Install FileBrowser web file manager (port 8080)?"; then
    if ! command -v filebrowser >/dev/null 2>&1; then
        curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash
    fi
    install -m 0644 "${REPO_DIR}/systemd/filebrowser.service" /etc/systemd/system/filebrowser.service
    systemctl daemon-reload
    systemctl enable --now filebrowser.service
    echo "  -> FileBrowser enabled on port 8080 (root: /home/shared)."
fi

if ask_yes "[4/11] Install Copyparty drag-and-drop file server (port 3923)?"; then
    curl -fsSL https://github.com/9001/copyparty/releases/latest/download/copyparty-sfx.py \
        -o /usr/local/bin/copyparty-sfx.py
    chmod 0755 /usr/local/bin/copyparty-sfx.py
    if [ ! -f /etc/default/copyparty ]; then
        RAND_PASS="$(python3 -c 'import secrets, string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14)))')"
        printf 'CP_ACCT=admin:%s\n' "$RAND_PASS" > /etc/default/copyparty
        chmod 0600 /etc/default/copyparty
        echo "  -> Generated starter Copyparty login in /etc/default/copyparty (user: admin)."
    fi
    install -m 0644 "${REPO_DIR}/systemd/copyparty.service" /etc/systemd/system/copyparty.service
    systemctl daemon-reload
    systemctl enable --now copyparty.service
    echo "  -> Copyparty enabled on port 3923."
fi

if ask_yes "[5/11] Configure Samba SMB LAN file share (/home/shared + Recycle Bin)?"; then
    if [ -f /etc/samba/smb.conf ] && ! grep -q '^\[shared\]' /etc/samba/smb.conf; then
        cp -p /etc/samba/smb.conf "/etc/samba/smb.conf.bak.$(date +%Y%m%d%H%M%S)"
        cat <<'EOF' >> /etc/samba/smb.conf

[shared]
   comment = Home Server Shared Storage
   path = /home/shared
   browseable = yes
   writable = yes
   guest ok = yes
   read only = no
   create mask = 0775
   directory mask = 0775
   vfs objects = recycle
   recycle:repository = .recycle
   recycle:keeptree = yes
   recycle:versions = yes
   recycle:touch = yes
   recycle:exclude = *.tmp, *.temp, ~$*
EOF
    fi
    systemctl enable --now smbd nmbd || systemctl restart smbd || true
    echo "  -> Samba share [shared] ready (\\\\<server-ip>\\shared)."
fi

if ask_yes "[6/11] Install AdGuard Home network-wide DNS ad-blocker (DNS :53, Web UI :8081)?"; then
    if [ ! -x /opt/AdGuardHome/AdGuardHome ]; then
        curl -s -S -L https://raw.githubusercontent.com/AdGuardTeam/AdGuardHome/master/scripts/install.sh | sh -s -- -v || true
    fi
    echo "  -> AdGuard Home installed! Complete initial wizard at http://<server-ip>:3000"
    echo "     TIP: Set Admin Web Interface port to 8081 (so FileBrowser keeps 8080) and DNS to 53."
fi

if ask_yes "[7/11] Install Tailscale VPN (secure remote access from anywhere)?"; then
    if ! command -v tailscale >/dev/null 2>&1; then
        curl -fsSL https://tailscale.com/install.sh | sh || true
    fi
    systemctl enable --now tailscaled || true
    echo "  -> Tailscale installed! Run 'sudo tailscale up' to link your device."
fi

if ask_yes "[8/11] Install Cockpit web server HUD (port 9090) & Safe USB Auto-Mount?"; then
    apt-get install -y --no-install-recommends cockpit || true
    systemctl enable --now cockpit.socket || true
    install -m 0755 "${REPO_DIR}/scripts/auto_usb_mount.sh" /usr/local/bin/auto_usb_mount.sh
    install -m 0644 "${REPO_DIR}/udev/99-auto-usb-mount.rules" /etc/udev/rules.d/99-auto-usb-mount.rules
    echo "  -> Cockpit enabled on https://<server-ip>:9090 and safe USB auto-mount installed."
fi

if ask_yes "[9/11] Install 7-Slide Fullscreen Wall Dashboard & X11 Autostart?"; then
    install -m 0755 "${REPO_DIR}/dashboard/newdash25.py" "${TARGET_HOME}/newdash25.py"
    install -m 0755 "${REPO_DIR}/dashboard/start_dashboard.sh" "${TARGET_HOME}/start_dashboard.sh"
    if [ ! -f "${TARGET_HOME}/dashboard_config.json" ]; then
        cp "${REPO_DIR}/dashboard/dashboard_config.example.json" "${TARGET_HOME}/dashboard_config.json"
    fi
    mkdir -p "${TARGET_HOME}/.config/autostart"
    cp "${REPO_DIR}/dashboard/dashboard.desktop.example" "${TARGET_HOME}/.config/autostart/dashboard.desktop"
    if [ "$TARGET_USER" != "root" ]; then
        chown -R "${TARGET_USER}:${TARGET_USER}" \
            "${TARGET_HOME}/newdash25.py" \
            "${TARGET_HOME}/start_dashboard.sh" \
            "${TARGET_HOME}/dashboard_config.json" \
            "${TARGET_HOME}/.config/autostart"
    fi
    echo "  -> Dashboard installed at ${TARGET_HOME}/newdash25.py (edit ${TARGET_HOME}/dashboard_config.json)."
fi

if ask_yes "[10/11] Install Hardened Telegram Bot, OpenCV USB Security Cam & SSH/sudo Auth Alerts?"; then
    if ! id -u vickybot >/dev/null 2>&1; then
        useradd -r -s /usr/sbin/nologin -G video vickybot
    else
        usermod -aG video vickybot || true
    fi
    mkdir -p /var/lib/vickyhome-tgbot
    chown vickybot:vickybot /var/lib/vickyhome-tgbot
    chmod 0700 /var/lib/vickyhome-tgbot

    install -m 0755 "${REPO_DIR}/telegram-bot/vickyhome-telegram-bot.py" /usr/local/bin/vickyhome-telegram-bot.py
    install -m 0755 "${REPO_DIR}/telegram-bot/vickyhome-auth-alert.py" /usr/local/bin/vickyhome-auth-alert.py

    if [ ! -f /etc/vickyhome-tgbot.conf ]; then
        cp "${REPO_DIR}/config/vickyhome-tgbot.conf.example" /etc/vickyhome-tgbot.conf
        chown vickybot:vickybot /etc/vickyhome-tgbot.conf
        chmod 0400 /etc/vickyhome-tgbot.conf
    fi
    if [ ! -f /etc/vickyhome-alerts.conf ]; then
        cp "${REPO_DIR}/config/vickyhome-alerts.conf.example" /etc/vickyhome-alerts.conf
        chown root:root /etc/vickyhome-alerts.conf
        chmod 0640 /etc/vickyhome-alerts.conf
    fi

    install -m 0644 "${REPO_DIR}/udev/99-usb-webcam-power.rules" /etc/udev/rules.d/99-usb-webcam-power.rules
    udevadm control --reload-rules && udevadm trigger --subsystem-match=usb || true

    for unit in \
        vickyhome-telegram-bot.service \
        vickyhome-auth-alert.service \
        vickyhome-briefing.service \
        vickyhome-briefing.timer \
        vickyhome-flight-recap.service \
        vickyhome-flight-recap.timer; do
        install -m 0644 "${REPO_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"
    done
    systemctl daemon-reload
    systemctl enable \
        vickyhome-telegram-bot.service \
        vickyhome-auth-alert.service \
        vickyhome-briefing.timer \
        vickyhome-flight-recap.timer
    echo "  -> Telegram bot & auth alert services enabled."
    echo "     Add your bot token/chat ID to /etc/vickyhome-tgbot.conf & /etc/vickyhome-alerts.conf, then run:"
    echo "     sudo systemctl start vickyhome-telegram-bot vickyhome-auth-alert vickyhome-briefing.timer vickyhome-flight-recap.timer"
fi

if ask_yes "[11/11] Configure UFW Firewall rules (keeps SSH 22 open)?"; then
    ufw allow 22/tcp comment 'SSH'
    ufw allow 53/tcp comment 'AdGuard DNS TCP'
    ufw allow 53/udp comment 'AdGuard DNS UDP'
    ufw allow 3000/tcp comment 'AdGuard Initial Setup'
    ufw allow 8081/tcp comment 'AdGuard Home Admin UI'
    ufw allow 8080/tcp comment 'FileBrowser Web UI'
    ufw allow 3923/tcp comment 'Copyparty File Server'
    ufw allow 8765/tcp comment 'Wall Dashboard Phone Remote'
    ufw allow 9090/tcp comment 'Cockpit Server HUD'
    ufw allow samba comment 'Samba LAN File Share' || true
    ufw allow in on tailscale0 comment 'Tailscale Private VPN' || true
    ufw --force enable
    echo "  -> UFW firewall active."
fi

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "============================================================"
echo "  Installation Complete!"
echo "============================================================"
echo "  FileBrowser   : http://${SERVER_IP:-<server-ip>}:8080"
echo "  Copyparty     : http://${SERVER_IP:-<server-ip>}:3923  (creds: /etc/default/copyparty)"
echo "  AdGuard Setup : http://${SERVER_IP:-<server-ip>}:3000  (set Admin UI to :8081)"
echo "  Dashboard UI  : http://${SERVER_IP:-<server-ip>}:8765/?token=CHANGE_ME_TOKEN"
echo "  Cockpit HUD   : https://${SERVER_IP:-<server-ip>}:9090"
echo "  Samba Share   : \\\\${SERVER_IP:-<server-ip>}\\shared"
echo ""
echo "  Next Steps:"
echo "   1. Edit ${TARGET_HOME}/dashboard_config.json (set city, lat/lon, phone token, AdGuard pass)"
echo "   2. Edit /etc/vickyhome-tgbot.conf & /etc/vickyhome-alerts.conf (Telegram bot token + chat ID)"
echo "   3. Run 'sudo tailscale up' for remote access from your phone anywhere"
echo "   4. Start Telegram services & reboot or run '${TARGET_HOME}/start_dashboard.sh &'"
echo "============================================================"
