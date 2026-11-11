# Direct host deployment

Current host: `ilya-grid-vm`. Production transport configuration is
`telegram_transport: direct` in `/etc/ttar/config.json`.

```mermaid
flowchart LR
  TG[Telegram] <--> TW[ttar-worker: direct long polling]
  TW <--> DB[(Local SQLite: jobs, ratings, outbox)]
  TW --> OCR[Local OCR]
  DB <--> M[Bookings and /work]
  M <--> CAL[Calendar API]
  M <--> GH[GitHub]
  TGB[Telegram bridge bot] <--> B[tg-vk-bridge: local durable inbox]
  B <--> VK[VK Groups Long Poll API]
```

The tennis worker uses the normal Telegram hostname and IPv6. Do not copy the
old cloud-only hardcoded IPv4 override. Both input and output persist locally;
Telegram offsets advance only after the whole incoming batch commits to SQLite.
Two-person confirmations, OCR, history, booking policy and owner permissions are
unchanged. Tests cover redelivery and restart deduplication.

The bridge source is in `bridge/`, deployed separately to `/opt/tg-vk-bridge`.
It retains photo, video, reply and reaction handling. `tg-vk-bridge.service`
uses `/etc/tg-vk-bridge.env`, readable only by root, and a dedicated service user.
Reply mappings, event deduplication and both upstream cursors are in
`/var/lib/tg-vk-bridge/history.sqlite3`. The Telegram user-session fallback for
large videos is retained in the same private directory and uses IPv6.
VK callback delivery must be disabled while Groups Long Poll is active.

The controller reads the runtime transport before deploying. Direct mode does
not request Cloud IAM, function metadata, or deploy cloud versions. Install the
controller with `scripts/install_maintenance.py --direct`; only the Calendar
credential is needed. The ordinary `/work` deployment updates tennis code;
bridge changes require deploying its separate service as well.

Migration backups (credentials included; never print or commit contents) are in
`/var/lib/ttar-migration-20261010`, owned by root with mode 0700. They include
the original bridge files, VK settings, original tennis configuration and cloud
credentials. SQLite backups preserve games independently of transport changes.

For rollback before cloud resources are retired: stop the local bridge and the
direct worker, restore the saved worker config/credential and VK callback
settings, disable VK long polling, then restart the original cloud pollers.
Never run two getUpdates consumers for the same bot. Import any newly processed
bridge mapping before rollback; never restore an old tennis database over new votes.

Read-only identity, member checks and photo downloads do not validate delivery.
Migration acceptance also requires real send/receive checks and a restart check.

## Codex execution user on this host

Since 2026-11-11, both Codex clients run as `ilya-grid`, authorized by the owner.
DNS queries for `auth.openai.com` and `chatgpt.com` returned `::1` for the former
service users (`ttar-code`, `ttar-ocr`), but working addresses for `ilya-grid`.

- `/etc/ttar/maintenance.json`: `work.code_user` is `ilya-grid`.
- `/etc/systemd/system/ttar-ocr.service.d/20-codex-user.conf` sets
  `User=ilya-grid`, keeping `Group=ttar-ocr` for worker socket access.
- OCR retains `HOME=/var/lib/ttar-ocr`; work retains
  `HOME=/var/lib/ttar-code`. Their separate `.codex` directories and sessions
  are owned by `ilya-grid`; no auth cache is copied between services.
- `/var/lib/ttar-code` and its `jobs` parent remain root-owned, as required by
  the release controller. New job directories use the configured executor UID.
- Existing OCR systemd restrictions and work/test sandbox settings remain in
  effect. The executor now shares a Unix identity with the owner, rather than
  having a dedicated service UID. Telegram and the privileged controller keep
  their existing service identities.

Ordinary releases preserve the systemd drop-in and maintenance configuration.
The generic installer still defaults to dedicated users; after reinstalling on
this host, restore the above override and auth-directory ownership before use.
The pre-change config and ownership metadata are saved privately in
`/var/backups/ttar/codex-user-20261111` (do not print or commit the config).
