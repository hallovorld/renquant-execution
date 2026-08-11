# Bounded read/connect timeout on the Alpaca broker's account-read calls

STATUS: complete. `AlpacaBroker.connect()` and `get_account_value()` now run their
Alpaca account read under a bounded `(connect, read)` timeout so a stalled socket
fails FAST instead of hanging on the OS-level TCP timeout. Order-submission calls are
untouched.

DATE: 2026-08-11
PAIRS-WITH: renquant-pipeline P-BROKER-CONNECT bounded retry (separate PR). The two
land together: the pipeline retries `connect()`+`get_account_value()` a bounded 3
times; those retries only stay well under the ~12-min intraday cadence BECAUSE this
change makes each attempt fail in seconds rather than ~82s. Neither is complete alone.

## The bug (verified 2026-08-11)

The `intraday_104` 07:01 run aborted:

```
✗ P-BROKER-CONNECT [HARD] broker connect failed:
  HTTPSConnectionPool(host='api.alpaca.markets', port=443):
  Read timed out. (read timeout=None)
```

A single transient Alpaca network blip aborted the whole intraday cycle (no orders),
recovering only on the next scheduled run ~12 min later. Root cause on this side: the
alpaca-py SDK exposes **no timeout knob**. `TradingClient` / the base `RESTClient`
([VERIFIED] alpaca-py 0.43.5, `alpaca/common/rest.py`) issue every call as
`self._session.request(method, url, **opts)` where `opts` carries only
`headers` / `allow_redirects` / `params`|`json` — never a `timeout`. So `requests`
defaults to `timeout=None` and a stalled read hangs until the OS TCP timeout
(~82s observed, 07:00:10 → 07:01:32) before the preflight can even fail.

## The fix — minimal, scoped, order-submission untouched

Since the SDK has no timeout parameter, we substitute the session object rather than
fork/monkeypatch the SDK request loop:

- `_bounded_timeout_session_class()` — lazily builds (once) a `requests.Session`
  subclass that injects a default `(connect, read)` timeout into any request that does
  not already specify one, but **only while armed** (`default_timeout` set). Unarmed it
  is byte-for-byte identical to the SDK's stock `Session`. An explicit caller `timeout`
  always wins. Built lazily (with a deferred `import requests`) because `requests` only
  ships with the `alpaca` extra and this module is designed to import without the broker
  SDK (paper/shadow orchestration) — consistent with the existing lazy `alpaca` import.
- `_install_bounded_timeout_session()` — in `connect()`, swaps the SDK's session for
  the bounded one, **unarmed**, carrying over its headers. Never fails the broker.
- `_bounded_account_timeout()` — a context manager that arms the timeout for the
  duration of one account read, then restores the prior state (including on error).
- `connect()` arms it around `get_account()`; `get_account_value()` arms it around
  its `_refresh_account()`.

Values (constructor kwargs, defaulted): **`connect_timeout_seconds=5.0`,
`read_timeout_seconds=10.0`** (`_DEFAULT_BROKER_CONNECT_TIMEOUT_SECONDS` /
`_DEFAULT_BROKER_READ_TIMEOUT_SECONDS`). A healthy `GET /v2/account` returns in well
under a second, so 5s/10s is ample slack for a blip without an open-ended hang.

**Scope is deliberate.** The timeout is armed at `get_account_value()` (not inside the
shared `_refresh_account()`), so every other `_refresh_account()` caller — notably
`_assert_account_active()` on the order path — and `submit_order` / all other broker
calls stay at their existing, unbounded socket behaviour. Order-submission semantics
are unchanged.

## Evidence

| claim | value | provenance |
|---|---|---|
| SDK has no timeout knob | `TradingClient.__init__` / `RESTClient.__init__` take none; `_one_request` calls `session.request(**opts)` with no `timeout` | [VERIFIED — alpaca-py 0.43.5 `trading/client.py`, `common/rest.py`] |
| observed live failure | `Read timed out. (read timeout=None)`, ~82s hang | [VERIFIED — intraday_104 log 2026-08-11 07:00:10→07:01:32] |
| default timeout applied | `(5.0, 10.0)` armed during connect/get_account_value | [VERIFIED — `test_alpaca_broker_bounded_timeout.py`] |
| session unarmed by default | no timeout injected when `default_timeout is None` (order path) | [VERIFIED — same, `test_bounded_timeout_session_injects_only_when_armed`] |
| explicit timeout not overridden | caller `timeout=1.0` passes through | [VERIFIED — same test] |
| timeout restored after read (incl. error) | `default_timeout is None` after the call | [VERIFIED — `..._arms_then_restores...`, `..._restores_timeout_even_on_error`] |
| connect installs + arms | session becomes bounded, armed `(5.0,10.0)` during `get_account`, disarmed after | [VERIFIED — `test_connect_installs_bounded_session_and_arms_get_account`] |
| import boundary preserved | importing `renquant_execution.alpaca_broker` pulls neither `requests` nor `alpaca` (fresh interpreter) | [VERIFIED — `test_importing_the_module_does_not_pull_requests`; `test_import_boundaries.py` still green] |
| new tests load-bearing | error at import against pre-change source (`_bounded_timeout_session_class` + arming did not exist) | [VERIFIED — `git stash push src/...`, re-run] |
| new tests | **5 passed** | [VERIFIED — `pytest -q tests/test_alpaca_broker_bounded_timeout.py`] |
| full execution suite | **607 passed, 1 skipped** (baseline 602 + 5 new) | [VERIFIED — `pytest -q`] |

artifact: none. No artifact produced, staged or promoted.
prod or exp: **production broker adapter**, but the change is confined to the two
  account-read calls' socket timeout. ASCII/normal behaviour is unchanged; only a
  previously open-ended hang now fails fast. Order submission is byte-for-byte unchanged.
existing data: yes — the defect was read from a live fleet log; no data generated.
best-known?: yes — the honest minimal fix is a bounded socket timeout on the stalling
  call. A session substitution (vs. an SDK fork) is the smallest surface that reaches
  the SDK's internal `session.request`.

NEXT: pin nothing here alone. Land alongside the pipeline P-BROKER-CONNECT retry PR;
the umbrella advances both subrepo pins together so the intraday preflight gets
fast-failing attempts AND the bounded retry in the same cutover.
