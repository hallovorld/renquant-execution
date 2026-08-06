# The header fix landed in one of the two senders

STATUS: complete. `renquant_execution.alerts` now encodes the ntfy `Title` header
through the shared `renquant_common.notify.encode_header`, on both the urllib path and
the curl fallback.

WHAT: a one-import, two-call-site change plus 8 tests. No alert text, taxonomy,
cooldown, dedup or transport behaviour changes; only how the title is put on the wire.

WHY/DIR: HTTP header values go on the wire as latin-1, so `urllib` raises
`UnicodeEncodeError` **while building the request** — the whole notification is
dropped, body included, not merely its title. `renquant_common.notify` was repaired on
2026-07-29 for exactly this, after a live alert (`rq104 blend 假想前10 — 2026-07-28`)
was lost. **`renquant_execution.alerts` is a second, independent sender that never got
the fix**, and it is the one the execution-side scripts use.

Observed live 2026-08-06, in a real fleet log:

```
ntfy publish attempt 1/3 failed ('latin-1' codec can't encode character '—'
  in position 25); retrying
ntfy publish attempt 2/3 failed (same); retrying
ntfy sent via curl fallback: PROTECTIVE CENSUS FAILED — broker unreachable
```

**Every urllib attempt failed on a single em dash.** The alert arrived only because
`curl` does not latin-1-encode headers. So the retry loop and the fallback were
together masking a total failure of the primary path — a send that looks resilient in
the log is actually 0-for-3 on its real transport, and would be lost outright on any
host without `curl` or with `RENQUANT_NTFY_DISABLE_CURL_FALLBACK=1`.

## Disclosure: I caused the alert that surfaced this

The log line above is from a `protective_census.py` run **I** started while trying to
measure post-open fills. Credentials are not in my environment, so it correctly
reported `broker unreachable` and paged the operator. That page was avoidable and mine
— I ran a production ops script without first checking whether it notifies on failure.
Recorded here rather than omitted, because the finding is only credible alongside how
it was obtained.

## The fix is a reference, not a copy

```python
from renquant_common.notify import encode_header
...
headers={"Title": encode_header(event.title), "Priority": event.priority}
...
"-H", f"Title: {encode_header(event.title)}",
```

`renquant-execution` already declares `renquant-common>=0.1.0` and imports it in four
other modules, so this crosses no boundary. Copying the six-line encoder instead would
reproduce the exact condition being fixed: **two copies of a rule is how one of them
stops being fixed.** `test_the_encoder_is_the_shared_one_not_a_local_copy` asserts
identity against the canonical function so a future copy-paste fails the suite.

The curl fallback is encoded too. curl tolerates a raw UTF-8 header, so before this the
two paths delivered *differently encoded* titles and the reader could not tell which
sender had run.

EVIDENCE:

| claim | value | provenance |
|---|---|---|
| the primary path failed 3/3 on one em dash | yes | [VERIFIED — live log, 2026-08-06] |
| the canonical sender already had the fix | `encode_header` present, dated 2026-07-29 | [VERIFIED — `renquant-common/src/renquant_common/notify.py`] |
| this sender did not | `headers={"Title": event.title, …}` raw at line 195 | [VERIFIED — pre-change source] |
| new tests | **8 passed** | [VERIFIED — `pytest -q tests/test_alert_header_encoding.py`] |
| the new tests are load-bearing | **7 of 8 fail** against the pre-change module | [VERIFIED — `git stash push src/…`, re-run] |
| the 8th is the anti-vacuity control | an ASCII title must pass through untouched, and does before and after | [VERIFIED — same run] |
| full execution suite | **602 passed, 1 skipped** | [VERIFIED — `pytest -q`] |

artifact: none. No artifact is produced, staged or promoted.
prod or exp: **production alerting path.** Every execution-side ntfy alert builds its
  header here. The change is confined to encoding: ASCII titles are byte-identical
  before and after, non-ASCII ones now arrive instead of being dropped.
existing data: yes — the defect was read from a live fleet log line, and the fix reuses
  a function that has been in `renquant-common` since 2026-07-29. Nothing was generated
  to support it.
best-known?: yes. RFC 2047 encoded words are what ntfy decodes, which is why the
  canonical sender chose them; ASCII passes through so ordinary alerts are unchanged.
  The alternatives are worse: stripping non-ASCII loses operator-written content, and
  relying on the curl fallback keeps a 0-for-3 primary path and a host dependency.
scope: one import and two call sites in `alerts.py`, plus one new test file. No other
  module, repo, config or schedule is touched.

NEXT: audit whether any **third** sender exists. This bug survived nine days in a
second copy purely because nobody asked how many senders there were; a grep for direct
`headers={"Title"` construction across the fleet is the cheap version of that question,
and it belongs in its own change rather than widening this one.
