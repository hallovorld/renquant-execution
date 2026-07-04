# AlpacaBrokerPort — the BrokerPort's live Alpaca adapter (relocated from orchestrator)

DATE:     2026-07-03
PR:       #21 (feat/alpaca-broker-port, MERGED 2026-07-03) — this doc +
          the fractional-seam docstring note landed as a follow-up
          (docs/alpaca-broker-port-progress) because #21 merged while the
          architecture fix was still being coordinated cross-repo.
CONTEXT:  renquant-orchestrator#291 (RFC #208 sprint D2, Stage-2 live
          executor) defined a real Alpaca REST adapter INSIDE the
          orchestrator — violating that repo's hard CLAUDE.md boundary
          "do not implement broker adapters here". This repo owns broker
          execution, and slice 1's `order_state_machine.BrokerPort`
          docstring already reserved the spot: "The ONLY seam to a real
          broker (Alpaca adapter implements this later)."

DONE:     `src/renquant_execution/alpaca_broker_port.py` — the adapter
          moved verbatim (where sound) from orchestrator #291:
          - `client_order_id` == the slice-1 `child_order_id` (broker-side
            idempotency: Alpaca rejects a duplicate client_order_id);
          - DAY time-in-force always (RFC #208 §11b no-carry);
          - limit vs market pre-declared by the caller (never per-order):
            entries default marketable-limit at a reference price ±
            `limit_price_offset_bps` (Alpaca sub-penny rounding applied),
            exits default market; a limit ENTRY without a positive
            reference price fails closed (`BrokerPortContractError`, a NEW
            execution-owned error — no reverse dependency on orchestrator);
          - `open_orders` / `order_status` are GET-only reads;
          - credentials from `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`, lazy,
            same env names the orchestrator's Stage-1 GET-only sources use.
          `tests/test_alpaca_broker_port.py` — the request-shaping test
          moved verbatim from orchestrator's `test_intraday_live_executor`
          (injected fake `TradingClient`; NO network / live broker call).

NOT DUPLICATED (deliberate): BUY-side fractional validation
          (`validate_fractional_order`) + the no-submit status
          classification live on the s-frac stage-1 surface (execution#22,
          open, not yet on main). Stacking the architecture repair behind
          that deprioritized epic would have blocked it, so the adapter
          stays minimal off main; its docstring records `submit_order` as
          the integration seam for when #22 lands. Nothing from #22 is
          copied here; until then `qty` passes through unvalidated and
          callers own share sizing (the Stage-2 canary binds risk with its
          own daily entry-notional cap).

MERGE ORDER (settled): #21 merged FIRST (2026-07-03); orchestrator #291
          follows. #291 additionally made its adapter import LAZY, inside
          its default port factory (invoked only after its §9.3a arming
          gate) and fail-closed at arming — so its suite/CI is green with
          or without this adapter on the deployed execution main, and a
          future rollback of the execution pin cannot break its import.

VERIFIED: execution suite 114 passed (this repo, no network);
          orchestrator #291 suite 1534 passed / 3 skipped against
          execution main WITH the adapter AND 1533 / 4 against a
          pre-adapter execution main (bad0415); orchestrator's PR diff
          contains zero TradingClient/APCA/alpaca-SDK code.
