# IGV short-plan automation (98/90 put spread)

Cron-driven monitor that runs the operator's discretionary IGV put-spread plan
as a deterministic state machine and — **only when explicitly armed** — places
the live multi-leg order on Alpaca. Lives in `renquant-execution` because it is
execution-side: a broker order primitive (`options_executor`) plus the small
state machine that drives it.

> ⚠️ **Real-money options on a timer.** This overrides the CLAUDE.md §4.1
> paper-cron mandate, and *only* when the operator arms it (see §Arming). The
> entry triggers are discretionary by nature; review the alerts. Default posture
> is dry-run (alerts, no orders).

## The plan (encoded in `igv_short_state.py`)

| IGV action | Plan response |
|---|---|
| bounce $97.5–99, hourly close back < 97.5 (rejection) | **enter** 98/90 put debit spread |
| break < $94.8, then bounce $95–96 rejects (close < 95) | **enter** (path B) |
| reclaim $100 | stand down — do not enter this bounce |
| recover $101.5–102 | **void** the plan |
| in position, $92–93 | take profit: close **half** |
| in position, $88–90 | take profit: close **most** → done |
| in position, ≥ $100.5 | stop: cut **half** |
| in position, daily **close** ≥ $101.5 | stop: **exit all** → done |

"Rejection" = zone touched in the recent window AND the latest **closed hourly
bar** closes back below the zone low.

## Modules

- `renquant_execution/igv_short_state.py` — PURE state machine (no I/O); 15
  tests in `tests/test_igv_short_monitor.py` cover every transition.
- `renquant_execution/options_executor.py` — narrow Alpaca multi-leg layer:
  resolves the 98/90 puts at the nearest weekly expiry from the live chain (no
  hand-built OCC), submits a **limit** spread, enforces a hard contract cap +
  debit-sanity bound, deterministic `client_order_id` (idempotent).
- `renquant_execution/igv_short_monitor.py` — orchestrator (cron entry):
  kill-switch + market-clock guard, fetch bars/price, `step()`, persist, alert,
  execute-if-armed.

## Safety gates (ALL required for a live order)

1. `config.mode == "live"`
2. env `IGV_LIVE_ARMED == "1"`
3. no kill-switch: env `IGV_KILL != "1"` **and** no file `$IGV_STATE_DIR/IGV_KILL`

Any gate off ⇒ **dry-run**: state advances and alerts fire, but no order is sent.
Always-on rails: limit orders only, `IGV_MAX_CONTRACTS` ceiling (default 5),
one-entry (the machine never re-enters), debit bound `0 < debit < strike width`.

## Setup

1. **Enable options** (level 2 / spreads) on the Alpaca account.
2. `cp configs/igv_short_plan.example.json configs/igv_short_plan.json` and edit:
   set `contracts`, `max_debit`, `dte_min/max`. Leave `mode: "paper"` to start.
   (`configs/igv_short_plan.json` + `igv_state/` are gitignored.)
3. Dry-run / paper first (creds via your `.env`):
   ```bash
   set -a; source .env; set +a
   export IGV_CONFIG_PATH=$PWD/configs/igv_short_plan.json IGV_STATE_DIR=$PWD/igv_state
   PYTHONPATH=src .venv/bin/python -m renquant_execution.igv_short_monitor --once
   ```
4. Install the cron (review the plist first — edit repo path + ntfy URL):
   ```bash
   cp ops/launchd/com.renquant.igv-monitor.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.renquant.igv-monitor.plist
   ```

## Arming live (deliberate)

```bash
# configs/igv_short_plan.json: set "mode": "live"
# plist EnvironmentVariables: set IGV_LIVE_ARMED = 1, reload the agent
```

## Kill switch (instant halt)

```bash
touch "$IGV_STATE_DIR/IGV_KILL"      # or: export IGV_KILL=1
```
Cancel/close any already-filled orders in the broker if needed.

## Notes

- One-entry by design: after a void/close the machine does not re-enter — start
  a new `plan_id` for a new setup.
- Post-close stop (daily close ≥ 101.5): add a second launchd entry ~5 min after
  the close with `IGV_IGNORE_HOURS=1`.
- Production note: umbrella `live/alpaca_broker.py` is a Phase-1 mirror of this
  repo's broker layer; this new automation is standalone and runs directly from
  `renquant-execution` (no umbrella mirror required).
