import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"


@dataclass
class Session:
    session_id: str
    session_dir: Path
    trigger: str          # "mavlink" | "manual"
    start_tai_us: int
    stop_tai_us: Optional[int] = None
    duration_s: Optional[float] = None

    @property
    def cam0_avi(self) -> Path:
        return self.session_dir / "cam0.avi"

    @property
    def cam1_avi(self) -> Path:
        return self.session_dir / "cam1.avi"

    @property
    def cam0_csv(self) -> Path:
        return self.session_dir / "cam0_frames.csv"

    @property
    def cam1_csv(self) -> Path:
        return self.session_dir / "cam1_frames.csv"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "trigger": self.trigger,
            "start_tai_us": self.start_tai_us,
            "stop_tai_us": self.stop_tai_us,
            "duration_s": self.duration_s,
            "cam0_avi": str(self.cam0_avi),
            "cam1_avi": str(self.cam1_avi),
            "cam0_csv": str(self.cam0_csv),
            "cam1_csv": str(self.cam1_csv),
        }

    def write_json(self):
        with open(self.session_dir / "session.json", "w") as f:
            json.dump(self.to_dict(), f, indent=2)


class SessionManager:
    def __init__(self):
        self.state = State.IDLE
        self.current: Optional[Session] = None
        self._start_monotonic: Optional[float] = None

    def start(self, data_dir: Path, trigger: str) -> Session:
        if self.state == State.RECORDING:
            raise RuntimeError("Already recording")

        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        session_dir = data_dir / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        tai_us = int(time.clock_gettime(time.CLOCK_TAI) * 1e6)

        self.current = Session(
            session_id=session_id,
            session_dir=session_dir,
            trigger=trigger,
            start_tai_us=tai_us,
        )
        self._start_monotonic = time.monotonic()

        # Write immediately so power loss leaves recoverable metadata
        self.current.write_json()
        self.state = State.RECORDING
        return self.current

    def stop(self) -> Optional[Session]:
        if self.state == State.IDLE or self.current is None:
            return None

        tai_us = int(time.clock_gettime(time.CLOCK_TAI) * 1e6)
        self.current.stop_tai_us = tai_us
        self.current.duration_s = round(time.monotonic() - self._start_monotonic, 2)
        self.current.write_json()

        finished = self.current
        self.current = None
        self._start_monotonic = None
        self.state = State.IDLE
        return finished

    def elapsed_s(self) -> Optional[float]:
        if self._start_monotonic is None:
            return None
        return round(time.monotonic() - self._start_monotonic, 1)
