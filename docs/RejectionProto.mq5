//+------------------------------------------------------------------+
//|                                               RejectionProto.mq5 |
//|          Prototype — M15 price-rejection detector (no trading)   |
//|                                                                  |
//| Detection only — places NO orders. Think in closed M15 bars      |
//| regardless of chart TF; every verdict fires once, on bar close   |
//| (closed-bar discipline, no repaint).                             |
//|                                                                  |
//| Two detection families:                                          |
//|  1. LEVEL sweep — the bar wicks through a reference level and    |
//|     closes back inside; the close back inside IS the rejection.  |
//|     References: unbroken M15 fractal swing highs/lows            |
//|     (InpSwingBars, newest-of-equals tie-break) and the running   |
//|     day high/low (server day).                                   |
//|  2. ENGULFING bar — all three standard definitions evaluated per |
//|     bar (DailyForex/Dukascopy taxonomy); the journal records     |
//|     which matched (defs=...):                                    |
//|     D1 body engulf — bar body strictly covers previous body,     |
//|        opposite colors (the most common definition);             |
//|     D2 range engulf ("outside bar") — bar high/low engulfs the   |
//|        previous high/low, opposite colors, strictly wider range; |
//|     D3 decisive engulf — D1 body coverage plus engulfing body    |
//|        >= 2/3 of the bar's full range (short wicks, no           |
//|        indecision).                                              |
//| A bar closing THROUGH a level is not a rejection — the level is  |
//| BROKEN (body-close-through, logged) and stops being a reference. |
//| (Pure pin-bar wick anatomy was dropped in v1.04, the sweep-only  |
//| gold-dot marking in v1.05, and plain engulfing bars without a    |
//| level sweep in v1.13 — owner calls: no standalone edge.          |
//| Sweeps still matter as the annotation on engulfing bars.)        |
//|                                                                  |
//| Momentum annotations (v1.08, journal-only — no gating yet):      |
//| every REJECT line carries a "mom:" block — clv (close            |
//| location within the bar range, %, direction-corrected), br       |
//| (body/range %), exp (body vs avg body of the prior               |
//| InpAvgBodyBars bars, tenths of an x), and on REJECT lines depth  |
//| (deepest sweep beyond a level, % of the bar range). A follow-up  |
//| C3 line logs the outcome window for each reject: the next bar's  |
//| cont/hold/rev verdict vs the engulf body (ICT C3 test) plus      |
//| MFE/MAE over InpOutcomeBars bars, all in % of the engulf range.  |
//| Research basis: ICT displacement quality (body/range >75%        |
//| A-tier / <60% skip), Bulkowski long-day expansion (body >= 3x    |
//| avg body), sweep-then-reclaim as the confluence that matters.    |
//|                                                                  |
//| Retrace-entry outcome (v1.09, reject-only since v1.13): after    |
//| every reject close a                                             |
//| virtual limit waits at the conservative body-midpoint (floor for |
//| bull, ceil for bear — a half-point midpoint is not placeable)    |
//| for InpRetraceBars bars. On fill, the next InpOutcomeBars bars   |
//| are measured FROM THE FILL, in % of risk (fill minus the engulf  |
//| extreme = the would-be stop): rmfe/rmae are R-multiples, and     |
//| sl=1 flags the stop level being touched. RET lines log fill      |
//| price + outcome, or nofill if the pullback never came.           |
//| Tick-exact virtual trades (v1.12, journal-only): every REJECT    |
//| bar opens a virtual position at its close — SL = engulf extreme, |
//| TP = InpTradeRRTenths/10 R, flat after 16 bars — resolved from   |
//| ACTUAL tester ticks (OnTick, every tick), so SL/TP order inside  |
//| a bar is exact, not assumed. TRD lines log entry/sl/tp/risk,     |
//| exit type (SL/TP/TIME), exit price, r = % of risk, bars held.    |
//| Offline OHLC replay cannot reproduce tick order — the verifier   |
//| skips TRD; battery checks apply instead. Filter: risk < 30 pts   |
//| skipped (matches the offline sim).                               |
//|                                                                  |
//| Journal tags: REJECT (engulf+sweep — the only annotated engulf;  |
//| plain ENGULF lines dropped in v1.13) / BROKEN /                  |
//| PIVOT / BAR / C3 / RET / TRD / SUMMARY, prefix [RPROTO]. Chart:  |
//| arrows = rejects only (always wide), silver dots = confirmed     |
//| swing pivots. Reject arrows carry the bucket color               |
//| (v1.11): gold = A-grade reject (weak close + small bar), magenta |
//| = momentum reject (big displacement); other rejects keep the     |
//| direction color. Attach to an                                    |
//| M15 chart.                                                       |
//+------------------------------------------------------------------+
#property copyright "KISS repo"
#property link      ""
#property version   "1.13"
#property description "Prototype: engulfing + level-sweep rejection detector on closed M15 bars. Detection only — no trading."

//--- fixed identity (keep in sync with #property version)
const string EA_NAME    = "RejectionProto";
const string EA_VERSION = "1.13";
const string OBJ_PFX    = "RPROTO_";

//--- the only timeframe this prototype thinks in
const ENUM_TIMEFRAMES WORK_TF = PERIOD_M15;

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input group "=== Detection (M15 closed bars) ==="
input int    InpMinRangePoints = 10;     // Min bar range in points — engulfing noise gate (0 = off)
input int    InpSwingBars      = 3;      // Fractal pivot width for M15 swing levels
input bool   InpUseSwingLevels = true;   // Sweep-check vs unbroken M15 swing high/low
input bool   InpUseDayExtreme  = true;   // Sweep-check vs running day high/low

input group "=== Virtual trade simulator (journal-only) ==="
input bool InpTradeSim     = true;        // Tick-exact virtual trades on REJECT bars
input int  InpTradeRRTenths = 20;         // Virtual TP in R, tenths (20 = 2.0R)
input group "=== Momentum annotations (journal-only) ==="
input int  InpAvgBodyBars  = 20;          // Expansion lookback: avg body of prior N bars (0 = off)
input int  InpOutcomeBars  = 4;           // C3 outcome horizon in bars after the engulf close (0 = off)
input int  InpMomExpTenths = 20;          // Momentum marker: body >= N/10 x avg body (0 = off)
input int  InpWeakClosePct = 50;          // A-grade reject: close location value below N %
input int  InpBigExpTenths = 30;          // Momentum reject: body >= N/10 x avg body (0 = off)
input int  InpRetraceBars  = 8;           // Retrace-entry window after engulf close, bars (0 = off)
input int  InpRetracePct   = 50;          // Retrace-entry limit depth into the engulf body, % (20..80)

input group "=== Prototype ==="
input int  InpBackfillBars = 200;         // Closed M15 bars scanned on attach (0 = live only)
input bool InpDrawArrows   = true;        // Draw chart markers on detections
input bool InpMarkSwings   = true;        // Draw silver dots at confirmed swing pivots
input int  InpLogLevel     = 1;           // 0=detections, 1=+level breaks/pivots, 2=+per-bar verdicts

//+------------------------------------------------------------------+
//| Globals                                                          |
//+------------------------------------------------------------------+
//--- swing level book (chronological; newest at the largest index)
struct SwingLevel
  {
   datetime time;                       // pivot bar time
   double   price;                      // pivot extreme
   bool     broken;                     // closed through since formation
  };
SwingLevel g_swingHigh[];
SwingLevel g_swingLow[];

//--- running day extremes (server day; reference for the NEXT bars)
datetime g_dayStamp = 0;
double   g_dayHigh  = 0.0;
double   g_dayLow   = 0.0;
bool     g_dayHas   = false;

//--- processing cursor: time of the newest processed CLOSED M15 bar
datetime g_lastClosed = 0;

//--- pending engulf outcome windows (C3 layer): one entry per engulf
struct PendOutcome
  {
   datetime t;                          // engulf bar time
   int      dir;                        // +1 bull / -1 bear
   int      oPts;                       // engulf open, integer points
   int      cPts;                       // engulf close, integer points
   int      rngPts;                     // engulf range, integer points
   int      seen;                       // outcome bars consumed so far
   int      mfePct;                     // max favorable excursion, % of engulf range
   int      maePct;                     // max adverse excursion, % of engulf range
   int      next;                       // next-bar verdict: 1=cont 2=hold 3=rev 0=pending
  };
PendOutcome g_pend[];

//--- pending retrace-entry windows (v1.09): virtual limit at the engulf body
//--- midpoint after each engulf; outcome measured from the fill in % of risk
struct RetracePend
  {
   datetime t;                          // engulf bar time
   int      dir;                        // +1 bull / -1 bear
   int      midPts;                     // limit: conservative body-midpoint (integer points)
   int      slPts;                      // would-be stop: engulf extreme (low bull / high bear)
   int      seen;                       // bars since engulf close (fill-window budget)
   bool     filled;                     // limit touched?
   int      fillPts;                    // fill price (integer points)
   int      bars;                       // outcome bars consumed after fill
   int      rmfePct;                    // max favorable excursion from fill, % of risk
   int      rmaePct;                    // max adverse excursion from fill, % of risk
   bool     hitSl;                      // engulf extreme touched after fill
  };
RetracePend g_ret[];

//--- tick-exact virtual trade (v1.12): opened at REJECT-bar close;
//--- SL = engulf extreme, TP = InpTradeRRTenths/10 R, flat after 16 bars.
//--- Resolved from tester ticks, so intra-bar SL/TP order is exact.
struct VirtTrade
  {
   datetime t;                          // entry bar time
   int      dir;                        // +1 bull / -1 bear
   int      ePts;                       // entry price (signal close, integer points)
   int      slPts;                      // stop (engulf extreme)
   int      tpPts;                      // target
   int      rkPts;                      // risk in points
   int      bkt;                        // 0 = A-grade, 1 = momentum, 2 = other
   int      bars;                       // closed bars since entry
  };
VirtTrade g_vt[];

//--- session stats (since attach)
int g_statC3     = 0;
int g_statReject = 0;
int g_statBroken = 0;
int g_statEngulf  = 0;
int g_statEngulf1 = 0;
int g_statEngulf2 = 0;
int g_statEngulf3 = 0;

//+------------------------------------------------------------------+
//| Small helpers                                                    |
//+------------------------------------------------------------------+
//+---+
//| Log() wrapper: [RPROTO] prefix + greppable tag, level-gated      |
//+---+
void Log(const int level, const string tag, const string msg)
  {
   if(level > InpLogLevel)
      return;
   PrintFormat("[RPROTO] %s | %s", tag, msg);
  }

//+---+
//| Server-day midnight stamp of a bar time                          |
//+---+
datetime DateOf(const datetime t)
  {
   return (datetime)(((long)t / 86400) * 86400);
  }

//+---+
//| Append one swept-level description to a comma list               |
//+---+
string AppendSwept(const string cur, const string item)
  {
   return (cur == "") ? item : cur + ", " + item;
  }

//+---+
//| Append a swing to a chronological book                           |
//+---+
void AddSwing(SwingLevel &arr[], const datetime t, const double price)
  {
   int n = ArraySize(arr);
   ArrayResize(arr, n + 1);
   arr[n].time   = t;
   arr[n].price  = price;
   arr[n].broken = false;
  }

//+---+
//| Warmup bars fetched before the backfill window (references)      |
//+---+
int WarmupBars()
  {
   return 150 + 2 * InpSwingBars;
  }

//+------------------------------------------------------------------+
//| Reference maintenance                                            |
//+------------------------------------------------------------------+
//+---+
//| Roll day stamp at server midnight; fold a closed bar into the    |
//| running day extremes (call AFTER the bar was evaluated)          |
//+---+
void UpdateDayExtremes(const MqlRates &b)
  {
   datetime d = DateOf(b.time);
   if(d != g_dayStamp)
     {
      g_dayStamp = d;
      g_dayHas   = false;
     }
   if(!g_dayHas)
     {
      g_dayHigh = b.high;
      g_dayLow  = b.low;
      g_dayHas  = true;
     }
   else
     {
      if(b.high > g_dayHigh) g_dayHigh = b.high;
      if(b.low  < g_dayLow)  g_dayLow  = b.low;
     }
  }

//+---+
//| Body-close through an unbroken swing breaks it as a reference    |
//+---+
void MarkBrokenByClose(const MqlRates &b)
  {
   int n;
   n = ArraySize(g_swingHigh);
   for(int i = n - 1; i >= 0; i--)
     {
      if(g_swingHigh[i].broken)
         continue;
      if(b.close > g_swingHigh[i].price)
        {
         g_swingHigh[i].broken = true;
         g_statBroken++;
         Log(1, "BROKEN", StringFormat("swingHigh@%s (pivot %s) closed above by bar %s",
             DoubleToString(g_swingHigh[i].price, _Digits),
             TimeToString(g_swingHigh[i].time, TIME_DATE | TIME_MINUTES),
             TimeToString(b.time, TIME_DATE | TIME_MINUTES)));
        }
     }
   n = ArraySize(g_swingLow);
   for(int i = n - 1; i >= 0; i--)
     {
      if(g_swingLow[i].broken)
         continue;
      if(b.close < g_swingLow[i].price)
        {
         g_swingLow[i].broken = true;
         g_statBroken++;
         Log(1, "BROKEN", StringFormat("swingLow@%s (pivot %s) closed below by bar %s",
             DoubleToString(g_swingLow[i].price, _Digits),
             TimeToString(g_swingLow[i].time, TIME_DATE | TIME_MINUTES),
             TimeToString(b.time, TIME_DATE | TIME_MINUTES)));
        }
     }
  }

//+---+
//| If bar p is an N-bar fractal pivot, add it to the book.          |
//| Tie-break (MQL5 CHoCH convention, art. 20355): equality is       |
//| allowed against the OLDER flank but disqualified against the     |
//| NEWER flank — on a flat top/bottom (EQH/EQL) the NEWEST touch    |
//| keeps the swing reference instead of the pair cancelling out     |
//+---+
void IfPivotAdd(const MqlRates &r[], const int p, const int total)
  {
   if(p - InpSwingBars < 0 || p + InpSwingBars > total - 1)
      return;
   double h = r[p].high;
   double l = r[p].low;
   bool   isHigh = true;
   bool   isLow  = true;
   for(int k = 1; k <= InpSwingBars && (isHigh || isLow); k++)
     {
      if(r[p + k].high > h || r[p - k].high >= h)      // older: eq ok | newer: strict
         isHigh = false;
      if(r[p + k].low < l || r[p - k].low <= l)        // older: eq ok | newer: strict
         isLow = false;
     }
   if(isHigh)
     {
      AddSwing(g_swingHigh, r[p].time, h);
      DrawSwingMarker(r, p, true);
      Log(1, "PIVOT", StringFormat("swingHigh@%s confirmed (pivot %s)",
          DoubleToString(h, _Digits), TimeToString(r[p].time, TIME_DATE | TIME_MINUTES)));
     }
   if(isLow)
     {
      AddSwing(g_swingLow, r[p].time, l);
      DrawSwingMarker(r, p, false);
      Log(1, "PIVOT", StringFormat("swingLow@%s confirmed (pivot %s)",
          DoubleToString(l, _Digits), TimeToString(r[p].time, TIME_DATE | TIME_MINUTES)));
     }
  }

//+------------------------------------------------------------------+
//| Detection                                                        |
//+------------------------------------------------------------------+
//+---+
//| Evaluate one closed M15 bar: engulfing + level sweep tests.      |
//| References are the books as of the PREVIOUS close — the          |
//| evaluated bar never sweeps or breaks with hindsight.             |
//+---+
void EvaluateBar(const MqlRates &r[], const int s, const int total)
  {
   MqlRates b = r[s];
   double range  = b.high - b.low;
   bool   tooSmall = (InpMinRangePoints > 0 && range < InpMinRangePoints * _Point);

   //--- integer-point anatomy (ULP-safe; used by sweep depth + mom fields)
   int hPts    = (int)MathRound(b.high / _Point);
   int lPts    = (int)MathRound(b.low / _Point);
   int cPts    = (int)MathRound(b.close / _Point);
   int oPts    = (int)MathRound(b.open / _Point);
   int bodyPts = MathAbs(cPts - oPts);
   int rngPts  = hPts - lPts;

   //--- level sweep test (wick through + close back inside);
   //--- depth tracks the deepest sweep beyond a level, % of bar range
   string swept  = "";
   int    nSwept = 0;
   int    maxDepth = 0;
   if(InpUseSwingLevels)
     {
      int n = ArraySize(g_swingHigh);
      for(int i = n - 1; i >= 0; i--)
        {
         if(g_swingHigh[i].broken)
            continue;
         if(b.high > g_swingHigh[i].price && b.close < g_swingHigh[i].price)
           {
            swept = AppendSwept(swept, StringFormat("swingHigh@%s(%s)",
                DoubleToString(g_swingHigh[i].price, _Digits),
                TimeToString(g_swingHigh[i].time, TIME_DATE | TIME_MINUTES)));
            nSwept++;
            int d = (int)MathRound(100.0 * (hPts - (int)MathRound(g_swingHigh[i].price / _Point)) / rngPts);
            if(d > maxDepth) maxDepth = d;
           }
        }
      n = ArraySize(g_swingLow);
      for(int i = n - 1; i >= 0; i--)
        {
         if(g_swingLow[i].broken)
            continue;
         if(b.low < g_swingLow[i].price && b.close > g_swingLow[i].price)
           {
            swept = AppendSwept(swept, StringFormat("swingLow@%s(%s)",
                DoubleToString(g_swingLow[i].price, _Digits),
                TimeToString(g_swingLow[i].time, TIME_DATE | TIME_MINUTES)));
            nSwept++;
            int d = (int)MathRound(100.0 * ((int)MathRound(g_swingLow[i].price / _Point) - lPts) / rngPts);
            if(d > maxDepth) maxDepth = d;
           }
        }
     }
   if(InpUseDayExtreme && g_dayHas && g_dayStamp == DateOf(b.time))
     {
      if(b.high > g_dayHigh && b.close < g_dayHigh)
        {
         swept = AppendSwept(swept, StringFormat("dayHigh@%s", DoubleToString(g_dayHigh, _Digits)));
         nSwept++;
         int d = (int)MathRound(100.0 * (hPts - (int)MathRound(g_dayHigh / _Point)) / rngPts);
         if(d > maxDepth) maxDepth = d;
        }
      if(b.low < g_dayLow && b.close > g_dayLow)
        {
         swept = AppendSwept(swept, StringFormat("dayLow@%s", DoubleToString(g_dayLow, _Digits)));
         nSwept++;
         int d = (int)MathRound(100.0 * ((int)MathRound(g_dayLow / _Point) - lPts) / rngPts);
         if(d > maxDepth) maxDepth = d;
        }
     }

   //--- engulfing test (vs previous closed bar, same min-range gate)
   int eng = 0;
   int engDefs = 0;
   if(!tooSmall && s + 1 <= total - 1)
      eng = EngulfEval(r[s], r[s + 1], engDefs);

   //--- journal + marker: engulf+sweep = REJECT (wide arrow); v1.13 is
   //--- reject-only — plain engulfs (no sweep) are counted in the
   //--- session totals but not journaled, drawn, or instrumented

   Log(2, "BAR", StringFormat("%s | O=%s H=%s L=%s C=%s | verdict: sweeps=%d engulf=%s%s",
       TimeToString(b.time, TIME_DATE | TIME_MINUTES),
       DoubleToString(b.open, _Digits), DoubleToString(b.high, _Digits),
       DoubleToString(b.low, _Digits), DoubleToString(b.close, _Digits),
       nSwept,
       eng == 0 ? "none" : StringFormat("%s[d%s]", eng > 0 ? "bull" : "bear", DefsMask(engDefs)),
       tooSmall ? " range<min" : ""));

   if(eng != 0)
     {
      g_statEngulf++;
      if((engDefs & 1) != 0) g_statEngulf1++;
      if((engDefs & 2) != 0) g_statEngulf2++;
      if((engDefs & 4) != 0) g_statEngulf3++;
      string engBody = StringFormat("%s | %s engulf defs=%s | body=%dpt prevBody=%dpt",
                        TimeToString(b.time, TIME_DATE | TIME_MINUTES), eng > 0 ? "bull" : "bear",
                        DefsMask(engDefs),
                        (int)MathRound(MathAbs(b.close - b.open) / _Point),
                        (int)MathRound(MathAbs(r[s + 1].close - r[s + 1].open) / _Point));
      //--- momentum annotations (v1.08): ALL fields derive from the
      //--- integer-point anatomy above (the D3 lesson — double
      //--- subtractions carry ULP noise that flips x.5 rounding
      //--- knife-edges); the journal replay reproduces every field
      int clvPct  = (int)MathRound(100.0 * (eng > 0 ? (cPts - lPts) : (hPts - cPts)) / rngPts);
      int brPct   = (int)MathRound(100.0 * bodyPts / rngPts);
      string momStr = StringFormat(" | mom: clv=%d%% br=%d%%", clvPct, brPct);
      int exp10 = 0;
      if(InpAvgBodyBars > 0)
        {
         long sumBody = 0;
         int  cnt     = 0;
         for(int k = s + 1; k <= s + InpAvgBodyBars && k <= total - 1; k++)
           {
            sumBody += (int)MathRound(MathAbs(r[k].close - r[k].open) / _Point);
            cnt++;
           }
         if(cnt > 0 && sumBody > 0)
           {
            exp10 = (int)MathRound(10.0 * bodyPts * cnt / (double)sumBody);
            momStr += StringFormat(" exp=%d.%dx", exp10 / 10, exp10 % 10);
           }
        }
      if(nSwept > 0)
        {
         g_statReject++;
         momStr += StringFormat(" depth=%d%%", maxDepth);
         Log(0, "REJECT", engBody + " | swept: " + swept + momStr);
         //--- bucket classification colors the reject arrow itself
         bool   haveExp = (InpAvgBodyBars > 0 && exp10 > 0);
         color  bclr    = clrNONE;
         string btag    = "";
         if(haveExp && InpBigExpTenths > 0 && exp10 >= InpBigExpTenths)
           {
            bclr = clrMagenta;
            btag = " + momentum reject";
           }
         else if(haveExp && exp10 < InpMomExpTenths && clvPct < InpWeakClosePct)
           {
            bclr = clrGold;
            btag = " + A-grade (weak close + small bar)";
           }
         DrawEngulfArrow(b, eng, engDefs, true, bclr, btag);

         //--- tick-exact virtual trade (v1.12): entry at close, SL =
         //--- engulf extreme, TP = InpTradeRRTenths/10 R; risk < 30 pts
         //--- skipped (matches the offline sim's tradeable filter)
         if(InpTradeSim)
           {
            int riskPts = (eng > 0) ? (cPts - lPts) : (hPts - cPts);
            if(riskPts >= 30)
              {
               int tpStep   = (riskPts * InpTradeRRTenths + 5) / 10;   // half-up, placeable
               int tpPts    = (eng > 0) ? (cPts + tpStep) : (cPts - tpStep);
               int bkt      = 2;
               if(haveExp && InpBigExpTenths > 0 && exp10 >= InpBigExpTenths)
                  bkt = 1;
               else if(haveExp && exp10 < InpMomExpTenths && clvPct < InpWeakClosePct)
                  bkt = 0;
               int nv = ArraySize(g_vt);
               ArrayResize(g_vt, nv + 1);
               g_vt[nv].t = b.time; g_vt[nv].dir = eng; g_vt[nv].ePts = cPts;
               g_vt[nv].slPts = (eng > 0) ? lPts : hPts;
               g_vt[nv].tpPts = tpPts; g_vt[nv].rkPts = riskPts;
               g_vt[nv].bkt = bkt; g_vt[nv].bars = 0;
              }
           }
        }

      //--- schedule the C3 outcome window for this reject (v1.13)
      if(nSwept > 0 && InpOutcomeBars > 0)
        {
         int n = ArraySize(g_pend);
         ArrayResize(g_pend, n + 1);
         g_pend[n].t       = b.time;
         g_pend[n].dir     = eng;
         g_pend[n].oPts    = oPts;
         g_pend[n].cPts    = cPts;
         g_pend[n].rngPts  = rngPts;
         g_pend[n].seen    = 0;
         g_pend[n].mfePct  = 0;
         g_pend[n].maePct  = 0;
         g_pend[n].next    = 0;
        }

      //--- retrace-entry window (v1.09; reject-only since v1.13):
      //--- virtual limit InpRetracePct deep into the body from the close
      //--- side; stop = engulf extreme. Depth rounds half-up to a
      //--- placeable point price (deterministic)
      if(nSwept > 0 && InpRetraceBars > 0)
        {
         int depthPts = (bodyPts * InpRetracePct + 50) / 100;
         int limitPts = (eng > 0) ? (cPts - depthPts) : (cPts + depthPts);
         int slPts    = (eng > 0) ? lPts : hPts;
         int n = ArraySize(g_ret);
         ArrayResize(g_ret, n + 1);
         g_ret[n].t       = b.time;
         g_ret[n].dir     = eng;
         g_ret[n].midPts  = limitPts;
         g_ret[n].slPts   = slPts;
         g_ret[n].seen    = 0;
         g_ret[n].filled  = false;
         g_ret[n].fillPts = 0;
         g_ret[n].bars    = 0;
         g_ret[n].rmfePct = 0;
         g_ret[n].rmaePct = 0;
         g_ret[n].hitSl   = false;
        }
     }
  }

//+---+
//| Advance every pending C3 outcome window with this closed bar.    |
//| Next-bar verdict (ICT C3 test) is decided by the IMMEDIATE next  |
//| candle only: cont = closes beyond the engulf close, rev = closes |
//| back through the engulf open, hold = inside the body. MFE/MAE    |
//| accumulate over the whole horizon, in % of the engulf range      |
//+---+
void OutcomeUpdate(const MqlRates &b)
  {
   int n = ArraySize(g_pend);
   int w = 0;
   for(int i = 0; i < n; i++)
     {
      g_pend[i].seen++;
      int hPts = (int)MathRound(b.high / _Point);
      int lPts = (int)MathRound(b.low / _Point);
      int cPts = (int)MathRound(b.close / _Point);
      double fav, adv;
      if(g_pend[i].dir > 0)
        {
         fav = 100.0 * (hPts - g_pend[i].cPts) / g_pend[i].rngPts;
         adv = 100.0 * (g_pend[i].cPts - lPts) / g_pend[i].rngPts;
        }
      else
        {
         fav = 100.0 * (g_pend[i].cPts - lPts) / g_pend[i].rngPts;
         adv = 100.0 * (hPts - g_pend[i].cPts) / g_pend[i].rngPts;
        }
      if(fav < 0.0) fav = 0.0;
      if(adv < 0.0) adv = 0.0;
      if(g_pend[i].mfePct < (int)MathRound(fav)) g_pend[i].mfePct = (int)MathRound(fav);
      if(g_pend[i].maePct < (int)MathRound(adv)) g_pend[i].maePct = (int)MathRound(adv);
      if(g_pend[i].seen == 1)
        {
         if(g_pend[i].dir > 0)
            g_pend[i].next = (cPts > g_pend[i].cPts) ? 1 :
                             ((cPts < g_pend[i].oPts) ? 3 : 2);
         else
            g_pend[i].next = (cPts < g_pend[i].cPts) ? 1 :
                             ((cPts > g_pend[i].oPts) ? 3 : 2);
        }
      if(g_pend[i].seen >= InpOutcomeBars)
        {
         g_statC3++;
         Log(1, "C3", StringFormat("%s | %s | next=%s mfe=%d%% mae=%d%% (%d bars)",
             TimeToString(g_pend[i].t, TIME_DATE | TIME_MINUTES),
             g_pend[i].dir > 0 ? "bull" : "bear",
             g_pend[i].next == 1 ? "cont" : (g_pend[i].next == 3 ? "rev" : "hold"),
             g_pend[i].mfePct, g_pend[i].maePct, InpOutcomeBars));
        }
      else
        {
         if(w != i)
            g_pend[w] = g_pend[i];
         w++;
        }
     }
   if(w != n)
      ArrayResize(g_pend, w);
  }

//+---+
//| Advance every pending retrace-entry window with this closed bar. |
//| Unfilled: a limit sits InpRetracePct into the body from the      |
//| close side; gap-through fills at the open (worse price). Filled: |
//| the NEXT InpOutcomeBars bars are measured from the fill in % of  |
//| risk (fill minus the engulf extreme); the fill bar's own range   |
//| is excluded — with OHLC-only data its intra-bar order is unknown |
//+---+
void RetraceUpdate(const MqlRates &b)
  {
   int n = ArraySize(g_ret);
   int w = 0;
   int hPts = (int)MathRound(b.high / _Point);
   int lPts = (int)MathRound(b.low / _Point);
   int oPts = (int)MathRound(b.open / _Point);
   for(int i = 0; i < n; i++)
     {
      if(!g_ret[i].filled)
        {
         g_ret[i].seen++;
         bool hit = false;
         if(g_ret[i].dir > 0)
           {
            if(oPts <= g_ret[i].midPts)      { g_ret[i].fillPts = oPts; hit = true; }
            else if(lPts <= g_ret[i].midPts) { g_ret[i].fillPts = g_ret[i].midPts; hit = true; }
           }
         else
           {
            if(oPts >= g_ret[i].midPts)      { g_ret[i].fillPts = oPts; hit = true; }
            else if(hPts >= g_ret[i].midPts) { g_ret[i].fillPts = g_ret[i].midPts; hit = true; }
           }
         if(hit)
            g_ret[i].filled = true;
         else if(g_ret[i].seen >= InpRetraceBars)
           {
            Log(1, "RET", StringFormat("%s | %s | nofill",
                TimeToString(g_ret[i].t, TIME_DATE | TIME_MINUTES),
                g_ret[i].dir > 0 ? "bull" : "bear"));
            continue;                        // window expired unfilled
           }
        }
      else
        {
         g_ret[i].bars++;
         int riskPts = (g_ret[i].dir > 0) ? (g_ret[i].fillPts - g_ret[i].slPts)
                                          : (g_ret[i].slPts - g_ret[i].fillPts);
         if(riskPts > 0)
           {
            double fav = (g_ret[i].dir > 0) ? 100.0 * (hPts - g_ret[i].fillPts) / riskPts
                                            : 100.0 * (g_ret[i].fillPts - lPts) / riskPts;
            double adv = (g_ret[i].dir > 0) ? 100.0 * (g_ret[i].fillPts - lPts) / riskPts
                                            : 100.0 * (hPts - g_ret[i].fillPts) / riskPts;
            if(fav < 0.0) fav = 0.0;
            if(adv < 0.0) adv = 0.0;
            if(g_ret[i].rmfePct < (int)MathRound(fav)) g_ret[i].rmfePct = (int)MathRound(fav);
            if(g_ret[i].rmaePct < (int)MathRound(adv)) g_ret[i].rmaePct = (int)MathRound(adv);
           }
         if((g_ret[i].dir > 0 && lPts <= g_ret[i].slPts) ||
            (g_ret[i].dir < 0 && hPts >= g_ret[i].slPts))
            g_ret[i].hitSl = true;
         if(g_ret[i].bars >= InpOutcomeBars)
           {
            Log(1, "RET", StringFormat("%s | %s | fill=%s | rmfe=%d%% rmae=%d%% sl=%d",
                TimeToString(g_ret[i].t, TIME_DATE | TIME_MINUTES),
                g_ret[i].dir > 0 ? "bull" : "bear",
                DoubleToString(g_ret[i].fillPts * _Point, _Digits),
                g_ret[i].rmfePct, g_ret[i].rmaePct, g_ret[i].hitSl ? 1 : 0));
            continue;                        // outcome complete
           }
        }
      if(w != i)
         g_ret[w] = g_ret[i];
      w++;
     }
   if(w != n)
      ArrayResize(g_ret, w);
  }

//+---+
//| Engulfing evaluation vs the previous closed bar, per the three   |
//| standard definitions. All require opposite colors (a doji        |
//| previous bar never engulfs). Returns 0 none / +1 bull / -1 bear; |
//| defs receives the matched-definition bitmask (bit0=D1 body,      |
//| bit1=D2 range-outside, bit2=D3 decisive)                         |
//+---+
int EngulfEval(const MqlRates &b, const MqlRates &p, int &defs)
  {
   defs = 0;
   bool bullPair = (p.close < p.open && b.close > b.open);   // bull after bear
   bool bearPair = (p.close > p.open && b.close < b.open);   // bear after bull
   if(!bullPair && !bearPair)
      return 0;

   double body   = MathAbs(b.close - b.open);
   double pbody  = MathAbs(p.close - p.open);
   double range  = b.high - b.low;
   double pRange = p.high - p.low;

   // D1 — body engulf: bar body strictly covers the previous body
   bool d1 = (body > pbody) &&
             ((bullPair && b.open <= p.close && b.close >= p.open) ||
              (bearPair && b.open >= p.close && b.close <= p.open));
   // D2 — range engulf ("outside bar"): high/low engulf previous high/low
   bool d2 = (range > pRange && b.high >= p.high && b.low <= p.low);
   // D3 — decisive engulf: D1 coverage + body >= 2/3 of the full range.
   // Integer-point math: exact at the boundary (body == 2/3*range fires
   // deterministically — float compare mislands knife-edge bars)
   int bodyPts  = (int)MathRound(body / _Point);
   int rangePts = (int)MathRound(range / _Point);
   bool d3 = (d1 && rangePts > 0 && bodyPts * 3 >= rangePts * 2);

   if(d1) defs |= 1;
   if(d2) defs |= 2;
   if(d3) defs |= 4;
   if(defs == 0)
      return 0;
   return bullPair ? 1 : -1;
  }

//+---+
//| Matched-definition bitmask as a journal/tooltip string ("1,3")   |
//+---+
string DefsMask(const int defs)
  {
   string s = "";
   if((defs & 1) != 0)
      s = "1";
   if((defs & 2) != 0)
      s += (s == "") ? "2" : ",2";
   if((defs & 4) != 0)
      s += (s == "") ? "3" : ",3";
   return s;
  }

//+---+
//| One closed M15 bar, in strict order: detect -> ratchet refs      |
//+---+
void ProcessBar(const MqlRates &r[], const int s, const int total)
  {
   TradeSimBarClose(r[s]);                  // tick-sim time exits at 16 bars
   RetraceUpdate(r[s]);                     // retrace-entry windows first
   OutcomeUpdate(r[s]);                     // C3 windows (bar s is a NEW bar for older pendings)
   EvaluateBar(r, s, total);                // uses references as of prev close
   UpdateDayExtremes(r[s]);                 // fold bar into day extremes
   MarkBrokenByClose(r[s]);                 // body-close breaks swing refs
   int p = s + InpSwingBars;                // pivot newly confirmed by bar s
   if(p + InpSwingBars <= total - 1)
      IfPivotAdd(r, p, total);
  }

//+------------------------------------------------------------------+
//| Chart markers                                                    |
//+------------------------------------------------------------------+
//+---+
//| Arrow for an engulfing bar (wide when it also swept a level).    |
//| Reject-bar arrows carry the bucket color: gold = A-grade (weak   |
//| close + small bar), magenta = momentum reject; bucketClr ==      |
//| clrNONE keeps the direction color (lime bull / orange-red bear)  |
//+---+
void DrawEngulfArrow(const MqlRates &b, const int dir, const int defs,
                     const bool swept, const color bucketClr, const string bucketTag)
  {
   if(!InpDrawArrows)
      return;
   string name = OBJ_PFX + "A_" + IntegerToString((long)b.time);
   if(ObjectFind(0, name) >= 0)
      return;
   double price = (dir > 0) ? b.low : b.high;
   if(!ObjectCreate(0, name, OBJ_ARROW, 0, b.time, price))
      return;
   ObjectSetInteger(0, name, OBJPROP_ARROWCODE, dir > 0 ? 233 : 234);
   ObjectSetInteger(0, name, OBJPROP_COLOR,
                    bucketClr != clrNONE ? bucketClr : (dir > 0 ? clrLimeGreen : clrOrangeRed));
   ObjectSetInteger(0, name, OBJPROP_WIDTH, swept ? 3 : 1);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, dir > 0 ? ANCHOR_TOP : ANCHOR_BOTTOM);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, EA_NAME + " " + (dir > 0 ? "bull" : "bear") +
                   " engulfing bar [defs=" + DefsMask(defs) + "]" +
                   (swept ? " + level sweep" : "") + bucketTag);
   ChartRedraw();
  }

//+---+
//| Silver dot at a confirmed swing pivot (above high / below low)   |
//+---+
void DrawSwingMarker(const MqlRates &r[], const int p, const bool isHigh)
  {
   if(!InpMarkSwings)
      return;
   string name = OBJ_PFX + (isHigh ? "PH_" : "PL_") + IntegerToString((long)r[p].time);
   if(ObjectFind(0, name) >= 0)
      return;
   double price = isHigh ? r[p].high : r[p].low;
   if(!ObjectCreate(0, name, OBJ_ARROW, 0, r[p].time, price))
      return;
   ObjectSetInteger(0, name, OBJPROP_ARROWCODE, 159);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clrSilver);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, isHigh ? ANCHOR_BOTTOM : ANCHOR_TOP);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, EA_NAME + " M15 swing " +
                   (isHigh ? "high" : "low") + " @ " + DoubleToString(price, _Digits) +
                   " (pivot " + TimeToString(r[p].time, TIME_DATE | TIME_MINUTES) + ")");
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Backfill scan on attach                                          |
//+---+
void Backfill()
  {
   g_lastClosed = 0;
   if(InpBackfillBars <= 0)
      return;

   MqlRates r[];
   ArraySetAsSeries(r, true);
   int want = InpBackfillBars + WarmupBars() + 8;
   int got  = CopyRates(_Symbol, WORK_TF, 0, want, r);
   if(got < 2 * InpSwingBars + 4)
     {
      Log(0, "INIT", StringFormat("backfill skipped — only %d M15 bars available", got));
      return;
     }
   int first = MathMin(InpBackfillBars, got - 2 * InpSwingBars - 2);
   if(first < 1)
     {
      Log(0, "INIT", "backfill skipped — history window too small");
      return;
     }

   // warm up references over the pre-window bars (no detections there)
   for(int s = got - 1; s > first; s--)
      UpdateDayExtremes(r[s]);
   for(int p = got - 1 - InpSwingBars; p >= first + InpSwingBars + 1; p--)
      IfPivotAdd(r, p, got);

   // process the backfill window, oldest -> newest
   for(int s = first; s >= 1; s--)
      ProcessBar(r, s, got);

   g_lastClosed = r[1].time;
   Log(0, "SUMMARY", StringFormat("backfill: %d closed M15 bars scanned -> %d engulf (d1=%d d2=%d d3=%d, %d with sweep) / %d level breaks",
       first, g_statEngulf, g_statEngulf1, g_statEngulf2, g_statEngulf3, g_statReject, g_statBroken));
  }

//+------------------------------------------------------------------+
//| Event handlers                                                   |
//+------------------------------------------------------------------+
//+---+
//| Validate inputs, echo settings, run the backfill scan            |
//+---+
int OnInit()
  {
   if(InpMinRangePoints < 0)
     {
      Log(0, "INIT", "ABORT: InpMinRangePoints must be >= 0");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpSwingBars < 1 || InpSwingBars > 10)
     {
      Log(0, "INIT", "ABORT: InpSwingBars must be 1..10");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpBackfillBars < 0 || InpBackfillBars > 5000)
     {
      Log(0, "INIT", "ABORT: InpBackfillBars must be 0..5000");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpLogLevel < 0 || InpLogLevel > 2)
     {
      Log(0, "INIT", "ABORT: InpLogLevel must be 0..2");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpAvgBodyBars < 0 || InpAvgBodyBars > 500)
     {
      Log(0, "INIT", "ABORT: InpAvgBodyBars must be 0..500");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpOutcomeBars < 0 || InpOutcomeBars > 100)
     {
      Log(0, "INIT", "ABORT: InpOutcomeBars must be 0..100");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpMomExpTenths < 0 || InpMomExpTenths > 100)
     {
      Log(0, "INIT", "ABORT: InpMomExpTenths must be 0..100");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpRetraceBars < 0 || InpRetraceBars > 100)
     {
      Log(0, "INIT", "ABORT: InpRetraceBars must be 0..100");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpRetracePct < 20 || InpRetracePct > 80)
     {
      Log(0, "INIT", "ABORT: InpRetracePct must be 20..80");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpWeakClosePct < 0 || InpWeakClosePct > 100)
     {
      Log(0, "INIT", "ABORT: InpWeakClosePct must be 0..100");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpBigExpTenths < 0 || InpBigExpTenths > 100)
     {
      Log(0, "INIT", "ABORT: InpBigExpTenths must be 0..100");
      return INIT_PARAMETERS_INCORRECT;
     }

   if(InpTradeRRTenths < 5 || InpTradeRRTenths > 50)
     {
      Log(0, "INIT", "ABORT: InpTradeRRTenths must be 5..50");
      return INIT_PARAMETERS_INCORRECT;
     }

   Log(0, "INIT", StringFormat("%s v%s | %s | minRange=%dpt swingN=%d swingLevels=%s dayExtreme=%s backfill=%d avgBody=%d outcomeBars=%d momMarker=%d weakClose=%d bigExp=%d retrace=%db/%d%% trd=%d rr=%d.%dR",
       EA_NAME, EA_VERSION, _Symbol, InpMinRangePoints, InpSwingBars,
       InpUseSwingLevels ? "on" : "off", InpUseDayExtreme ? "on" : "off", InpBackfillBars,
       InpAvgBodyBars, InpOutcomeBars, InpMomExpTenths, InpWeakClosePct, InpBigExpTenths,
       InpRetraceBars, InpRetracePct, InpTradeSim ? 1 : 0,
       InpTradeRRTenths / 10, InpTradeRRTenths % 10));

   Backfill();
   return INIT_SUCCEEDED;
  }

//+---+
//| New-bar pump: process every closed M15 bar exactly once          |
//+---+
void OnTick()
  {
   if(InpTradeSim)
      TradeSimTick();                       // tick-exact virtual trades (EVERY tick)

   datetime t1 = iTime(_Symbol, WORK_TF, 1);
   if(t1 == 0 || t1 == g_lastClosed)
      return;

   int oldest = 1;
   if(g_lastClosed > 0)
     {
      int shiftLast = iBarShift(_Symbol, WORK_TF, g_lastClosed, true);
      if(shiftLast > 1)
         oldest = shiftLast - 1;             // all closed bars newer than cursor
     }

   int look = MathMax(2 * InpSwingBars, InpAvgBodyBars);
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int got = CopyRates(_Symbol, WORK_TF, 0, oldest + look + 8, r);
   if(got < oldest + look + 2)
     {
      oldest = got - look - 2;               // clamp to what history allows
      if(oldest < 1)
         return;
     }

   for(int s = oldest; s >= 1; s--)
      ProcessBar(r, s, got);

   g_lastClosed = r[1].time;
  }

//+---+
//| Resolve open virtual trades against the current tick.            |
//| A tick crossing SL or TP closes the trade at the TICK price      |
//| (real stop/limit semantics — gaps fill at the gap). Same-tick    |
//| SL and TP is impossible at one price; SL checked first anyway    |
//+---+
void TradeSimTick()
  {
   int n = ArraySize(g_vt);
   if(n == 0)
      return;
   int pts = (int)MathRound(SymbolInfoDouble(_Symbol, SYMBOL_BID) / _Point);
   int w = 0;
   for(int i = 0; i < n; i++)
     {
      string ex = "";
      double r = 0.0;
      if(g_vt[i].dir > 0)
        {
         if(pts <= g_vt[i].slPts)      { ex = "SL"; r = 100.0 * (pts - g_vt[i].ePts) / g_vt[i].rkPts; }
         else if(pts >= g_vt[i].tpPts) { ex = "TP"; r = 100.0 * (pts - g_vt[i].ePts) / g_vt[i].rkPts; }
        }
      else
        {
         if(pts >= g_vt[i].slPts)      { ex = "SL"; r = 100.0 * (g_vt[i].ePts - pts) / g_vt[i].rkPts; }
         else if(pts <= g_vt[i].tpPts) { ex = "TP"; r = 100.0 * (g_vt[i].ePts - pts) / g_vt[i].rkPts; }
        }
      if(ex == "")
        {
         if(w != i)
            g_vt[w] = g_vt[i];
         w++;
         continue;
        }
      Log(1, "TRD", StringFormat("%s | %s | bkt=%s | e=%s sl=%s tp=%s rk=%d | exit=%s xp=%s r=%.2f b=%d",
          TimeToString(g_vt[i].t, TIME_DATE | TIME_MINUTES),
          g_vt[i].dir > 0 ? "bull" : "bear",
          g_vt[i].bkt == 0 ? "A" : (g_vt[i].bkt == 1 ? "M" : "O"),
          DoubleToString(g_vt[i].ePts * _Point, _Digits),
          DoubleToString(g_vt[i].slPts * _Point, _Digits),
          DoubleToString(g_vt[i].tpPts * _Point, _Digits),
          g_vt[i].rkPts, ex,
          DoubleToString(pts * _Point, _Digits), r, g_vt[i].bars));
     }
   if(w != n)
      ArrayResize(g_vt, w);
  }

//+---+
//| Time exit: flat after the 16th closed bar, at that bar's close   |
//| (mirrors the offline sim's TIME_EXIT=16 semantics)               |
//+---+
void TradeSimBarClose(const MqlRates &b)
  {
   if(!InpTradeSim)
      return;
   int n = ArraySize(g_vt);
   if(n == 0)
      return;
   int w = 0;
   int xp = (int)MathRound(b.close / _Point);
   for(int i = 0; i < n; i++)
     {
      g_vt[i].bars++;
      if(g_vt[i].bars >= 16)
        {
         double r = 100.0 * ((g_vt[i].dir > 0) ? (xp - g_vt[i].ePts)
                                               : (g_vt[i].ePts - xp)) / g_vt[i].rkPts;
         Log(1, "TRD", StringFormat("%s | %s | bkt=%s | e=%s sl=%s tp=%s rk=%d | exit=TIME xp=%s r=%.2f b=%d",
             TimeToString(g_vt[i].t, TIME_DATE | TIME_MINUTES),
             g_vt[i].dir > 0 ? "bull" : "bear",
             g_vt[i].bkt == 0 ? "A" : (g_vt[i].bkt == 1 ? "M" : "O"),
             DoubleToString(g_vt[i].ePts * _Point, _Digits),
             DoubleToString(g_vt[i].slPts * _Point, _Digits),
             DoubleToString(g_vt[i].tpPts * _Point, _Digits),
             g_vt[i].rkPts,
             DoubleToString(xp * _Point, _Digits), r, g_vt[i].bars));
         continue;
        }
      if(w != i)
         g_vt[w] = g_vt[i];
      w++;
     }
   if(w != n)
      ArrayResize(g_vt, w);
  }

//+---+
//| Remove our chart markers, log session totals                     |
//+---+
void OnDeinit(const int reason)
  {
   Log(0, "SUMMARY", StringFormat("session totals: %d engulf (d1=%d d2=%d d3=%d, %d with sweep) / %d level breaks / %d outcomes — removing markers",
       g_statEngulf, g_statEngulf1, g_statEngulf2, g_statEngulf3, g_statReject, g_statBroken, g_statC3));
   ObjectsDeleteAll(0, OBJ_PFX);
   ChartRedraw();
  }
//+------------------------------------------------------------------+
