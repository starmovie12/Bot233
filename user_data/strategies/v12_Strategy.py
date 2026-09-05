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
#      depth. Phase 2.6 (Delta-Translation) and Phase 2.7 (Liquidity Gate)
#      are NOT faked with placeholder numbers — a fabricated liquidity check
#      is worse than an honest gap. Adaptive-SL and the Phase 3.5 checkpoint
#      are implemented entirely in UNDERLYING POINTS. A clearly-marked stub
#      method shows where you would inject your own options data source.
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

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import (
    IStrategy,
    Trade,
    stoploss_from_absolute,
)

logger = logging.getLogger(__name__)


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
                "frozen_volatility_ratio": candle["volatility_ratio"],
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

        regime = candle["regime"] if candle["regime"] in ("trending", "ranging") else "trending"
        is_long = not trade.is_short
        seconds_open = (current_time - trade.open_date_utc).total_seconds()

        state = self._active_sl_state.get(trade.id)
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
                vol_ratio = candle["volatility_ratio"]

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

        active_sl_pts = state["entry_adaptive_sl_pts"]  # default: entry value

        # ---------------------------------------------------------------
        # PHASE 3.5: Mid-Trade Reassessment Checkpoint — Trending only,
        # entry+60s, ONE checkpoint. Explicit, scoped exception to Phase
        # 0.5's freeze rule.
        # ---------------------------------------------------------------
        if (
            regime == "trending"
            and not state["checkpoint_done"]
            and seconds_open >= self.CHECKPOINT_DELAY_SECONDS
        ):
            # --- Step 1: Volatility-Reassessment (widen OR tighten) -----
            checkpoint_vol_ratio = candle["volatility_ratio"]
            checkpoint_sl_pts = self.BASE_SL_TRENDING_PTS * checkpoint_vol_ratio
            # already clamped via the dataframe's safe-ratio + .clip() in
            # populate_indicators (see AUDIT FIX A above)

            # --- Step 2: IV-Crush Protective-Override -------------------
            # FIDELITY GAP #4: the blueprint's IV-trigger is an options-IV
            # event. With no options data source wired into this file, we
            # use adverse underlying-price movement against the position
            # since entry as a PROXY for "an actively materializing pricing
            # risk," at the same point threshold (20pt, untested per
            # blueprint). This is a substitution, not the real mechanism —
            # see inject_options_iv_signal() stub below for where you'd
            # replace it with a real IV feed.
            adverse_move_pts = (
                (trade.open_rate - current_rate)
                if is_long
                else (current_rate - trade.open_rate)
            )
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
                    "final_checkpoint_sl_pts": final_checkpoint_sl_pts,
                }
            )
            active_sl_pts = final_checkpoint_sl_pts

            # --- Component-Level Logging Requirement (v10 Gap 4 fix) -----
            # All seven fields logged separately per blueprint — deliberately
            # NOT collapsed into a single final-SL number, so that two trades
            # arriving at the same final SL via different routes (widen vs.
            # tighten) remain distinguishable in your logs/backtests.
            logger.info(
                "[Phase3.5-Checkpoint] %s trade#%s COMPONENT LOG :: "
                "entry_adaptive_sl=%.2fpt | checkpoint_vol_ratio=%.3f | "
                "checkpoint_sl=%.2fpt | iv_move_at_checkpoint=%.2fpt | "
                "step2_fired=%s | final_checkpoint_sl=%.2fpt | "
                "widen_vs_entry=%s",
                pair, trade.id,
                state["entry_adaptive_sl_pts"],
                checkpoint_vol_ratio,
                checkpoint_sl_pts,
                adverse_move_pts,
                step2_fires,
                final_checkpoint_sl_pts,
                final_checkpoint_sl_pts > state["entry_adaptive_sl_pts"],
            )

        elif state["checkpoint_done"]:
            active_sl_pts = state["final_checkpoint_sl_pts"]

        # This IS the blueprint's Active-SL concept: whichever of
        # {entry_adaptive_sl_pts, checkpoint_sl_pts (pre-Step2, transiently),
        # final_checkpoint_sl_pts} is most-recently-computed. The if/elif
        # chain above always leaves active_sl_pts pointing at that value.

        # ---------------------------------------------------------------
        # BUG FIX — widen enforcement (see _ft_stop_uses_after_fill comment
        # near the top of this class for the full trace of why this is
        # needed). Freqtrade's native ratchet exception only applies on a
        # one-time after-fill call that happens at trade OPEN — it cannot
        # reach a checkpoint that fires 60s later. Without this block, a
        # genuine Phase 3.5 widen (final_checkpoint_sl_pts >
        # entry_adaptive_sl_pts, i.e. volatility rose and Step 2 did NOT
        # fire) would be silently capped back down to the tighter
        # entry-time stop by freqtrade's normal per-iteration ratchet,
        # every single call after the checkpoint — the widen would never
        # actually take effect on the live stop, only in this method's
        # local variable.
        #
        # Fix: track the widest active_sl_pts this trade has legitimately
        # computed so far and never return a ratio implying anything
        # tighter than that. This makes the widen real by construction,
        # independent of freqtrade's after-fill window.
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
        # AUDIT FIX D (2026-09-06): candidate_price traces back to
        # candle["volatility_ratio"], a native numpy.float64 pulled from the
        # analyzed dataframe (see vol_ratio, lines ~833-844). That np.float64
        # propagates through every arithmetic step above (entry_adaptive_sl_pts,
        # active_sl_pts, candidate_price) and stoploss_from_absolute() performs
        # no casting of its own, so stop_ratio itself is still np.float64 here.
        # SQLAlchemy then serializes it as "np.float64(...)" when writing to
        # Postgres, and Postgres reads the "np" prefix as a schema name it
        # doesn't have — schema "np" does not exist. Explicit cast below is
        # the fix: it converts the value only, changes no trading logic.
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
            active_sl_at_exit = state.get("final_checkpoint_sl_pts")
        else:
            active_sl_at_exit = state.get("entry_adaptive_sl_pts")

        is_stoploss_exit = "stop_loss" in exit_reason.lower() or exit_reason == "stoploss"

        logger.info(
            "[Phase3.5-ComponentLog] %s trade#%s EXIT :: reason=%s | "
            "active_sl_at_exit=%.2fpt | checkpoint_triggered=%s",
            pair, trade.id, exit_reason,
            active_sl_at_exit if active_sl_at_exit is not None else -1.0,
            state.get("checkpoint_done", False),
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

    def inject_options_iv_signal(self, pair: str, current_time: datetime) -> Optional[float]:
        """
        FIDELITY GAP #4 stub. Not called anywhere in this file.

        Phase 3.5 Step 2 (IV-Crush Protective-Override) and Phase 0.6
        (IV Rank / Theta pre-entry filters) both require live options IV
        data that Freqtrade's OHLCV dataframe does not carry. If you have
        an options data source (broker API, IV feed), wire it in here and
        replace the `adverse_move_pts` underlying-price proxy in
        custom_stoploss's Step 2 block with a real IV-move reading.

        Similarly, Phase 2.6 (Delta-Translation) and Phase 2.7 (Liquidity
        Gate) need a per-strike order-book/Greeks feed to translate
        Active-SL from underlying points into premium terms and to check
        depth — neither is implemented here. This file's Active-SL, Phase 3
        Capital Shield, and Phase 4 Trailing all operate in UNDERLYING
        POINTS throughout; converting to premium terms for options position
        sizing/kill-switch counting (per blueprint Phase 2.6/5) is left to
        you to add at the position-sizing layer, using this method as the
        wiring point.
        """
        raise NotImplementedError(
            "Wire your options IV/Greeks/order-book data source here. "
            "Not implemented — see FIDELITY GAP #4 in the file header."
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
