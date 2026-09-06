# =============================================================================
# v12_Strategy.py
#
# Freqtrade IStrategy translation of "Adaptive_Regime_Strategy_Blueprint_v11".
#
# ⚠️ READ THIS BLOCK BEFORE YOU RUN IT. ⚠️
#
# This file is a best-effort, single-file mapping of a tick-level, options/
# futures intraday blueprint onto Freqtrade's candle- and iteration-driven
# IStrategy execution model. Several of the blueprint's own mechanisms do not
# have a native Freqtrade equivalent. Rather than silently approximate them,
# every substitution is flagged in a comment block headed "FIDELITY GAP" at
# the point where it happens, with (a) what the blueprint asked for, (b) what
# this file actually does, and (c) why they differ. Search this file for
# "FIDELITY GAP" to get the full list before you trade real capital on it.
#
# The five load-bearing gaps, summarized up front:
#
#   1. TIMING: custom_stoploss/custom_exit/confirm_trade_entry fire once per
#      bot-loop iteration (process_throttle_secs, realistic floor ~1s), not
#      on ticks. Phase 1's 15-30s tick-tracking and Phase 1.5's "1-2 polling
#      cycles" wait are approximated as wall-clock-elapsed-time state
#      machines checked each iteration. This is NOT the same guarantee as
#      the blueprint's tick-level polling.
#
#   2. 3-READING AVERAGING: Phase 0.3/0.4's "3-reading polling average" has
#      no raw-tick equivalent inside populate_indicators (candle-only data).
#      It is approximated as a 3-bar rolling mean of ADX/DI on your lowest
#      configured timeframe. This is a real substitution with different
#      statistical properties, not a formality.
#
#   3. STOPLOSS RATCHET (widen-vs-tighten) — RESOLVED AND FIXED against
#      freqtrade 2026.8 source (freqtradebot.py, trade_model.py,
#      interface.py, all read directly). Freqtrade's native "after order
#      filled" exception (allow_refresh=after_fill) DOES allow bidirectional
#      stop movement, but ONLY on a one-time call that fires at order-fill
#      time — i.e. at trade OPEN. Phase 3.5's checkpoint fires ~60s later,
#      on the regular per-iteration path, which is always after_fill=False.
#      So the previous version of this file's widen path was silently
#      unreachable: any genuine widen would have been re-tightened by
#      freqtrade's normal ratchet on the very next iteration. FIXED by
#      having this strategy track its own per-trade high-water mark
#      (`widest_sl_pts_seen`) and never return a ratio narrower than it —
#      see the comment on `_ft_stop_uses_after_fill` near the top of the
#      class body, and the "BUG FIX — widen enforcement" block inside
#      custom_stoploss(), for the full trace and the fix itself.
#
#   4. OPTIONS MECHANICS: Freqtrade's Trade object is underlying-instrument-
#      priced. There is no Delta, no premium layer, no per-strike order-book
#      depth. PARTIALLY RESOLVED 2026-09-06: Phase 0.6 (pre-entry Theta/IV
#      filter), Phase 2.6 (Delta-Translation), and Phase 3.5 Step 2 (IV-
#      Crush Override) now read LIVE Delta/Theta/IV from a real Delta
#      Exchange options contract (OPTIONS_SYMBOL_OVERRIDE — you must set
#      this; there is no auto-selection) via _delta_options_reading(), gated
#      behind OPTIONS_OVERLAY_ENABLED (default False). This bot still places
#      NO options orders and Active-SL/breakeven/trailing still operate
#      entirely in UNDERLYING POINTS throughout custom_stoploss — the
#      options data is a risk-sizing/logging OVERLAY, not a second position.
#      Phase 2.7 (Liquidity Gate / per-strike order-book depth) remains
#      UNRESOLVED — Delta's ticker endpoint used here does not expose order-
#      book depth, and this file still does not fake one. See the "OPTIONS
#      RISK OVERLAY" class-constant block and _delta_options_reading's own
#      docstring (search this file for "FIDELITY GAP #4 RESOLUTION") for the
#      full account of what changed, what didn't, and the honest limitations
#      of the local IV Rank approximation used in Phase 0.6.
#
#   5. KILL-SWITCH STATE: Phase 5's "two independent counters, 24hr window"
#      is implemented with in-memory per-pair dictionaries on the strategy
#      instance. These do NOT survive a bot restart. A commented-out stub
#      shows how you would rebuild the counters from Freqtrade's trade
#      history (Trade.get_trades()) on startup instead — not wired by
#      default, because persistence strategy is a config-adjacent decision
#      this file shouldn't force on you.
#
#   6. LEVERAGE: IStrategy's default leverage() hard-returns 1.0 and only
#      fires in futures mode — your stated use case. This file overrides it
#      as a pass-through of proposed_leverage/max_leverage (your config's own
#      values), because the blueprint's Phase 2 gives a position-size SPLIT
#      RATIO, not a leverage multiplier, and this file will not invent a
#      leverage number the blueprint doesn't specify. If you want the
#      blueprint's confidence tiers to also scale leverage, that's a
#      product decision for you to make explicitly in this method.
#
# Everything else (regime detection via ADX, direction confidence via DI,
# confirmation tiers, adaptive stop-loss sizing, the mid-trade checkpoint,
# profit trailing) is a direct logic port. "Untested" thresholds from the
# blueprint are kept as named class-level constants so you can tune them
# without hunting through method bodies — the blueprint itself flags almost
# every numeric threshold as untested, and this file preserves that honesty
# by making them visibly overridable rather than burying them as magic
# numbers.
#
# NOTE ON SCOPE: per your instruction, Section E5/E6's multi-file
# architecture is fully ignored. No config, no smc_math, no docker. This is
# strategy logic ONLY — it still needs a config.json with your pair,
# timeframe, stake, and exchange settings to actually run.
#
# =============================================================================
# 2026-09-05 AUDIT PASS — three additional defects found and fixed in this
# revision (full Verification Gauntlet re-run against this file; the
# earlier boot crash was in config.json's currency setting, NOT in this
# file, but this file was re-audited in full anyway rather than assumed
# clean by association):
#
#   A. NaN-poisoning guard added to volatility_ratio (populate_indicators).
#      atr / atr_baseline can divide by zero/NaN on the first
#      VOLATILITY_BASELINE_PERIOD-1 candles before the rolling window fills
#      — .clip() does NOT sanitize a NaN, and a NaN silently makes every
#      downstream ">" comparison permanently False with no error or log
#      line. Freqtrade's startup_candle_count=60 *should* prevent this from
#      ever reaching a live decision, but that's an assumption living in a
#      different file (config.json) — this file now defends itself instead
#      of silently trusting an external setting to protect it. See the
#      "NaN-GUARD" comment in populate_indicators below.
#
#   B. leverage() floor guard added — previously `min(proposed_leverage,
#      max_leverage)` had no protection against a malformed 0/negative
#      proposed_leverage from ever reaching a live order. Now floors at 1.0
#      and logs a warning if the floor had to be applied, so a malformed
#      upstream value fails loudly instead of quietly placing a leverage-0
#      or negative-leverage order.
#
#   C. _medium_tier_wait entries are now cleared on trade close
#      (confirm_trade_exit), mirroring how _active_sl_state already cleans
#      itself up. Previously, consumed wait-state dict entries were kept
#      forever, keyed by pair — a slow per-pair memory leak that is low-
#      impact for a single-pair StaticPairList (this deployment's actual
#      config) but would become a real unbounded leak if pair_whitelist is
#      ever expanded to more pairs later without this fix.
#
# None of these three were the cause of the boot-crash you saw in the logs
# (that was purely config.json's stake_currency/pair_whitelist mismatch with
# Delta Exchange, fixed separately) — these are defects that would have
# stayed silent until specific, harder-to-notice conditions occurred later.
# =============================================================================
# 2026-09-06 AUDIT PASS — one additional defect found and fixed, specifically
# while investigating why realized trade losses looked larger than the
# strategy's own stated stop-loss sizing should allow:
#
#   D. Phase 3.5 Step 2 (IV-Crush Protective-Override) was being silently
#      cancelled by the widen-enforcement fix from Gap #3 above, on almost
#      every trade where Step 2 fired. Trace: Gap #3's fix compares each
#      new active_sl_pts against a per-trade high-water mark
#      (widest_sl_pts_seen) and refuses to return anything narrower — that
#      logic is correct and necessary for Step 1 (volatility widens must
#      survive freqtrade's ratchet). But the previous code fed that SAME
#      comparison the value AFTER Step 2's 0.7x protective tighten had
#      already been applied. Any time Step 2 fired without an
#      independently-large widen from Step 1 (the overwhelmingly common
#      case), the post-tighten number came out smaller than the high-water
#      mark, and the widen-floor — unable to distinguish "Step 2
#      deliberately tightening" from "freqtrade trying to illegitimately
#      re-tighten a widen" — reverted it straight back to the wider,
#      entry-time stop. Step 2 fired, logged as fired, and then had zero
#      effect on the stop freqtrade actually enforced, on exactly the
#      trades (a fast adverse move already 20+ points in) where a faster
#      exit was the entire point of the mechanism.
#
#      FIXED by tracking Step 1's checkpoint_sl_pts and Step 2's tighten
#      factor separately (step2_multiplier) instead of pre-multiplying them:
#      the widen-floor now runs on the pre-Step-2 value only, and Step 2's
#      multiplier is applied AFTER that floor resolves — so a genuine
#      volatility widen still floors exactly as before, and Step 2 can now
#      actually reduce the delivered stop distance instead of being
#      discarded by the same mechanism meant to protect widens. The
#      Active-SL-at-exit value logged in confirm_trade_exit is updated to
#      match (state["resolved_active_sl_pts"]) so the exit log reports what
#      was actually enforced rather than the pre-fix figure. Full trace is
#      inline in custom_stoploss at the two "AUDIT FIX D" comment blocks.
#
#      This does not touch, and is not a substitute for reviewing,
#      config.json's stake_amount="unlimited" + max_open_trades=1 setting
#      (100% of balance in one trade) — that is documented in
#      RISK_AND_LIMITATIONS.md as a deliberate, explicit choice, not a bug,
#      and this fix leaves it exactly as configured. It also does not
#      change anything about the fixed BASE_SL_*_PTS / breakeven / trailing
#      / IV-crush thresholds being denominated in absolute underlying
#      points rather than a percentage of price — under this repo's current
#      config (StaticPairList, single pair, BTC/USDT:USDT only), that has
#      no cross-asset consequence; it would need re-examining before ever
#      adding a second, differently-priced pair to pair_whitelist.
# =============================================================================
# 2026-09-06 FEATURE ADDITION — Phase 1.5 SMC Structural Confirmation Gate.
#
# Added per a second blueprint doc you supplied
# ("smc-phase-1.5-confirmation-gate.md"), which extracts four functions from
# the `smart-money-concepts` package (github.com/joshyattridge/smart-money-
# concepts, commit 1b62fd6c41e1f508e7ed76831a039fa4c82d42f6, package
# v0.0.27, MIT License, Copyright (c) 2020 NeuralNine) and proposes gating
# Phase 1.5's Trending breakout entries on Smart Money Concepts structure:
# Fair Value Gaps (FVG), Break of Structure / Change of Character (BOS/
# CHoCH, built on swing_highs_lows), and Order Blocks (OB). The stated goal
# is to reject "retail-trap" breakouts — thin, one-sided pushes through a
# level with no real displacement behind them — by requiring at least
# SMC_MIN_CONFIRMATIONS of these 3 concepts to agree before a Trending
# breakout is allowed through, stacked ON TOP OF (not merged into) the
# existing ADX/DI/breakout-magnitude/tick-consistency tier gate above.
#
# All four functions (fvg, swing_highs_lows, bos_choch, ob) are ported
# below, immediately before the class body, near-verbatim from the
# blueprint doc — see that section's own comment block for the exact
# changes made (only: @classmethod -> plain function, since none of the
# four bodies ever reference `cls`) and for a real discrepancy this port
# caught between the blueprint doc's own PROSE description of the FVG
# function and what its CODE actually does — see "MITIGATEDINDEX
# CORRECTION" in that section.
#
# NEW FIDELITY GAP #7 — SMC STRUCTURE IS FORWARD-LOOKING; IT REPAINTS AT THE
# LIVE EDGE. This is the load-bearing caveat for this whole feature, stated
# here up front and again inline at the point of the code:
#
#   swing_highs_lows() (which bos_choch() and ob() both depend on) decides
#   whether candle i is a swing extreme by comparing it against a window
#   that extends `swing_length` candles AFTER i, not just before — that is
#   inherent to the algorithm, not an implementation slip (the blueprint
#   doc says so explicitly: "the most recent swing_length/2 bars are
#   provisional and will repaint as new candles arrive"). Consequence: at
#   the exact instant a breakout candle closes — the only moment this
#   strategy can act on it — zero future candles exist yet, so the
#   breakout candle itself can essentially never carry a settled BOS/CHoCH/
#   OB classification. This file does not paper over that with a fake
#   same-candle check. Instead, BOS/CHoCH/OB confirmation below is
#   evaluated as "was a same-direction, still-valid structural event
#   confirmed somewhere in the last SMC_STRUCTURE_LOOKBACK_BARS candles" —
#   corroborating recent structure, NOT a same-candle stamp of approval on
#   the specific breakout being gated. FVG is less affected (it only needs
#   ONE forward candle per gap, not a whole swing_length window) but the
#   breakout candle's OWN FVG is still unknowable at decision time for the
#   same reason, one candle short — handled by checking
#   SMC_FVG_LOOKBACK_BARS candles immediately BEHIND the breakout instead
#   of the breakout candle itself. See "PHASE 1.5 SMC EXTENSION" inside
#   populate_indicators below for the full trace, and bos_choch()'s own
#   additional retroactive-invalidation behavior (a confirmed BOS can later
#   be wiped out by a subsequent overlapping one — see that function's own
#   source comment, "if there are any unbroken bos or choch that started
#   before this one and ended after this one then remove them") for a
#   second, independent reason none of these columns are a stable, one-time
#   stamp you can cache and trust forever.
#
# SMC_GATE_ENABLED (new class constant, default True) turns this entire
# gate on/off without touching any other logic, specifically so you can A/B
# test Trending-entry frequency and quality with and without it before
# committing real capital to either configuration.
# =============================================================================
# 2026-09-06 AUDIT PASS (LIVE CRASH) — one additional defect found and fixed,
# surfaced by an actual paper-trading crash traceback (bot exited a LINK/
# USDT:USDT position via phase3_timebomb_exit, then died on the very next DB
# commit):
#
#   H. custom_stoploss's returned stop_ratio — and the entry_adaptive_sl_pts/
#      checkpoint_sl_pts values feeding it — were numpy.float64, not a
#      native Python float. Root cause: candle["volatility_ratio"] is read
#      from the analyzed dataframe in three places (confirm_trade_entry's
#      Medium-tier-wait freeze, and custom_stoploss's own live-value and
#      Phase 3.5 checkpoint branches), and a pandas Series scalar pulled
#      from a float64-dtype column is numpy.float64, not float. Every value
#      downstream in the chain (entry_adaptive_sl_pts -> active_sl_pts ->
#      candidate_price -> stop_ratio via stoploss_from_absolute()) inherits
#      that dtype, since numpy.float64 arithmetic contaminates the plain
#      Python floats it's combined with, not the other way round.
#
#      Freqtrade persists this method's return value into
#      trades.stop_loss_pct via SQLAlchemy/psycopg2. psycopg2 has no
#      adapter registered for numpy.float64, so on the UPDATE it fell back
#      to inlining repr(np.float64(-0.3238...)) as literal SQL text instead
#      of a bound parameter. Postgres then read the dot in "np.float64(...)"
#      as a schema-qualified identifier and raised psycopg2.errors.
#      InvalidSchemaName: schema "np" does not exist — which SQLAlchemy
#      escalated into a PendingRollbackError on the very next commit
#      attempt (freqtradebot.py's cleanup() handler), taking the whole
#      worker process down instead of just failing one update.
#
#      FIXED at the source: all three candle["volatility_ratio"] reads are
#      now wrapped in float() at the point they leave the dataframe, so
#      nothing downstream can inherit the numpy dtype regardless of which
#      path runs (live value vs. frozen Medium-tier-wait value, entry vs.
#      checkpoint). custom_stoploss's final return is also wrapped in
#      float() as defense-in-depth, matching this file's existing pattern
#      of guarding at both the source and the boundary (see the
#      NaN-poisoning guard and leverage() floor guard above). That
#      return-site cast alone would have stopped this specific crash, but
#      not the same numpy.float64 also reaching _push_dashboard_webhook's
#      JSON payload (json.dumps cannot serialize numpy.float64 either) if
#      you ever flip DASHBOARD_WEBHOOK_ENABLED on — the source-level fix
#      covers that path too.
# =============================================================================

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import requests  # ADDED 2026-09-06: FIDELITY GAP #4 resolution (Delta
                  # Exchange options data client) and the Phase 3.5/entry/
                  # exit dashboard webhook both need blocking HTTP calls.
                  # See DeltaOptionsClient and _push_dashboard_webhook below
                  # for why these are blocking, not async, and what that
                  # costs the bot-loop iteration.
import pandas as pd  # ADDED 2026-09-06: the ported SMC functions below
                      # (fvg/swing_highs_lows/bos_choch/ob) call pd.Series(...)
                      # and pd.concat(...) throughout their bodies; only
                      # `from pandas import DataFrame` existed before, which
                      # would have raised NameError: name 'pd' is not defined
                      # on the very first populate_indicators call.
import talib.abstract as ta
from pandas import DataFrame, Series  # Series ADDED: the ported functions'
                                       # own `-> Series` return-type
                                       # annotations need this name in scope
                                       # at function-definition time, even
                                       # though (see the SMC EXTRACTION
                                       # section below) what they actually
                                       # return is a multi-column DataFrame.

from freqtrade.strategy import (
    IStrategy,
    Trade,
    stoploss_from_absolute,
)

logger = logging.getLogger(__name__)


# =============================================================================
# SMC EXTRACTION — Phase 1.5 Structural Confirmation Gate.
# Ported from smartmoneyconcepts/smc.py (github.com/joshyattridge/smart-
# money-concepts, commit 1b62fd6c41e1f508e7ed76831a039fa4c82d42f6, package
# v0.0.27, MIT License, Copyright (c) 2020 NeuralNine), via the
# smc-phase-1.5-confirmation-gate.md reference doc you supplied. See the
# "2026-09-06 FEATURE ADDITION" block in the file header for what this is
# FOR and FIDELITY GAP #7 for what it does NOT guarantee.
#
# PORTING NOTES (what changed vs. the reference doc's code, and why):
#
#   - @classmethod -> plain module-level function, `cls` parameter dropped.
#     None of the four bodies below ever reference `cls` — the source
#     library only uses @classmethod to hang these on a shared `SMC`
#     namespace class for its own public API; there is no actual class-
#     level state involved, so this changes nothing about behavior. It
#     also sidesteps needing "the two small decorators the class depends
#     on" mentioned in the reference doc's companion .py file, which was
#     not supplied to this session — the doc's OWN code block for each
#     function shows no decorator besides @classmethod, so nothing else
#     was actually load-bearing for these four functions specifically.
#
#   - Return-type annotations kept exactly as `-> Series`, even though
#     every one of these functions actually returns a multi-column
#     DataFrame (via pd.concat). This is a pre-existing quirk of the
#     upstream library's own annotations, not something introduced here —
#     left as-is rather than silently "corrected," consistent with
#     treating this as a verified port of reviewed third-party source, not
#     a rewrite.
#
#   - MITIGATEDINDEX CORRECTION (a real discrepancy this port caught): the
#     reference doc's own PROSE says to gate on FVG "still unmitigated
#     (MitigatedIndex is NaN)". Reading fvg()'s actual code below line by
#     line: `mitigated_index` starts as `np.zeros(...)` and is ONLY
#     overwritten (to the mitigating candle's index) inside
#     `if np.any(mask):` — i.e. only once a real mitigation is found. If a
#     gap never gets mitigated in the available data, its MitigatedIndex
#     stays at its initialized value of 0 forever, and the function's
#     closing line (`np.where(np.isnan(fvg), np.nan, mitigated_index)`)
#     only inserts NaN where FVG ITSELF is NaN (no gap at all) — it never
#     touches an unmitigated-but-real gap's MitigatedIndex back to NaN. So
#     for any row where a gap exists (FVG is 1 or -1), "still open" is
#     `MitigatedIndex == 0`, NOT `MitigatedIndex` being NaN — a real
#     mitigation index can never legitimately come out as 0 (it's always
#     `j = np.argmax(mask) + i + 2 >= i + 2 >= 2`), so 0 can't be confused
#     with a genuine mitigation. This file's gate logic in
#     populate_indicators below uses the CORRECT test
#     (`(FVG == direction) & (MitigatedIndex == 0)`) — if you reuse these
#     functions elsewhere, don't copy the doc's "MitigatedIndex is NaN"
#     phrasing literally.
#
#   - Both `bos_choch()` and `ob()` take a parameter also named
#     `swing_highs_lows`, which shadows this module's own
#     `swing_highs_lows()` function by name inside those two functions'
#     bodies. This is exactly as given in the source and is harmless —
#     neither function calls the module-level `swing_highs_lows()` itself;
#     each only consumes its pre-computed OUTPUT, passed in by the caller
#     (see populate_indicators below). Flagged so it doesn't read as a bug
#     on a future pass.
#
#   - `ob()`'s body also has a local variable named `ob`, shadowing the
#     function's own name inside its own body. Also exactly as given, also
#     harmless — there's no recursion here, so a function has no need to
#     reference its own name from inside itself.
#
#   - All four functions assume `ohlc` has a plain 0..N-1 positional index
#     — they slice with bracket notation like `ohlc["low"][i + 2 :]`,
#     which is LABEL-based on the Series' own index, and only matches
#     "from position i+2 onward" if that index already IS 0..N-1.
#     Freqtrade dataframes are exactly that by default and nothing else in
#     this file re-indexes them, but populate_indicators below still
#     defensively `.reset_index(drop=True)`s a throwaway copy before
#     calling into these functions, so this dependency can never be
#     silently violated by some future upstream change elsewhere in the
#     pipeline.
# =============================================================================


def fvg(ohlc: DataFrame, join_consecutive=False) -> Series:
    """
    FVG - Fair Value Gap
    A fair value gap is when the previous high is lower than the next low if the current candle is bullish.
    Or when the previous low is higher than the next high if the current candle is bearish.

    parameters:
    join_consecutive: bool - if there are multiple FVG in a row then they will be merged into one using the highest top and the lowest bottom

    returns:
    FVG = 1 if bullish fair value gap, -1 if bearish fair value gap
    Top = the top of the fair value gap
    Bottom = the bottom of the fair value gap
    MitigatedIndex = the index of the candle that mitigated the fair value gap
    """

    fvg = np.where(
        (
            (ohlc["high"].shift(1) < ohlc["low"].shift(-1))
            & (ohlc["close"] > ohlc["open"])
        )
        | (
            (ohlc["low"].shift(1) > ohlc["high"].shift(-1))
            & (ohlc["close"] < ohlc["open"])
        ),
        np.where(ohlc["close"] > ohlc["open"], 1, -1),
        np.nan,
    )

    top = np.where(
        ~np.isnan(fvg),
        np.where(
            ohlc["close"] > ohlc["open"],
            ohlc["low"].shift(-1),
            ohlc["low"].shift(1),
        ),
        np.nan,
    )

    bottom = np.where(
        ~np.isnan(fvg),
        np.where(
            ohlc["close"] > ohlc["open"],
            ohlc["high"].shift(1),
            ohlc["high"].shift(-1),
        ),
        np.nan,
    )

    # if there are multiple consecutive fvg then join them together using the highest top and lowest bottom and the last index
    if join_consecutive:
        for i in range(len(fvg) - 1):
            if fvg[i] == fvg[i + 1]:
                top[i + 1] = max(top[i], top[i + 1])
                bottom[i + 1] = min(bottom[i], bottom[i + 1])
                fvg[i] = top[i] = bottom[i] = np.nan

    mitigated_index = np.zeros(len(ohlc), dtype=np.int32)
    for i in np.where(~np.isnan(fvg))[0]:
        mask = np.zeros(len(ohlc), dtype=np.bool_)
        if fvg[i] == 1:
            mask = ohlc["low"][i + 2 :] <= top[i]
        elif fvg[i] == -1:
            mask = ohlc["high"][i + 2 :] >= bottom[i]
        if np.any(mask):
            j = np.argmax(mask) + i + 2
            mitigated_index[i] = j

    mitigated_index = np.where(np.isnan(fvg), np.nan, mitigated_index)

    return pd.concat(
        [
            pd.Series(fvg, name="FVG"),
            pd.Series(top, name="Top"),
            pd.Series(bottom, name="Bottom"),
            pd.Series(mitigated_index, name="MitigatedIndex"),
        ],
        axis=1,
    )


def swing_highs_lows(ohlc: DataFrame, swing_length: int = 50) -> Series:
    """
    Swing Highs and Lows
    A swing high is when the current high is the highest high out of the swing_length amount of candles before and after.
    A swing low is when the current low is the lowest low out of the swing_length amount of candles before and after.

    parameters:
    swing_length: int - the amount of candles to look back and forward to determine the swing high or low

    returns:
    HighLow = 1 if swing high, -1 if swing low
    Level = the level of the swing high or low
    """

    swing_length *= 2
    # set the highs to 1 if the current high is the highest high in the last 5 candles and next 5 candles
    swing_highs_lows = np.where(
        ohlc["high"]
        == ohlc["high"].shift(-(swing_length // 2)).rolling(swing_length).max(),
        1,
        np.where(
            ohlc["low"]
            == ohlc["low"].shift(-(swing_length // 2)).rolling(swing_length).min(),
            -1,
            np.nan,
        ),
    )

    while True:
        positions = np.where(~np.isnan(swing_highs_lows))[0]

        if len(positions) < 2:
            break

        current = swing_highs_lows[positions[:-1]]
        next = swing_highs_lows[positions[1:]]

        highs = ohlc["high"].iloc[positions[:-1]].values
        lows = ohlc["low"].iloc[positions[:-1]].values

        next_highs = ohlc["high"].iloc[positions[1:]].values
        next_lows = ohlc["low"].iloc[positions[1:]].values

        index_to_remove = np.zeros(len(positions), dtype=bool)

        consecutive_highs = (current == 1) & (next == 1)
        index_to_remove[:-1] |= consecutive_highs & (highs < next_highs)
        index_to_remove[1:] |= consecutive_highs & (highs >= next_highs)

        consecutive_lows = (current == -1) & (next == -1)
        index_to_remove[:-1] |= consecutive_lows & (lows > next_lows)
        index_to_remove[1:] |= consecutive_lows & (lows <= next_lows)

        if not index_to_remove.any():
            break

        swing_highs_lows[positions[index_to_remove]] = np.nan

    positions = np.where(~np.isnan(swing_highs_lows))[0]

    if len(positions) > 0:
        if swing_highs_lows[positions[0]] == 1:
            swing_highs_lows[0] = -1
        if swing_highs_lows[positions[0]] == -1:
            swing_highs_lows[0] = 1
        if swing_highs_lows[positions[-1]] == -1:
            swing_highs_lows[-1] = 1
        if swing_highs_lows[positions[-1]] == 1:
            swing_highs_lows[-1] = -1

    level = np.where(
        ~np.isnan(swing_highs_lows),
        np.where(swing_highs_lows == 1, ohlc["high"], ohlc["low"]),
        np.nan,
    )

    return pd.concat(
        [
            pd.Series(swing_highs_lows, name="HighLow"),
            pd.Series(level, name="Level"),
        ],
        axis=1,
    )


def bos_choch(
    ohlc: DataFrame, swing_highs_lows: DataFrame, close_break: bool = True
) -> Series:
    """
    BOS - Break of Structure
    CHoCH - Change of Character
    these are both indications of market structure changing

    parameters:
    swing_highs_lows: DataFrame - provide the dataframe from the swing_highs_lows function
    close_break: bool - if True then the break of structure will be mitigated based on the close of the candle otherwise it will be the high/low.

    returns:
    BOS = 1 if bullish break of structure, -1 if bearish break of structure
    CHOCH = 1 if bullish change of character, -1 if bearish change of character
    Level = the level of the break of structure or change of character
    BrokenIndex = the index of the candle that broke the level
    """

    swing_highs_lows = swing_highs_lows.copy()

    level_order = []
    highs_lows_order = []

    bos = np.zeros(len(ohlc), dtype=np.int32)
    choch = np.zeros(len(ohlc), dtype=np.int32)
    level = np.zeros(len(ohlc), dtype=np.float32)

    last_positions = []

    for i in range(len(swing_highs_lows["HighLow"])):
        if not np.isnan(swing_highs_lows["HighLow"][i]):
            level_order.append(swing_highs_lows["Level"][i])
            highs_lows_order.append(swing_highs_lows["HighLow"][i])
            if len(level_order) >= 4:
                # bullish bos
                bos[last_positions[-2]] = (
                    1
                    if (
                        np.all(highs_lows_order[-4:] == [-1, 1, -1, 1])
                        and np.all(
                            level_order[-4]
                            < level_order[-2]
                            < level_order[-3]
                            < level_order[-1]
                        )
                    )
                    else 0
                )
                level[last_positions[-2]] = (
                    level_order[-3] if bos[last_positions[-2]] != 0 else 0
                )

                # bearish bos
                bos[last_positions[-2]] = (
                    -1
                    if (
                        np.all(highs_lows_order[-4:] == [1, -1, 1, -1])
                        and np.all(
                            level_order[-4]
                            > level_order[-2]
                            > level_order[-3]
                            > level_order[-1]
                        )
                    )
                    else bos[last_positions[-2]]
                )
                level[last_positions[-2]] = (
                    level_order[-3] if bos[last_positions[-2]] != 0 else 0
                )

                # bullish choch
                choch[last_positions[-2]] = (
                    1
                    if (
                        np.all(highs_lows_order[-4:] == [-1, 1, -1, 1])
                        and np.all(
                            level_order[-1]
                            > level_order[-3]
                            > level_order[-4]
                            > level_order[-2]
                        )
                    )
                    else 0
                )
                level[last_positions[-2]] = (
                    level_order[-3]
                    if choch[last_positions[-2]] != 0
                    else level[last_positions[-2]]
                )

                # bearish choch
                choch[last_positions[-2]] = (
                    -1
                    if (
                        np.all(highs_lows_order[-4:] == [1, -1, 1, -1])
                        and np.all(
                            level_order[-1]
                            < level_order[-3]
                            < level_order[-4]
                            < level_order[-2]
                        )
                    )
                    else choch[last_positions[-2]]
                )
                level[last_positions[-2]] = (
                    level_order[-3]
                    if choch[last_positions[-2]] != 0
                    else level[last_positions[-2]]
                )

            last_positions.append(i)

    broken = np.zeros(len(ohlc), dtype=np.int32)
    for i in np.where(np.logical_or(bos != 0, choch != 0))[0]:
        mask = np.zeros(len(ohlc), dtype=np.bool_)
        # if the bos is 1 then check if the candles high has gone above the level
        if bos[i] == 1 or choch[i] == 1:
            mask = ohlc["close" if close_break else "high"][i + 2 :] > level[i]
        # if the bos is -1 then check if the candles low has gone below the level
        elif bos[i] == -1 or choch[i] == -1:
            mask = ohlc["close" if close_break else "low"][i + 2 :] < level[i]
        if np.any(mask):
            j = np.argmax(mask) + i + 2
            broken[i] = j
            # if there are any unbroken bos or choch that started before this one and ended after this one then remove them
            for k in np.where(np.logical_or(bos != 0, choch != 0))[0]:
                if k < i and broken[k] >= j:
                    bos[k] = 0
                    choch[k] = 0
                    level[k] = 0

    # remove the ones that aren't broken
    for i in np.where(
        np.logical_and(np.logical_or(bos != 0, choch != 0), broken == 0)
    )[0]:
        bos[i] = 0
        choch[i] = 0
        level[i] = 0

    # replace all the 0s with np.nan
    bos = np.where(bos != 0, bos, np.nan)
    choch = np.where(choch != 0, choch, np.nan)
    level = np.where(level != 0, level, np.nan)
    broken = np.where(broken != 0, broken, np.nan)

    bos = pd.Series(bos, name="BOS")
    choch = pd.Series(choch, name="CHOCH")
    level = pd.Series(level, name="Level")
    broken = pd.Series(broken, name="BrokenIndex")

    return pd.concat([bos, choch, level, broken], axis=1)


def ob(
    ohlc: DataFrame,
    swing_highs_lows: DataFrame,
    close_mitigation: bool = False,
) -> Series:
    """
    OB - Order Blocks
    This method detects order blocks when there is a high amount of market orders exist on a price range.

    parameters:
    swing_highs_lows: DataFrame - provide the dataframe from the swing_highs_lows function
    close_mitigation: bool - if True then the order block will be mitigated based on the close of the candle otherwise it will be the high/low.

    returns:
    OB = 1 if bullish order block, -1 if bearish order block
    Top = top of the order block
    Bottom = bottom of the order block
    OBVolume = volume + 2 last volumes amounts
    Percentage = strength of order block (min(highVolume, lowVolume)/max(highVolume, lowVolume))
    """

    ohlc_len = len(ohlc)
    _open = ohlc["open"].values
    _high = ohlc["high"].values
    _low = ohlc["low"].values
    _close = ohlc["close"].values
    _volume = ohlc["volume"].values
    swing_hl = swing_highs_lows["HighLow"].values

    # Pre-allocate arrays
    crossed = np.full(ohlc_len, False, dtype=bool)
    ob = np.zeros(ohlc_len, dtype=np.int32)
    top_arr = np.zeros(ohlc_len, dtype=np.float32)
    bottom_arr = np.zeros(ohlc_len, dtype=np.float32)
    obVolume = np.zeros(ohlc_len, dtype=np.float32)
    lowVolume = np.zeros(ohlc_len, dtype=np.float32)
    highVolume = np.zeros(ohlc_len, dtype=np.float32)
    percentage = np.zeros(ohlc_len, dtype=np.float32)
    mitigated_index = np.zeros(ohlc_len, dtype=np.int32)
    breaker = np.full(ohlc_len, False, dtype=bool)

    # Precompute swing indices (assumed sorted)
    swing_high_indices = np.flatnonzero(swing_hl == 1)
    swing_low_indices = np.flatnonzero(swing_hl == -1)

    # List to track active bullish order blocks
    active_bullish = []
    for i in range(ohlc_len):
        close_index = i
        # Update existing bullish OB
        for idx in active_bullish.copy():
            if breaker[idx]:
                if _high[close_index] > top_arr[idx]:
                    # Reset this OB
                    ob[idx] = 0
                    top_arr[idx] = 0.0
                    bottom_arr[idx] = 0.0
                    obVolume[idx] = 0.0
                    lowVolume[idx] = 0.0
                    highVolume[idx] = 0.0
                    mitigated_index[idx] = 0
                    percentage[idx] = 0.0
                    active_bullish.remove(idx)
            else:
                if ((not close_mitigation and _low[close_index] < bottom_arr[idx])
                    or (close_mitigation and min(_open[close_index], _close[close_index]) < bottom_arr[idx])):
                    breaker[idx] = True
                    mitigated_index[idx] = close_index - 1

        # Find last swing high index less than current candle (using binary search)
        pos = np.searchsorted(swing_high_indices, close_index)
        last_top_index = swing_high_indices[pos - 1] if pos > 0 else None

        if last_top_index is not None:
            if _close[close_index] > _high[last_top_index] and not crossed[last_top_index]:
                crossed[last_top_index] = True
                # Initialise with default values from previous candle
                default_index = close_index - 1
                obBtm = _high[default_index]
                obTop = _low[default_index]
                obIndex = default_index
                # Look for a lower low between last_top_index and current candle
                if close_index - last_top_index > 1:
                    start = last_top_index + 1
                    end = close_index  # up to but not including close_index
                    if end > start:
                        segment = _low[start:end]
                        min_val = segment.min()
                        # In case of ties, take the last occurrence
                        candidates = np.nonzero(segment == min_val)[0]
                        if candidates.size:
                            candidate_index = start + candidates[-1]
                            obBtm = _low[candidate_index]
                            obTop = _high[candidate_index]
                            obIndex = candidate_index
                # Set bullish OB values
                ob[obIndex] = 1
                top_arr[obIndex] = obTop
                bottom_arr[obIndex] = obBtm
                vol_cur = _volume[close_index]
                vol_prev1 = _volume[close_index - 1] if close_index >= 1 else 0.0
                vol_prev2 = _volume[close_index - 2] if close_index >= 2 else 0.0
                obVolume[obIndex] = vol_cur + vol_prev1 + vol_prev2
                lowVolume[obIndex] = vol_prev2
                highVolume[obIndex] = vol_cur + vol_prev1
                max_vol = max(highVolume[obIndex], lowVolume[obIndex])
                percentage[obIndex] = (min(highVolume[obIndex], lowVolume[obIndex]) / max_vol * 100.0) if max_vol != 0 else 100.0
                active_bullish.append(obIndex)

    # List to track active bearish order blocks
    active_bearish = []
    for i in range(ohlc_len):
        close_index = i
        # Update existing bearish OB
        for idx in active_bearish.copy():
            if breaker[idx]:
                if _low[close_index] < bottom_arr[idx]:
                    ob[idx] = 0
                    top_arr[idx] = 0.0
                    bottom_arr[idx] = 0.0
                    obVolume[idx] = 0.0
                    lowVolume[idx] = 0.0
                    highVolume[idx] = 0.0
                    mitigated_index[idx] = 0
                    percentage[idx] = 0.0
                    active_bearish.remove(idx)
            else:
                if ((not close_mitigation and _high[close_index] > top_arr[idx])
                    or (close_mitigation and max(_open[close_index], _close[close_index]) > top_arr[idx])):
                    breaker[idx] = True
                    mitigated_index[idx] = close_index

        # Find last swing low index less than current candle
        pos = np.searchsorted(swing_low_indices, close_index)
        last_btm_index = swing_low_indices[pos - 1] if pos > 0 else None

        if last_btm_index is not None:
            if _close[close_index] < _low[last_btm_index] and not crossed[last_btm_index]:
                crossed[last_btm_index] = True
                default_index = close_index - 1
                obTop = _high[default_index]
                obBtm = _low[default_index]
                obIndex = default_index
                if close_index - last_btm_index > 1:
                    start = last_btm_index + 1
                    end = close_index
                    if end > start:
                        segment = _high[start:end]
                        max_val = segment.max()
                        candidates = np.nonzero(segment == max_val)[0]
                        if candidates.size:
                            candidate_index = start + candidates[-1]
                            obTop = _high[candidate_index]
                            obBtm = _low[candidate_index]
                            obIndex = candidate_index
                ob[obIndex] = -1
                top_arr[obIndex] = obTop
                bottom_arr[obIndex] = obBtm
                vol_cur = _volume[close_index]
                vol_prev1 = _volume[close_index - 1] if close_index >= 1 else 0.0
                vol_prev2 = _volume[close_index - 2] if close_index >= 2 else 0.0
                obVolume[obIndex] = vol_cur + vol_prev1 + vol_prev2
                lowVolume[obIndex] = vol_cur + vol_prev1
                highVolume[obIndex] = vol_prev2
                max_vol = max(highVolume[obIndex], lowVolume[obIndex])
                percentage[obIndex] = (min(highVolume[obIndex], lowVolume[obIndex]) / max_vol * 100.0) if max_vol != 0 else 100.0
                active_bearish.append(obIndex)

    # Convert zeros to NaN where OB was not set
    ob = np.where(ob != 0, ob, np.nan)
    top_arr = np.where(~np.isnan(ob), top_arr, np.nan)
    bottom_arr = np.where(~np.isnan(ob), bottom_arr, np.nan)
    obVolume = np.where(~np.isnan(ob), obVolume, np.nan)
    mitigated_index = np.where(~np.isnan(ob), mitigated_index, np.nan)
    percentage = np.where(~np.isnan(ob), percentage, np.nan)

    ob_series = pd.Series(ob, name="OB")
    top_series = pd.Series(top_arr, name="Top")
    bottom_series = pd.Series(bottom_arr, name="Bottom")
    obVolume_series = pd.Series(obVolume, name="OBVolume")
    mitigated_index_series = pd.Series(mitigated_index, name="MitigatedIndex")
    percentage_series = pd.Series(percentage, name="Percentage")

    return pd.concat(
        [
            ob_series,
            top_series,
            bottom_series,
            obVolume_series,
            mitigated_index_series,
            percentage_series,
        ],
        axis=1,
    )


class v12_Strategy(IStrategy):
    """
    Single-file Freqtrade translation of Adaptive_Regime_Strategy_Blueprint_v11.

    Phase -> Freqtrade hook map (per your explicit request):
        Phase 0   (Regime/ADX)              -> populate_indicators
        Phase 0.3 (Direction Confidence/DI)  -> populate_indicators
        Phase 1   (Entry Logic)              -> populate_entry_trend
        Phase 1.5 (Confirmation Gate tiers)  -> populate_entry_trend + confirm_trade_entry
        Phase 2.5 (Adaptive Stop-Loss)       -> custom_stoploss
        Phase 3.5 (Mid-Trade Checkpoint)     -> custom_stoploss
        Phase 4   (Profit Trailing)          -> custom_stoploss
        Phase 5   (Kill-Switch)              -> confirm_trade_entry (blocks new
                                                 entries) + custom_exit (logs)
    """

    # -------------------------------------------------------------------
    # Freqtrade interface requirements
    # -------------------------------------------------------------------
    INTERFACE_VERSION = 3
    timeframe = "1m"  # Lowest reasonable Freqtrade timeframe. See FIDELITY
                       # GAP #1 above — this is NOT tick data.

    # We manage stoploss entirely ourselves (Phase 2.5 / 3.5, returned as a
    # ratio via stoploss_from_absolute() — see custom_stoploss), so these
    # are permissive placeholders, not the real risk control. The REAL stop
    # is computed and enforced in custom_stoploss.
    stoploss = -0.99
    trailing_stop = False  # Freqtrade's native trailing is bypassed; Phase 4
                            # trailing is hand-rolled in custom_stoploss so it
                            # can share the Active-SL resolution logic instead
                            # of running as an independent, conflicting layer.

    use_custom_stoploss = True

    # ⚠️ VERIFIED AGAINST INSTALLED FREQTRADE SOURCE — see resolution below.
    #
    # This flag was previously UNVERIFIED. It has now been checked directly
    # against freqtrade 2026.8's actual source (freqtradebot.py,
    # trade_model.py, interface.py) rather than paraphrase or docs alone.
    # Resolution:
    #
    #   - custom_stoploss() is called from exactly two places:
    #     (1) ft_stoploss_reached() -> ft_stoploss_adjust(), every bot-loop
    #         iteration, ALWAYS with after_fill=False.
    #     (2) freqtradebot.py's _update_trade_after_fill(), ONCE, at the
    #         moment an order fills (entry fill, partial fill, etc.),
    #         ALWAYS with after_fill=True.
    #   - trade.adjust_stop_loss(..., allow_refresh=after_fill) is what
    #     gates bidirectional movement (trade_model.py). So allow_refresh
    #     is only ever True on that one-time, fill-triggered call — never
    #     on the regular per-iteration path.
    #   - freqtrade's stable-docs "only walks up" statement is NOT wrong;
    #     it correctly describes the per-iteration path. The after-fill
    #     exception is real but narrower than that docs page implies.
    #
    # CONSEQUENCE FOR THIS STRATEGY (a real bug, not just a caveat):
    # Phase 3.5's checkpoint is designed to fire ~60s AFTER entry, checked
    # on the regular per-iteration path (custom_stoploss called every loop
    # tick) — which is ALWAYS after_fill=False, per the trace above. This
    # flag being True does nothing for the checkpoint's widen case: by the
    # time 60s have passed, the one-time after-fill call already happened
    # (at trade open) and won't fire again for this trade. Practically,
    # this means Phase 3.5 Step 1 could only ever TIGHTEN the stop in the
    # previous version of this file, never genuinely WIDEN it — the widen
    # path was silently dead code, even with this flag set.
    #
    # FIX APPLIED: rather than relying on freqtrade's native ratchet
    # exception (which cannot reach a 60s-delayed checkpoint), this class's
    # custom_stoploss() now tracks its own "high-water mark" of the
    # widest active_sl_pts computed so far per trade (see
    # `state["widest_sl_pts_seen"]` below) and always returns a ratio
    # computed against THAT figure, never a narrower one, regardless of
    # what freqtrade's own ratchet would have allowed. This makes widening
    # work by construction — the strategy itself remembers "the loosest
    # stop we've legitimately computed" and never asks freqtrade to
    # tighten below it — rather than depending on an after-fill window
    # that structurally cannot align with a 60-second-delayed checkpoint.
    # This flag is kept True anyway (harmless, and correct if you ever add
    # code that genuinely runs inside an after-fill call), but Phase 3.5's
    # widen behavior no longer depends on it.
    _ft_stop_uses_after_fill = True

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = True
    position_adjustment_enable = False

    # Freqtrade requires SOME minimal_roi; Phase 4/3 handle exits, so this is
    # set far out to avoid ROI silently overriding blueprint exit logic.
    minimal_roi = {"0": 10.0}

    startup_candle_count: int = 60

    can_short = True  # Phase 1's Direction Filter allows both sides.

    # =====================================================================
    # BLUEPRINT CONSTANTS
    # Every one of these is flagged "(untested)" in the source blueprint.
    # Kept as class attributes, not inline literals, so they're all visible
    # and overridable in one place instead of scattered magic numbers.
    # =====================================================================

    # --- Phase 0: Regime Detection --------------------------------------
    ADX_TRENDING_THRESHOLD = 25.0
    ADX_RANGING_THRESHOLD = 20.0
    # 20-25 is the Transition Zone -> skip. No backup range/HH-HL method is
    # implemented here: Freqtrade already guarantees ADX/DMI availability
    # every candle (no "ADX access down" scenario like the blueprint's
    # discretionary-execution backup path was written for), so the
    # blueprint's own backup-method branch is structurally inapplicable in
    # this environment and is intentionally omitted rather than faked.

    # --- Phase 0.3: Direction Confidence Zone ---------------------------
    DI_GAP_CONFIDENT_THRESHOLD = 4.0  # |+DI - -DI|, 3-reading avg

    # --- Phase 0.4: Reading-Consistency Rule ----------------------------
    READING_AVG_WINDOW = 3  # bars, see FIDELITY GAP #2

    # --- Phase 1.5: Final Confirmation Gate tiers -----------------------
    HIGH_TIER_ADX_MARGIN = 3.0
    HIGH_TIER_DI_MARGIN = 2.0
    HIGH_TIER_BREAKOUT_PTS = 0.5
    # Medium tier = margins positive (signal didn't outright fail) but below
    # High-tier thresholds. Low tier = margins barely-crossed-zero.
    MEDIUM_TIER_WAIT_CYCLES = 2  # "1-2 extra polling cycles" -> upper bound.
                                  # See FIDELITY GAP #1: this is iterations of
                                  # the bot loop, not ticks.

    # --- Phase 1.5 (SMC Extension): Structural Confirmation Gate --------
    # See "2026-09-06 FEATURE ADDITION" in the file header and FIDELITY
    # GAP #7 for what this does and does NOT guarantee. Every value below
    # is untested, same as everything else in this section — these are
    # new invented starting points, not blueprint-specified numbers (the
    # smc-phase-1.5-confirmation-gate.md doc never proposes concrete
    # thresholds; it names the concepts and leaves calibration to you).
    SMC_GATE_ENABLED = True  # False = the diagnostic columns below still
                              # populate normally, but they can never block
                              # a Trending entry — use this to A/B the
                              # tier-gate-only vs. tier-gate+SMC-gate
                              # configurations against each other.
    SMC_SWING_LENGTH = 5  # candles each side, fed to swing_highs_lows().
                            # Deliberately far below the smc library's own
                            # default of 50: every candle of swing_length
                            # is a candle of forward data this strategy
                            # cannot have yet at decision time (Gap #7), so
                            # this is a starting point for the smallest
                            # window that still produces a recognizable
                            # swing/BOS/OB structure, traded off against
                            # noisier, less structurally significant
                            # "swings" than the library's own tested
                            # default would produce. Re-tune this
                            # explicitly before trusting the gate.
    SMC_STRUCTURE_LOOKBACK_BARS = 15  # how far back (in candles) to scan
                            # for a still-valid, already-confirmed BOS or
                            # active Order Block. Kept at roughly
                            # 3x SMC_SWING_LENGTH: a settled swing needs
                            # ~SMC_SWING_LENGTH candles just to stop being
                            # provisional (Gap #7), and BOS/CHoCH's own
                            # break-confirmation needs an unbounded,
                            # variable number MORE candles on top of that —
                            # 3x is a heuristic margin, not a derived or
                            # guaranteed-sufficient figure.
    SMC_FVG_LOOKBACK_BARS = 2  # how many candles immediately BEHIND the
                            # breakout candle to scan for an unmitigated,
                            # same-direction FVG. Deliberately excludes the
                            # breakout candle's own (unshifted) FVG value —
                            # that value structurally cannot exist yet at
                            # decision time, per Gap #7 — only shift(1)..
                            # shift(SMC_FVG_LOOKBACK_BARS) are checked.
    SMC_OB_MIN_PERCENTAGE = 60.0  # ob()'s Percentage field (0-100,
                            # min(highVolume,lowVolume)/max(...)*100 across
                            # the 3 candles around the breakout that
                            # created the block) must clear this to count
                            # as a genuine, two-sided-enough push per the
                            # blueprint's "a low score flags a thin,
                            # one-sided push" framing.
    SMC_MIN_CONFIRMATIONS = 2  # of 3 (FVG, BOS, OB) required to pass the
                            # gate — the blueprint's own "2-of-3 (or all 3)"
                            # framing. Set to 3 for the strictest reading.

    # --- Phase 2: Position Sizing (Trending) ----------------------------
    SIZING_ADX_HIGH = 30.0
    SIZING_DI_GAP_MIN = 4.0

    # --- Phase 2.5: Adaptive Stop-Loss Sizing ---------------------------
    BASE_SL_TRENDING_PTS = 8.0
    BASE_SL_RANGING_PTS = 4.0
    VOLATILITY_RATIO_CLAMP_MIN = 0.5
    VOLATILITY_RATIO_CLAMP_MAX = 2.0
    ATR_PERIOD = 14
    VOLATILITY_BASELINE_PERIOD = 20  # rolling baseline window for ATR ratio

    # --- Phase 3: Capital Shield -----------------------------------------
    RANGING_BREAKEVEN_TRIGGER_PTS = 2.0
    RANGING_TIMEBOMB_SECONDS = 60  # adaptive, scaled by volatility ratio
    TRENDING_BREAKEVEN_TRIGGER_PTS = 4.5  # blueprint gives 4-5pt; midpoint
    TRENDING_TIMEBOMB_SECONDS = 105  # blueprint gives 90-120s; midpoint,
                                      # NOT adaptive (per blueprint text)

    # --- Phase 3.5: Mid-Trade Checkpoint (Trending only) ----------------
    CHECKPOINT_DELAY_SECONDS = 60
    IV_CRUSH_TRIGGER_PTS = 20.0  # underlying-point proxy, see FIDELITY GAP #4
    IV_CRUSH_TIGHTEN_MULTIPLIER = 0.7

    # --- Phase 4: Profit Trailing ----------------------------------------
    TRAILING_RANGING_PTS = 3.5  # blueprint gives 3-4pt; midpoint
    TRAILING_TRENDING_PTS = 7.5  # blueprint gives 7-8pt; midpoint

    # --- Phase 5: Kill-Switch --------------------------------------------
    KILL_SWITCH_CONSECUTIVE_SL = 3
    KILL_SWITCH_WINDOW_HOURS = 24

    # =====================================================================
    # OPTIONS RISK OVERLAY (Delta Exchange live data) — resolves FIDELITY
    # GAP #4, added 2026-09-06.
    #
    # SCOPE, STATED PLAINLY: this bot still trades BTC/ETH/etc PERPETUAL
    # FUTURES on Bybit (config.json's trading_mode/exchange are unchanged).
    # No options orders are placed by this file. What changed is that
    # Phase 0.6 (pre-entry filter), Phase 2.6 (Delta-translation of the
    # underlying-points stop into an options-premium figure for logging/
    # kill-switch purposes), and Phase 3.5 Step 2 (IV-crush override) now
    # read LIVE Delta, Theta, and IV from a real Delta Exchange BTC options
    # contract that you name below, instead of a fabricated Black-Scholes
    # calculation or an underlying-price proxy. This is a RISK-SIZING
    # OVERLAY on the futures trade, not a hedge, not a second position, and
    # not options trading — Delta Exchange is never sent an order by this
    # file.
    #
    # WHY NOT py_vollib: Delta Exchange's own /v2/tickers endpoint already
    # returns server-side-computed `greeks.delta`, `greeks.theta`, and
    # `quotes.ask_iv`/`quotes.bid_iv` for every options symbol, calculated
    # by Delta's own risk engine against their own volatility model. Layering
    # an independent py_vollib Black-Scholes-Merton calculation on top would
    # produce a SECOND, mechanically-guaranteed-to-disagree IV/Delta/Theta
    # for the same contract (different vol-surface assumptions, no shared
    # source of truth) with no principled way to decide which one governs a
    # real stop-loss. Given a choice between "the exchange's own number" and
    # "my own model of the exchange's own instrument," this file uses the
    # exchange's own number. If you want an independent cross-check later,
    # add it as a SEPARATE diagnostic path that logs disagreement — do not
    # feed a second IV source into the same sizing formula as the first.
    #
    # OPTIONS_SYMBOL_OVERRIDE: the ONE Delta Exchange options contract to
    # poll for this overlay, e.g. "C-BTC-70000-261225" (see Delta's own
    # symbology: <C or P>-BTC-<strike>-<expiry ddMMyy>). This file does NOT
    # auto-select a contract (nearest-expiry/ATM) — you must set this
    # yourself and keep it updated as the contract approaches expiry. Left
    # as None by default so the overlay FAILS LOUDLY (raises, does not
    # silently no-op) if you enable it without setting a real symbol —
    # consistent with this file's existing "fail loud, never silently
    # swallow a malformed value" standard (see the leverage() AUDIT FIX B/F
    # comments). Auto-resolution (nearest expiry, strike nearest spot) was
    # explicitly NOT added — that is a real design decision (rollover rule,
    # ATM-vs-OTM choice) this file will not make silently on your behalf.
    OPTIONS_OVERLAY_ENABLED = False  # explicit opt-in; False = every method
                                      # below in this section returns None/
                                      # no-ops instead of calling Delta at
                                      # all. Flip to True only after setting
                                      # OPTIONS_SYMBOL_OVERRIDE for real.
    OPTIONS_SYMBOL_OVERRIDE: Optional[str] = None  # e.g. "C-BTC-70000-261225"
    DELTA_EXCHANGE_API_BASE = "https://api.india.delta.exchange"  # public
                                      # /v2/tickers/{symbol} endpoint, NO
                                      # authentication required for this
                                      # data (confirmed against Delta's own
                                      # docs — this is public market data,
                                      # not account data). If you trade on
                                      # Delta Global rather than Delta
                                      # India, change this to your region's
                                      # base URL; api-key/secret are NOT
                                      # used anywhere in this section.
    OPTIONS_CACHE_TTL_SECONDS = 5.0  # custom_stoploss fires once per
                                      # bot-loop iteration per open trade
                                      # (realistically ~1s apart, per
                                      # FIDELITY GAP #1) — polling Delta on
                                      # every single call would burn the
                                      # weight-3 rate-limit budget fast and
                                      # add avoidable latency to every
                                      # iteration. This TTL means at most
                                      # one real HTTP call per this many
                                      # seconds per symbol; calls inside the
                                      # TTL window reuse the cached reading.
                                      # This is still a BLOCKING call on a
                                      # cache miss (Freqtrade calls these
                                      # hooks synchronously, not as
                                      # coroutines — an `async def` here
                                      # would return an un-awaited
                                      # coroutine object instead of running,
                                      # which Freqtrade would misuse as the
                                      # actual stoploss ratio). Tune down
                                      # for fresher data at the cost of more
                                      # calls/latency; tune up for the
                                      # reverse.
    OPTIONS_HTTP_TIMEOUT_SECONDS = 2.0  # short timeout so a slow/unreachable
                                      # Delta API stalls one bot-loop
                                      # iteration by at most this long, not
                                      # indefinitely.
    THETA_DECAY_BLOCK_PCT = 3.0  # Phase 0.6: block new entries if
                                      # abs(theta) / premium * 100 exceeds
                                      # this (blueprint's "Theta decay is
                                      # >3% of the premium").
    IV_RANK_BLOCK_THRESHOLD = 70.0  # Phase 0.6: block new entries if
                                      # IV Rank >= this. See the
                                      # `_delta_options_reading` docstring
                                      # for how IV Rank is computed here
                                      # (NOT provided directly by Delta's
                                      # ticker endpoint) and what it
                                      # approximates.
    IV_RANK_LOOKBACK_READINGS = 288  # rolling window (in NUMBER OF
                                      # `_delta_options_reading` CALLS, not
                                      # a fixed wall-clock span, since calls
                                      # are irregular — see the OPTIONS_CACHE_TTL
                                      # comment above) used to derive IV
                                      # Rank locally. 288 readings at one
                                      # every ~5s (OPTIONS_CACHE_TTL_SECONDS)
                                      # is roughly 24 minutes of history if
                                      # trades are frequent enough to keep
                                      # the cache populated continuously —
                                      # in practice, with this bot's ~60-105s
                                      # trade lifetimes (Phase 3 time-bomb),
                                      # actual wall-clock coverage will be
                                      # far patchier and dominated by
                                      # between-trade gaps. This is a
                                      # DELIBERATE APPROXIMATION, not
                                      # Delta's own IV Rank (Delta's ticker
                                      # endpoint does not expose one) — see
                                      # the docstring below for the honest
                                      # accounting of what this number does
                                      # and does not represent.
    IV_CRUSH_SPIKE_POINTS = 20.0  # Phase 3.5 Step 2 replacement: fire if
                                      # live options IV rises by this many
                                      # IV POINTS (e.g. 0.45 -> 0.65 is a
                                      # 20-point spike) between entry and
                                      # the 60s checkpoint. Matches the
                                      # blueprint's stated "+20 points"
                                      # language; NOTE this is a different
                                      # unit than the old IV_CRUSH_TRIGGER_PTS
                                      # (underlying price points) it
                                      # replaces for entries opened while
                                      # OPTIONS_OVERLAY_ENABLED — the old
                                      # underlying-points proxy is still
                                      # used as an automatic fallback if the
                                      # live IV reading is unavailable at
                                      # checkpoint time (see custom_stoploss
                                      # Step 2 below).

    # --- Dashboard webhook (entry / Phase 3.5 checkpoint / exit) ---------
    # Freqtrade's OWN webhook config-block (see config.json's new "webhook"
    # section) already covers generic entry/entry_fill/exit/exit_fill/
    # status notifications with a fixed field set filled via string.format
    # — that is a real, working mechanism and this file does not duplicate
    # it. What Freqtrade's native webhook CANNOT do is push the Phase 3.5
    # component-log fields (checkpoint_vol_ratio, final_checkpoint_sl,
    # iv_move_at_checkpoint, etc.) — those values exist only inside this
    # strategy's own state dicts, computed mid-trade inside custom_stoploss,
    # with no Freqtrade-native event or field name for them. This section
    # adds a SEPARATE, strategy-triggered POST specifically for those
    # component fields, fired at entry, at the Phase 3.5 checkpoint, and at
    # exit, so your dashboard can show the full component breakdown
    # Freqtrade's own webhook was never designed to carry.
    DASHBOARD_WEBHOOK_ENABLED = False  # explicit opt-in; see
                                      # DASHBOARD_WEBHOOK_URL below for why
                                      # this defaults off.
    DASHBOARD_WEBHOOK_URL: Optional[str] = None  # e.g.
                                      # "http://dashboard-receiver:5001/api/webhook"
                                      # inside the docker-compose network,
                                      # or a real external URL. Left as None
                                      # so this fails loudly rather than
                                      # POSTing to a placeholder IP that
                                      # was never a real service — see the
                                      # docker-compose.yml / dashboard
                                      # receiver this session also produced,
                                      # which is what this URL should point
                                      # at if you use it as-is.
    DASHBOARD_WEBHOOK_TIMEOUT_SECONDS = 1.5  # short and best-effort: a
                                      # slow/unreachable dashboard must
                                      # never be allowed to meaningfully
                                      # delay a live trading decision.

    def __init__(self, config: dict) -> None:
        super().__init__(config)

        # -----------------------------------------------------------------
        # In-memory state for mechanisms Freqtrade has no native slot for.
        # FIDELITY GAP #5: none of this survives a bot restart. See the
        # commented rebuild-from-trade-history stub at the bottom of the
        # Kill-Switch section for how you'd add persistence.
        # -----------------------------------------------------------------

        # Phase 1.5 Medium-tier wait state, keyed by pair. Anti-infinite-loop
        # safeguard per blueprint: a pair can only enter the Medium-tier wait
        # ONCE per signal lifecycle (tracked via 'consumed' flag).
        #
        # AUDIT FIX C (2026-09-05): entries used to live here forever once
        # created, even after being consumed. Now also tracks which trade id
        # (if any) a wait belongs to once one opens, so confirm_trade_exit
        # can clear it when that trade closes — see the cleanup call at the
        # bottom of confirm_trade_exit. For a single-pair StaticPairList
        # (this repo's actual config) the old behavior was one stale dict
        # key, effectively harmless; this fix matters the moment
        # pair_whitelist ever grows past one pair.
        self._medium_tier_wait: dict[str, dict] = {}

        # Phase 2.5 / 3.5 Active-SL resolution state, keyed by trade id.
        # Holds: entry_adaptive_sl, checkpoint_sl, final_checkpoint_sl,
        # checkpoint_done (bool), active_sl (the current single-source-of-
        # truth value per blueprint's Active-SL concept).
        self._active_sl_state: dict[int, dict] = {}

        # Phase 5 Kill-Switch: two independent consecutive-SL counters
        # (blueprint says "two independent counters" without naming their
        # split further in the sections provided; implemented per-pair here
        # since that is the dimension Freqtrade naturally exposes — adjust
        # if your two counters are meant to split some other way, e.g.
        # long-side vs short-side).
        self._sl_streak: dict[str, list[datetime]] = {}
        self._kill_switch_flagged: dict[str, bool] = {}

        # Options overlay: single shared HTTP session (connection reuse
        # across calls) and a TTL cache keyed by options symbol, holding
        # the last successful (reading, fetched_at) pair. See
        # OPTIONS_CACHE_TTL_SECONDS above for why this cache exists at all.
        self._options_http_session = requests.Session()
        self._options_reading_cache: dict[str, tuple[dict, float]] = {}
        # Rolling history of raw ask_iv readings per symbol, used ONLY to
        # derive the local IV_RANK approximation described on
        # IV_RANK_LOOKBACK_READINGS above. Bounded to that same length.
        self._options_iv_history: dict[str, list[float]] = {}
        # Per-trade snapshot of the options reading taken at ENTRY (first
        # custom_stoploss call for that trade), so Phase 3.5 Step 2 can
        # compare "IV now" against "IV at entry" rather than against some
        # other trade's most recent reading. Keyed by trade id, cleared in
        # confirm_trade_exit alongside the other per-trade state dicts.
        self._options_entry_snapshot: dict[int, dict] = {}

    # =====================================================================
    # PHASE 0 + 0.3: populate_indicators
    # Regime Detection (ADX) + Direction Confidence Zone (DI)
    # =====================================================================
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # --- Core DMI system: ADX, +DI, -DI ---------------------------
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["plus_di"] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe["minus_di"] = ta.MINUS_DI(dataframe, timeperiod=14)

        # FIDELITY GAP #2 (Phase 0.4, Reading-Consistency Rule):
        # The blueprint requires Phase 0, 0.3, 1, and 2 to all read from the
        # SAME 3-reading polling average, at the same timestamps, to make a
        # methodology mismatch structurally impossible. There is no raw tick
        # feed inside populate_indicators — only closed candles. We
        # approximate "3-reading average" as a 3-bar rolling mean of each
        # DMI output on this dataframe's timeframe. Every consumer below
        # (Phase 0 regime call, Phase 0.3 confidence call, Phase 1 entry,
        # Phase 2 sizing) reads from these SAME rolling-mean columns, so the
        # blueprint's "same timestamps, same methodology" invariant is
        # preserved AT THIS CANDLE RESOLUTION — it is not preserved at the
        # tick resolution the blueprint was written for.
        dataframe["adx_avg3"] = dataframe["adx"].rolling(
            window=self.READING_AVG_WINDOW
        ).mean()
        dataframe["plus_di_avg3"] = dataframe["plus_di"].rolling(
            window=self.READING_AVG_WINDOW
        ).mean()
        dataframe["minus_di_avg3"] = dataframe["minus_di"].rolling(
            window=self.READING_AVG_WINDOW
        ).mean()
        dataframe["di_gap_avg3"] = (
            dataframe["plus_di_avg3"] - dataframe["minus_di_avg3"]
        ).abs()

        # --- Phase 0: Regime classification -----------------------------
        # >25 Trending, <20 Ranging, 20-25 Transition Zone (skip).
        dataframe["regime"] = np.where(
            dataframe["adx_avg3"] > self.ADX_TRENDING_THRESHOLD,
            "trending",
            np.where(
                dataframe["adx_avg3"] < self.ADX_RANGING_THRESHOLD,
                "ranging",
                "transition",
            ),
        )

        # --- Phase 0.3: Direction Confidence Zone -----------------------
        # |+DI - -DI| (3-reading avg) >= 4 -> Confident, else Ambiguous/skip.
        dataframe["direction_confident"] = (
            dataframe["di_gap_avg3"] >= self.DI_GAP_CONFIDENT_THRESHOLD
        )
        dataframe["direction_bias"] = np.where(
            dataframe["plus_di_avg3"] > dataframe["minus_di_avg3"], "long", "short"
        )

        # --- Phase 2.5: Volatility Ratio (feeds Adaptive-SL in custom_stoploss) ---
        # Primary: ATR(14) vs 20-period rolling baseline of that same ATR.
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self.ATR_PERIOD)
        dataframe["atr_baseline"] = dataframe["atr"].rolling(
            window=self.VOLATILITY_BASELINE_PERIOD
        ).mean()

        # -----------------------------------------------------------------
        # AUDIT FIX A (2026-09-05) — NaN-GUARD.
        #
        # atr_baseline is 0 or NaN for the first
        # VOLATILITY_BASELINE_PERIOD-1 candles of any dataframe (the
        # rolling window hasn't filled yet), and can also be exactly 0.0 in
        # a genuinely flat/no-range market on some instruments. Dividing by
        # that produces NaN or inf. The previous version's .clip() call
        # does NOT sanitize a NaN — NaN survives .clip() unchanged, and
        # every later ">" or ">=" comparison against a NaN is silently
        # False in Python/NumPy, with no exception raised anywhere. In this
        # file that would show up as: entry_adaptive_sl_pts becomes NaN on
        # an early candle, and the widen-vs-tighten high-water-mark check
        # in custom_stoploss ("if active_sl_pts > state['widest_sl_pts_seen']")
        # would then permanently take the False branch for that trade —
        # freezing the stop-loss logic with no error and no log line
        # explaining why.
        #
        # Freqtrade's own startup_candle_count=60 (set below on this class)
        # is what's SUPPOSED to prevent any live decision from ever seeing
        # an unfilled rolling window — but that protection lives in a
        # different setting entirely, and this file previously had no
        # defense of its own if that assumption were ever violated (e.g. a
        # future edit accidentally lowers startup_candle_count without
        # realizing this dependency). Fixed by computing the ratio safely
        # here, replacing any resulting NaN/inf with a neutral ratio of 1.0
        # (i.e. "assume normal volatility" rather than silently propagating
        # a poisoned value), THEN clipping as before.
        # -----------------------------------------------------------------
        raw_ratio = dataframe["atr"] / dataframe["atr_baseline"]
        safe_ratio = raw_ratio.replace([np.inf, -np.inf], np.nan).fillna(1.0)
        dataframe["volatility_ratio"] = safe_ratio.clip(
            lower=self.VOLATILITY_RATIO_CLAMP_MIN,
            upper=self.VOLATILITY_RATIO_CLAMP_MAX,
        )

        # --- Phase 1: Entry-logic support columns -----------------------
        # Trending: breakout tracking. Ranging: bounce tracking off a local low.
        dataframe["rolling_low_20"] = dataframe["low"].rolling(window=20).min()
        dataframe["rolling_high_20"] = dataframe["high"].rolling(window=20).max()
        # Breakout magnitude: how far the close cleared the prior 20-bar high/low,
        # used both for entry trigger and as the (frozen, per blueprint Step B)
        # breakout-magnitude confirmation-tier input.
        dataframe["breakout_magnitude_long"] = (
            dataframe["close"] - dataframe["rolling_high_20"].shift(1)
        )
        dataframe["breakout_magnitude_short"] = (
            dataframe["rolling_low_20"].shift(1) - dataframe["close"]
        )
        # Tick-consistency has no candle-level equivalent (it is inherently a
        # sub-candle, tick-by-tick property in the blueprint). As an honest
        # proxy we use "did the last 3 candles close in the breakout
        # direction without reversal" — flagged here rather than silently
        # presented as equivalent to true tick consistency.
        dataframe["tick_consistency_long"] = (
            (dataframe["close"] > dataframe["open"])
            .rolling(window=3)
            .sum()
            == 3
        )
        dataframe["tick_consistency_short"] = (
            (dataframe["close"] < dataframe["open"])
            .rolling(window=3)
            .sum()
            == 3
        )

        # -----------------------------------------------------------------
        # PHASE 1.5 SMC EXTENSION (2026-09-06 feature addition): Fair Value
        # Gaps, swing-based Break of Structure / Change of Character, and
        # Order Blocks — see the "2026-09-06 FEATURE ADDITION" header block
        # and FIDELITY GAP #7 for what this is for and its load-bearing
        # repainting caveat. Computed here (not a separate method) so it
        # follows the same per-candle vectorized pattern as every other
        # indicator in this method, and so the gate columns below are
        # available to populate_entry_trend on the same dataframe pass.
        #
        # Defensive copy with a reset index: the four ported SMC functions
        # slice by bracket-notation position (e.g. ohlc["low"][i + 2 :]),
        # which is label-based and only matches "from position i+2" if the
        # Series' index is already a plain 0..N-1 range. Freqtrade
        # dataframes are exactly that by default, and nothing upstream in
        # THIS file changes it — but this copy makes that assumption
        # impossible to silently violate rather than trusting it forever.
        # -----------------------------------------------------------------
        _smc_input = dataframe[["open", "high", "low", "close", "volume"]].reset_index(
            drop=True
        )
        _smc_swing = swing_highs_lows(_smc_input, swing_length=self.SMC_SWING_LENGTH)
        _smc_bos = bos_choch(_smc_input, _smc_swing, close_break=True)
        _smc_ob = ob(_smc_input, _smc_swing, close_mitigation=False)
        _smc_fvg = fvg(_smc_input, join_consecutive=False)

        # Raw columns, assigned by position (.values) rather than by index,
        # so this is correct regardless of what index `dataframe` itself
        # carries — see the reset_index note above.
        dataframe["smc_swing_hl"] = _smc_swing["HighLow"].values
        dataframe["smc_swing_level"] = _smc_swing["Level"].values
        dataframe["smc_bos"] = _smc_bos["BOS"].values
        # smc_choch is stored for visibility only and intentionally NOT
        # wired into the pass/fail gate below: the blueprint doc's own
        # "Summary — stacking the gate" table lists FVG/BOS/OB as the 3
        # gate inputs and explicitly routes CHoCH as "a reversal warning,
        # not a breakout signal" rather than as a 4th confirmation or a
        # veto. Using it as a veto (e.g. blocking a long breakout if a
        # bearish CHoCH just printed) is a reasonable future extension but
        # is not what was specified here, so it isn't invented on your
        # behalf — the column is available if you want to add that later.
        dataframe["smc_choch"] = _smc_bos["CHOCH"].values
        dataframe["smc_ob"] = _smc_ob["OB"].values
        dataframe["smc_ob_percentage"] = _smc_ob["Percentage"].values
        dataframe["smc_fvg"] = _smc_fvg["FVG"].values
        dataframe["smc_fvg_mitigated_index"] = _smc_fvg["MitigatedIndex"].values

        # --- FVG confirmation: unmitigated, same-direction gap in the last
        # SMC_FVG_LOOKBACK_BARS candles BEHIND the breakout candle. Never
        # checks the breakout candle's own (unshifted) FVG value — that
        # value needs one candle of forward data that cannot exist yet at
        # decision time (Gap #7). MITIGATEDINDEX CORRECTION (see the SMC
        # EXTRACTION section above fvg()'s definition): "still open" is
        # `== 0`, not `.isna()`.
        smc_fvg_confirms_long = pd.Series(False, index=dataframe.index)
        smc_fvg_confirms_short = pd.Series(False, index=dataframe.index)
        for _smc_k in range(1, self.SMC_FVG_LOOKBACK_BARS + 1):
            _fvg_shift = dataframe["smc_fvg"].shift(_smc_k)
            _mit_shift = dataframe["smc_fvg_mitigated_index"].shift(_smc_k)
            smc_fvg_confirms_long = smc_fvg_confirms_long | (
                (_fvg_shift == 1) & (_mit_shift == 0)
            )
            smc_fvg_confirms_short = smc_fvg_confirms_short | (
                (_fvg_shift == -1) & (_mit_shift == 0)
            )
        dataframe["smc_fvg_confirms_long"] = smc_fvg_confirms_long
        dataframe["smc_fvg_confirms_short"] = smc_fvg_confirms_short

        # --- BOS confirmation: a same-direction BOS (never CHOCH — see the
        # comment on smc_choch above) confirmed anywhere in the last
        # SMC_STRUCTURE_LOOKBACK_BARS candles. This is corroborating recent
        # structure, NOT a same-candle stamp on this specific breakout —
        # see Gap #7. .astype(int) before .rolling(...).max() (not a bare
        # bool Series) so an under-filled window at the very start of a
        # dataframe returns a real 0/1 via min_periods=1 rather than a NaN
        # that .astype(bool) would silently turn into True (NaN is
        # truthy) — the same class of NaN-poisoning AUDIT FIX A already
        # guards against elsewhere in this file, just NaN-to-True instead
        # of NaN-to-always-False.
        dataframe["smc_bos_confirms_long"] = (
            (dataframe["smc_bos"] == 1)
            .astype(int)
            .rolling(self.SMC_STRUCTURE_LOOKBACK_BARS, min_periods=1)
            .max()
            .astype(bool)
        )
        dataframe["smc_bos_confirms_short"] = (
            (dataframe["smc_bos"] == -1)
            .astype(int)
            .rolling(self.SMC_STRUCTURE_LOOKBACK_BARS, min_periods=1)
            .max()
            .astype(bool)
        )

        # --- OB confirmation: an active (not yet invalidated back to NaN —
        # ob() re-derives this from scratch on every populate_indicators
        # call, so an already-invalidated block simply won't show as
        # 1/-1 anymore) order block, above the Percentage floor, present
        # anywhere in the last SMC_STRUCTURE_LOOKBACK_BARS candles. Same
        # corroborating-structure caveat and NaN-guard as BOS above.
        _smc_ob_bull_strong = (dataframe["smc_ob"] == 1) & (
            dataframe["smc_ob_percentage"] >= self.SMC_OB_MIN_PERCENTAGE
        )
        _smc_ob_bear_strong = (dataframe["smc_ob"] == -1) & (
            dataframe["smc_ob_percentage"] >= self.SMC_OB_MIN_PERCENTAGE
        )
        dataframe["smc_ob_confirms_long"] = (
            _smc_ob_bull_strong.astype(int)
            .rolling(self.SMC_STRUCTURE_LOOKBACK_BARS, min_periods=1)
            .max()
            .astype(bool)
        )
        dataframe["smc_ob_confirms_short"] = (
            _smc_ob_bear_strong.astype(int)
            .rolling(self.SMC_STRUCTURE_LOOKBACK_BARS, min_periods=1)
            .max()
            .astype(bool)
        )

        # --- Combine: the blueprint's "2-of-3 (or all 3)" gate. Purely a
        # binary pre-filter on top of the existing tier system below, NOT
        # a replacement for it and NOT itself tiered — see the "2026-09-06
        # FEATURE ADDITION" header block for why this file keeps the two
        # mechanisms separate rather than merging them.
        dataframe["smc_confirmation_count_long"] = (
            dataframe["smc_fvg_confirms_long"].astype(int)
            + dataframe["smc_bos_confirms_long"].astype(int)
            + dataframe["smc_ob_confirms_long"].astype(int)
        )
        dataframe["smc_confirmation_count_short"] = (
            dataframe["smc_fvg_confirms_short"].astype(int)
            + dataframe["smc_bos_confirms_short"].astype(int)
            + dataframe["smc_ob_confirms_short"].astype(int)
        )
        if self.SMC_GATE_ENABLED:
            dataframe["smc_gate_passed_long"] = (
                dataframe["smc_confirmation_count_long"] >= self.SMC_MIN_CONFIRMATIONS
            )
            dataframe["smc_gate_passed_short"] = (
                dataframe["smc_confirmation_count_short"] >= self.SMC_MIN_CONFIRMATIONS
            )
        else:
            # SMC_GATE_ENABLED=False: the diagnostic columns above still
            # populate normally (so you can inspect/tune before switching
            # this on), but the gate itself becomes a permissive no-op.
            dataframe["smc_gate_passed_long"] = True
            dataframe["smc_gate_passed_short"] = True

        return dataframe

    # =====================================================================
    # PHASE 1 + 1.5 (trigger half): populate_entry_trend
    # Entry Logic + first-pass Confirmation-Gate tier classification.
    #
    # NOTE: the FULL Phase 1.5 logic (including the Medium-tier wait and
    # Step A/B/C re-verification) cannot live entirely in
    # populate_entry_trend, because that method is only called once per
    # candle to produce a boolean signal column — it has no ability to wait
    # across iterations. The wait/re-verify state machine lives in
    # confirm_trade_entry() below, which Freqtrade calls once per bot
    # iteration for every candle where an entry signal is active, and CAN
    # legitimately delay/deny order placement. This split is the closest
    # honest match to "populate_entry_trend for Phase 1 and Phase 1.5" that
    # Freqtrade's hook boundaries allow.
    # =====================================================================
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["confirmation_tier"] = "none"

        # --- Phase 1 direction filter (applies to both regimes) --------
        # +DI > -DI, gap >= 4 -> Buy. -DI > +DI, gap >= 4 -> Sell.
        # Gap < 4 -> skip (Phase 0.3's direction_confident already encodes this).
        long_direction_ok = (
            (dataframe["plus_di_avg3"] > dataframe["minus_di_avg3"])
            & dataframe["direction_confident"]
        )
        short_direction_ok = (
            (dataframe["minus_di_avg3"] > dataframe["plus_di_avg3"])
            & dataframe["direction_confident"]
        )

        # --- Trending entries: breakout + tick-confirm -------------------
        trending = dataframe["regime"] == "trending"
        # Phase 2 sizing table also rejects ADX 25-29.99 with gap<4 at
        # Phase-1 already, per blueprint's table row 4 ("Phase 1 mein hi
        # reject") — enforced here via long_direction_ok / short_direction_ok
        # already requiring direction_confident (gap>=4).
        trend_long_trigger = (
            trending
            & long_direction_ok
            & (dataframe["breakout_magnitude_long"] > 0)
            & dataframe["tick_consistency_long"]
        )
        trend_short_trigger = (
            trending
            & short_direction_ok
            & (dataframe["breakout_magnitude_short"] > 0)
            & dataframe["tick_consistency_short"]
        )

        # --- Ranging entries: bounce off local low/high -------------------
        # "lowestPrice + 2pt bounce" for long; symmetric high-2pt for short.
        ranging = dataframe["regime"] == "ranging"
        ranging_long_trigger = (
            ranging
            & long_direction_ok
            & (dataframe["close"] >= dataframe["rolling_low_20"] + 2.0)
            & (dataframe["close"].shift(1) < dataframe["rolling_low_20"].shift(1) + 2.0)
        )
        ranging_short_trigger = (
            ranging
            & short_direction_ok
            & (dataframe["close"] <= dataframe["rolling_high_20"] - 2.0)
            & (dataframe["close"].shift(1) > dataframe["rolling_high_20"].shift(1) - 2.0)
        )

        dataframe.loc[trend_long_trigger | ranging_long_trigger, "enter_long"] = 1
        dataframe.loc[trend_short_trigger | ranging_short_trigger, "enter_short"] = 1

        # --- Phase 1.5: first-pass tier classification (Trending only) ---
        # The blueprint's tier table is explicitly Trending-specific
        # ("Ranging-trades ke liye yeh apply nahi hoti... Ranging apna alag
        # simpler-confirmation-path leti hai" — Phase 1.5 Step A note).
        # Ranging entries below are tagged 'high' unconditionally: this is a
        # SIMPLIFICATION, not a blueprint-specified rule — the blueprint
        # never defines a Ranging-side tier table, so no wait/skip gating is
        # applied to Ranging entries here. Flagged rather than invented.
        adx_margin = dataframe["adx_avg3"] - self.ADX_TRENDING_THRESHOLD
        di_margin = dataframe["di_gap_avg3"] - self.DI_GAP_CONFIDENT_THRESHOLD
        breakout_mag = np.where(
            trend_long_trigger,
            dataframe["breakout_magnitude_long"],
            dataframe["breakout_magnitude_short"],
        )
        tick_clean = np.where(
            trend_long_trigger,
            dataframe["tick_consistency_long"],
            dataframe["tick_consistency_short"],
        )

        is_high_tier = (
            (adx_margin >= self.HIGH_TIER_ADX_MARGIN)
            & (di_margin >= self.HIGH_TIER_DI_MARGIN)
            & (breakout_mag >= self.HIGH_TIER_BREAKOUT_PTS)
            & tick_clean
        )
        # Medium: margins positive (didn't fail Phase-1's own gates) but
        # short of High-tier thresholds. Low: this file treats "reached
        # populate_entry_trend at all" as already having cleared the
        # blueprint's Low-tier floor ("barely-crossed"), so anything that
        # trended-triggered but isn't High is classified Medium here, and
        # the Low/skip decision for genuinely-marginal cases is left to
        # Phase 1.5's Step-A/C re-verification in confirm_trade_entry,
        # which re-applies the full tier table on FRESH data before firing.
        dataframe.loc[
            (trend_long_trigger | trend_short_trigger) & is_high_tier,
            "confirmation_tier",
        ] = "high"
        dataframe.loc[
            (trend_long_trigger | trend_short_trigger) & ~is_high_tier,
            "confirmation_tier",
        ] = "medium"
        dataframe.loc[ranging_long_trigger | ranging_short_trigger, "confirmation_tier"] = "high"

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exits are handled entirely by custom_stoploss / custom_exit
        # (Phase 3 Capital Shield, Phase 3.5 Checkpoint, Phase 4 Trailing).
        # No signal-based exit columns are set here, matching the blueprint
        # (which never describes a "reverse signal exit" mechanism).
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    # =====================================================================
    # PHASE 1.5 (wait/re-verify half) + PHASE 5 (entry-blocking half):
    # confirm_trade_entry
    #
    # Freqtrade calls this once per bot iteration, for every pair with an
    # active buy/sell signal, immediately before order placement. This is
    # the ONLY hook that can legitimately delay an entry across iterations,
    # so it is where the Medium-tier wait and Step A/B/C re-verification
    # live, per FIDELITY GAP #1.
    # =====================================================================
    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> bool:

        # --- Phase 5 Kill-Switch: block new entries if flagged ------------
        # Blueprint: "Correlated-Failure Log Flag (diagnostic-only, action-
        # question explicitly open)". The blueprint is explicit that ACTION
        # on this flag is an open question, not a specified rule. Blocking
        # new entries here is a conservative default choice made for this
        # implementation, not something the blueprint mandates — flagged so
        # you can consciously decide whether you want a hard block or a
        # log-only diagnostic here.
        if self._kill_switch_flagged.get(pair, False):
            logger.warning(
                "[Phase5-KillSwitch] %s: blocking new entry — 3rd consecutive "
                "SL flag active within %sh window. This is an IMPLEMENTATION "
                "CHOICE (blueprint leaves action-on-flag as an open "
                "question) — see comment above this check to change it.",
                pair,
                self.KILL_SWITCH_WINDOW_HOURS,
            )
            return False

        # --- Phase 0.6: Options Pre-Entry Filter (Theta decay / IV Rank) --
        # FIDELITY GAP #4 RESOLUTION (2026-09-06). Only active when
        # OPTIONS_OVERLAY_ENABLED is True AND a live Delta Exchange reading
        # is available; otherwise this block is a no-op and entries proceed
        # exactly as before the overlay existed (fail-open, not fail-closed
        # — a missing options reading blocks the OPTIONS FILTER, not the
        # trade, since this bot's core strategy is futures, not options,
        # and Phase 0.6's whole premise (blueprint's own options-mechanics
        # section) assumed options data would be present to filter ON in
        # the first place — no data means no filter to apply, not an
        # automatic reject).
        if self.OPTIONS_OVERLAY_ENABLED:
            options_reading = self._delta_options_reading(current_time)
            if options_reading is not None:
                theta_decay_pct = (
                    abs(options_reading["theta"]) / options_reading["premium"] * 100.0
                    if options_reading["premium"] > 0
                    else 0.0
                )
                if theta_decay_pct > self.THETA_DECAY_BLOCK_PCT:
                    logger.warning(
                        "[Phase0.6-OptionsFilter] %s: blocking entry — Theta "
                        "decay %.2f%% of premium exceeds %.1f%% threshold "
                        "(symbol=%s, theta=%.4f, premium=%.4f).",
                        pair, theta_decay_pct, self.THETA_DECAY_BLOCK_PCT,
                        self.OPTIONS_SYMBOL_OVERRIDE,
                        options_reading["theta"], options_reading["premium"],
                    )
                    return False

                if options_reading["iv_rank"] >= self.IV_RANK_BLOCK_THRESHOLD:
                    logger.warning(
                        "[Phase0.6-OptionsFilter] %s: blocking entry — IV Rank "
                        "%.1f >= %.1f threshold (symbol=%s, iv=%.4f). NOTE: "
                        "this IV Rank is a LOCAL approximation from this "
                        "bot's own observed readings, not the option's true "
                        "historical IV Rank — see _delta_options_reading's "
                        "docstring.",
                        pair, options_reading["iv_rank"],
                        self.IV_RANK_BLOCK_THRESHOLD,
                        self.OPTIONS_SYMBOL_OVERRIDE, options_reading["iv"],
                    )
                    return False
            else:
                logger.info(
                    "[Phase0.6-OptionsFilter] %s: overlay enabled but no "
                    "live options reading available this call — filter "
                    "skipped, entry proceeds on futures-only logic (see "
                    "_delta_options_reading docstring for why None means "
                    "'no data', not 'reject').",
                    pair,
                )

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return False
        candle = dataframe.iloc[-1]

        # Ranging entries skip the tier-wait machinery entirely — the
        # blueprint's Phase 1.5 tier table is Trending-only.
        if candle["regime"] != "trending":
            return True

        tier = candle["confirmation_tier"]

        if tier == "high":
            # High-tier: fire immediately, no wait, no refresh question.
            return True

        if tier != "medium":
            # Anything else here is a Low-tier / non-firing classification
            # that should not have produced a signal in the first place;
            # fail safe and skip. ("jab doubt ho, skip karo".)
            return False

        # ------------------------------------------------------------------
        # Medium tier: 1-2 extra polling-cycle wait, THEN full Step A/B/C
        # re-verification (v10/v11 fix — NOT just a pass/fail re-check).
        # ------------------------------------------------------------------
        wait_state = self._medium_tier_wait.get(pair)

        if wait_state is None or wait_state.get("consumed"):
            # First time we've seen this pair in a Medium-tier wait, OR a
            # previous wait for this pair already fully resolved (fired or
            # skipped) — anti-infinite-loop safeguard from the blueprint:
            # "Medium-tier-wait sirf EK-baar trigger ho sakta hai poori
            # trade ke lifecycle mein." If wait_state exists and is
            # consumed, this is treated as a NEW signal occurrence, so a
            # fresh wait is permitted to start (the safeguard scopes "once"
            # to a single trade-lifecycle/signal-occurrence, not forever).
            self._medium_tier_wait[pair] = {
                "start_time": current_time,
                "cycles_seen": 0,
                "consumed": False,
                # Freeze Phase 2.5 Adaptive-SL and Intended-Quantity at
                # THIS moment, per blueprint's explicit resolution
                # ("Phase 2.5 frozen rehti hai... wait ke dauraan"). We
                # snapshot the volatility_ratio here so custom_stoploss can
                # use the frozen value rather than recomputing it later.
                # AUDIT FIX H (2026-09-06): cast to native float here, at
                # the source — see the "2026-09-06 AUDIT PASS (LIVE CRASH)"
                # header block above for the full numpy.float64 ->
                # PostgreSQL crash trace this snapshot was feeding.
                "frozen_volatility_ratio": float(candle["volatility_ratio"]),
            }
            logger.info(
                "[Phase1.5-MediumWait] %s: entering Medium-tier wait "
                "(up to %s cycles). Adaptive-SL frozen at volatility_ratio=%.3f.",
                pair,
                self.MEDIUM_TIER_WAIT_CYCLES,
                candle["volatility_ratio"],
            )
            return False  # Deny this iteration; re-checked next iteration.

        wait_state["cycles_seen"] += 1

        if wait_state["cycles_seen"] < self.MEDIUM_TIER_WAIT_CYCLES:
            # Still waiting. FIDELITY GAP #1 reminder: "cycles" here are bot
            # loop iterations (process_throttle_secs apart), not the
            # blueprint's tick-level polling cycles.
            return False

        # ------------------------------------------------------------------
        # Wait complete. Step A: re-verify Phase 0 / 0.3 on FRESH data.
        # (Phase 0.6/2.7 options re-verification is NOT implemented — see
        # FIDELITY GAP #4; this file has no options data source to
        # re-verify against.)
        # ------------------------------------------------------------------
        fresh_regime_ok = candle["regime"] == "trending"
        fresh_direction_ok = candle["direction_confident"]

        if not (fresh_regime_ok and fresh_direction_ok):
            logger.info(
                "[Phase1.5-StepA] %s: Medium-tier wait complete, Step A "
                "re-verification FAILED (regime=%s, direction_confident=%s) "
                "— skipping per blueprint's skip-on-doubt principle.",
                pair,
                candle["regime"],
                candle["direction_confident"],
            )
            wait_state["consumed"] = True
            return False

        # Step B: fresh ADX-margin / DI-gap-margin. Breakout magnitude and
        # tick-consistency stay at their ORIGINAL trigger-moment values per
        # blueprint's explicit scope limitation ("inka koi fresh version
        # nahi banaya ja raha").
        fresh_adx_margin = candle["adx_avg3"] - self.ADX_TRENDING_THRESHOLD
        fresh_di_margin = candle["di_gap_avg3"] - self.DI_GAP_CONFIDENT_THRESHOLD
        original_breakout_mag = (
            candle["breakout_magnitude_long"]
            if side == "long"
            else candle["breakout_magnitude_short"]
        )
        original_tick_clean = (
            candle["tick_consistency_long"]
            if side == "long"
            else candle["tick_consistency_short"]
        )

        # Step C: re-apply the FULL tier table to fresh margins + frozen
        # breakout/tick values.
        if (
            fresh_adx_margin >= self.HIGH_TIER_ADX_MARGIN
            and fresh_di_margin >= self.HIGH_TIER_DI_MARGIN
            and original_breakout_mag >= self.HIGH_TIER_BREAKOUT_PTS
            and original_tick_clean
        ):
            fresh_tier = "high"
        elif fresh_adx_margin > 0 and fresh_di_margin > 0:
            fresh_tier = "medium"
        else:
            fresh_tier = "low"

        wait_state["consumed"] = True  # Anti-infinite-loop: this wait is done,
                                        # win or lose, no re-wait regardless of
                                        # future reclassifications.

        if fresh_tier in ("high", "medium"):
            logger.info(
                "[Phase1.5-StepC] %s: Medium-tier wait resolved -> fresh_tier=%s. "
                "Firing now on frozen Adaptive-SL (volatility_ratio=%.3f), "
                "per blueprint's explicit no-double-wait rule.",
                pair,
                fresh_tier,
                wait_state["frozen_volatility_ratio"],
            )
            return True

        logger.info(
            "[Phase1.5-StepC] %s: Medium-tier wait resolved -> fresh_tier=low. "
            "Skipping (margins now barely-crossed) even though Step A passed — "
            "matches blueprint's Example-6a precedent.",
            pair,
        )
        return False

    # =====================================================================
    # PHASE 2.5 + 3 + 3.5 + 4: custom_stoploss
    # Adaptive Stop-Loss, Capital Shield (breakeven/timebomb), Mid-Trade
    # Checkpoint, and Profit Trailing — all resolved into a single
    # Active-SL value per trade, per the blueprint's v10 Active-SL concept.
    #
    # RATCHET NOTE (see Gap #3 in the file header for the full story): this
    # method computes candidate_price then returns ONLY a ratio via
    # stoploss_from_absolute() — it does NOT write trade.stop_loss directly
    # (an earlier version of this file did, and that was a bug: it could be
    # silently overridden by freqtrade's own follow-up
    # trade.adjust_stop_loss() call in the same outer method).
    # Whether Phase 3.5 Step 1's widen-or-tighten behavior actually survives
    # freqtrade's ratchet depends on the `_ft_stop_uses_after_fill` flag set
    # near the top of this class, which is UNVERIFIED — see that flag's
    # comment for what to check before trusting a widen to take effect.
    # =====================================================================
    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> Optional[float]:

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None  # Let Freqtrade fall back rather than crash.
        candle = dataframe.iloc[-1]

        # AUDIT FIX E (2026-09-06): regime must be locked at entry, not
        # re-derived from the LATEST candle on every call. Previously this
        # line ran unconditionally every call, so if the market's regime
        # classification moved (e.g. trending -> transition -> ranging, or
        # vice versa) at any point AFTER a trade opened, every later call
        # for that same trade would silently pick up the new value instead
        # of the one the trade actually entered under — even though
        # state["regime"] was already being stored at entry (below) for
        # exactly this purpose and simply wasn't being read back. Concrete
        # consequence: a trade opened under "ranging" (BASE_SL_RANGING_PTS
        # = 4.0pt) could, mid-trade, start being treated as "trending"
        # (BASE_SL_TRENDING_PTS = 8.0pt) purely because the current candle's
        # ADX had drifted into >25 territory — doubling the effective stop
        # distance on a position sized and risked for the tighter regime,
        # and making it newly eligible for the Phase 3.5 checkpoint (which
        # the blueprint scopes to Trending only) despite never having gone
        # through Trending's own confirmation-tier gate at entry. The
        # reverse (trending trade silently reclassified as ranging) would
        # instead shrink an already-live stop's basis unexpectedly. Fixed
        # by reading state["regime"] once a trade has one; the fresh
        # detection below now only ever executes on the FIRST call for a
        # given trade (state is None), which is the only time it should.
        state = self._active_sl_state.get(trade.id)
        if state is not None:
            regime = state["regime"]
        else:
            regime = candle["regime"] if candle["regime"] in ("trending", "ranging") else "trending"

        is_long = not trade.is_short
        seconds_open = (current_time - trade.open_date_utc).total_seconds()

        if state is None:
            # -------------------------------------------------------------
            # First call for this trade: compute Phase 2.5 Entry-Adaptive-SL.
            # If a Medium-tier wait froze the volatility ratio for this
            # pair, use THAT frozen value (blueprint's explicit resolution:
            # Phase 2.5 does not recompute during the wait). Otherwise use
            # the current candle's live ratio (High-tier path, no wait
            # occurred).
            # -------------------------------------------------------------
            wait_record = self._medium_tier_wait.get(pair)
            if wait_record is not None and "frozen_volatility_ratio" in wait_record:
                vol_ratio = wait_record["frozen_volatility_ratio"]
                logger.info(
                    "[Phase2.5] %s trade#%s: using FROZEN volatility_ratio=%.3f "
                    "from Medium-tier wait (blueprint's Step-A/B/C freeze rule).",
                    pair, trade.id, vol_ratio,
                )
                # AUDIT FIX C (2026-09-05): remember which trade this wait
                # produced, so confirm_trade_exit can clear it when THIS
                # trade closes rather than leaving the dict entry forever.
                wait_record["bound_trade_id"] = trade.id
            else:
                # AUDIT FIX H (2026-09-06): cast to native float — see the
                # "2026-09-06 AUDIT PASS (LIVE CRASH)" header block above.
                vol_ratio = float(candle["volatility_ratio"])

            base_sl = (
                self.BASE_SL_TRENDING_PTS
                if regime == "trending"
                else self.BASE_SL_RANGING_PTS
            )
            entry_adaptive_sl_pts = base_sl * vol_ratio  # already clamped upstream

            state = {
                "regime": regime,
                "entry_adaptive_sl_pts": entry_adaptive_sl_pts,
                "entry_volatility_ratio": vol_ratio,
                "checkpoint_done": False,
                "checkpoint_volatility_ratio": None,
                "checkpoint_sl_pts": None,
                "iv_move_at_checkpoint": None,
                "step2_fired": None,
                "final_checkpoint_sl_pts": None,
                # BUG FIX (see _ft_stop_uses_after_fill comment above for the
                # full trace): tracks the loosest (widest, most-room-for-
                # the-position) active_sl_pts this trade has legitimately
                # computed so far, in POINTS not price, so it survives
                # long/short direction differences cleanly. Every return
                # from this method is checked against this value and never
                # allowed to imply a tighter stop than the widest one we
                # already computed — this is what actually makes Phase 3.5's
                # "widen" case work, independent of freqtrade's own
                # after-fill ratchet exception (which cannot reach a
                # 60s-delayed checkpoint, per the trace above).
                "widest_sl_pts_seen": entry_adaptive_sl_pts,
                "breakeven_armed": False,
                "trailing_high_water": current_rate,  # Tracks the position's
                    # favorable-direction extreme since entry: highest price
                    # reached for longs, lowest for shorts. Named
                    # "high_water" for both sides for a single shared field;
                    # the max()/min() logic later in this method (not the
                    # initial value here) is what makes it track correctly
                    # for each side. Both branches start at current_rate
                    # because that's the only price known at this instant —
                    # there is nothing side-specific to differ on here.
                "trailing_armed": False,
            }
            self._active_sl_state[trade.id] = state
            logger.info(
                "[Phase2.5] %s trade#%s: Entry-Adaptive-SL=%.2fpt "
                "(base=%.1f x vol_ratio=%.3f), regime=%s.",
                pair, trade.id, entry_adaptive_sl_pts, base_sl, vol_ratio, regime,
            )

            # --- Phase 2.6: Delta-Translation (underlying pts -> premium) --
            # FIDELITY GAP #4 RESOLUTION (2026-09-06). Snapshot the live
            # options reading AT ENTRY (this "state is None" branch only
            # ever runs once per trade, on its first custom_stoploss call —
            # see AUDIT FIX E's comment above for why regime/entry values
            # must be locked here and not re-derived later). This is the
            # ONE place this file computes Premium_SL_pts, per your
            # request's formula: Premium_SL_pts = Underlying_SL_pts * Delta.
            #
            # WHAT THIS NUMBER IS FOR, AND WHAT IT IS NOT: this bot places
            # NO options order. Active-SL, breakeven, and trailing all
            # continue to operate ENTIRELY in underlying futures points
            # throughout this method, exactly as before the overlay —
            # `active_sl_pts`/`candidate_price`/the returned stop_ratio
            # below are UNCHANGED by this block. `premium_sl_pts` is
            # computed and logged/pushed to the dashboard PURELY as an
            # informational/kill-switch-accounting figure — "if this
            # futures stop distance were instead expressed as options
            # premium exposure via this contract's live Delta, what would
            # that figure be" — per your request's Phase 2.6 ask. If you
            # later want this number to actually DRIVE position sizing or
            # kill-switch counting (blueprint Phase 2.6/5), that is a
            # genuine product decision this file will not make silently on
            # your behalf — the number is computed and available in
            # `state["premium_sl_pts"]` for you to wire into a sizing
            # decision explicitly, the same "flag, don't invent" standard
            # this file uses throughout (see e.g. the leverage() method's
            # own comment on not inventing a blueprint-unspecified number).
            options_reading = self._delta_options_reading(current_time)
            if options_reading is not None:
                premium_sl_pts = entry_adaptive_sl_pts * abs(options_reading["delta"])
                state["premium_sl_pts"] = premium_sl_pts
                state["options_symbol"] = self.OPTIONS_SYMBOL_OVERRIDE
                self._options_entry_snapshot[trade.id] = options_reading
                logger.info(
                    "[Phase2.6-DeltaTranslation] %s trade#%s: "
                    "Underlying_SL=%.2fpt x |Delta|=%.4f (symbol=%s) -> "
                    "Premium_SL=%.4f (options-premium units, "
                    "INFORMATIONAL ONLY — does not alter the futures "
                    "stop-loss returned by this method).",
                    pair, trade.id, entry_adaptive_sl_pts,
                    abs(options_reading["delta"]), self.OPTIONS_SYMBOL_OVERRIDE,
                    premium_sl_pts,
                )
            else:
                state["premium_sl_pts"] = None
                state["options_symbol"] = None
                if self.OPTIONS_OVERLAY_ENABLED:
                    logger.info(
                        "[Phase2.6-DeltaTranslation] %s trade#%s: overlay "
                        "enabled but no live options reading available at "
                        "entry — Premium_SL not computed for this trade.",
                        pair, trade.id,
                    )

            self._push_dashboard_webhook(
                event="entry",
                pair=pair,
                trade_id=trade.id,
                payload={
                    "entry_adaptive_sl": entry_adaptive_sl_pts,
                    "regime": regime,
                    "entry_volatility_ratio": vol_ratio,
                    "options_symbol": state.get("options_symbol"),
                    "premium_sl_pts": state.get("premium_sl_pts"),
                    "options_reading_at_entry": options_reading,
                },
            )

        active_sl_pts = state["entry_adaptive_sl_pts"]  # default: entry value
        step2_multiplier = 1.0  # AUDIT FIX D — see below; overwritten once a
                                 # checkpoint has actually fired for this trade.

        # ---------------------------------------------------------------
        # PHASE 3.5: Mid-Trade Reassessment Checkpoint — Trending only,
        # entry+60s, ONE checkpoint. Explicit, scoped exception to Phase
        # 0.5's freeze rule.
        #
        # AUDIT FIX D (2026-09-06): Step 1's checkpoint_sl_pts (volatility
        # only) and Step 2's tighten factor are now tracked SEPARATELY
        # instead of being pre-multiplied into one number before the
        # widen-floor further down runs. Full reasoning is on that floor's
        # comment block; short version: pre-multiplying let the floor mistake
        # a deliberate Step-2 protective tighten for an illegitimate
        # re-tighten of a widen, and silently undo it almost every time.
        # ---------------------------------------------------------------
        if (
            regime == "trending"
            and not state["checkpoint_done"]
            and seconds_open >= self.CHECKPOINT_DELAY_SECONDS
        ):
            # --- Step 1: Volatility-Reassessment (widen OR tighten) -----
            # AUDIT FIX H (2026-09-06): cast to native float — see the
            # "2026-09-06 AUDIT PASS (LIVE CRASH)" header block above.
            checkpoint_vol_ratio = float(candle["volatility_ratio"])
            checkpoint_sl_pts = self.BASE_SL_TRENDING_PTS * checkpoint_vol_ratio
            # already clamped via the dataframe's safe-ratio + .clip() in
            # populate_indicators (see AUDIT FIX A above)

            # --- Step 2: IV-Crush Protective-Override -------------------
            # FIDELITY GAP #4 RESOLUTION (2026-09-06). Formerly used
            # adverse underlying-price movement as a PROXY for "an
            # actively materializing pricing risk" (see the file's prior
            # header comment on this, now superseded). Replaced with a
            # REAL live-IV-spike trigger against Delta Exchange's own
            # ask_iv/bid_iv for OPTIONS_SYMBOL_OVERRIDE, per your request:
            # "if IV spikes +20 points at the 60-second checkpoint, tighten
            # by 70%." The underlying-points proxy is KEPT, unchanged, as
            # an automatic fallback for when the overlay is disabled or no
            # live reading is available at checkpoint time — this ensures
            # Step 2 always has SOME signal to act on rather than silently
            # never firing whenever Delta Exchange is briefly unreachable
            # (a real, expected condition per this method's own
            # RequestException handling above).
            #
            # NOTE ON IV_CRUSH_TIGHTEN_MULTIPLIER'S DIRECTION: this file
            # keeps the existing 0.7 value UNCHANGED, meaning the result is
            # 70% of the pre-tighten value (a 30% reduction) — this is the
            # convention already threaded through the widen-floor logic
            # below and the exit-time logging. Your original request's
            # prose ("tighten by 70%") could also be read as a 70%
            # REDUCTION (multiplier=0.3, a much more aggressive cut) — that
            # is a real, deliberate product decision this file will not
            # make silently on an ambiguous phrase; if you want the more
            # aggressive reading, change IV_CRUSH_TIGHTEN_MULTIPLIER to 0.3
            # at its definition near the top of this class — nothing below
            # this point needs to change to support that.
            adverse_move_pts = (
                (trade.open_rate - current_rate)
                if is_long
                else (current_rate - trade.open_rate)
            )

            iv_at_checkpoint = None
            iv_spike_points = None
            step2_trigger_source = "underlying_proxy"  # overwritten below
                                      # if a real IV reading is used instead

            if self.OPTIONS_OVERLAY_ENABLED:
                entry_snapshot = self._options_entry_snapshot.get(trade.id)
                checkpoint_reading = self._delta_options_reading(current_time)
                if entry_snapshot is not None and checkpoint_reading is not None:
                    iv_at_checkpoint = checkpoint_reading["iv"]
                    # Both entry and checkpoint IV are decimals (e.g. 0.45),
                    # matching Delta's own quotes.*_iv convention. "+20
                    # points" per your request is read as 20 IV PERCENTAGE
                    # POINTS (0.45 -> 0.65), so the decimal difference is
                    # scaled by 100 for the comparison against
                    # IV_CRUSH_SPIKE_POINTS.
                    iv_spike_points = (
                        (checkpoint_reading["iv"] - entry_snapshot["iv"]) * 100.0
                    )
                    step2_trigger_source = "live_iv"
                else:
                    logger.info(
                        "[Phase3.5-Step2] %s trade#%s: overlay enabled but "
                        "entry snapshot and/or live checkpoint reading "
                        "unavailable — falling back to underlying-price "
                        "proxy for this checkpoint (entry_snapshot=%s, "
                        "checkpoint_reading=%s).",
                        pair, trade.id,
                        entry_snapshot is not None,
                        checkpoint_reading is not None,
                    )

            if step2_trigger_source == "live_iv":
                step2_fires = iv_spike_points >= self.IV_CRUSH_SPIKE_POINTS
            else:
                step2_fires = adverse_move_pts >= self.IV_CRUSH_TRIGGER_PTS

            if step2_fires:
                final_checkpoint_sl_pts = checkpoint_sl_pts * self.IV_CRUSH_TIGHTEN_MULTIPLIER
            else:
                final_checkpoint_sl_pts = checkpoint_sl_pts

            state.update(
                {
                    "checkpoint_done": True,
                    "checkpoint_volatility_ratio": checkpoint_vol_ratio,
                    "checkpoint_sl_pts": checkpoint_sl_pts,
                    "iv_move_at_checkpoint": adverse_move_pts,
                    "step2_fired": step2_fires,
                    "step2_trigger_source": step2_trigger_source,
                    "iv_at_checkpoint": iv_at_checkpoint,
                    "iv_spike_points": iv_spike_points,
                    "final_checkpoint_sl_pts": final_checkpoint_sl_pts,
                }
            )
            # AUDIT FIX D: feed the widen-floor below from checkpoint_sl_pts
            # (Step 1 ONLY, pre-Step-2) — NOT final_checkpoint_sl_pts. Step
            # 2's multiplier is applied AFTER the floor resolves, further
            # down, so a genuine protective tighten can actually reduce the
            # delivered stop distance instead of being floored back up.
            active_sl_pts = checkpoint_sl_pts
            step2_multiplier = self.IV_CRUSH_TIGHTEN_MULTIPLIER if step2_fires else 1.0

            # --- Component-Level Logging Requirement (v10 Gap 4 fix) -----
            # All seven fields logged separately per blueprint — deliberately
            # NOT collapsed into a single final-SL number, so that two trades
            # arriving at the same final SL via different routes (widen vs.
            # tighten) remain distinguishable in your logs/backtests.
            # EXTENDED 2026-09-06 with step2_trigger_source/iv_spike_points
            # so it's always visible in the logs whether a given trade's
            # Step 2 decision came from real Delta Exchange IV data or the
            # underlying-points fallback.
            logger.info(
                "[Phase3.5-Checkpoint] %s trade#%s COMPONENT LOG :: "
                "entry_adaptive_sl=%.2fpt | checkpoint_vol_ratio=%.3f | "
                "checkpoint_sl=%.2fpt | iv_move_at_checkpoint=%.2fpt | "
                "step2_trigger_source=%s | iv_spike_points=%s | "
                "step2_fired=%s | final_checkpoint_sl=%.2fpt | "
                "widen_vs_entry=%s",
                pair, trade.id,
                state["entry_adaptive_sl_pts"],
                checkpoint_vol_ratio,
                checkpoint_sl_pts,
                adverse_move_pts,
                step2_trigger_source,
                f"{iv_spike_points:.2f}" if iv_spike_points is not None else "n/a",
                step2_fires,
                final_checkpoint_sl_pts,
                final_checkpoint_sl_pts > state["entry_adaptive_sl_pts"],
            )

            self._push_dashboard_webhook(
                event="checkpoint",
                pair=pair,
                trade_id=trade.id,
                payload={
                    "entry_adaptive_sl": state["entry_adaptive_sl_pts"],
                    "checkpoint_vol_ratio": checkpoint_vol_ratio,
                    "checkpoint_sl": checkpoint_sl_pts,
                    "iv_move_at_checkpoint": adverse_move_pts,
                    "step2_trigger_source": step2_trigger_source,
                    "iv_spike_points": iv_spike_points,
                    "step2_fired": step2_fires,
                    "final_checkpoint_sl": final_checkpoint_sl_pts,
                },
            )

        elif state["checkpoint_done"]:
            # AUDIT FIX D: same pre-Step-2 value, read back on every
            # iteration after the checkpoint has already fired once.
            active_sl_pts = state["checkpoint_sl_pts"]
            step2_multiplier = (
                self.IV_CRUSH_TIGHTEN_MULTIPLIER if state["step2_fired"] else 1.0
            )

        # This IS the blueprint's Active-SL concept: whichever of
        # {entry_adaptive_sl_pts, checkpoint_sl_pts (pre-Step2, transiently),
        # final_checkpoint_sl_pts} is most-recently-computed. The if/elif
        # chain above always leaves active_sl_pts pointing at that value,
        # with step2_multiplier carrying Step 2's adjustment separately
        # until after the widen-floor below (AUDIT FIX D).

        # ---------------------------------------------------------------
        # BUG FIX — widen enforcement (see _ft_stop_uses_after_fill comment
        # near the top of this class for the full trace of why this is
        # needed). Freqtrade's native ratchet exception only applies on a
        # one-time after-fill call that happens at trade OPEN — it cannot
        # reach a checkpoint that fires 60s later. Without this block, a
        # genuine Phase 3.5 widen (checkpoint_sl_pts > entry_adaptive_sl_pts,
        # i.e. volatility rose) would be silently capped back down to the
        # tighter entry-time stop by freqtrade's normal per-iteration
        # ratchet, every single call after the checkpoint — the widen would
        # never actually take effect on the live stop, only in this method's
        # local variable.
        #
        # Fix: track the widest active_sl_pts this trade has legitimately
        # computed so far and never return a ratio implying anything
        # tighter than that. This makes the widen real by construction,
        # independent of freqtrade's after-fill window.
        #
        # AUDIT FIX D (2026-09-06) — this comparison now runs on the
        # PRE-Step-2 value only (see the split above), not the value after
        # Step 2's multiplier had already been applied. Previously, on any
        # trade where Step 2 fired, this floor read a NUMBER THAT HAD
        # ALREADY BEEN TIGHTENED, compared it against the widest PRE-tighten
        # value ever seen (typically entry_adaptive_sl_pts), found it
        # smaller — which is exactly what Step 2 is FOR — and silently
        # reverted it back to the wider, riskier entry-time stop via the
        # `else` branch below. That happened on essentially every Step-2
        # trigger unless volatility alone had independently risen by more
        # than roughly 1 / IV_CRUSH_TIGHTEN_MULTIPLIER (~43%) since entry.
        # Net effect: Step 2 — the mechanism specifically meant to cut a
        # trade's stop distance faster once price has moved 20+ points
        # against it — was visible in the component-log line above but
        # almost never reflected in the stop distance freqtrade actually
        # enforced, on exactly the trades where it mattered most (a
        # material adverse move already in progress). Splitting the floor
        # comparison from Step 2's multiplier (now applied below, AFTER
        # this floor resolves) fixes that without weakening what this
        # block exists to protect — a genuine volatility-driven widen still
        # floors exactly as before; Step 2 can now also actually take
        # effect on top of it instead of being discarded by it.
        # ---------------------------------------------------------------
        if active_sl_pts > state["widest_sl_pts_seen"]:
            logger.info(
                "[Phase3.5-WidenFix] %s trade#%s: active_sl widening "
                "%.2fpt -> %.2fpt. Enforcing via strategy-side high-water "
                "mark (freqtrade's native after-fill ratchet exception "
                "cannot reach a checkpoint this far after entry — see "
                "_ft_stop_uses_after_fill comment for why).",
                pair, trade.id, state["widest_sl_pts_seen"], active_sl_pts,
            )
            state["widest_sl_pts_seen"] = active_sl_pts
        else:
            active_sl_pts = state["widest_sl_pts_seen"]

        # AUDIT FIX D: apply Step 2's protective tighten LAST, after the
        # widen-floor above has resolved, so a genuine IV-crush override can
        # reduce the delivered stop distance below that floor instead of
        # being clamped back up to it. step2_multiplier is 1.0 (a no-op)
        # whenever no checkpoint has fired yet, or Step 2 didn't trigger.
        active_sl_pts = active_sl_pts * step2_multiplier

        # AUDIT FIX D: persist the fully-resolved figure (post-floor,
        # post-Step-2) so confirm_trade_exit's Active-SL-at-exit log reports
        # what was ACTUALLY enforced on this trade, not the naive pre-fix
        # final_checkpoint_sl_pts (which the two could now legitimately
        # differ from, by design, whenever Step 2 fires — see above).
        state["resolved_active_sl_pts"] = active_sl_pts

        # ---------------------------------------------------------------
        # PHASE 3: Capital Shield — breakeven trigger (frozen at entry
        # multiplier, per blueprint: breakeven/trailing are NOT touched by
        # the Phase 3.5 checkpoint, only the SL value is).
        # ---------------------------------------------------------------
        breakeven_trigger_pts = (
            self.RANGING_BREAKEVEN_TRIGGER_PTS
            if regime == "ranging"
            else self.TRENDING_BREAKEVEN_TRIGGER_PTS
        ) * state["entry_volatility_ratio"]  # frozen entry multiplier, per blueprint

        profit_pts = (
            (current_rate - trade.open_rate)
            if is_long
            else (trade.open_rate - current_rate)
        )

        if not state["breakeven_armed"] and profit_pts >= breakeven_trigger_pts:
            state["breakeven_armed"] = True
            logger.info(
                "[Phase3-CapitalShield] %s trade#%s: breakeven armed at +%.2fpt.",
                pair, trade.id, profit_pts,
            )

        # ---------------------------------------------------------------
        # PHASE 4: Profit Trailing — also frozen at entry multiplier.
        # ---------------------------------------------------------------
        trailing_distance_pts = (
            self.TRAILING_RANGING_PTS
            if regime == "ranging"
            else self.TRAILING_TRENDING_PTS
        ) * state["entry_volatility_ratio"]

        if is_long:
            state["trailing_high_water"] = max(state["trailing_high_water"], current_rate)
            trail_trigger_price = state["trailing_high_water"] - trailing_distance_pts
        else:
            state["trailing_high_water"] = min(state["trailing_high_water"], current_rate)
            trail_trigger_price = state["trailing_high_water"] + trailing_distance_pts

        if not state["trailing_armed"] and profit_pts >= trailing_distance_pts:
            state["trailing_armed"] = True
            logger.info(
                "[Phase4-Trailing] %s trade#%s: trailing armed at +%.2fpt "
                "(distance=%.2fpt).",
                pair, trade.id, profit_pts, trailing_distance_pts,
            )

        # ---------------------------------------------------------------
        # Resolve final stop price: worst-case-for-the-position among
        # {Active-SL from open_rate, breakeven floor, trailing floor}, i.e.
        # whichever gives the position the MOST room while still respecting
        # every armed protection. Breakeven and trailing only ever tighten
        # (move toward locking in profit) — that part is unaffected by the
        # fix above. Active-SL itself can now genuinely widen OR tighten
        # per Phase 3.5, enforced via the widest_sl_pts_seen high-water
        # mark above rather than freqtrade's after-fill ratchet exception
        # (confirmed, by reading freqtradebot.py, not to reach a
        # 60s-delayed checkpoint — see _ft_stop_uses_after_fill comment).
        # ---------------------------------------------------------------
        if is_long:
            active_sl_price = trade.open_rate - active_sl_pts
            candidate_price = active_sl_price
            if state["breakeven_armed"]:
                candidate_price = max(candidate_price, trade.open_rate)
            if state["trailing_armed"]:
                candidate_price = max(candidate_price, trail_trigger_price)
        else:
            active_sl_price = trade.open_rate + active_sl_pts
            candidate_price = active_sl_price
            if state["breakeven_armed"]:
                candidate_price = min(candidate_price, trade.open_rate)
            if state["trailing_armed"]:
                candidate_price = min(candidate_price, trail_trigger_price)

        # -----------------------------------------------------------------
        # RATCHET BYPASS — history of this block, for future maintainers.
        #
        # v1: wrote `trade.stop_loss` directly to bypass freqtrade's "only
        # walks up" ratchet. Reading trade_model.py/interface.py showed this
        # was actively unsafe — a same-call follow-up could silently
        # override the direct write.
        #
        # v2: stopped writing trade.stop_loss directly; instead set
        # `_ft_stop_uses_after_fill = True` and relied on freqtrade's native
        # allow_refresh=after_fill exception, believing it would cover
        # Phase 3.5's widen case. Flagged UNVERIFIED at the time because
        # freqtradebot.py wasn't available to confirm.
        #
        # v3: confirmed against freqtrade 2026.8 source (freqtradebot.py)
        # that after_fill=True fires exactly ONCE, at order-fill time —
        # i.e. at trade OPEN, not 60s later. So v2's widen path was never
        # actually reachable by the Phase 3.5 checkpoint; every post-
        # checkpoint call from the normal per-iteration path is
        # after_fill=False, where freqtrade's own ratchet would silently
        # re-tighten any genuine widen. Fixed by tracking
        # `widest_sl_pts_seen` on this trade's state (see the block right
        # after the Active-SL resolution above) and never returning a
        # ratio narrower than that high-water mark — this makes the widen
        # real, enforced by this strategy rather than by freqtrade's
        # after-fill exception. `_ft_stop_uses_after_fill` is left True
        # (harmless) but Phase 3.5's widen no longer depends on it.
        # `trade.stop_loss` is still never written directly; only a ratio
        # is returned below, same as v2.
        #
        # v4 (this revision, 2026-09-05 audit pass): no change to this
        # block itself — re-verified the reasoning above still holds, and
        # confirmed candidate_price can no longer be NaN-poisoned via
        # active_sl_pts thanks to AUDIT FIX A upstream in
        # populate_indicators.
        # -----------------------------------------------------------------
        stop_ratio = stoploss_from_absolute(
            candidate_price, current_rate, is_short=trade.is_short, leverage=trade.leverage
        )
        # AUDIT FIX H (2026-09-06): defense-in-depth cast to native float.
        # The three candle["volatility_ratio"] reads upstream are now fixed
        # at the source (see the "2026-09-06 AUDIT PASS (LIVE CRASH)" header
        # block above), so stop_ratio should already be a plain float by the
        # time it gets here — this cast is a cheap guarantee, not the fix
        # itself, matching this file's existing guard-at-both-ends pattern.
        return float(stop_ratio)

    # =====================================================================
    # PHASE 3: Capital Shield — Time-Bomb exit.
    # Ranging: 60s adaptive (scaled by volatility ratio). Trending: 90-120s
    # (midpoint 105s), explicitly NOT adaptive per blueprint text.
    #
    # PHASE 5: Kill-Switch bookkeeping happens here too, since custom_exit
    # is called every iteration for open trades and is a natural place to
    # both decide time-bomb exits AND, on any stoploss-triggered exit
    # elsewhere, have somewhere to log the Active-SL-at-exit value. The
    # actual SL-hit exit path is Freqtrade's own stoploss mechanism, driven
    # by the ratio custom_stoploss returns each call (see that method —
    # it does not write trade.stop_loss directly); this method handles the
    # timebomb exit and streak bookkeeping for BOTH exit paths via
    # confirm_trade_exit below.
    # =====================================================================
    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:

        state = self._active_sl_state.get(trade.id, {})
        regime = state.get("regime", "trending")
        seconds_open = (current_time - trade.open_date_utc).total_seconds()

        if regime == "ranging":
            vol_ratio = state.get("entry_volatility_ratio", 1.0)
            timebomb_seconds = self.RANGING_TIMEBOMB_SECONDS * vol_ratio
        else:
            timebomb_seconds = self.TRENDING_TIMEBOMB_SECONDS  # NOT adaptive, per blueprint

        if seconds_open >= timebomb_seconds:
            logger.info(
                "[Phase3-TimeBomb] %s trade#%s: time-bomb exit at %.1fs "
                "(threshold=%.1fs, regime=%s).",
                pair, trade.id, seconds_open, timebomb_seconds, regime,
            )
            return "phase3_timebomb_exit"

        return None

    # =====================================================================
    # LEVERAGE — required override for futures mode.
    #
    # IStrategy's default leverage() (interface.py) hard-returns 1.0 and is
    # documented as "only called in futures mode." You are explicitly
    # trading futures/options; leaving this unimplemented would have meant
    # this strategy silently ran at 1x leverage regardless of your exchange
    # config or intent, with no error or warning anywhere. That is exactly
    # the kind of gap that matters here — a config value being ignored is
    # worse than an error, because nothing tells you it happened.
    #
    # The blueprint gives Phase 2 (Position Sizing) as a SPLIT RATIO
    # (full/50-50/etc. of position size), not a leverage multiplier — it
    # never specifies a target leverage value. Rather than invent a number
    # the blueprint doesn't support, this passes through `proposed_leverage`
    # (whatever your config/exchange settings already establish) capped at
    # `max_leverage` (whatever the exchange allows for this pair). This
    # makes your config's leverage setting actually take effect, without
    # this file overriding it with a fabricated blueprint-derived number.
    # If you DO want the blueprint's ADX/DI confidence tiers (Phase 2's
    # sizing table) to also scale leverage — e.g. Option A (full) trades
    # get higher leverage than Option B (split) trades — that is a genuine
    # product decision the blueprint doesn't make for you, and belongs
    # here if you want it; it is not implemented, to avoid inventing a
    # number on your behalf.
    #
    # AUDIT FIX B (2026-09-05): added an explicit floor. Previously this
    # was a bare `min(proposed_leverage, max_leverage)` with no guard
    # against a malformed 0 or negative proposed_leverage ever reaching a
    # live order silently. Freqtrade's own call site shouldn't normally
    # produce such a value, but "shouldn't" is not "can't" -- if it ever
    # did, the strategy previously would have returned 0/negative leverage
    # with zero indication anything was wrong. Now floors at 1.0 and logs
    # loudly if the floor had to be applied, per Part 22's error-handling
    # standard: fail loud, never silently swallow a malformed value.
    # =====================================================================
    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        # AUDIT FIX F (2026-09-06): added an explicit NaN check ahead of the
        # existing `resolved < 1.0` floor. NaN was not covered by that floor
        # despite the surrounding comment's own "fail loud, never silently
        # swallow a malformed value" standard, because NaN breaks the
        # comparison it depends on: in Python/IEEE-754, `nan < 1.0` is
        # itself False (every comparison against NaN is False, not True),
        # so a NaN proposed_leverage or max_leverage would produce
        # `resolved = min(proposed_leverage, max_leverage)` evaluating to
        # NaN, then silently skip the floor block entirely (since
        # `nan < 1.0` does not raise or evaluate True) and return NaN
        # straight through to Freqtrade's order-placement call — exactly
        # the "malformed value reaches a live order with zero indication"
        # failure mode Gap #2/AUDIT FIX B already exists to prevent for the
        # zero/negative case, just via a path that check doesn't cover.
        # math.isnan() is used (not `!= itself`) for clarity; behavior is
        # identical either way. Deliberately checked BEFORE computing
        # `resolved`, since min() itself already returns NaN if either
        # input is NaN, and a NaN input is worth its own explicit log line
        # rather than being indistinguishable from the sub-1.0 case below.
        if math.isnan(proposed_leverage) or math.isnan(max_leverage):
            logger.warning(
                "[Leverage-Guard] %s: proposed_leverage=%s / max_leverage=%s "
                "-- NaN input detected, which would silently bypass the "
                "< 1.0 floor below (NaN comparisons are always False). "
                "Flooring to 1.0 rather than risking a NaN leverage value "
                "reaching a live order.",
                pair, proposed_leverage, max_leverage,
            )
            return 1.0

        resolved = min(proposed_leverage, max_leverage)
        if resolved < 1.0:
            logger.warning(
                "[Leverage-Guard] %s: proposed_leverage=%.4f / max_leverage=%.4f "
                "resolved to %.4f, below the 1.0 floor -- this should not "
                "happen from Freqtrade's own call site and suggests a "
                "misconfigured exchange/pair setting upstream. Flooring to "
                "1.0 rather than silently placing a sub-1x/negative-leverage "
                "order.",
                pair, proposed_leverage, max_leverage, resolved,
            )
            return 1.0
        return resolved

    # -------------------------------------------------------------------
    # WHERE'S THE "100% BALANCE PER TRADE" SETTING? Not in this file.
    #
    # Position sizing (how much of the wallet goes into each trade) is a
    # config.json concern in Freqtrade, not a strategy-file concern — this
    # class has no stake_amount method to override. The "all balance, one
    # trade at a time" behavior the user asked for lives in config.json as
    # stake_amount="unlimited" + max_open_trades=1 (see the $comment_stake_*
    # block there for why this is flagged as a deliberate deviation from
    # the blueprint's own Phase 2 split-ratio sizing, not a default).
    # -------------------------------------------------------------------

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        """
        Final gate before an exit order is placed. Used here purely for
        Phase 5 Kill-Switch bookkeeping and the Active-SL-at-exit component
        log — does not veto exits (always returns True) since the blueprint
        never describes exit-vetoing logic.
        """
        state = self._active_sl_state.get(trade.id, {})

        # Active-SL-at-exit: whichever of entry/checkpoint/final-checkpoint
        # was most-recently-computed at the moment of exit (mirrors the
        # resolution already tracked incrementally in custom_stoploss).
        if state.get("checkpoint_done"):
            # AUDIT FIX D (2026-09-06): report the fully-resolved value
            # (post widen-floor, post Step-2 multiplier) rather than the
            # naive final_checkpoint_sl_pts — the two can now legitimately
            # differ whenever Step 2 fires. See the AUDIT FIX D comment
            # block inside custom_stoploss for the full trace. Falls back to
            # final_checkpoint_sl_pts only if resolved_active_sl_pts was
            # somehow never set, which should not happen in normal use.
            active_sl_at_exit = state.get(
                "resolved_active_sl_pts", state.get("final_checkpoint_sl_pts")
            )
        else:
            active_sl_at_exit = state.get("entry_adaptive_sl_pts")

        # AUDIT FIX G (2026-09-06): "liquidation" added to the stoploss-exit
        # match. Previously this check only matched exit_reason strings
        # containing "stop_loss" or exactly equal to "stoploss" — Freqtrade
        # also produces a distinct "liquidation" exit_reason when an
        # exchange force-closes a position (margin liquidation), which is
        # at minimum as severe a failure signal as a normal stop-loss hit —
        # arguably more severe, since it means the position moved against
        # the strategy badly/fast enough that its own stop-loss mechanism
        # (custom_stoploss's ratio, checked every process_throttle_secs)
        # either never fired in time or was itself miscalculated. This
        # deployment runs "trading_mode": "futures" (config.json), where
        # liquidation is a real, reachable exit path, not a theoretical
        # one. Previously, a run of liquidations would NOT increment
        # self._sl_streak at all, meaning the Kill-Switch's entire purpose
        # — stopping new entries after repeated severe losses in a 24h
        # window — could be silently defeated by the exact failure mode
        # (uncontrolled forced closure) it most needs to catch. Matching
        # is deliberately substring-based ("liquidation" in ...lower()),
        # mirroring the existing "stop_loss" substring match's own style,
        # to also catch any exchange-specific variant Freqtrade or ccxt
        # might surface (e.g. a prefixed or suffixed liquidation reason
        # string) rather than requiring an exact string match.
        is_stoploss_exit = (
            "stop_loss" in exit_reason.lower()
            or exit_reason == "stoploss"
            or "liquidation" in exit_reason.lower()
        )

        logger.info(
            "[Phase3.5-ComponentLog] %s trade#%s EXIT :: reason=%s | "
            "active_sl_at_exit=%.2fpt | checkpoint_triggered=%s",
            pair, trade.id, exit_reason,
            active_sl_at_exit if active_sl_at_exit is not None else -1.0,
            state.get("checkpoint_done", False),
        )

        self._push_dashboard_webhook(
            event="exit",
            pair=pair,
            trade_id=trade.id,
            payload={
                "exit_reason": exit_reason,
                "active_sl_at_exit": active_sl_at_exit,
                "checkpoint_triggered": state.get("checkpoint_done", False),
                "step2_fired": state.get("step2_fired"),
                "step2_trigger_source": state.get("step2_trigger_source"),
                "premium_sl_pts": state.get("premium_sl_pts"),
                "options_symbol": state.get("options_symbol"),
            },
        )

        # -----------------------------------------------------------------
        # PHASE 5: Kill-Switch bookkeeping.
        # "Do independent counters, 3rd-consecutive-SL dono mein 24hr-window
        # -> Correlated-Failure Log Flag."
        # FIDELITY GAP #5: in-memory only, resets on bot restart. See the
        # commented rebuild-from-trade-history stub below this method for
        # how to persist this against Freqtrade's own trade DB instead.
        # -----------------------------------------------------------------
        if is_stoploss_exit:
            streak = self._sl_streak.setdefault(pair, [])
            streak.append(current_time)
            cutoff = current_time - timedelta(hours=self.KILL_SWITCH_WINDOW_HOURS)
            streak[:] = [t for t in streak if t >= cutoff]

            if len(streak) >= self.KILL_SWITCH_CONSECUTIVE_SL:
                self._kill_switch_flagged[pair] = True
                logger.warning(
                    "[Phase5-KillSwitch] %s: %s SL hits within %sh window — "
                    "Correlated-Failure Flag SET. Blueprint marks the ACTION "
                    "on this flag as an explicitly open question; this "
                    "implementation blocks new entries (see "
                    "confirm_trade_entry) as a conservative default — "
                    "change that if you want log-only behavior instead.",
                    pair, len(streak), self.KILL_SWITCH_WINDOW_HOURS,
                )
        else:
            # Non-SL exit (timebomb, manual, etc.) does not reset the SL
            # streak per the blueprint's "consecutive" framing being about
            # SL-hits specifically, not about trades generally. If you
            # intend "consecutive" to mean "consecutive trades regardless of
            # exit type," clear self._sl_streak[pair] here instead.
            pass

        # Clean up per-trade state now that the trade is closing.
        self._active_sl_state.pop(trade.id, None)
        # ADDED 2026-09-06: mirrors the cleanup above for the options
        # overlay's per-trade entry snapshot (see Phase 2.6 in
        # custom_stoploss for where this is written). Without this, closed
        # trades' snapshots would accumulate in memory for the life of the
        # bot process — the same class of leak AUDIT FIX C already fixed
        # for _medium_tier_wait, applied here to the new dict.
        self._options_entry_snapshot.pop(trade.id, None)

        # -----------------------------------------------------------------
        # AUDIT FIX C (2026-09-05): clean up the Medium-tier wait entry that
        # was bound to THIS trade, if any, now that the trade is closing.
        # Previously _medium_tier_wait entries were created and consumed
        # in-place but never removed -- for a single-pair StaticPairList
        # (this repo's actual deployed config) that meant exactly one
        # stale dict key sitting in memory forever, which is harmless in
        # practice here. But the code had no way of knowing it would only
        # ever see one pair, and this fix makes it correct regardless of
        # how many pairs pair_whitelist ever grows to hold.
        # -----------------------------------------------------------------
        wait_record = self._medium_tier_wait.get(pair)
        if wait_record is not None and wait_record.get("bound_trade_id") == trade.id:
            del self._medium_tier_wait[pair]

        return True

    # =====================================================================
    # STUBS — intentionally NOT wired up, per FIDELITY GAPS #4 and #5.
    # Left in place, clearly separated from the active code path above, so
    # you have a concrete place to plug in real data instead of this file
    # inventing placeholder numbers for you.
    # =====================================================================

    def _delta_options_reading(self, current_time: datetime) -> Optional[dict]:
        """
        FIDELITY GAP #4 RESOLUTION (2026-09-06). Formerly a NotImplementedError
        stub (`inject_options_iv_signal`); replaced with a real client
        against Delta Exchange's public options-ticker endpoint.

        WHAT THIS RETURNS, AND WHAT IT DOES NOT:

        Returns a dict with live data for OPTIONS_SYMBOL_OVERRIDE:
            {
                "delta": float,       # Delta's own greeks.delta, signed
                                       # per Delta's convention (calls
                                       # positive, puts negative)
                "theta": float,       # Delta's own greeks.theta (per-day
                                       # decay, Delta's own convention —
                                       # this file does not rescale it)
                "iv": float,          # mid of quotes.ask_iv/bid_iv when
                                       # both present, else whichever is
                                       # present, as a DECIMAL (0.65 = 65%),
                                       # matching Delta's own quotes.*_iv
                                       # convention
                "iv_rank": float,     # 0-100, see the LOCAL APPROXIMATION
                                       # warning below — this is NOT Delta's
                                       # own IV Rank (their ticker endpoint
                                       # does not expose one)
                "premium": float,     # mark_price of the OPTIONS contract
                                       # itself (i.e. the options premium),
                                       # NOT the underlying spot/futures
                                       # price
                "spot_price": float,  # Delta's own spot_price field for
                                       # this contract's underlying
                "fetched_at": float,  # time.time() this reading was
                                       # obtained (may be OLDER than "now"
                                       # if served from the TTL cache)
                "from_cache": bool,
            }
        or None if the overlay is disabled, misconfigured, or the live
        fetch failed for any reason (network, timeout, malformed response,
        symbol not found). EVERY CALL SITE in this file (Phase 0.6, Phase
        2.6, Phase 3.5 Step 2) MUST treat None as "no options data
        available right now" and fall back to its documented pre-overlay
        behavior — never treat None as zero, and never let a None here
        raise up into Freqtrade's bot-loop. This mirrors the file's
        existing NaN-guard/leverage-floor standard: a missing external
        reading should degrade the strategy's behavior in a known,
        documented way, not crash it or silently corrupt a downstream
        calculation.

        LOCAL "IV RANK" APPROXIMATION — READ THIS BEFORE TRUSTING IT:
        Delta Exchange's /v2/tickers endpoint gives a live ask_iv/bid_iv
        SNAPSHOT, not a rolling IV Rank. A genuine IV Rank needs a real
        rolling window of *historical* IV, ideally sampled continuously
        over a meaningful period (the industry-standard definition uses a
        trailing 52 weeks) — this bot has neither Delta's historical-IV
        endpoint wired in, nor continuous uptime guaranteed (RISK_AND_
        LIMITATIONS.md already documents Render's free tier restarting
        this bot; IV Rank is rebuilt from that "restart" trigger AS WELL,
        exactly as with IV history because both live only in this
        process's memory (self._options_iv_history), not in Delta's own
        API or a persisted store). What is computed here is: percentile
        rank of the CURRENT iv reading against the last
        IV_RANK_LOOKBACK_READINGS raw ask_iv values THIS BOT ITSELF
        observed, across however many _delta_options_reading calls that
        spans — NOT a fixed wall-clock window, and NOT comparable to a
        genuine 52-week IV Rank a real options platform would show. With
        fewer than 20 readings collected so far, this returns 50.0 (a
        neutral midpoint) rather than a rank computed on too little data
        to mean anything — Phase 0.6's IV_RANK_BLOCK_THRESHOLD=70.0 check
        will therefore never block a trade on IV Rank grounds until the
        bot has been running long enough to accumulate that history. This
        is a real, load-bearing limitation, not a formality — treat this
        field as a rough, self-relative signal, not a genuine percentile
        against the option's real historical IV distribution.
        """
        if not self.OPTIONS_OVERLAY_ENABLED:
            return None

        if not self.OPTIONS_SYMBOL_OVERRIDE:
            # Fail LOUD here (log, don't raise) — enabling the overlay
            # without setting a real symbol is a configuration mistake,
            # and this file's standard (see leverage() AUDIT FIX B/F) is
            # to surface that loudly rather than silently no-op forever.
            logger.error(
                "[OptionsOverlay] OPTIONS_OVERLAY_ENABLED=True but "
                "OPTIONS_SYMBOL_OVERRIDE is not set. Set it to a real "
                "Delta Exchange options symbol (e.g. 'C-BTC-70000-261225') "
                "before enabling the overlay. Overlay calls will keep "
                "returning None (falling back to pre-overlay behavior) "
                "until this is fixed."
            )
            return None

        symbol = self.OPTIONS_SYMBOL_OVERRIDE
        now = time.time()

        cached = self._options_reading_cache.get(symbol)
        if cached is not None:
            reading, fetched_at = cached
            if now - fetched_at < self.OPTIONS_CACHE_TTL_SECONDS:
                reading = dict(reading)
                reading["from_cache"] = True
                return reading

        url = f"{self.DELTA_EXCHANGE_API_BASE}/v2/tickers/{symbol}"
        try:
            resp = self._options_http_session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=self.OPTIONS_HTTP_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            logger.warning(
                "[OptionsOverlay] %s: Delta Exchange fetch failed (%s). "
                "Falling back to pre-overlay behavior for this call.",
                symbol, exc,
            )
            return None
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "[OptionsOverlay] %s: Delta Exchange returned unparseable "
                "JSON (%s). Falling back to pre-overlay behavior.",
                symbol, exc,
            )
            return None

        if not payload.get("success"):
            logger.warning(
                "[OptionsOverlay] %s: Delta Exchange responded success=false "
                "(%s). Falling back to pre-overlay behavior.",
                symbol, payload.get("error"),
            )
            return None

        result = payload.get("result")
        if not isinstance(result, dict):
            logger.warning(
                "[OptionsOverlay] %s: Delta Exchange response missing "
                "'result' object. Falling back to pre-overlay behavior.",
                symbol,
            )
            return None

        try:
            greeks = result.get("greeks") or {}
            quotes = result.get("quotes") or {}

            delta_val = float(greeks["delta"])
            theta_val = float(greeks["theta"])

            ask_iv_raw = quotes.get("ask_iv")
            bid_iv_raw = quotes.get("bid_iv")
            iv_samples = [float(v) for v in (ask_iv_raw, bid_iv_raw) if v is not None]
            if not iv_samples:
                raise KeyError("quotes.ask_iv/bid_iv both missing")
            iv_val = sum(iv_samples) / len(iv_samples)

            premium_val = float(result["mark_price"])
            spot_val = float(result["spot_price"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "[OptionsOverlay] %s: Delta Exchange response missing/"
                "malformed a required field (%s). Falling back to "
                "pre-overlay behavior. Raw result keys: %s",
                symbol, exc, list(result.keys()),
            )
            return None

        # Update the rolling IV history used for the local IV Rank
        # approximation (see docstring above for exactly what this is and
        # is not). Bounded to IV_RANK_LOOKBACK_READINGS entries.
        history = self._options_iv_history.setdefault(symbol, [])
        history.append(iv_val)
        if len(history) > self.IV_RANK_LOOKBACK_READINGS:
            del history[: len(history) - self.IV_RANK_LOOKBACK_READINGS]

        if len(history) < 20:
            iv_rank = 50.0  # neutral midpoint; see docstring — insufficient
                             # history to compute a meaningful percentile.
        else:
            below_or_equal = sum(1 for v in history if v <= iv_val)
            iv_rank = 100.0 * below_or_equal / len(history)

        reading = {
            "delta": delta_val,
            "theta": theta_val,
            "iv": iv_val,
            "iv_rank": iv_rank,
            "premium": premium_val,
            "spot_price": spot_val,
            "fetched_at": now,
            "from_cache": False,
        }
        self._options_reading_cache[symbol] = (reading, now)
        return dict(reading)

    # Kept as a thin, explicitly-deprecated alias so any external code or
    # notes referencing the old stub name by its original signature still
    # resolves to real behavior instead of silently vanishing. New code in
    # this file calls _delta_options_reading directly.
    def inject_options_iv_signal(self, pair: str, current_time: datetime) -> Optional[float]:
        """
        DEPRECATED ALIAS (2026-09-06) — kept only for backward-compatible
        naming. Returns just the `iv` field from _delta_options_reading(),
        or None under the same conditions that method returns None. Prefer
        calling _delta_options_reading() directly anywhere you need more
        than the bare IV number (Delta, Theta, premium, IV Rank, etc.) —
        this method throws away everything else it fetched to match the
        old stub's `Optional[float]` signature.
        """
        reading = self._delta_options_reading(current_time)
        return reading["iv"] if reading is not None else None

    def _push_dashboard_webhook(self, event: str, pair: str, trade_id: int, payload: dict) -> None:
        """
        Best-effort POST of the Phase 3.5 Component-Level Logging payload to
        DASHBOARD_WEBHOOK_URL. Added 2026-09-06 alongside the options
        overlay above.

        This is DELIBERATELY SEPARATE from Freqtrade's own native
        "webhook" config-block (see config.json). Freqtrade's native
        webhook already handles generic entry/entry_fill/exit/exit_fill/
        status notifications with a fixed field set — this method exists
        ONLY because those fixed fields cannot carry this strategy's own
        mid-trade component values (checkpoint_vol_ratio,
        final_checkpoint_sl, iv_move_at_checkpoint, the options-overlay
        readings, etc.), which live only in this file's own state dicts
        and have no Freqtrade-native event or field name.

        FAILURE HANDLING: this method NEVER raises. A slow, unreachable,
        or misconfigured dashboard must not be allowed to delay or corrupt
        a live trading decision — every exception is caught, logged at
        WARNING, and swallowed. Called from three places: the first
        custom_stoploss call for a trade (event="entry"), the Phase 3.5
        checkpoint firing inside custom_stoploss (event="checkpoint"), and
        confirm_trade_exit (event="exit").
        """
        if not self.DASHBOARD_WEBHOOK_ENABLED:
            return

        if not self.DASHBOARD_WEBHOOK_URL:
            logger.error(
                "[DashboardWebhook] DASHBOARD_WEBHOOK_ENABLED=True but "
                "DASHBOARD_WEBHOOK_URL is not set. Set it to a real "
                "receiver URL (see the docker-compose.yml dashboard-receiver "
                "service this session produced) before enabling. Webhook "
                "pushes will keep silently no-op'ing until this is fixed."
            )
            return

        body = {
            "event": event,  # "entry" | "checkpoint" | "exit"
            "pair": pair,
            "trade_id": trade_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }

        try:
            self._options_http_session.post(
                self.DASHBOARD_WEBHOOK_URL,
                json=body,
                timeout=self.DASHBOARD_WEBHOOK_TIMEOUT_SECONDS,
            )
            # Response status is intentionally NOT checked/raised on here.
            # This push is fire-and-forget from the trading loop's
            # perspective — a dashboard-side 4xx/5xx is a dashboard
            # problem to fix by reading ITS logs, not a reason to disrupt
            # bot_loop timing or retry-with-backoff inside a live
            # stop-loss calculation.
        except requests.RequestException as exc:
            logger.warning(
                "[DashboardWebhook] %s trade#%s: push failed for event=%s "
                "(%s). Trading logic is unaffected; this only means the "
                "dashboard did not receive this update.",
                pair, trade_id, event, exc,
            )

    # def _rebuild_kill_switch_from_trade_history(self) -> None:
    #     """
    #     FIDELITY GAP #5 stub. Not called anywhere in this file.
    #
    #     self._sl_streak and self._kill_switch_flagged are in-memory dicts
    #     that reset to empty on every bot restart. If you want Phase 5's
    #     "24hr window" to survive restarts, rebuild the streaks from
    #     Freqtrade's own trade database on startup instead, e.g.:
    #
    #         from freqtrade.persistence import Trade
    #         cutoff = datetime.now(timezone.utc) - timedelta(
    #             hours=self.KILL_SWITCH_WINDOW_HOURS
    #         )
    #         closed_trades = Trade.get_trades(
    #             [Trade.close_date >= cutoff, Trade.is_open == False]
    #         ).order_by(Trade.close_date.asc())
    #         for t in closed_trades:
    #             if t.exit_reason and "stop_loss" in t.exit_reason.lower():
    #                 self._sl_streak.setdefault(t.pair, []).append(t.close_date)
    #         # then re-run the len(...) >= KILL_SWITCH_CONSECUTIVE_SL check
    #         # per pair to re-derive self._kill_switch_flagged.
    #
    #     Call this from bot_loop_start() or a similar startup hook if you
    #     add it — not called anywhere by default in this file.
    #     """
    #     pass
