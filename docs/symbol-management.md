# Symbol Management

This document explains how the app decides **which symbols** to ingest,
analyze, train on, and display on the dashboard — across all three layers:
config-file discovery, the persistent enable/disable selection, and the
dashboard UI filter + settings modal.

---

## Three layers, in order

```
config.yaml symbols       (the static list)
       │
       ▼
[ --all-symbols ]  ────►  MT5 discovery (group / visibility / tradeability)
                            │
                            ▼
                       cached list  ─►  data/discovered_symbols.json
                            │
                            ▼
                  per-symbol enable/disable  ─►  data/symbol_selection.json
                            │
                            ▼
                       CLI / pipeline operates on this filtered list
                            │
                            ▼
                   dashboard dropdown + settings modal
```

Every layer is independent and composable. You can:

- Use only the static list (default)
- Use MT5 discovery but skip the selection (everything in Market Watch)
- Use MT5 discovery + a saved selection (subset you actively trade)
- Use the static list + a selection (override individual symbols)

---

## Layer 1 — Static list (`config.yaml`)

```yaml
symbols:
  - EURUSD
  - GBPUSD
  - USDJPY
  - AUDUSD
  - USDCHF
  - USDCAD
  - NZDUSD

timeframes:
  - M15
  - H1
  - H4
```

Used when `discover.enabled` is **false** (the default).

---

## Layer 2 — MT5 discovery

When `discover.enabled` is **true** *or* you pass `--all-symbols` on a command,
the app queries MT5 instead of using the static list.

Entry point: `app/data/mt5_client.py::discover_symbols`.

### Filters, in order

1. **Group pattern** — `mt5.symbols_get(group=...)`. Examples:

   | Pattern | Effect |
   |---|---|
   | `"*"` | Every symbol MT5 knows about |
   | `"*USD*"` | Symbols containing "USD" |
   | `"Forex\\*"` | Anything under a `Forex` path |
   | `"Metals*"` | Gold, silver, etc. |
   | `"*EUR*,*GBP*,*JPY*"` | Multiple patterns (comma-separated) |

2. **`only_visible`** — drops symbols not currently shown in Market Watch.
   In MT5: right-click Market Watch → **Symbols** → choose what to display.

3. **`only_tradeable`** — drops symbols with `trade_mode == SYMBOL_TRADE_MODE_DISABLED`
   (0). Disabled symbols can't be traded and have no actionable data.

4. **`max_count`** — safety cap. Brokers can expose 5,000+ instruments; this
   keeps the pipeline tractable. Default `200`.

### Sort order

Symbols are returned in this order:

1. **Majors first** — EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD,
   XAUUSD, XAGUSD (anything in the priority list).
2. **Everything else** — alphabetical.

### Persistence

The discovered list is written to `data/discovered_symbols.json` so the
dashboard can populate its dropdown without MT5 being open:

```json
{
  "symbols": [
    { "symbol": "EURUSD", "path": "Forex\\Majors", "description": "Euro vs US Dollar",
      "trade_mode": 4, "visible": true },
    ...
  ],
  "count": 4
}
```

If MT5 isn't reachable at startup (e.g., dashboard running on a server),
`resolve_symbols()` falls back to this cached file before falling back to the
static config list.

Entry point: `app/symbols.py::resolve_symbols`.

---

## Layer 3 — Per-symbol enable/disable selection

Once the symbol *candidates* are known, the app applies a persistent
enable/disable list.

Entry point: `app/symbol_selection.py::SymbolSelection`.

### File format

`data/symbol_selection.json`:

```json
{
  "enabled": ["EURUSD", "GBPUSD", "XAUUSD"]
}
```

| State | Meaning |
|---|---|
| File missing or `"enabled": []` | **Default policy: enable everything.** |
| File present with names | Only those symbols pass through. |

### Default-policy semantics

`clear()` doesn't *disable* everything — it falls back to "enable everything".
This is intentional: a fresh install doesn't require a long bootstrap.

```python
def is_enabled(self, symbol: str) -> bool:
    if not self._enabled:            # empty list / file missing
        return True                  # default: enable all
    return symbol in self._enabled
```

### API

```python
sel = SymbolSelection("data/symbol_selection.json")

sel.is_enabled("EURUSD")                # True (default policy)
sel.is_enabled("EXOTIC")                # True (default policy)

sel.set(["EURUSD", "GBPUSD"])           # explicit selection
sel.is_enabled("EURUSD")                # True
sel.is_enabled("GBPUSD")                # True
sel.is_enabled("EXOTIC")                # False

sel.enable_all(["A","B","C"])           # explicit "enable every known"
sel.enabled_list(["A","B","C","D"])     # ["A","B","C"] — D is unknown

sel.clear()                             # back to default policy (enable all)
sel.is_enabled("EURUSD")                # True
```

### CLI

```bat
:: Print current selection (or note that none exists)
python -m app.main select-symbols

:: Add symbols to the enabled set
python -m app.main select-symbols --enable EURUSD GBPUSD XAUUSD

:: Remove symbols
python -m app.main select-symbols --disable EURUSD

:: Enable every resolved symbol (explicit)
python -m app.main select-symbols --all

:: Clear the selection (back to "enable all" default)
python -m app.main select-symbols --none
```

### Dashboard API

| Endpoint | Purpose |
|---|---|
| `GET /api/symbols/all` | Every known symbol with path, description, selection status, and last-analyzed timestamp. Drives the settings modal. |
| `GET /api/symbols/selection` | Current `enabled` list and `has_explicit_selection` flag. |
| `POST /api/symbols/selection` | Body: `{"enabled": [...]}`. Atomic write. |
| `POST /api/symbols/select-all` | Enable every known symbol. |
| `POST /api/symbols/clear` | Clear the selection (default policy). |

### Where it's applied

Every CLI command (`ingest`, `analyze`, `train`, `run`) and the dashboard
server filter through the selection before doing anything. Disabled symbols
are simply skipped — no error, no warning.

```python
def _symbols_for(cfg, args, mt5):
    if getattr(args, "all_symbols", False):
        cfg.setdefault("discover", {})["enabled"] = True
    syms = resolve_symbols(cfg, mt5=mt5, verbose=True)
    selection = SymbolSelection("data/symbol_selection.json")
    return selection.enabled_list(syms)
```

The dashboard's `/api/refresh` background job uses the *filtered* list, so a
"Run analysis" button never burns CPU on disabled pairs.

---

## Dashboard UI

Two pieces of UI in the header:

### Filter input

Live text filter on the symbol dropdown. Substring match, case-insensitive.
Shows `visible/total` count next to the input.

```
[ EUR filter… ] ×  2/47  [EURUSD ▼] [M15 ▼] [⚙] ...
```

Filtering to "EUR" leaves EURUSD visible; selecting another symbol re-loads
its analysis automatically.

### Settings modal (⚙ button)

Lists every known symbol with checkboxes, paths, and last-analyzed
timestamps:

```
┌─ Symbol list ───────────────────────[×]┐
│ [filter…] [×]                         │
│                                        │
│ ☑ EURUSD   Forex\Major  14:32         │
│ ☑ GBPUSD   Forex\Major  14:32         │
│ ☐ USDJPY   Forex\Major  —             │
│ ☑ XAUUSD   Metals       14:32         │
│ ☐ EXOTIC1  Exotics      —             │
│                                        │
│ [Select all] [Clear] [Invert]  2/5    │
│                              enabled   [Save] │
└────────────────────────────────────────┘
```

- **Save** writes the selection to `data/symbol_selection.json` and reloads
  the page state.
- **Cancel** (close modal) discards unsaved changes.
- **Invert** flips only the currently-visible symbols (respects the filter).
- **Select all / Clear** apply to the visible filter scope, not the whole
  list, so you can quickly enable just the "USD*" symbols, for example.

After saving, the symbol dropdown updates to show only the enabled list.

---

## Common workflows

### "I just want to trade the majors"

```bat
python -m app.main ingest
python -m app.main select-symbols --all    :: start with everything
python -m app.main select-symbols --disable AUDUSD USDCAD NZDUSD USDCHF XAUUSD ...
python -m app.main analyze
python -m app.main serve
```

Or use the ⚙ modal: filter "USD", invert, save.

### "I want every FX pair my broker offers"

```yaml
# config.yaml
discover:
  enabled: true
  group: "Forex\\*"
  only_tradeable: true
  only_visible: true
```

```bat
python -m app.main list-symbols --all-symbols    :: preview
python -m app.main ingest --all-symbols
python -m app.main analyze --all-symbols
```

### "Auto-trade a fixed basket, ignore everything else"

```bat
python -m app.main select-symbols --enable EURUSD GBPUSD USDJPY XAUUSD
python -m app.main run --interval 300
```

The `run` loop applies the selection on every iteration, so even if MT5
shows 100 symbols, only your basket gets ingested and analyzed.

### "Temporarily disable a pair during news"

```bat
python -m app.main select-symbols --disable EURUSD
python -m app.main analyze              :: EURUSD skipped
python -m app.main select-symbols --enable EURUSD
```

---

## Implementation map

| File | Role |
|---|---|
| `app/data/mt5_client.py::discover_symbols` | Query MT5 for symbols with filters |
| `app/data/mt5_client.py::persist_discovered_symbols` | Save list to JSON |
| `app/data/mt5_client.py::load_discovered_symbols` | Load JSON cache |
| `app/symbols.py::resolve_symbols` | Compose config + discovery + cache → final list |
| `app/symbol_selection.py::SymbolSelection` | Persistent enable/disable with default policy |
| `app/main.py::_symbols_for` | CLI helper: resolve + filter |
| `app/main.py::cmd_select_symbols` | CLI command for managing selection |
| `app/dashboard/server.py` | `/api/symbols/*` endpoints + selection filtering |
| `app/dashboard/static/index.html` | Filter input + settings modal |

---

## Possible improvements

- **Symbol groups in config** — declare named groups (`majors`, `metals`,
  `exotics`) and select which group is active.
- **Session-aware selection** — auto-enable only the symbols liquid during
  the current session (London / NY / Asian).
- **Per-symbol risk limits** — extend the selection with a `risk_pct` field
  per symbol for portfolio-level position sizing.
- **Volatility-tiered selection** — auto-disable symbols whose ATR percentile
  drops below a threshold (dead pairs).
- **Import / export** — save and load selection files from disk so you can
  share a basket across machines.
