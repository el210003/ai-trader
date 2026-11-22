"""Persistent per-symbol enable/disable selection.

Stored in `data/symbol_selection.json` so it survives server restarts.
When the file is missing, all symbols are enabled by default.

Used by:
- the dashboard (`/api/symbols/selection` GET/POST, and as a filter on the
  symbol dropdown)
- the pipeline (`enabled_list` is applied before ingest/analyze)
"""
import json
import threading
from pathlib import Path
from typing import Optional


class SymbolSelection:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._enabled: set = set()
        self._loaded = False
        self._mtime: Optional[float] = None
        with self._lock:
            self._load()

    # --------------------------------------------------------------- I/O
    def _stat_mtime(self) -> Optional[float]:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return None

    def _load(self):
        mtime = self._stat_mtime()
        if mtime is not None:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._enabled = set(data.get("enabled", []))
            except Exception:
                self._enabled = set()
        else:
            self._enabled = set()
        self._mtime = mtime
        self._loaded = True

    def _ensure_fresh(self):
        if not self._loaded or self._stat_mtime() != self._mtime:
            self._load()

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"enabled": sorted(self._enabled)}, f, indent=2)
        self._mtime = self._stat_mtime()
        self._loaded = True

    # -------------------------------------------------------------- public
    def is_enabled(self, symbol: str) -> bool:
        """If no selection file exists yet, all symbols are enabled.
        If a file exists, only the symbols in `enabled` are enabled."""
        with self._lock:
            self._ensure_fresh()
            if not self._enabled:
                # No explicit selection -> default policy: enable everything
                return True
            return symbol in self._enabled

    def enabled_list(self, all_symbols: list) -> list:
        """Return the subset of `all_symbols` that is currently enabled."""
        with self._lock:
            self._ensure_fresh()
            if not self._enabled:
                return list(all_symbols)
            return [s for s in all_symbols if s in self._enabled]

    def get(self) -> list:
        with self._lock:
            self._ensure_fresh()
            return sorted(self._enabled)

    def set(self, symbols: list):
        with self._lock:
            self._enabled = set(s for s in symbols if isinstance(s, str))
            self._save()

    def enable_all(self, all_symbols: list):
        with self._lock:
            self._enabled = set(all_symbols)
            self._save()

    def clear(self):
        with self._lock:
            self._enabled = set()
            self._save()
