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

The owner may change all project code, including access policy, work_runner,
maintenance, release, instructions, tests, dependencies and deployment files.
The prior policy describes current behavior and does not overrule a new explicit
owner instruction. Secrets, Git internals, runtime state and symlinks are not
published. Code and tests still run as the isolated ttar-code user, not as root.
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

The service retains separate ttar-code and root release identities. Root owns
/var/lib/ttar-release, the repository-limited deploy key and cloud release key.
The code workspace never receives those keys. The test unit has no network,
production database, credentials or Codex authentication.

Optional deploy/owner_apply.py is an owner-authorized deployment hook, invoked
after tests with candidate directory and previous commit arguments under root.
It must be idempotent, preserve data and avoid printing secrets. It is unnecessary
for ordinary Python/control/policy changes. Generic script changes are copied
alongside runtime files; the hook can apply additional requested infrastructure
or dependency changes. Cloud Telegram ingress remains on the separate cloud VM;
changes there require a working deployment route, not direct Telegram from the
internal VM. Failures must be reported honestly.

booking-policy.json regular_minutes is validated and atomically applied to the
scheduler. The current regular target is 19:00–21:30. The room may reject it under
its own 90-minute policy; the bot must not silently shorten or split a booking.

A full verification must exercise the actual system service. A root-shell run
alone does not check systemd privilege restrictions or controller handoff.
