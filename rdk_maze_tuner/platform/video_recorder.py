"""ffmpeg-backed Webots screen and real-camera JPEG recording."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Deque

from .database import Database
from .event_store import SAFE_RUN_ID


class VideoRecorderError(RuntimeError):
    """Raised when a recorder command or frame is invalid."""


class VideoBandwidthError(VideoRecorderError):
    """Raised when real-camera input exceeds the server limit."""


class FfmpegVideoRecorder:
    def __init__(
        self,
        *,
        output_path: Path,
        fps: int = 10,
        max_bits_per_second: int = 3_000_000,
        process_factory: Callable[..., Any] = subprocess.Popen,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not 1 <= int(fps) <= 60:
            raise ValueError("fps must be between 1 and 60")
        if max_bits_per_second <= 0:
            raise ValueError("max_bits_per_second must be positive")
        self.output_path = Path(output_path)
        self.fps = int(fps)
        self.max_bits_per_second = int(max_bits_per_second)
        self.process_factory = process_factory
        self.monotonic_ns = monotonic_ns
        self.process: Any = None
        self.mode: str | None = None
        self._frames: Deque[tuple[int, int]] = deque()
        self.frame_count = 0
        self.failure: str | None = None
        self.started_monotonic_ns: int | None = None
        self.ended_monotonic_ns: int | None = None

    def start_real(self) -> None:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-framerate",
            str(self.fps),
            "-i",
            "-",
            "-vf",
            "scale=640:360:force_original_aspect_ratio=decrease,"
            "pad=640:360:(ow-iw)/2:(oh-ih)/2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            str(self.max_bits_per_second),
            str(self.output_path),
        ]
        self.mode = "real"
        self._start(command, use_stdin=True)

    def start_simulation(
        self,
        *,
        display: str,
        geometry: str = "640x360",
    ) -> None:
        self.mode = "simulation"
        self._start(
            self.simulation_command(
                output_path=self.output_path,
                display=display,
                geometry=geometry,
                fps=self.fps,
            ),
            use_stdin=False,
        )

    @staticmethod
    def simulation_command(
        *,
        output_path: Path,
        display: str,
        geometry: str,
        fps: int,
    ) -> list[str]:
        if not display or not display.startswith(":"):
            raise ValueError("display must look like :99")
        if "x" not in geometry:
            raise ValueError("geometry must look like 640x360")
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "x11grab",
            "-video_size",
            geometry,
            "-framerate",
            str(int(fps)),
            "-i",
            display,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]

    def write_jpeg(
        self,
        frame: bytes,
        *,
        timestamp_ns: int | None = None,
    ) -> None:
        if self.mode != "real" or self.process is None:
            raise VideoRecorderError("real recorder is not running")
        if (
            not isinstance(frame, bytes)
            or len(frame) < 4
            or not frame.startswith(b"\xff\xd8")
            or not frame.endswith(b"\xff\xd9")
        ):
            raise VideoRecorderError("frame must be a complete JPEG")
        now_ns = self.monotonic_ns() if timestamp_ns is None else int(timestamp_ns)
        self._frames.append((now_ns, len(frame)))
        cutoff = now_ns - 1_000_000_000
        while self._frames and self._frames[0][0] < cutoff:
            self._frames.popleft()
        if sum(size for _, size in self._frames) * 8 > self.max_bits_per_second:
            self._frames.pop()
            raise VideoBandwidthError("JPEG stream exceeds 3 Mbps limit")
        try:
            self.process.stdin.write(frame)
            self.process.stdin.flush()
        except Exception as exc:
            self.failure = str(exc)
            raise VideoRecorderError("ffmpeg frame write failed") from exc
        self.frame_count += 1

    def stop(self, *, timeout_s: float = 10.0) -> dict[str, Any]:
        if self.process is None:
            return self._result(complete=False, reason="not_started")
        try:
            if self.mode == "real" and self.process.stdin is not None:
                self.process.stdin.close()
            return_code = self.process.wait(timeout=timeout_s)
        except Exception as exc:
            self.ended_monotonic_ns = self.monotonic_ns()
            self.failure = str(exc)
            try:
                self.process.terminate()
            except Exception:
                pass
            return self._result(complete=False, reason="ffmpeg_stop_failed")
        self.ended_monotonic_ns = self.monotonic_ns()
        complete = (
            return_code == 0
            and self.output_path.is_file()
            and self.output_path.stat().st_size > 0
        )
        return self._result(
            complete=complete,
            reason=None if complete else "ffmpeg_output_incomplete",
        )

    def _start(self, command: list[str], *, use_stdin: bool) -> None:
        if self.process is not None:
            raise VideoRecorderError("recorder is already running")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.process = self.process_factory(
                command,
                stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.started_monotonic_ns = self.monotonic_ns()
        except Exception as exc:
            self.failure = str(exc)
            raise VideoRecorderError("cannot start ffmpeg") from exc

    def _result(
        self,
        *,
        complete: bool,
        reason: str | None,
    ) -> dict[str, Any]:
        return {
            "complete": complete,
            "mode": self.mode,
            "path": str(self.output_path),
            "frame_count": self.frame_count,
            "reason": reason,
            "failure": self.failure,
            "started_monotonic_ns": self.started_monotonic_ns,
            "ended_monotonic_ns": self.ended_monotonic_ns,
        }


class VideoArtifactRegistry:
    def __init__(
        self,
        *,
        database: Database,
        data_dir: Path,
        utc_now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self.data_dir = Path(data_dir)
        self.utc_now = utc_now or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def register(
        self,
        *,
        run_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if SAFE_RUN_ID.fullmatch(str(run_id)) is None:
            raise ValueError("run_id contains unsafe characters")
        path = Path(str(result.get("path") or "")).resolve()
        root = self.data_dir.resolve()
        if not path.is_relative_to(root):
            raise ValueError("video path escapes data directory")
        relative = path.relative_to(root).as_posix()
        now = self.utc_now()
        metadata = {
            "status": (
                "complete" if result.get("complete") else "incomplete"
            ),
            "mode": result.get("mode"),
            "frame_count": result.get("frame_count", 0),
            "reason": result.get("reason"),
            "started_monotonic_ns": result.get("started_monotonic_ns"),
            "ended_monotonic_ns": result.get("ended_monotonic_ns"),
        }
        sha256 = (
            _file_digest(path)
            if result.get("complete") and path.is_file()
            else None
        )
        artifact_id = str(self.id_factory())
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, run_id, kind, relative_path, sha256, metadata_json,
                    retained_until_utc, pinned, created_at_utc
                ) VALUES (?, ?, 'video', ?, ?, ?, ?, 0, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    sha256 = excluded.sha256,
                    metadata_json = excluded.metadata_json,
                    retained_until_utc = excluded.retained_until_utc
                """,
                (
                    artifact_id,
                    run_id,
                    relative,
                    sha256,
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    _utc_text(now + timedelta(days=30)),
                    _utc_text(now),
                ),
            )
        return {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "kind": "video",
            "relative_path": relative,
            "sha256": sha256,
            "metadata": metadata,
        }


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
