# Owner /work executor

The bot authenticates the owner and persists the operation. A separate root
maintenance service authenticates it again and calls `run_work(request, job_id,
config, progress)`. Install this controller in `/opt/ttar-control`; the editable
bot release must never replace that installed controller automatically.

`run_work` returns `status`, `summary`, `technical`, `commit`, and, after deployment,
`rollback_status`. `status` is `done`, `unchanged`, `failed`, or `uncertain`.
An uncertain operation must be reconciled with GitHub and production before a new
release. Existing job directories are not reused and deployments are not replayed.

## Host preparation

- `/var/lib/ttar-code` and `jobs` are root-owned, mode 0755. Each generated job
  directory and `.codex` belong to the unprivileged `ttar-code` user. That user
  must not belong to production, credential, or administration groups.
- `/var/lib/ttar-release` is root-owned, mode 0711 or 0755. The `repo` clone and
  `candidates` directories are root-owned, readable/traversable for testing.
  The GitHub deploy key `github_ed25519` is mode 0600. Its verified `known_hosts`
  file lives alongside it. The release repository tracks origin/main.
- The maintenance service needs an encrypted `maintenance.json` credential and
  `/etc/ttar/maintenance.json` configuration. No Telegram token is on this host.
- The trusted release helper owns cloud function packaging, production backup,
  service restart, health checks, and rollback. Its code and entry point live in
  `/opt/ttar-control`, not in the generated candidate.

Example work configuration (no secrets):

```json
{
  "code_user": "ttar-code",
  "code_home": "/var/lib/ttar-code",
  "release_root": "/var/lib/ttar-release",
  "codex": "/opt/ttar/bin/codex",
  "code_timeout": 1800,
  "deploy_command": ["/opt/ttar/.venv/bin/python", "/opt/ttar-control/release_entry.py"]
}
```

The deploy command receives two extra argv values: the root-owned candidate
directory and the base origin/main commit. It must emit one JSON object with
`status` (`done`, `failed`, or `uncertain`), `summary`, `technical`, and
`rollback_status`. If its outcome cannot be read, the job remains uncertain.
It must independently know the actual prior deployed version for rollback; the
base GitHub commit may differ after a previous failed deployment.

## Execution and limits

Codex executes as `ttar-code` with `workspace-write` sandbox, network disabled
for shell commands, plugins disabled, and a minimal environment. The workspace
contains a fresh archive of origin/main and no deploy keys or production data.
The account needs its own Codex authentication; the model process can access that
account's authentication, which is never included in the workspace or reports.

Only Python bot modules, Python tests, README, and Markdown docs may change.
The runner, maintenance controller, release helper, dependencies, infrastructure,
links and executable file modes are excluded. Requests requiring those changes
report that automatic publication is unsupported. The test unit has no network,
Codex authentication, production DB or service credentials, and sees the candidate
read-only. Tests execute the full pytest suite under `ttar-code`.

After tests pass, the runner commits and pushes to main, then invokes the fixed
release helper. A rejected push does not deploy. Production failures can therefore
leave a tested commit on GitHub while production remains on its prior version;
the final report includes the commit and deployment/rollback outcome.

Provider and subprocess output is captured and never copied to chat. The final
structured agent summary is bounded and redacted. Passing `redact_values` adds
known secrets to the built-in pattern filter. No diff is sent to Telegram.

Smoke-test without a deployment: use a fresh job ID and request an inspection with
no edits and an `unchanged` result. This still checks GitHub fetch, workspace
isolation, Codex authentication and structured output.
