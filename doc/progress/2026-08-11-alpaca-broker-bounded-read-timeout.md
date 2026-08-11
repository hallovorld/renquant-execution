# Bounded read/connect timeout on the Alpaca broker's account-read calls   (PR #41)

STATUS: delivered.

WHAT: `AlpacaBroker.connect()` and `get_account_value()` now run their Alpaca
account read under a bounded `(connect, read)` timeout (default 5s/10s, constructor
kwargs) so a stalled socket fails FAST instead of hanging on the OS-level TCP
timeout (~82s). The timeout is applied by TEMPORARILY WRAPPING the SDK session's
own `request` method for the duration of that one read (never replacing the session
object), then restoring it exactly. Order submission never enters that context, so
its socket semantics are byte-for-byte unchanged.

WHY/DIR: closes the P-BROKER-CONNECT single-blip abort (2026-08-11 07:00 intraday
cycle lost, no orders for ~12 min). Pairs with the renquant-pipeline
P-BROKER-CONNECT bounded-retry PR (#286): the pipeline retries
`connect()`+`get_account_value()` a bounded 3×, which is only defensible
because this change bounds NO-PROGRESS stalls — the failure mode actually
observed (a socket delivering nothing for ~82s). It is NOT a wall-clock cap on
every cycle: Requests' `timeout` is an inactivity timer, so a peer that keeps
trickling bytes outlasts it indefinitely
`[VERIFIED — measured 30.1s of wall clock under `timeout=(5,10)` against a
local server sending 1 byte every 2s; the read timer never fired]`. Neither is complete alone; the umbrella advances both
subrepo pins in one cutover.

EVIDENCE:

The bug (verified 2026-08-11) — the `intraday_104` 07:01 run aborted:

```
✗ P-BROKER-CONNECT [HARD] broker connect failed:
  HTTPSConnectionPool(host='api.alpaca.markets', port=443):
  Read timed out. (read timeout=None)
```

Root cause on this side: the alpaca-py SDK exposes **no timeout knob**.
`TradingClient` / the base `RESTClient` ([VERIFIED] alpaca-py 0.43.5,
`alpaca/common/rest.py`) issue every call as `self._session.request(method, url,
**opts)` where `opts` carries only `headers` / `allow_redirects` / `params`|`json`
— never a `timeout`. So `requests` defaults to `timeout=None` and a stalled read
hangs until the OS TCP timeout (~82s observed, 07:00:10 → 07:01:32) before the
preflight can even fail.

The fix — WRAP, don't replace (Codex #41 rounds 1–2). Earlier revisions swapped
`client._session` for a `requests.Session` subclass copying only `headers`; that
silently reset the SDK's seeded `proxies` / `verify` / `cert` / `cookies` / `hooks`
/ `params` / `auth` / mounted adapters back to defaults, so `connect()` could
mutate broker behaviour OUTSIDE the account-read path (finding 2, HIGH). The
redesign never touches the session object:

- `_bounded_account_timeout()` — a `@contextmanager` that, for the duration of one
  account read, sets `session.request` to a small wrapper doing
  `kwargs.setdefault("timeout", (connect, read))` then delegating to the ORIGINAL
  `request`. On exit it restores the original exactly: it captures whether
  `"request"` was already an instance attribute (`"request" in session.__dict__`)
  and either restores that instance attr or `del`s the temporary one so the class
  method is re-exposed — the session is byte-for-byte what it was before the `with`.
  Because it is the SAME object, all transport state is preserved by construction.
- No silent degrade (finding 3, MED): if the client has no usable session
  (`_session` is None, or its `request` is not callable because SDK internals
  changed), the context manager RAISES a diagnosable `RuntimeError` naming the
  session type rather than yielding UNBOUNDED. Callers run inside the fail-closed
  P-BROKER-CONNECT retry, so a raise fails loud and closed — a silent unbounded
  fallback would defeat the fast-fail bound the paired retry depends on.
- `_install_bounded_timeout_session()` and `_bounded_timeout_session_class()` (the
  session-replacement path, its module-level cache, and the connect() install call)
  are DELETED. connect()/get_account_value() keep calling
  `with self._bounded_account_timeout():`. Values (defaulted constructor kwargs):
  `connect_timeout_seconds=5.0`, `read_timeout_seconds=10.0`.

Order submission is untouched — now GENUINELY, not by scoping alone: since the
session object is never mutated (only its `request` is wrapped, and only inside the
context connect/get_account_value enter), `submit_order`, `_assert_account_active`,
and every other broker call see the untouched original session — proxies, TLS,
cookies, auth, adapters and socket semantics all byte-for-byte unchanged.

| claim | value | provenance |
|---|---|---|
| SDK has no timeout knob | `TradingClient.__init__` / `RESTClient.__init__` take none; `_one_request` calls `session.request(**opts)` with no `timeout` | [VERIFIED — alpaca-py 0.43.5 `trading/client.py`, `common/rest.py`] |
| observed live failure | `Read timed out. (read timeout=None)`, ~82s hang | [VERIFIED — intraday_104 log 2026-08-11 07:00:10→07:01:32] |
| bounded timeout injected in-window | `(5.0, 10.0)` injected when caller omits it; explicit `timeout=1.0` not overridden | [VERIFIED — `test_bounded_account_timeout_injects_only_in_window`] |
| out-window request untouched (order path) | no injected timeout before/after the context; `session.request` == original; no leftover instance attr | [VERIFIED — same test + `..._wraps_then_restores_request`] |
| non-header session state preserved + SAME object | seeded `proxies`/`verify`/`cert`/`cookies`/`params`/`auth`/`hooks`/adapters all survive `connect()`; `session is` the SDK's own; read still bounded `(5.0,10.0)` | [VERIFIED — `test_connect_preserves_non_header_session_state_same_object`] |
| no silent degrade | `_session=None` or non-callable `.request` ⇒ `RuntimeError`, never an unbounded yield | [VERIFIED — `test_bounded_account_timeout_raises_when_session_unusable`] |
| request restored after read, incl. on error | `"request" not in session.__dict__`; original method restored | [VERIFIED — `..._wraps_then_restores_request`, `..._restores_request_even_on_error`] |
| import boundary preserved | importing `renquant_execution.alpaca_broker` pulls neither `requests` nor `alpaca` (fresh interpreter) | [VERIFIED — `test_importing_the_module_does_not_pull_requests`; `test_import_boundaries.py` still green] |
| new regressions load-bearing | 4/6 fail against the pre-redesign source (incl. state-preservation + raises-not-degrades) | [VERIFIED — restore src to HEAD, re-run: 4 failed, 2 passed] |
| new tests | **6 passed** | [VERIFIED — `pytest -q tests/test_alpaca_broker_bounded_timeout.py`] |
| full execution suite | **608 passed, 1 skipped** (baseline 602 + 6 new) | [VERIFIED — `pytest -q`] |
| doctor | green | [VERIFIED — `renquant-execution ok`] |

artifact:      src/renquant_execution/alpaca_broker.py (`_bounded_account_timeout`); tests/test_alpaca_broker_bounded_timeout.py
prod or exp:   prod (production broker adapter); change confined to the two account-read calls' socket timeout, applied by wrapping the session's `request` only inside that context
existing data: yes — the defect was read from a live fleet log (`intraday_104` 2026-08-11 07:00→07:01, `read timeout=None`); no data generated
best-known?:   yes — wrap-not-replace is the minimal surface that reaches the SDK's internal `session.request` WITHOUT dropping any session state; strictly better than the earlier replace-the-session variant Codex rejected (which reset proxies/verify/cert/cookies/hooks/params/auth)
scope:         "this is src/renquant_execution/alpaca_broker.py `_bounded_account_timeout` (prod), vs existing best = today's UNBOUNDED account read (`timeout=None`, ~82s OS-TCP hang); the wrap bounds no-progress connect/read stalls on the two preflight reads to (5s,10s) — an inactivity bound, not a wall-clock bound on the whole request — and leaves order submission byte-for-byte unchanged"

NEXT: land alongside the pipeline P-BROKER-CONNECT retry PR (#286); the umbrella
advances both subrepo pins together so the intraday preflight gets fast-failing
attempts AND the bounded retry in the same cutover.
