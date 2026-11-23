# Proposal: Hide Mitigated Order Blocks and Filled FVGs by Default

## 1. Overview & Motivation

Currently, the dashboard chart (`app/dashboard/static/index.html`) renders every entry from `a.smc.order_blocks.all` and `a.smc.fvgs.all`. Mitigated Order Blocks (`ob.mitigated === true`) and filled Fair Value Gaps (`f.filled === true`) are drawn with a low-opacity dashed style (`OB ·mit`, `FVG·filled`). Over a 400-bar chart window, consumed zones accumulate and create visual clutter around active price action.

This change makes mitigated OBs and filled FVGs **hidden by default** via configuration (`dashboard.show_mitigated_zones: false`), while exposing a runtime toggle in the chart legend so users can inspect historical/consumed zones on demand.

---

## 2. Data Flow

```
config.yaml (dashboard.show_mitigated_zones: false)
       │
       ▼
app/config.py (DEFAULTS["dashboard"]["show_mitigated_zones"] = False)
       │
       ▼
app/dashboard/server.py (GET /api/meta -> "show_mitigated_zones": bool)
       │
       ▼
app/dashboard/static/index.html
  ├── loadMeta()     -> initializes #tgMitigated checkbox from state.meta.show_mitigated_zones
  ├── #legend        -> user can toggle #tgMitigated live without server restart
  └── renderBoxes()  -> skips ob.mitigated / f.filled when !#tgMitigated.checked
```

---

## 3. Detailed Implementation Specification

### 3.1 `app/config.py` (Lines 125–129)

Add `"show_mitigated_zones": False` to `DEFAULTS["dashboard"]` so existing `config.yaml` files that omit the key safely inherit `False` via `_deep_merge`.

**Before:**
```python
    "dashboard": {"host": "127.0.0.1", "port": 8000, "refresh_seconds": 60,
                  "auto_run_interval": 0, "bar_close_watcher": False,
                  "bar_poll_seconds": 10, "debug": True,
                  "log_file": "logs/ai-trader.log",
                  "parallel_analysis": True, "analysis_workers": 0},
```

**After:**
```python
    "dashboard": {"host": "127.0.0.1", "port": 8000, "refresh_seconds": 60,
                  "auto_run_interval": 0, "bar_close_watcher": False,
                  "bar_poll_seconds": 10, "debug": True,
                  "log_file": "logs/ai-trader.log",
                  "parallel_analysis": True, "analysis_workers": 0,
                  "show_mitigated_zones": False},
```

---

### 3.2 `config.yaml` (Lines 236–239)

Document and explicitly set `show_mitigated_zones: false` under the `dashboard:` section.

**Before:**
```yaml
  bar_close_watcher: true
  bar_poll_seconds: 10       # how often to peek MT5 for a new bar
```

**After:**
```yaml
  bar_close_watcher: true
  bar_poll_seconds: 10       # how often to peek MT5 for a new bar
  show_mitigated_zones: false # show filled FVGs and mitigated OBs on the chart (hidden by default)
```

---

### 3.3 `app/dashboard/server.py` (Lines 314–322)

Expose the configured flag in `GET /api/meta` so the browser initializes the toggle state on startup/reload.

**Before:**
```python
            "auto_run_seconds": auto_run if not bar_close else 0,
            "bar_close_watcher": bar_close,
            "sessions": cfg.get("dashboard", {}).get("sessions", {}),
```

**After:**
```python
            "auto_run_seconds": auto_run if not bar_close else 0,
            "bar_close_watcher": bar_close,
            "show_mitigated_zones": bool(cfg.get("dashboard", {}).get("show_mitigated_zones", False)),
            "sessions": cfg.get("dashboard", {}).get("sessions", {}),
```

---

### 3.4 `app/dashboard/static/index.html`

Four targeted edits in the single-file UI:

#### A. Chart Legend Checkbox (Lines 436–438)
Add a `Mitigated / Filled` checkbox right after `FVGs`:

**Before:**
```html
      <label><input type="checkbox" id="tgOB" checked> <span class="sw" style="background:var(--ob-long);border:1px solid var(--green)"></span>Order Blocks</label>
      <label><input type="checkbox" id="tgFVG" checked> <span class="sw" style="background:var(--fvg-long);border:1px solid var(--blue)"></span>FVGs</label>
```

**After:**
```html
      <label><input type="checkbox" id="tgOB" checked> <span class="sw" style="background:var(--ob-long);border:1px solid var(--green)"></span>Order Blocks</label>
      <label><input type="checkbox" id="tgFVG" checked> <span class="sw" style="background:var(--fvg-long);border:1px solid var(--blue)"></span>FVGs</label>
      <label title="Show mitigated Order Blocks and filled FVGs"><input type="checkbox" id="tgMitigated"> Mitigated / Filled</label>
```

#### B. Zone Filtering in `renderBoxes()` (Lines 700–724)
Read `#tgMitigated` once at the start of the zone loop and skip `dead` (`ob.mitigated` / `f.filled`) zones when unchecked:

**Before:**
```js
  if ($('tgOB').checked && a.smc.order_blocks){
    for (const ob of a.smc.order_blocks.all){
      // mitigated OBs draw faded (dashed) instead of hidden — like TradingView
      // SMC indicators — so consumed zones stay visible for context
      const dead = !!ob.mitigated;
      const s = ob.direction === 'bullish'
        ? {bg: dead ? 'rgba(45,212,167,.05)' : 'var(--ob-long)',
           border: dead ? '1px dashed rgba(45,212,167,.25)' : '1px solid rgba(45,212,167,.45)'}
        : {bg: dead ? 'rgba(242,95,92,.05)' : 'var(--ob-short)',
           border: dead ? '1px dashed rgba(242,95,92,.25)' : '1px solid rgba(242,95,92,.45)'};
      boxes.push({t1:ob.origin_time, t2:null, top:ob.top, bottom:ob.bottom, style:s,
                  label:`OB ${ob.direction==='bullish'?'▲':'▼'}${dead?' ·mit':''}`});
    }
  }
  if ($('tgFVG').checked && a.smc.fvgs){
    for (const f of a.smc.fvgs.all){
      const dead = !!f.filled;
      const s = dead
        ? {bg:'rgba(120,130,150,.04)', border:'1px dashed rgba(120,130,150,.22)', label:'FVG·filled'}
        : (f.direction === 'bullish'
        ? {bg:'var(--fvg-long)', border:'1px solid rgba(108,158,255,.4)', label:'FVG'}
        : {bg:'var(--fvg-short)', border:'1px solid rgba(245,184,61,.4)', label:'FVG'});
      boxes.push({t1:f.origin_time, t2:null, top:f.top, bottom:f.bottom, style:s});
    }
  }
```

**After:**
```js
  const showDead = $('tgMitigated') ?$('tgMitigated').checked : false;
  if ($('tgOB').checked && a.smc.order_blocks){
    for (const ob of a.smc.order_blocks.all){
      const dead = !!ob.mitigated;
      if (dead && !showDead) continue;
      const s = ob.direction === 'bullish'
        ? {bg: dead ? 'rgba(45,212,167,.05)' : 'var(--ob-long)',
           border: dead ? '1px dashed rgba(45,212,167,.25)' : '1px solid rgba(45,212,167,.45)'}
        : {bg: dead ? 'rgba(242,95,92,.05)' : 'var(--ob-short)',
           border: dead ? '1px dashed rgba(242,95,92,.25)' : '1px solid rgba(242,95,92,.45)'};
      boxes.push({t1:ob.origin_time, t2:null, top:ob.top, bottom:ob.bottom, style:s,
                  label:`OB ${ob.direction==='bullish'?'▲':'▼'}${dead?' ·mit':''}`});
    }
  }
  if ($('tgFVG').checked && a.smc.fvgs){
    for (const f of a.smc.fvgs.all){
      const dead = !!f.filled;
      if (dead && !showDead) continue;
      const s = dead
        ? {bg:'rgba(120,130,150,.04)', border:'1px dashed rgba(120,130,150,.22)', label:'FVG·filled'}
        : (f.direction === 'bullish'
        ? {bg:'var(--fvg-long)', border:'1px solid rgba(108,158,255,.4)', label:'FVG'}
        : {bg:'var(--fvg-short)', border:'1px solid rgba(245,184,61,.4)', label:'FVG'});
      boxes.push({t1:f.origin_time, t2:null, top:f.top, bottom:f.bottom, style:s});
    }
  }
```

#### C. Initialize from `/api/meta` in `loadMeta()` (Lines 1104–1112)
Apply the server-configured default when metadata loads:

**Before:**
```js
async function loadMeta(){
  const r = await fetch('/api/meta'); state.meta = await r.json();
  try{ const sr = await fetch('/api/status'); updateStamp(await sr.json()); }catch(e){}
  state.allSymbols = state.meta.symbols || [];
```

**After:**
```js
async function loadMeta(){
  const r = await fetch('/api/meta'); state.meta = await r.json();
  if ($('tgMitigated'))$('tgMitigated').checked = !!state.meta.show_mitigated_zones;
  try{ const sr = await fetch('/api/status'); updateStamp(await sr.json()); }catch(e){}
  state.allSymbols = state.meta.symbols || [];
```

#### D. Legend Toggle Listener (Lines 1643–1644)
Include `'tgMitigated'` in the legend change listener array so toggling immediately re-renders the chart overlay:

**Before:**
```js
for (const id of ['tgOB','tgFVG','tgPD','tgSweeps','tgStructure','tgSwings','tgRej','tgSessions'])
  $(id).onchange = () => { if (state.data) renderChart(); };
```

**After:**
```js
for (const id of ['tgOB','tgFVG','tgMitigated','tgPD','tgSweeps','tgStructure','tgSwings','tgRej','tgSessions'])
  $(id).onchange = () => { if (state.data) renderChart(); };
```

---

### 3.5 `docs/dashboard-architecture.md` (Lines 85–95)

Add `"show_mitigated_zones": false` to the sample `GET /api/meta` JSON response block.

---

## 4. Edge Cases & Behavioral Notes

- **Interaction with `Order Blocks` (`#tgOB`) and `FVGs` (`#tgFVG`) toggles**:
  - If `#tgOB` is unchecked, **no** Order Blocks (fresh or mitigated) are drawn, even if `#tgMitigated` is checked.
  - If `#tgFVG` is unchecked, **no** FVGs (fresh or filled) are drawn, even if `#tgMitigated` is checked.
  - `#tgMitigated` acts purely as an visibility filter for `dead` zones within whichever parent zone layers (`#tgOB`, `#tgFVG`) are enabled.
- **Sidebar `Market Structure` card (`renderStructure()`)**:
  - Line 950 displays `Fresh OB / FVG` using `s.order_blocks?.fresh?.length` and `s.fvgs?.fresh?.length`. Those counts already reflect only unmitigated/unfilled zones and remain unchanged.
- **Backend / ML / Execution isolation**:
  - `app/smc/order_blocks.py`, `app/smc/fvg.py`, `app/engine/setup_builder.py`, `app/ai/features.py`, and `app/execution/executor.py` are completely untouched.

---

## 5. Verification Plan

1. **Config loading check**:
   - Run `python -c "from app.config import load_config; c = load_config(); assert c['dashboard']['show_mitigated_zones'] is False"` to verify both `DEFAULTS` and `config.yaml` resolve cleanly.
2. **API check**:
   - Start dashboard (`python -m app.main serve --demo`) and confirm `GET /api/meta` returns `"show_mitigated_zones": false`.
3. **UI check**:
   - Open `http://127.0.0.1:8000` and verify `#tgMitigated` (`Mitigated / Filled`) is unchecked on load and no `.zone-label` elements with `·mit` or `FVG·filled` exist in `#overlay`.
   - Check `Mitigated / Filled` in the legend and verify faded dashed `OB ·mit` / `FVG·filled` zones appear immediately.
   - Set `dashboard.show_mitigated_zones: true` in `config.yaml`, restart server, reload page, and confirm `#tgMitigated` starts checked.
