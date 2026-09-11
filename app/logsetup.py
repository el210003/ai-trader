"""Process logging: tees ALL console output (every print() from the pipeline,
server, watcher, resolver...) into a timestamped, size-rotated log file, and
provides debug() for progress lines.

Enabled by default (dashboard.debug: true, dashboard.log_file: logs/ai-trader.log)
so you can always see what a long analysis cycle is actually doing.
"""
import sys
import time
from pathlib import Path

_active = False
_log_path = None


class _Tee:
    """Duplicate a stream (stdout/stderr) into the log file, prefixing each
    line with a timestamp."""
    def __init__(self, stream, path: Path, max_bytes: int = 5_000_000, backups: int = 3):
        self.stream = stream
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._buf = ""

    def _rotate_if_needed(self):
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                for i in range(self.backups - 1, 0, -1):
                    src = self.path.with_suffix(self.path.suffix + f".{i}")
                    dst = self.path.with_suffix(self.path.suffix + f".{i+1}")
                    if src.exists():
                        src.replace(dst)
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            pass

    def write(self, s):
        try:
            self.stream.write(s)
            self.stream.flush()
        except Exception:
            pass
        self._buf += s
        lines = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            lines.append(line)
        if lines:
            try:
                self._rotate_if_needed()
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                    for line in lines:
                        f.write(f"{ts} | {line}\n")
            except OSError:
                pass
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass

    # tolerate libraries poking at stream attributes
    def __getattr__(self, name):
        return getattr(self.stream, name)


def setup_process_logging(cfg: dict) -> bool:
    """Install the stdout/stderr tee. Returns True when logging is active."""
    global _active, _log_path
    if _active:
        return True
    d = cfg.get("dashboard", {})
    path = d.get("log_file", "logs/ai-trader.log")
    if not path:
        return False
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = _Tee(sys.stdout, p)
    sys.stderr = _Tee(sys.stderr, p)
    _active = True
    _log_path = str(p)
    print(f"[logging] console output is mirrored to {p}")
    return True


def debug_enabled(cfg: dict) -> bool:
    return bool(cfg.get("dashboard", {}).get("debug", True))


def debug(msg: str):
    """Progress line — always visible while dashboard.debug is on (default)."""
    print(f"[debug] {msg}", flush=True)
