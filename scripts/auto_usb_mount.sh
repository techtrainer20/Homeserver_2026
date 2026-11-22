#!/bin/bash
# Safe, non-destructive USB storage auto-mount for FileBrowser / Copyparty / Samba.
# Mounts an existing formatted USB partition read-write at /media/usbdrive and links it into /home/shared/USB-Drive.
# Never formats or modifies partition tables.

set -euo pipefail

MOUNT_DIR="/media/usbdrive"
LINK_DIR="/home/shared/USB-Drive"
LOG_FILE="/var/log/usb_auto_mount.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG_FILE"
}

DEV_NAME="${1:-}"
if [ -n "$DEV_NAME" ]; then
    PART="/dev/${DEV_NAME}"
else
    # Pick first unmounted removable partition with a recognized filesystem
    PART=$(lsblk -rno NAME,RM,TYPE,FSTYPE,MOUNTPOINT | awk '!f && $2=="1" && $3=="part" && $4!="" && $5=="" {print "/dev/" $1; f=1}')
fi

if [ -z "${PART:-}" ] || [ ! -b "$PART" ]; then
    log "No unmounted formatted USB partition found."
    exit 0
fi

FSTYPE=$(blkid -s TYPE -o value "$PART" 2>/dev/null || true)
if [ -z "$FSTYPE" ]; then
    log "Skipping $PART (no recognized filesystem; refusing to format)."
    exit 0
fi

mkdir -p "$MOUNT_DIR" /home/shared

if mountpoint -q "$MOUNT_DIR"; then
    log "$MOUNT_DIR is already mounted; skipping $PART."
    exit 0
fi

log "Mounting $PART ($FSTYPE) to $MOUNT_DIR..."
case "$FSTYPE" in
    vfat|exfat|ntfs)
        mount -o rw,umask=000 "$PART" "$MOUNT_DIR"
        ;;
    *)
        mount -o rw "$PART" "$MOUNT_DIR"
        ;;
esac

ln -sfn "$MOUNT_DIR" "$LINK_DIR"
log "Mounted $PART at $MOUNT_DIR and linked $LINK_DIR."
