# Database Backup & Restore Guide

For ExcelProtocol on Fly.io. Run these **before** deploying changes that touch
the database schema.

This deploy adds 4 new columns via `ALTER TABLE ADD COLUMN`:
- `stream_events.ended_at`
- `global_stream_events.ended_at`
- `server_settings.welcome_channel_id`
- `server_settings.welcome_message`

`ALTER TABLE ADD COLUMN` in SQLite is one of the safest possible migrations —
it can't fail partway, it doesn't rewrite existing rows, and it's idempotent
(the bot's startup wraps each one in `try/except: pass` for that reason).
But you still want a backup. Always.

---

## Take a backup BEFORE deploying

You'll need `flyctl` installed locally. Pick one of the methods below.

### Method 1 — SSH in and copy the file out (simplest)

```bash
# 1) Connect to the running machine
flyctl ssh console -a excelprotocol

# 2) Inside the container — verify the DB exists
ls -lh /data/twitch_bot.db
# Should show something like: -rw-r--r-- 1 root root 4.2M ... twitch_bot.db

# 3) Make a consistent point-in-time snapshot using SQLite's .backup
#    (this is safer than `cp` because it correctly handles WAL state)
sqlite3 /data/twitch_bot.db ".backup '/data/backup_$(date +%Y%m%d_%H%M%S).db'"

# 4) Confirm the backup file
ls -lh /data/backup_*.db

# 5) Exit SSH
exit
```

Now pull the backup down to your laptop:

```bash
# This uses flyctl's SFTP — find your backup name first
flyctl ssh sftp shell -a excelprotocol
# Inside the SFTP shell:
#   ls /data
#   get /data/backup_20260507_143022.db ./backup.db
#   exit
```

If `flyctl ssh sftp shell` isn't available on your version, use `flyctl ssh
console` and pipe through `cat`:

```bash
flyctl ssh console -a excelprotocol --command "cat /data/backup_20260507_143022.db" > backup.db
```

### Method 2 — Fly volume snapshot (managed by Fly)

Fly automatically snapshots volumes every day for 5 days. List them:

```bash
flyctl volumes list -a excelprotocol
# Note the volume ID for twitch_bot_data, e.g. vol_xxxxxxx

flyctl volumes snapshots list <volume_id>
```

You can restore one of these — see "Restore from a Fly snapshot" below.

This is automatic; you don't need to do anything to *create* it. But you should
**also** take a manual backup before deploying because the automatic snapshot
might be older than your most recent change.

---

## Verify the backup is valid

Before relying on it, confirm it's a working SQLite file:

```bash
# On your laptop, after pulling the file down:
sqlite3 backup.db "SELECT COUNT(*) FROM monitored_streamers;"
sqlite3 backup.db "SELECT COUNT(*) FROM server_settings;"
sqlite3 backup.db "PRAGMA integrity_check;"
# All three should succeed; the integrity_check should print "ok"
```

If any of these fail, redo the backup. Don't deploy with a bad backup.

---

## Deploy

Push your changes to `main` and let GitHub Actions deploy. Watch the bot's
log channel for messages like:

```
Migration: added ended_at to stream_events
Migration: added ended_at to global_stream_events
Migration: added welcome_channel_id to server_settings
Migration: added welcome_message to server_settings
```

Each migration is wrapped in try/except, so even if one is already applied
or fails, the bot will continue running. Check the logs after deploy.

---

## If something breaks: restore from your backup

### Restore Method 1 — Push your local backup back to the volume

```bash
# 1) Stop the bot so nothing's writing to the DB during restore
flyctl scale count 0 -a excelprotocol

# 2) SSH in and rename the broken DB so you can compare later if needed
flyctl ssh console -a excelprotocol
mv /data/twitch_bot.db /data/twitch_bot.db.broken
exit

# 3) Push your backup back. Easiest path is via SFTP:
flyctl ssh sftp shell -a excelprotocol
# Inside SFTP:
#   put ./backup.db /data/twitch_bot.db
#   exit

# 4) Bring the bot back up
flyctl scale count 1 -a excelprotocol

# 5) Watch logs to confirm it started cleanly
flyctl logs -a excelprotocol
```

If `flyctl ssh sftp` doesn't have a `put` command on your version, you can
use this fallback (slower but works everywhere):

```bash
# Encode the file as base64 and pipe it in
base64 < backup.db | flyctl ssh console -a excelprotocol --command \
  "base64 -d > /data/twitch_bot.db"
```

### Restore Method 2 — Roll back to a Fly volume snapshot

This restores the entire volume to the snapshot's state. It creates a new
volume from the snapshot rather than overwriting in place.

```bash
# 1) List snapshots
flyctl volumes list -a excelprotocol
flyctl volumes snapshots list <volume_id>
# Note the snapshot ID and timestamp

# 2) Create a new volume from the snapshot
flyctl volumes create twitch_bot_data_restored \
  --snapshot-id <snapshot_id> \
  --region fra \
  -a excelprotocol

# 3) Stop the app, swap volumes in fly.toml, redeploy
#    Edit fly.toml: change [[mounts]] source = "twitch_bot_data" to
#    source = "twitch_bot_data_restored"
flyctl deploy -a excelprotocol

# 4) Once confirmed working, you can destroy the old broken volume
#    (do NOT do this until you've verified the restore worked!)
flyctl volumes destroy <old_volume_id>
```

This is more involved but gives you a clean rollback if something is genuinely
corrupt. The local-backup-restore method is fine for "the migration broke
something" cases.

---

## Additional notes

**WAL files**: Your DB is in WAL mode. The `.backup` command in SQLite
handles WAL correctly. If you ever copy the DB file with plain `cp`, also
copy `twitch_bot.db-wal` and `twitch_bot.db-shm` from the same directory,
or run `sqlite3 /data/twitch_bot.db "PRAGMA wal_checkpoint(TRUNCATE);"`
first to flush WAL into the main DB file.

**Backup retention**: The bot doesn't auto-prune `/data/backup_*.db` files.
If you take backups regularly, clean up old ones manually:

```bash
# Inside SSH session
ls /data/backup_*.db
# Delete ones you don't want
rm /data/backup_20260101_*.db
```

**Volume free space**: Backups land in the same volume as the live DB. If
your DB is large (~100MB+) and the volume is small (default Fly volumes are
3 GB), don't pile up too many. Pull them down to your laptop and delete.

**Schema downgrades**: SQLite doesn't support `ALTER TABLE DROP COLUMN`
cleanly on older versions. If you ever need to roll the schema *back*
(not just data), restore from a backup that predates the migration —
don't try to drop the new columns.
