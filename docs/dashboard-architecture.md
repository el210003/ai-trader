# Dashboard Architecture

This document explains how the HTML dashboard is built, what each piece
does, and the data flow from the server into the chart and setup cards.

Entry points:
- Server: `app/dashboard/server.py::create_app(cfg)`
- UI: `app/dashboard/static/index.html` (vanilla JS, single file, no build step)

---

## Goals

1. **Zero build step** — open `index.html` and it runs. No webpack, no
   TypeScript compile, no node_modules. Just edit and refresh.
2. **Single source of truth** — the server holds the SQLite store; the UI
   reads from `/api/*` and never reaches into the DB.
3. **Live updates** — "Run analysis" triggers a background thread, the UI
   polls `/api/status`, then reloads.
4. **Performant overlays** — chart zones (OB/FVG/PD) drawn as absolutely
   positioned `<div>`s, recomputed on every visible range change.

---

## Layered architecture

```
┌──────────────────────────────────────────────────────────┐
│ Browser (index.html — vanilla JS + lightweight-charts)   │
│                                                          │
│   State (analysis JSON + chart refs)                     │
│   Renderers: chart, boxes, structure card, setup cards   │
│   Pollers: loadMeta, loadAnalysis, pollStatus            │
└──────────────────────────────────────────────────────────┘
                          ▲   ▲
                          │   │  fetch() / POST
                          │   ▼
┌──────────────────────────────────────────────────────────┐
│ FastAPI server (server.py)                               │
│                                                          │
│   /api/meta         → symbol/tf list + model health      │
│   /api/analysis     → candles + analysis JSON            │
│   /api/refresh      → start background pipeline          │
│   /api/retrain      → retrain ML model in background     │
│   /api/setups/history → journaled setups + stats         │
│   /api/symbols/*    → all symbols + selection (GET/POST) │
│   /api/status       → running? retraining? last_run? err?│
│   /                 → static index.html                 │
│   /static/*         → static assets                      │
└──────────────────────────────────────────────────────────┘
                          ▲
                          │  load_candles / save_analysis
                          │
┌──────────────────────────────────────────────────────────┐
│ SQLite store (Store)                                     │
│   candles(symbol, tf, time, OHLCV)                       │
│   analysis(symbol, tf, payload JSON)                      │
└──────────────────────────────────────────────────────────┘
                          ▲
                          │  read / write
                          │
┌──────────────────────────────────────────────────────────┐
│ Pipeline (run_all, analyze_symbol)                       │
│   SMC engine → setups → ML → LLM → hybrid               │
└──────────────────────────────────────────────────────────┘
```

---

## Server endpoints

### `GET /api/meta`

Returns the configuration the UI needs:

```json
{
  "symbols": ["EURUSD", "GBPUSD", "USDJPY"],
  "timeframes": ["M15", "H1"],
  "refresh_seconds": 60,
  "model_loaded": true,
  "model_age_days": 2.3,
  "model_stale": false,
  "model_env": {"numpy": "2.2.6", "sklearn": "1.7.2", "python": "3.10.10"},
  "runtime_numpy": "2.2.6",
  "env_mismatch": false,
  "auto_retrain": {"enabled": true, "max_age_days": 7, "on_refresh": "warn"},
  "model_metrics": {"n_samples": 1999, "cv_auc": 0.575, ...},
  "llm_enabled": false,
  "llm_model": "gpt-4o-mini",
  "analyses": [{"symbol": "EURUSD", "tf": "M15", "updated_at": 1234567890}, ...]
}
```

Used to populate the symbol/timeframe dropdowns, the ML/LLM status badges,
the model-age display, and the Retrain button visibility.

### Symbol endpoints (`/api/symbols/*`)

| Endpoint | Purpose |
|---|---|
| `GET /api/symbols/all` | Every known symbol with path, description, selection status, and last-analyzed timestamp. Drives the ⚙ settings modal. |
| `GET /api/symbols/selection` | Current `enabled` list and `has_explicit_selection` flag. |
| `POST /api/symbols/selection` | Body: `{"enabled": [...]}`. Atomic write to `data/symbol_selection.json`. |
| `POST /api/symbols/select-all` | Enable every known symbol. |
| `POST /api/symbols/clear` | Clear the selection (default policy: enable all). |

See [`symbol-management.md`](symbol-management.md) for the full semantics.

### `GET /api/analysis?symbol=&tf=&bars=400`

The hot endpoint. Returns two things:

```json
{
  "analysis": {
    "symbol": "EURUSD",
    "tf": "H1",
    "generated_at": 1234567890,
    "smc": {
      "trend": "bullish",
      "events": [...],
      "sweeps": [...],
      "liquidity_pools": [...],
      "dealing_range": {top, bottom, equilibrium, position, zone, ...},
      "order_blocks": {all: [...], fresh: [...]},
      "fvgs": {all: [...], fresh: [...]},
      "atr": 0.0012,
      "last_close": 1.11188
    },
    "setups": [
      {
        "direction": "long", "verdict": "WAIT",
        "entry", "stop_loss", "take_profit", "rr",
        "entry_zone": {...}, "range_position", "confluences",
        "ml_prob", "llm", "final_score", "aligned"
      },
      ...
    ]
  },
  "candles": [{"time": ..., "open": ..., "high": ..., "low": ..., "close": ...}, ...]
}
```

If no analysis exists in the store, the server **runs the pipeline
synchronously** for that `(symbol, tf)` so the UI always gets fresh data.
This is what makes the "load on selection" experience feel instant.

The `bars` parameter (default 400) controls how many candles come back.
Limited for performance — older data is available in SQLite but doesn't need
to be in the browser.

### `POST /api/retrain`

Manual retrain trigger (the dashboard **↻ Retrain** button). Spawns a
background thread running `pipeline.train`; sets `state["retraining"]`
while running and `state["last_retrain"]` on completion. A typical retrain
takes ~90s for 3 symbols × 2 timeframes (walk-forward labeling dominates).
Note: in `auto` mode the refresh job also retrains automatically when the
model is stale — the button remains useful to force an out-of-band retrain.

### `POST /api/refresh?demo=true|false`

Triggers a background thread that runs `pipeline.run_all`:

```python
@app.post("/api/refresh")
def refresh(demo: bool = False):
    with lock:
        if state["running"] or state["retraining"]:
            return {"ok": False, "message": "a job is already running"}
        state["running"] = True
        state["demo_mode"] = demo
        threading.Thread(target=_refresh_job, args=(demo,), daemon=True).start()
    return {"ok": True, "message": "analysis started"}
```

The lock prevents refresh and retrain jobs from running concurrently. In
`auto` mode the job starts with `pipeline.retrain_if_stale(...)` — a stale
model is retrained before the ingest + analyze cycle.

The worker:

```python
def _refresh_job(demo: bool):
    try:
        t0 = time.time()
        n = pipeline.run_all(cfg, store=store, demo=demo, ingest=True, verbose=False)
        state["last_run"] = {"at": int(time.time()), "analyses": n,
                             "seconds": round(time.time() - t0, 1)}
        state["last_error"] = None
    except Exception as e:
        state["last_error"] = f"{e}\n{traceback.format_exc(limit=3)}"
    finally:
        state["running"] = False
```

Failures are captured, not raised — the UI sees them via `/api/status`.

### `GET /api/status`

```json
{"running": false, "retraining": false, "last_run": {...},
 "last_retrain": null, "last_error": null, "demo_mode": false}
```

The UI polls this every 1.2s while a refresh or retrain is in progress.

### `GET /api/setups/history?symbol=&tf=&limit=200`

Returns the journaled setups (forward-validation data) plus aggregate
stats:

```json
{
  "rows": [{"symbol": "EURUSD", "tf": "H1", "formed_at": ..., "direction": "long",
            "verdict": "BUY", "score": 68.2, "ml_prob": 0.64, "rr": 2.1,
            "entry": 1.08406, "stop_loss": 1.08310, "take_profit": 1.08667,
            "payload": "{confluences...}"}, ...],
  "stats": {"total": 142, "by_verdict": {"BUY": 12, "WAIT": 128},
            "by_direction": {"long": 70, "short": 72}}
}
```

Rows are deduped on (symbol, tf, formed_at, direction) so repeated analyze
runs on the same bars don't duplicate. The CLI `python -m app.main history`
prints the same data. See [`validation-guide.md`](validation-guide.md)
Stage 5 for how to use it.

### `GET /`

Serves `index.html`.

### `/static/*`

Mounts the `app/dashboard/static/` directory. Currently only `index.html`
lives there, but it's there for future assets (icons, custom fonts).

---

## UI structure

```
┌─ header ─────────────────────────────────────────────────────────────┐
│ brand [UI v3.0] · [filter…] [symbol ▼] [tf ▼] [⚙] [auto] [Run]       │
│        [ML badge+age] [↻ Retrain] [LLM badge] [● status]             │
├───────────────────────────────────────────────────────────────────────┤
│                            │  sidebar (400px)                        │
│  chart area                │  ┌ TRADE SETUPS · LIVE · tabs ────────┐ │
│  ┌───────────────────┐     │  │ [Setups n] [Journal n]             │ │
│  │ legend toggles    │     │  ├────────────────────────────────────┤ │
│  │                   │     │  │ Market Structure card              │ │
│  │  candles          │     │  │  trend pill · dealing-range bar    │ │
│  │  markers          │     │  │  ──marker──EQ──                    │ │
│  │  price lines      │     │  ├────────────────────────────────────┤ │
│  │  OB/FVG boxes     │     │  │ Setup cards (per verdict):         │ │
│  │  PD bands + tags  │     │  │  accent strip · dir icon · RR pill │ │
│  │  EQ line          │     │  │  verdict banner · SVG score ring   │ │
│  └───────────────────┘     │  │  trade ladder (risk/reward zones)  │ │
│                            │  │  price boxes · ML bar · chips      │ │
│                            │  │  AI ANALYST card (MiniMax/etc.)    │ │
│                            │  ├────────────────────────────────────┤ │
│                            │  │ Journal tab: history + ML density  │ │
├───────────────────────────────────────────────────────────────────────┤
│  footer: disclaimer                                                   │
└───────────────────────────────────────────────────────────────────────┘
```

### Freshness stamp

Beside the BAR-CLOSE/AUTO badge: `pull 14:30:05 · analyzed 14:30:06` — the
last time bars were pulled from MT5 and the last time the SMC+ML+LLM
analysis ran (time-only when today, date+time otherwise). Sourced from
`/api/status` (`last_pull` / `last_analysis`), updates live during cycles
and on page load.

### Sidebar design notes (v3.0 redesign, ranked tab added in v3.1)

- **Tabs** — *Setups* (live cards for the selected pair), *Ranked* (all current
  setups across every analyzed pair, sorted by hybrid score — click a row to
  open that chart; lazy-loads `/api/setups/ranked`), and *Journal* (lazy-loads
  `/api/setups/history`: recent setups with verdict dots + signal-density-by-
  ML-bucket chart).
- **Verdict banners** — gradient + glow (BUY pulses); setup top strip colored by
  verdict; **SVG score ring** animated to the hybrid score.
- **Trade ladder** — horizontal SL→entry→TP map: red risk / green reward zones
  sized by true proportions, glowing ticks, mono price labels. Mirrored for shorts.
- **AI ANALYST card** — violet theme, model name, bias/conviction line with
  agrees/contradicts state, narrative, INVALIDATION strip, concerns list.
  Rendered only when the LLM returned a response.
- **Market Structure card** — trend pill, dealing-range bar with animated price
  marker + EQ tick, mono KV rows.
- **Price boxes** — stop/entry/target with auto-precision values and pip
distances (`distLabel`, instrument-aware).

> **⚠ Stacking gotcha (fixed in v3.0):** the zone overlay (`#overlay`) must
> carry an explicit `z-index` above lightweight-charts' internal canvas
> layers. LC v4 renders a canvas with `z-index: 2` inside its layout table;
> an overlay with `z-index: auto` silently paints *below* it — zone divs
> exist in the DOM but are invisible. `#overlay{z-index:3}` (legend is 5)
> resolves this. Symptom if regressed: zones in DOM, absent in screenshots.
> Note: `elementFromPoint` cannot diagnose this — it skips
> `pointer-events:none` elements.

### CSS

A dark theme with these tokens (`:root` in `index.html`):

```css
--bg: #0b0e14; --panel: #12161f; --panel2: #171c28;
--text: #d7dce6; --muted: #8b93a7; --accent: #4db6ac;
--green: #26a69a; --red: #ef5350; --amber: #f0b90b; --blue: #5b8def;
```

Verdict badges:

| Verdict | Color | Meaning |
|---|---|---|
| `BUY` | green | Long entry, score ≥ threshold, LLM aligned |
| `SELL` | red | Short entry, score ≥ threshold, LLM aligned |
| `WAIT` | amber | Setup exists but score below threshold |
| `AVOID` | gray | LLM contradicts direction |

Layout is responsive — below 900px width the sidebar slides under the chart
(see `@media (max-width:900px)`).

---

## Chart rendering

### Chart library

**`lightweight-charts@4.1.3`** loaded from unpkg. Pinned to v4.1.3 because
the API changed significantly in v5 (`chart.addSeries(CandlestickSeries)`
instead of `chart.addCandlestickSeries()`). The pin keeps the upgrade path
explicit.

### Candles

```js
const chart = LightweightCharts.createChart(container, {
  layout: { background: { type: 'solid', color: '#0b0e14' }, textColor: '#8b93a7' },
  grid: { vertLines: {color: '#151a24'}, horzLines: {color: '#151a24'} },
  rightPriceScale: { borderColor: '#1f2533' },
  timeScale: { borderColor: '#1f2533', timeVisible: true, secondsVisible: false, rightOffset: 8 },
  crosshair: { mode: 1 },
});
const series = chart.addCandlestickSeries({
  upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
  wickUpColor: '#26a69a', wickDownColor: '#ef5350',
});
series.setData(candles.map(c => ({time: c.time, open: c.open, high: c.high, low: c.low, close: c.close})));
```

### Markers (sweeps, BOS/CHoCH)

```js
series.setMarkers([
  { time, position: 'aboveBar', color: '#ef5350', shape: 'arrowDown', text: 'sweep' },
  { time, position: 'belowBar', color: '#26a69a', shape: 'arrowUp',   text: 'sweep' },
  { time, position: 'belowBar', color: '#8b93a7', shape: 'circle',    text: 'BOS'  },
  ...
]);
```

Markers whose `time` is outside the displayed candle range are **filtered
client-side** using a Set of candle times. Otherwise `setMarkers` throws or
silently drops them depending on the version.

### Price lines (entry / SL / TP / EQ)

```js
series.createPriceLine({
  price, color, title, lineWidth: 1,
  lineStyle: LightweightCharts.LineStyle.Dashed,   // or Dotted / Solid
  axisLabelVisible: true,
});
```

Cleared on every re-render with `series.removePriceLine(pl)` — see
`clearPriceLines()`.

### Zone overlay

The interesting bit. lightweight-charts doesn't have a box-drawing API, so
the overlay is a separate `<div>` over the chart container, populated with
absolutely positioned rectangles:

```html
<div id="chart"></div>          <!-- lightweight-charts canvas -->
<div id="overlay"></div>         <!-- absolutely positioned zones -->
```

`renderBoxes()` runs whenever the visible time range changes
(`chart.timeScale().subscribeVisibleTimeRangeChange`) or after data updates.
For each zone (OB / FVG / PD half):

```js
let x1 = (zone.t1 >= firstT) ? ts.timeToCoordinate(zone.t1) : null;
if (x1 == null) x1 = (zone.t1 <= firstT) ? 0 : null;
if (x1 == null) continue;

const x2 = (zone.t2 != null) ? ts.timeToCoordinate(zone.t2) : W;
if (x2 <= x1) continue;

const yTop = series.priceToCoordinate(zone.top);
const yBot = series.priceToCoordinate(zone.bottom);
if (yTop == null || yBot == null) continue;

const el = document.createElement('div');
el.className = 'zone';
el.style.cssText =
  `left: ${x1}px; top: ${Math.min(yTop, yBot)}px;` +
  `width: ${x2 - x1}px; height: ${Math.abs(yBot - yTop)}px;`;
overlay.appendChild(el);
```

| Edge case | Handled by |
|---|---|
| Zone older than the loaded candle window | `timeToCoordinate` returns null → `x1 = 0` (zone stretches from left edge). |
| Zone `top` or `bottom` off the visible price range | `priceToCoordinate` returns null → zone skipped. |
| Zone height < 1px | `Math.max(..., 1)` ensures visibility. |
| Zone to the left of visible window | `x2 <= x1` → skipped. |

The ResizeObserver on `#chartWrap` triggers a re-layout on window resize:

```js
new ResizeObserver(() => {
  const w = chartWrap.clientWidth, h = chartWrap.clientHeight;
  chart.applyOptions({ width: w, height: h });
  renderBoxes();
}).observe(chartWrap);
```

### Legend toggles

Five checkboxes control which overlay layers are drawn:

| Toggle | Shows |
|---|---|
| Order Blocks | All unmitigated OBs (green/red shaded) |
| FVGs | All unfilled FVGs (blue/amber shaded) |
| Premium/Discount | Top-half / bottom-half of the dealing range |
| Sweeps | Sweep markers (arrows above/below bar) |
| BOS/CHoCH | Structural event markers (small circles) |

Toggling re-runs `renderBoxes()` and rebuilds the marker array without
re-fetching data.

---

## Setup card rendering

Each setup in `analysis.setups` becomes one card:

```html
<div class="setup long">       <!-- border-left green for long, red for short -->
  <div class="setup-head">
    <span class="dir long">▲ LONG</span>
    <span class="muted">EURUSD H1 · RR 1:2.0</span>
    <span class="verdict BUY">BUY</span>
  </div>
  <div class="prices">
    <div class="price-box"><div class="lbl">Entry</div><div class="val">1.08406</div></div>
    <div class="price-box"><div class="lbl">Stop</div><div class="val" style="color:var(--red)">1.08310</div></div>
    <div class="price-box"><div class="lbl">Target</div><div class="val" style="color:var(--blue)">1.08667</div></div>
    <div class="price-box"><div class="lbl">Zone</div><div class="val">fvg</div></div>
  </div>
  <div class="score-row">
    <span class="muted">Hybrid score</span>
    <div class="score-bar"><div class="score-fill" style="width:65%; background:var(--green)"></div></div>
    <b>65</b>
  </div>
  <div class="ml-row">
    <div class="muted">ML win-probability: <b>68%</b></div>
    <div class="ml-bar"><div class="ml-fill" style="width:68%"></div></div>
  </div>
  <div class="chips">
    <span class="chip">market structure aligned</span>
    <span class="chip">entry in discount zone</span>
    ...
  </div>
  <div class="llm-box">
    <div class="muted">LLM analyst (gpt-4o-mini) · bias bullish · 72/100 · BUY</div>
    <div class="narrative">Strong structure...</div>
    <div class="invalidation">⚠ Invalidation: break below 1.0825</div>
    <ul class="concerns"><li>London session overlap</li>...</ul>
  </div>
</div>
```

Sections appear conditionally:

| Section | Shown when |
|---|---|
| ML win-probability bar | `setup.ml_prob != null` |
| LLM box | `setup.llm != null` |
| Confluence chips | Always |
| Score source label | Always (ML+LLM / ML / LLM / heuristic) |

The price formatter `fp(v)` uses the **instrument's auto-detected decimal
places** (`detectPrecision` samples the last 80 candles): EURUSD shows
`1.08406` (5 dp), USDJPY `154.213` (3 dp), XAUUSD `2350.25` (2 dp). The same
precision is applied to the chart's price axis and crosshair via
`priceFormat: {precision, minMove}` — so the axis, entry/SL/TP lines, and
cards all show full broker precision, never trimmed. Pip distances in the
price boxes use the same precision (`distLabel`).

---

## Market structure card

Always shown at the top of the sidebar:

```html
<div class="card">
  <h3>Market Structure</h3>
  <div class="trend bullish">BULLISH</div>
  <span class="muted">EURUSD · H1 · ATR 0.0012</span>
  <div class="pd-bar"><div class="pd-marker" style="left:20.6%"></div></div>
  <div class="kv"><span>Dealing range</span><b>1.10800 – 1.11500</b></div>
  <div class="kv"><span>Price zone</span><b>discount (20.6%)</b></div>
  <div class="kv"><span>Last event</span><b>BOS bullish @ 1.11150</b></div>
  <div class="kv"><span>Last sweep</span><b>sellside (swing low)</b></div>
  <div class="kv"><span>Fresh OBs / FVGs</span><b>3 / 8</b></div>
  <div class="kv"><span>Generated</span><b>14:32:05</b></div>
</div>
```

The PD bar visualizes where current price sits in the dealing range — green
left half (discount), red right half (premium), white marker on the current
position.

---

## Refresh & polling lifecycle

```
[ user clicks "Run analysis" ]            [ user clicks "↻ Retrain" ]
   → POST /api/refresh                      → POST /api/retrain
   → running = true                         → retraining = true
   → (auto mode: retrain if stale first)    → pipeline.train in background
   → pipeline.run_all in background            (~90s typical)
   → pollStatus() every 1.2s                   → pollStatus() every 1.5s
       → running / retraining? keep polling        → done: badge refreshes,
       → last_error? dot → red                       age resets to 0
       → last_run / last_retrain? dot → green, reload meta + analysis
```

Auto-refresh:

```js
function setupAuto() {
  if (autoTimer) clearInterval(autoTimer);
  if (autoRefresh.checked && state.meta)
    autoTimer = setInterval(loadAnalysis,
                            Math.max(15, state.meta.refresh_seconds) * 1000);
}
```

`Math.max(15, ...)` protects against a misconfigured 0-second interval
hammering the server. Default is 60s.

---

## Performance notes

| Action | Time |
|---|---|
| Load `/api/analysis` (cached) | ~50ms |
| Load `/api/analysis` (freshly computed) | ~300–800ms |
| `run_all` for 6 pairs | ~600ms |
| ML prediction per setup | <1ms |
| LLM call per setup | 1–3s (network-bound) |
| Render boxes | <5ms for ~20 zones |
| **Full ML retrain** (3 sym × 2 tf) | **~90s** (walk-forward labeling dominates; permutation importance adds a few seconds) |
| Full retrain (20+ symbols) | 3–5 min |

LLM calls dominate the refresh latency. The pipeline currently calls the
LLM for **both chosen setups** (one per direction). For 3 symbols × 2
timeframes = 6 pairs × 2 setups = 12 LLM calls per refresh, taking 12–36s
with a remote API. That's the main reason the worker runs in a background
thread.

---

## Possible improvements

- **WebSocket push** instead of polling — FastAPI supports `WebSocket`
  routes, the server could push `state.last_run` the moment the worker
  finishes.
- **Server-side candle compression** — return only the visible window based
  on chart zoom state.
- **Setup history UI** — the journal exists (`setups_history` table +
  `/api/setups/history`); add a dashboard panel showing recent setups with
  their outcomes once forward data accumulates.
- **Outcome marking** — one-click "mark this setup as taken/won/lost" to
  close the loop into ML training data.
- **Multi-symbol overview** — top bar with each symbol's current trend,
  latest verdict, and ATR, all in one row.
- **Light theme** — currently dark only; add a CSS theme switcher.
