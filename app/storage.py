import os
import subprocess
from pathlib import Path
from typing import Optional

# Bytes per second for both cameras at 320x240 YUY2 @ 15fps
# 320 * 240 * 2 bytes/pixel * 15fps * 2 cameras
BYTES_PER_SECOND = 320 * 240 * 2 * 15 * 2


def _mount_ssd(device: str, mountpoint: Path) -> bool:
    """Attempt to mount an exFAT device. Returns True on success."""
    mountpoint.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["mount", "-t", "exfat", device, str(mountpoint)],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[storage] Mount failed: {e}")
        return False


def find_data_dir() -> Optional[Path]:
    """
    Scan /media and /mnt for a mounted USB drive.
    Returns the path to the stellar-recorder subdirectory, creating it if needed.
    Falls back to attempting to mount /dev/sda2 if nothing is found.
    """
    search_bases = [Path("/media"), Path("/mnt")]

    for base in search_bases:
        if not base.exists():
            continue
        try:
            candidates = sorted(base.iterdir())
        except PermissionError:
            continue
        for candidate in candidates:
            # Skip known non-USB mounts
            if candidate.name in ("host", "boot", "rootfs"):
                continue
            try:
                if candidate.is_mount():
                    data_dir = candidate / "stellar-recorder"
                    data_dir.mkdir(parents=True, exist_ok=True)
                    return data_dir
            except PermissionError:
                continue

    # Fallback: try mounting the known SSD partition
    print("[storage] No mounted USB drive found — attempting to mount /dev/sda2")
    mountpoint = Path("/media/ssd")
    if _mount_ssd("/dev/sda2", mountpoint):
        print(f"[storage] Mounted /dev/sda2 at {mountpoint}")
        data_dir = mountpoint / "stellar-recorder"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    print("[storage] Could not find or mount SSD — recording unavailable")
    return None


def free_space_gb(path: Path) -> float:
    """Return free space in GB at the given path."""
    try:
        stat = os.statvfs(path)
        return (stat.f_bavail * stat.f_frsize) / 1e9
    except OSError:
        return 0.0


def estimated_minutes_remaining(free_gb: float) -> float:
    """Estimate recording minutes remaining based on free space and stream config."""
    if BYTES_PER_SECOND == 0:
        return 0.0
    return (free_gb * 1e9) / (BYTES_PER_SECOND * 60)


def list_sessions(data_dir: Path) -> list[dict]:
    """Return metadata for all completed sessions on the SSD."""
    sessions_dir = data_dir / "sessions"
    if not sessions_dir.exists():
        return []

    results = []
    for session_path in sorted(sessions_dir.iterdir(), reverse=True):
        if not session_path.is_dir():
            continue
        session_json = session_path / "session.json"
        entry = {"session_id": session_path.name, "files": []}

        # Collect file sizes
        total_bytes = 0
        for f in session_path.iterdir():
            size = f.stat().st_size
            total_bytes += size
            entry["files"].append({"name": f.name, "size_mb": round(size / 1e6, 1)})

        entry["total_size_mb"] = round(total_bytes / 1e6, 1)

        if session_json.exists():
            import json
            try:
                with open(session_json) as fh:
                    entry.update(json.load(fh))
            except Exception:
                pass

        results.append(entry)

    return results
