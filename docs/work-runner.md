# Work runner

All group members can ask questions. Only the authenticated owner Telegram ID
220427487 can enter the writable stage. The controller reads the actor from the
durable Telegram operation, never from the prompt or the model result. Anonymous
senders are rejected. Booking commands remain owner-only.

Every request first runs a read-only answer/intent pass with a root-owned tennis
JSON snapshot outside Git. Questions return one answer without editing, testing,
pushing or deploying. The snapshot excludes Telegram IDs, raw photos, chat text,
credentials and configuration secrets. Progress stays in the journal; Telegram
receives only a final result.

The owner can also correct scores and cancel draft or confirmed photos through
natural-language `/work` requests, for example:

- `/work В последнем фото Рома — Валя 8:11, но победил Рома, исправь на 11:8`
- `/work Отмени последнюю фотографию`

The snapshot includes photo IDs, draft rows, game numbers and current statuses.
Ambiguous requests produce a clarification. The read-only model returns a typed
`edit_game` or `cancel_photo` plan; it never gets database write access. The
controller independently checks the owner, chat and photo snapshot token before
applying it. A concurrent edit or confirmation rejects a stale plan. Mutation,
audit, card refresh queue and final operation result commit atomically; replaying
the operation cannot apply it twice. Data actions do not edit or deploy code.

Editing a draft clears votes and refreshes its confirmation buttons. Editing a
confirmed game updates its displayed score and recalculates subsequent Elo.
Cancellation retains photo/game records for audit, marks games inactive and
removes confirmation buttons. The Google backup retains cancelled game IDs with
the cancelled label. Changed ratings refresh existing photo cards; new `/graph`
and `/stat` requests use the recalculated history.

The owner may change all project code, including access policy, work_runner,
maintenance, release, instructions, tests, dependencies and deployment files.
The prior policy describes current behavior and does not overrule a new explicit
owner instruction. Secrets, Git internals, runtime state and symlinks are not
published. Code and tests run as the configured `work.code_user`, not as root.
On the current host this is `ilya-grid`; see `direct-host.md` for the host override.
Linux requires bubblewrap and working user namespaces; isolation must not be
silently disabled. The maintenance system unit omits User=root because that
setting loses CAP_SETUID on this host with NoNewPrivileges.

After tests pass, the runner pushes a commit and calls the previously installed
release helper. It installs runtime code and the controller snapshot. A changed
controller saves the current result as awaiting_reload and exits gracefully;
systemd restarts it. Only the new controller completes the task and queues its
final success message after startup. A copied standalone guard checks startup
and restores prior modules, units and policy on failure without restoring the
SQLite database. The guard never interrupts the old controller while it is
still finishing a task.

The service retains separate executor and root release identities. Root owns
/var/lib/ttar-release, the repository-limited deploy key and cloud release key.
The code workspace never receives those keys. The test unit has no network,
production database, credentials or Codex authentication.

The editor uses `/opt/ttar/.venv/bin/python -m pytest`, which includes the test
dependencies. The controller independently tests a root-owned candidate. Failed
tests and collection errors return bounded, redacted stdout/stderr to the editor
in the same workspace, with the original request. There are at most three full
test attempts (two repairs). Each attempt rebuilds and revalidates the candidate
from the original base; only the final passing tree can be pushed and deployed.
Infrastructure startup failures, timeouts, pushes and deployments are never
retried by this loop. Exhausted repairs produce one final failure naming the
remaining tests. Progress and raw diagnostics stay out of Telegram.

Optional deploy/owner_apply.py is an owner-authorized deployment hook, invoked
after tests with candidate directory and previous commit arguments under root.
It must be idempotent, preserve data and avoid printing secrets. It is unnecessary
for ordinary Python/control/policy changes. Generic script changes are copied
alongside runtime files; the hook can apply additional requested infrastructure
or dependency changes. Telegram ingress and replies now run directly on the
worker VM using IPv6. Direct releases do not access Yandex Cloud. The separate
TG-VK bridge is deployed from `bridge/`; see `direct-host.md`.
Failures must be reported honestly.

booking-policy.json regular_minutes is validated and atomically applied to the
scheduler. The current regular target is 19:00–21:30. The room may reject it under
its own 90-minute policy; the bot must not silently shorten or split a booking.

A full verification must exercise the actual system service. A root-shell run
alone does not check systemd privilege restrictions or controller handoff.

## Codex authentication recovery

The work executor and OCR service must use independent ChatGPT logins. The
installer never copies the OCR auth cache into the work executor's home. On a
fresh installation, `work_login_required: true` means the owner must sign in
before using `/work`; an existing auth file alone does not prove a valid session.

```bash
ssh -t ilya-grid-vm "sudo -u ilya-grid env HOME=/var/lib/ttar-code CODEX_HOME=/var/lib/ttar-code/.codex sh -c 'cd /var/lib/ttar-code && exec /opt/ttar/bin/codex login --device-auth'"
```

Complete the device-code flow in the owner's browser. Do not copy the resulting
credentials into another service's home. Expired, revoked or reused refresh-token
errors in either Codex stage produce one chat-safe message asking the owner to
sign in again and resubmit the request. Other process failures retain their own
error classification. Failed operations are not replayed automatically.

## Periodic connection checks

Every six hours the maintenance service sends a tiny real model request through
each independent login: OCR and `/work`. Successful checks stay silent. An auth
failure produces one Telegram alert naming the affected service. Other failures
are checked again after one minute before an alert; the same failure does not
produce repeated notices until recovery (or a different failure reason).

The schedule, last success and incident state persist in SQLite `codex_health`.
Checks run on the idle work thread, separately from booking scheduling. OCR
checks use the existing socket and serialize with photo recognition. Work checks
run as `work.code_user`, with tools disabled, under the release lock, and skip while
another release holds it. No credentials are copied between accounts. Set
`codex_health_enabled=false` in the maintenance config to disable the checks.
These requests exercise actual authentication and inference; they do not promise
that the provider will never revoke a session. Inspect the `ttar-maintenance`
journal for `Codex health target=... status=...`.
