# RISK_AND_LIMITATIONS.md

This file is referenced by `config.json`'s own comments and never existed
until now — it was a placeholder pointing at a document that hadn't been
written. This is that document.

Read this before you ever set `dry_run` to `false`. Nothing here is
optional reading; it explains, in plain terms, what this specific setup
does and doesn't protect you from.

---

## 1. The single biggest risk: 100% of your balance in one trade

Your `config.json` is configured with:

```
"stake_amount": "unlimited",
"max_open_trades": 1,
"tradable_balance_ratio": 1.0
```

This means **every single trade uses your entire available balance.**
There is no second trade running to average out a loss, no partial
allocation held in reserve, and no fallback if the one open position moves
against you.

Combined with **futures trading**, which is leveraged, this has a specific
consequence worth stating directly: a sharp adverse price move can produce
a loss that consumes a very large share of your capital, quickly — not
over days, but potentially within the lifetime of a single trade. This
strategy's own `custom_exit` logic includes a 60–120 second "time-bomb"
exit specifically because it is designed to hold positions only briefly,
which limits (but does not eliminate) this exposure.

This is not a hypothetical edge case. It is the direct, intended behavior
of `stake_amount: "unlimited"` with `max_open_trades: 1` — it is not a bug,
it is not a default that should be "fixed," it is what you asked for. This
section exists so that choice is made with the consequence stated plainly,
not buried in a code comment.

**If you want a different balance between risk and capital efficiency**,
the change is small: replace `"unlimited"` with a fixed number (e.g.
`50000`) and raise `max_open_trades` above 1. This is the direction
`config.json`'s own `$comment_stake_*` block already points to. It is
your call, not a technical requirement — but it is worth understanding
that the current setting is the maximum-aggression end of the spectrum,
not a moderate one.

## 2. This is paper trading, but the moment you flip `dry_run` to `false`, it stops being paper trading

Right now, three separate safety layers are active:
- `dry_run: true` in `config.json` — Freqtrade simulates orders internally.
- `exchange.sandbox: true` — Delta's own demo environment, a second,
  independent layer of simulation.
- Empty `key` / `secret` — there is no real account connected at all.

**All three must be deliberately changed together** to trade real money:
real API keys, `sandbox: false`, and `dry_run: false`. `start.sh`'s own
comments already flag this as something to do "jaan-boojh kar... ek saath,
alag-alag nahi" (deliberately, together, not one at a time) — but it is
worth restating here because it is the single most consequential setting
change in this entire project. There is no confirmation prompt built into
Freqtrade for this; changing three values in a config file is all that
stands between simulated and real capital.

## 3. What the strategy file itself already tells you (read `v12_Strategy.py`'s header)

The strategy file you provided is unusually well-documented about its own
limitations — it labels every approximation "FIDELITY GAP" with reasoning.
This section does not repeat that content in full; it summarizes it so you
know what to go read in the source file itself.

- **Timing approximation**: the original blueprint was written for
  tick-level polling (checking price 15–30 times a second). Freqtrade
  operates on a candle/loop-iteration basis, realistically once per second
  at best. Every timing-sensitive part of this strategy (confirmation
  waits, checkpoints, time-bomb exits) is implemented as wall-clock
  elapsed time, which is a reasonable approximation but is **not the same
  guarantee** as true tick-level responsiveness. In a fast-moving market,
  this means the strategy may react measurably later than the original
  blueprint's design intent.

- **This is a futures strategy standing in for an options blueprint.**
  The original design (`Adaptive_Regime_Strategy_Blueprint_v11`) was
  written for options trading — it references implied volatility, Greeks,
  premium pricing, and per-strike liquidity. None of that exists in
  Freqtrade's futures-trading data model. Every part of this file's
  stop-loss and checkpoint logic operates in **underlying BTC price
  points**, not options premium. If you ever intended to trade actual
  options contracts rather than futures, this file does not do that, and
  adapting it would require a different data source entirely (see
  `inject_options_iv_signal()` in the strategy file, which is an
  intentionally unimplemented stub for exactly this reason).

- **The kill-switch resets on every bot restart.** Phase 5's "3 stop-loss
  hits in 24 hours" tracking lives only in memory. If Render restarts your
  bot (a free-tier Render service sleeps and restarts on its own schedule),
  that counter goes back to zero, silently. A losing streak that would have
  paused new entries may not, if a restart happens to reset the count
  first. The strategy file has a commented-out stub showing how to rebuild
  this from Freqtrade's own trade database on startup, which is not wired
  up by default.

- **The stoploss "widen or tighten" behavior was a real, verified bug that
  has now been fixed in this copy of the file.** The version you originally
  uploaded flagged this as unverified; this build checked it against
  actual Freqtrade 2026.8 source and found that the intended mechanism
  could not reach a 60-second-delayed checkpoint at all — meaning a
  volatility-driven "widen" of the stop-loss would have been silently
  undone on the very next check. This is now fixed at the strategy level
  (see the "BUG FIX — widen enforcement" comment inside
  `custom_stoploss()` in the strategy file for the technical detail). This
  is mentioned here because it's the kind of gap that would have failed
  silently — no error, no warning, just a stop-loss that quietly behaved
  differently than intended.

## 4. Every numeric threshold in this strategy is explicitly untested

`v12_Strategy.py` keeps every threshold (ADX levels, DI gaps, stop-loss
point values, trailing distances) as a named class constant specifically
so you can see and adjust them — but the file's own header states that
*every one of these thresholds is flagged "(untested)" in the original
blueprint it was translated from.* This is not a strategy with a backtested
track record behind these specific numbers. Treat every number in the
"BLUEPRINT CONSTANTS" section of the strategy file as a starting point for
your own testing, not as a validated setting.

## 5. Data persistence on Render's free tier

If you don't set a `DB_URL` environment variable, trade history is stored
in a local SQLite file inside the container. Render's free tier can
restart or sleep a service, and **that restart wipes the local
filesystem** — meaning your entire paper-trading trade history could
disappear without warning. See `DEPLOY_GUIDE.md` for how to connect a free
external Postgres database (Neon or Supabase) so this doesn't happen.

## 6. What this file is not

This is not a claim that the strategy is unsafe to paper-trade, or that
paper trading itself carries financial risk — it doesn't; no real funds
are at risk under the current `dry_run: true` / `sandbox: true`
configuration. This file exists so that if and when you consider changing
that, the specific mechanics of what changes are understood in advance,
not discovered afterward.
