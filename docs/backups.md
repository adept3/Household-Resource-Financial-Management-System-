# Encrypted backups to Google Drive

HomeBase keeps the ledger on this computer. A backup is a second copy, encrypted by restic before it leaves the machine, stored in a Google Drive folder. Google Drive's free tier is **15 GB**, shared with Gmail and Google Photos. These backups do not use a paid storage service.

You need two programs installed on the server. HomeBase does not vendor them and does not add Python packages:

- **restic** encrypts the files, stores snapshots, and prunes old ones.
- **rclone** is the transport that talks to Google Drive.

The password that unlocks the backups is yours. If you lose it, the snapshots cannot be opened. Store a copy in a password manager. There is no password reset.

## 1. Install the tools

On the Ubuntu Server machine:

```bash
sudo apt update
sudo apt install restic rclone
restic version
rclone version
```

Ubuntu's restic package is enough when it is 0.14 or newer (`restic version`). `snapshots --json` and `check --read-data-subset` need that.

## 2. Create the homebase user and folders

The systemd units run as an unprivileged user named `homebase`, with the ledger under `/var/lib/homebase`.

```bash
sudo useradd --system --create-home --home-dir /var/lib/homebase --shell /usr/sbin/nologin homebase
sudo chmod 700 /var/lib/homebase
sudo mkdir -p /opt/homebase /etc/homebase
```

Put this repository at `/opt/homebase` so `PYTHONPATH=/opt/homebase` can import the `homebase` package (the directory that contains `db.py` must be `/opt/homebase/homebase`).

```bash
sudo cp -a homebase /opt/homebase/homebase
sudo chown -R root:root /opt/homebase
```

Create the ledger once, as the homebase user:

```bash
sudo -u homebase env HOME=/var/lib/homebase HOMEBASE_DATA_DIR=/var/lib/homebase \
  PYTHONPATH=/opt/homebase python3 -m homebase init
```

That creates `/var/lib/homebase/homebase.db` (mode `0600`). Receipt images or JSON files you want included can live in `/var/lib/homebase/receipts`. If that directory is missing, the backup still runs and just sends the database snapshot.

## 3. Connect rclone to Google Drive (headless)

Do this on the server over SSH. You will finish the Google sign-in on another computer that has a browser, such as a Windows Surface.

```bash
rclone config
```

Answer the prompts:

1. `n` for a new remote.
2. Name it `gdrive` (this matches HomeBase's default).
3. Storage type: `drive` (Google Drive).
4. Leave `client_id` and `client_secret` blank unless you created your own Google API client.
5. Scope: choose the line described as access only to files created by rclone (`drive.file`). That keeps rclone out of the rest of the Drive. A dedicated Google account used only for these backups is a reasonable alternative if you prefer full Drive access for that account alone.
6. Leave the root folder id and service account file blank.
7. Skip advanced config.
8. When asked to use auto config, say **no**. The server has no browser.

rclone then prints a command like:

```text
rclone authorize "drive" "eyJ..."
```

On the Windows computer:

1. Install rclone from [https://rclone.org/downloads/](https://rclone.org/downloads/).
2. Open PowerShell and paste the `rclone authorize` command.
3. Sign in to the Google account that should hold the backups.
4. rclone prints a token. Copy it.

Back on the server, paste the token into the waiting `rclone config` prompt and save the remote.

Check that the remote works, then create the backup folder:

```bash
sudo -u homebase rclone lsd gdrive:
sudo -u homebase rclone mkdir gdrive:homebase-backups
```

The rclone config lands in `/var/lib/homebase/.config/rclone/rclone.conf` because the service sets `HOME=/var/lib/homebase`. If you ran `rclone config` as your own user, move that config:

```bash
sudo mkdir -p /var/lib/homebase/.config
sudo cp -a ~/.config/rclone /var/lib/homebase/.config/rclone
sudo chown -R homebase:homebase /var/lib/homebase/.config
sudo chmod 700 /var/lib/homebase/.config/rclone
```

`drive.file` can only see folders rclone created. `rclone mkdir` above is what creates `homebase-backups`, so do that with the same remote you will use for restic.

## 4. Create the restic password

Generate a password and keep a copy in your password manager before you continue. Losing this file means losing the backups.

```bash
sudo mkdir -p /etc/homebase
sudo openssl rand -base64 32 | sudo tee /var/lib/homebase/restic-password >/dev/null
sudo chown homebase:homebase /var/lib/homebase/restic-password
sudo chmod 600 /var/lib/homebase/restic-password
```

Copy the contents into your password manager (`sudo cat /var/lib/homebase/restic-password`), then confirm the mode is `0600`:

```bash
sudo -u homebase stat -c '%a %n' /var/lib/homebase/restic-password
```

HomeBase refuses to run if that file is readable by group or others. Do not pass the password as a command-line argument. Do not put it in `.env`.

Write `/etc/homebase/homebase.env`. This file holds paths, not the password itself. It is group-readable by `homebase` so you can run a backup by hand, and it is not world-readable:

```bash
sudo tee /etc/homebase/homebase.env >/dev/null <<'EOF'
RESTIC_PASSWORD_FILE=/var/lib/homebase/restic-password
RESTIC_REPOSITORY=rclone:gdrive:homebase-backups
HOMEBASE_DATA_DIR=/var/lib/homebase
HOMEBASE_BACKUP_KEEP_DAILY=7
HOMEBASE_BACKUP_KEEP_WEEKLY=4
HOMEBASE_BACKUP_KEEP_MONTHLY=12
EOF
sudo chown root:homebase /etc/homebase/homebase.env
sudo chmod 640 /etc/homebase/homebase.env
```

`RESTIC_REPOSITORY` defaults to `rclone:gdrive:homebase-backups` when it is unset. `HOMEBASE_RCLONE_REMOTE` changes the remote name if you did not call it `gdrive`.

## 5. Initialize and run one backup

```bash
sudo -u homebase env \
  HOME=/var/lib/homebase \
  XDG_CONFIG_HOME=/var/lib/homebase/.config \
  XDG_CACHE_HOME=/var/lib/homebase/.cache \
  PYTHONPATH=/opt/homebase \
  bash -c 'set -a; source /etc/homebase/homebase.env; set +a; python3 -m homebase backup init'

sudo -u homebase env \
  HOME=/var/lib/homebase \
  XDG_CONFIG_HOME=/var/lib/homebase/.config \
  XDG_CACHE_HOME=/var/lib/homebase/.cache \
  PYTHONPATH=/opt/homebase \
  bash -c 'set -a; source /etc/homebase/homebase.env; set +a; python3 -m homebase backup run'
```

`init` creates the encrypted repository. `run` does four things:

1. Copies the live database with Python's `Connection.backup` into a temporary directory mode `0700`, so a running writer cannot produce a torn file.
2. Copies the receipts directory into that same temp directory when it exists.
3. Runs `restic backup` of those copies, then deletes the temp directory.
4. Prunes snapshots to the retention counts (7 daily, 4 weekly, 12 monthly unless you changed the env vars).

List what was stored:

```bash
sudo -u homebase env HOME=/var/lib/homebase PYTHONPATH=/opt/homebase \
  bash -c 'set -a; source /etc/homebase/homebase.env; set +a; python3 -m homebase backup list'
```

Each successful or failed backup appends an `audit_log` row with the snapshot id and a status. The password is not in that row. If a backup fails and `HOMEBASE_NTFY_URL` is set, a short notice is sent with no dollar amounts.

## 6. Turn on the timers

```bash
sudo cp deploy/systemd/homebase-backup.service /etc/systemd/system/
sudo cp deploy/systemd/homebase-backup.timer /etc/systemd/system/
sudo cp deploy/systemd/homebase-backup-check.service /etc/systemd/system/
sudo cp deploy/systemd/homebase-backup-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homebase-backup.timer
sudo systemctl enable --now homebase-backup-check.timer
systemctl list-timers 'homebase-backup*'
```

The nightly timer fires around 02:30, waits a random extra delay so a fleet does not upload at the same second, and uses `Persistent=true` so a laptop that was asleep still runs the job after it wakes. The monthly timer runs `backup test-restore`: it downloads the newest snapshot into a temp directory, checks `schema_version`, checks that every journal sums to zero, then deletes the temp directory.

The service is hardened for an unprivileged user: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`, and `ReadWritePaths` limited to `/var/lib/homebase` (the ledger, the rclone config, and the restic cache for this user). If you move the data directory, change `ReadWritePaths` and `HOMEBASE_DATA_DIR` together.

A manual integrity read of a portion of the stored data (separate from the monthly test restore) is:

```bash
python3 -m homebase backup check
```

That runs `restic check --read-data-subset=10%`. Change the portion with `HOMEBASE_BACKUP_READ_SUBSET`.

## 7. Restore

A restore never writes over the live database. You pick an empty directory and copy the file yourself only after you have looked at it.

```bash
python3 -m homebase backup list
python3 -m homebase backup restore --snapshot SNAPSHOT_ID --target /var/lib/homebase-restore
```

The command prints where it put the files and repeats the path of the live ledger, which is unchanged. Inside the target directory, find `ledger.db` (restic keeps the snapshot's path, so it may be nested). Open it, confirm the dates and balances, then, if you mean to make it the live file:

```bash
sudo systemctl stop homebase-backup.timer
sudo -u homebase cp /var/lib/homebase/homebase.db /var/lib/homebase/homebase.db.before-restore
sudo -u homebase cp /var/lib/homebase-restore/.../ledger.db /var/lib/homebase/homebase.db
sudo chmod 600 /var/lib/homebase/homebase.db
sudo systemctl start homebase-backup.timer
```

Replace the `...` with the directory the restore command told you. Do this only when you intend to roll the ledger back.

## Optional USB copy

Set `HOMEBASE_BACKUP_LOCAL_REPOSITORY` to a directory on the USB disk, for example `/mnt/usb/homebase-restic`. Leave it unset to keep the second copy off. `backup init` and `backup run` then use the Drive repository and that directory. `list`, `restore`, `check`, and `test-restore` accept `--local` to use the USB repository instead of Drive.

Mount the disk before the timer runs, or the local half of `backup run` fails. The Drive half is still attempted first.

## What you have to do yourself

- The Google sign-in in step 3 (the server cannot open a browser).
- Saving the restic password in a password manager.
- `chmod 600` on the password file. HomeBase will not run a backup if that mode is looser.
- Installing `restic` and `rclone` with apt. They are not Python dependencies.
