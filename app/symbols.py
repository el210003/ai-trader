"""Resolve the effective symbol list at runtime.

When `discover.enabled` is true in `config.yaml`, query MT5 and replace the
static `cfg["symbols"]` list with whatever is currently visible / tradeable
in Market Watch.  Otherwise (and in demo mode), use the static list as-is.

This module is the single place that decides which symbols the app trades.
Every CLI command and the dashboard server call `resolve_symbols(cfg)`.
"""
from typing import List, Optional

from .data import mt5_client
from .data.mt5_client import (
    discover_symbols, persist_discovered_symbols, load_discovered_symbols,
)


def resolve_symbols(cfg: dict, mt5=None, verbose: bool = True) -> List[str]:
    """Return the list of symbols to operate on.

    - If `cfg["discover"]["enabled"]` is True AND MT5 is reachable, query
      Market Watch, persist the list to `discover.cache_path`, and return it.
    - If discovery is enabled but MT5 isn't reachable, fall back to the
      cached list (`discover.cache_path`); if that's missing too, fall back
      to `cfg["symbols"]`.
    - If discovery is disabled, return `cfg["symbols"]` unchanged.
    """
    disc = cfg.get("discover", {}) or {}
    if not disc.get("enabled", False):
        return list(cfg.get("symbols", []))

    cached = load_discovered_symbols(disc.get("cache_path", ""))
    if mt5 is not None:
        try:
            discovered = discover_symbols(
                mt5,
                group=disc.get("group", "*"),
                only_tradeable=bool(disc.get("only_tradeable", True)),
                only_visible=bool(disc.get("only_visible", True)),
                max_count=int(disc.get("max_count", 200)),
            )
            names = [d["symbol"] for d in discovered]
            if names:
                persist_discovered_symbols(disc["cache_path"], discovered)
                if verbose:
                    print(f"discovered {len(names)} symbols from MT5 "
                          f"(group={disc.get('group','*')!r})")
                return names
        except Exception as e:
            if verbose:
                print(f"  [warn] MT5 symbol discovery failed: {e}")

    if cached:
        names = [d["symbol"] for d in cached]
        if verbose:
            print(f"using {len(names)} cached discovered symbols "
                  f"({disc.get('cache_path')})")
        return names

    if verbose:
        print("  [warn] no MT5 and no cache; falling back to config.yaml symbols")
    return list(cfg.get("symbols", []))
