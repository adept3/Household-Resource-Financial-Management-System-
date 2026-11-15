"""Encrypted backups with restic and rclone.

The ledger is copied with SQLite's online backup API, then restic encrypts
that copy (and the receipts directory) before anything is uploaded. The
password is read by restic from a mode-0600 file. It is never placed on the
command line or written to the audit log.

Google Drive is the default remote: ``rclone:gdrive:homebase-backups``.
A second, local repository is used only when HOMEBASE_BACKUP_LOCAL_REPOSITORY
is set.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from homebase.db import audit, default_data_dir, default_db_path, open_db
from homebase.notify import deliver_notifications, notification_text
from homebase.util import HomeBaseError, LedgerImbalanceError

DEFAULT_REMOTE = "gdrive"
DEFAULT_FOLDER = "homebase-backups"
DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4
DEFAULT_KEEP_MONTHLY = 12
DEFAULT_READ_SUBSET = "10%"
LEDGER_NAME = "ledger.db"


def password_file_path() -> Path:
    raw = os.environ.get("RESTIC_PASSWORD_FILE", "").strip()
    if raw == "" or "\n" in raw or "\x00" in raw:
        raise HomeBaseError(
            "RESTIC_PASSWORD_FILE is not set. Create a mode 0600 file that contains the restic "
            "password and point this variable at it. The password is not accepted on the command line."
        )
    return Path(raw).expanduser()


def assert_password_file_mode(path: Path) -> None:
    """Reject a password file that group or others can access."""
    if not path.is_file():
        raise HomeBaseError(f"RESTIC_PASSWORD_FILE does not exist: {path}")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise HomeBaseError(
            f"RESTIC_PASSWORD_FILE is mode {mode:03o}, which is looser than 0600. "
            f"Run: chmod 600 {path}"
        )


def rclone_remote_name() -> str:
    name = os.environ.get("HOMEBASE_RCLONE_REMOTE", DEFAULT_REMOTE).strip() or DEFAULT_REMOTE
    if not name.replace("-", "").replace("_", "").isalnum():
        raise HomeBaseError("HOMEBASE_RCLONE_REMOTE must contain only letters, numbers, '_' or '-'")
    return name


def _validate_repository(value: str) -> str:
    if value == "" or "\n" in value or "\x00" in value or value.startswith("-"):
        raise HomeBaseError("The restic repository value is invalid")
    return value


def primary_repository() -> str:
    explicit = os.environ.get("RESTIC_REPOSITORY", "").strip()
    if explicit:
        return _validate_repository(explicit)
    return "rclone:" + rclone_remote_name() + ":" + DEFAULT_FOLDER


def local_repository() -> str | None:
    raw = os.environ.get("HOMEBASE_BACKUP_LOCAL_REPOSITORY", "").strip()
    if raw == "":
        return None
    return _validate_repository(raw)


def configured_repositories() -> list[tuple[str, str]]:
    repos = [("primary", primary_repository())]
    extra = local_repository()
    if extra:
        repos.append(("local", extra))
    return repos


def selected_repository(local: bool) -> tuple[str, str]:
    if local:
        repo = local_repository()
        if repo is None:
            raise HomeBaseError(
                "HOMEBASE_BACKUP_LOCAL_REPOSITORY is not set. The second copy stays off until you set it."
            )
        return "local", repo
    return "primary", primary_repository()


def _keep_count(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    if not raw.isdigit() or int(raw) < 1 or int(raw) > 9999:
        raise HomeBaseError(f"{name} must be a positive integer")
    return int(raw)


def retention_args() -> list[str]:
    """restic forget arguments. Defaults are 7 daily, 4 weekly, 12 monthly."""
    daily = _keep_count("HOMEBASE_BACKUP_KEEP_DAILY", DEFAULT_KEEP_DAILY)
    weekly = _keep_count("HOMEBASE_BACKUP_KEEP_WEEKLY", DEFAULT_KEEP_WEEKLY)
    monthly = _keep_count("HOMEBASE_BACKUP_KEEP_MONTHLY", DEFAULT_KEEP_MONTHLY)
    return [
        "--keep-daily",
        str(daily),
        "--keep-weekly",
        str(weekly),
        "--keep-monthly",
        str(monthly),
    ]


def receipts_dir() -> Path:
    raw = os.environ.get("HOMEBASE_RECEIPTS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_data_dir() / "receipts"


def read_subset() -> str:
    raw = os.environ.get("HOMEBASE_BACKUP_READ_SUBSET", DEFAULT_READ_SUBSET).strip() or DEFAULT_READ_SUBSET
    if len(raw) < 2 or not raw.endswith("%") or not raw[:-1].isdigit():
        raise HomeBaseError("HOMEBASE_BACKUP_READ_SUBSET must look like 10%")
    percent = int(raw[:-1])
    if not 1 <= percent <= 100:
        raise HomeBaseError("HOMEBASE_BACKUP_READ_SUBSET must be from 1% to 100%")
    return str(percent) + "%"


def require_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise HomeBaseError(
            f"Required program {name!r} was not found on PATH. "
            f"Install it with: sudo apt install {name}"
        )
    return found


def tools_for_repository(repository: str) -> None:
    require_tool("restic")
    if repository.startswith("rclone:"):
        require_tool("rclone")


def restic_env(password_file: Path, repository: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RESTIC_PASSWORD", None)
    env["RESTIC_PASSWORD_FILE"] = str(password_file)
    env["RESTIC_REPOSITORY"] = repository
    return env


def _spawn(cmd: list[str], env: dict[str, str], cwd: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def run_restic(args: list[str], *, repository: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run restic. The password travels only through RESTIC_PASSWORD_FILE."""
    for arg in args:
        if "password" in arg.lower():
            raise HomeBaseError("Refusing to pass a password on the restic command line")
    password_file = password_file_path()
    assert_password_file_mode(password_file)
    tools_for_repository(repository)
    cmd = [require_tool("restic"), *args]
    proc = _spawn(cmd, restic_env(password_file, repository), None if cwd is None else str(cwd))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1][:300] if detail else "exit " + str(proc.returncode)
        raise HomeBaseError("restic failed (" + str(proc.returncode) + "): " + tail)
    return proc


def snapshot_database(source: Path, dest: Path) -> None:
    """Write a consistent copy of ``source`` to ``dest`` using Connection.backup.

    The source is opened read-only. ``dest`` is mode 0600. Callers put it in a
    directory they created as mode 0700 and delete that directory afterwards.
    """
    if not source.is_file():
        raise HomeBaseError(
            f"Ledger database does not exist: {source}. Run: python -m homebase init"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    source_uri = "file:" + quote(str(source.resolve()), safe="/") + "?mode=ro"
    previous_umask = os.umask(0o077)
    source_conn = None
    dest_conn = None
    try:
        source_conn = sqlite3.connect(source_uri, uri=True)
        dest_conn = sqlite3.connect(dest)
        source_conn.backup(dest_conn)
    finally:
        if dest_conn is not None:
            dest_conn.close()
        if source_conn is not None:
            source_conn.close()
        os.umask(previous_umask)
    os.chmod(dest, 0o600)


def prepare_staging(db_path: Path) -> Path:
    """0700 directory containing ledger.db and, when present, a receipts copy."""
    staging = Path(tempfile.mkdtemp(prefix="homebase-backup-"))
    try:
        os.chmod(staging, 0o700)
        snapshot_database(db_path, staging / LEDGER_NAME)
        receipts = receipts_dir()
        if receipts.exists():
            if receipts.is_symlink() or not receipts.is_dir():
                raise HomeBaseError("The receipts path must be a real directory, not a symlink")
            shutil.copytree(receipts, staging / "receipts", symlinks=False)
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def parse_snapshot_id(stdout: str) -> str:
    """Pull the newest snapshot id from `restic snapshots --json` output."""
    text = stdout.strip()
    if text == "":
        raise HomeBaseError("restic did not report a snapshot id")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HomeBaseError("restic snapshot list was not JSON") from exc
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise HomeBaseError("restic did not report a snapshot id")
    snapshot_id = str(payload[-1].get("id") or payload[-1].get("short_id") or "")
    if snapshot_id == "":
        raise HomeBaseError("restic did not report a snapshot id")
    return snapshot_id


def _record_audit(action: str, status: str, snapshot_id: str | None, target: str) -> None:
    if not default_db_path().is_file():
        return
    detail = json.dumps(
        {"snapshot_id": snapshot_id, "status": status, "target": target},
        sort_keys=True,
    )
    conn = open_db()
    try:
        audit(conn, action, "backup", None, detail)
    finally:
        conn.close()


def _safe_audit(action: str, status: str, snapshot_id: str | None, target: str) -> None:
    try:
        _record_audit(action, status, snapshot_id, target)
    except Exception:
        return


def notify_backup_failure(kind: str) -> None:
    """Tell ntfy that a backup failed, without amounts or the restic password."""
    message = notification_text(kind, "HomeBase could not finish a backup", include_amounts=False)
    if "$" in message:
        return
    try:
        deliver_notifications([message], include_amounts=False)
    except Exception:
        return


def _backup_paths(staging: Path) -> list[str]:
    paths = [LEDGER_NAME]
    if (staging / "receipts").is_dir():
        paths.append("receipts")
    return paths


def _backup_one(label: str, repository: str, staging: Path) -> str:
    try:
        run_restic(
            ["backup", "--tag", "homebase", *_backup_paths(staging)],
            repository=repository,
            cwd=staging,
        )
        listed = run_restic(["snapshots", "--json", "--tag", "homebase"], repository=repository)
        snapshot_id = parse_snapshot_id(listed.stdout)
        run_restic(["forget", "--prune", *retention_args()], repository=repository)
    except HomeBaseError:
        _safe_audit("backup", "failed", None, label)
        notify_backup_failure("Backup failed")
        raise
    _record_audit("backup", "ok", snapshot_id, label)
    print(f"Backup {label} snapshot {snapshot_id}")
    return snapshot_id


def backup_init() -> None:
    for label, repository in configured_repositories():
        run_restic(["init"], repository=repository)
        print(f"Initialized restic repository ({label})")


def backup_run() -> None:
    db_path = default_db_path()
    repos = configured_repositories()
    assert_password_file_mode(password_file_path())
    for _label, repository in repos:
        tools_for_repository(repository)
    if not db_path.is_file():
        raise HomeBaseError(f"Ledger database does not exist: {db_path}. Run: python -m homebase init")
    staging = prepare_staging(db_path)
    try:
        for label, repository in repos:
            _backup_one(label, repository, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def backup_list(local: bool = False) -> None:
    _label, repository = selected_repository(local)
    proc = run_restic(["snapshots", "--json"], repository=repository)
    text = proc.stdout.strip() or "[]"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HomeBaseError("restic snapshot list was not JSON") from exc
    if not payload:
        print("No snapshots.")
        return
    for snap in payload:
        short = str(snap.get("short_id") or str(snap.get("id", ""))[:8])
        when = str(snap.get("time", ""))
        tags = ",".join(snap.get("tags") or [])
        print(f"{short}  {when}  {tags}".rstrip())


_SYSTEM_DIRS = {
    Path("/"),
    Path("/tmp"),
    Path("/var"),
    Path("/var/tmp"),
    Path("/usr"),
    Path("/home"),
    Path("/etc"),
    Path("/root"),
    Path("/opt"),
}


def assert_restore_target_safe(target: Path, live_db: Path) -> None:
    """Refuse a target that could replace the live ledger in place."""
    target = target.expanduser().resolve()
    live_db = live_db.expanduser().resolve()
    live_dir = live_db.parent
    if target in _SYSTEM_DIRS:
        raise HomeBaseError("Refusing to restore into a system directory. Choose a new empty folder.")
    if target == live_db or target == live_dir:
        raise HomeBaseError(
            "Refusing to restore onto the live ledger. Pass a different --target directory."
        )
    if _is_inside(target, live_db) or _is_inside(live_dir, target) or _is_inside(target, live_dir):
        raise HomeBaseError(
            "Refusing to restore into the live ledger directory, or into a directory that contains it. "
            "Choose an empty folder elsewhere."
        )


def _is_inside(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return child.resolve() != parent.resolve()


def _validate_snapshot(snapshot: str) -> str:
    if snapshot == "latest":
        return snapshot
    if len(snapshot) < 8 or len(snapshot) > 64 or any(ch not in "0123456789abcdefABCDEF" for ch in snapshot):
        raise HomeBaseError("Snapshot id must be 'latest' or 8-64 hex characters")
    return snapshot


def backup_restore(snapshot: str, target: Path, local: bool = False) -> None:
    snapshot = _validate_snapshot(snapshot)
    live_db = default_db_path()
    assert_restore_target_safe(target, live_db)
    if target.exists() and not target.is_dir():
        raise HomeBaseError("--target must be a directory")
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    label, repository = selected_repository(local)
    try:
        run_restic(["restore", snapshot, "--target", str(target.resolve())], repository=repository)
    except HomeBaseError:
        _safe_audit("restore", "failed", snapshot, label)
        notify_backup_failure("Restore failed")
        raise
    _record_audit("restore", "ok", snapshot, label)
    print(f"Restored snapshot {snapshot} into {target.resolve()}")
    print("The live ledger was not modified.")
    print(f"Live ledger (unchanged): {live_db}")
    print("Next steps:")
    print(f"  1. Look under {target.resolve()} for {LEDGER_NAME}.")
    print("  2. Open that copy and confirm it is the snapshot you wanted.")
    print("  3. Replace the live file only by copying it yourself, after stopping the backup timers.")


def backup_check(local: bool = False) -> None:
    label, repository = selected_repository(local)
    subset = read_subset()
    try:
        run_restic(["check", "--read-data-subset=" + subset], repository=repository)
    except HomeBaseError:
        _safe_audit("backup_check", "failed", None, label)
        notify_backup_failure("Backup check failed")
        raise
    _record_audit("backup_check", "ok", None, label)
    print(f"restic check read {subset} of the {label} repository")


def find_restored_ledger(root: Path) -> Path:
    matches = [path for path in root.rglob(LEDGER_NAME) if path.is_file()]
    if not matches:
        raise HomeBaseError(f"Expected a {LEDGER_NAME} under the restore directory, found none")
    matches.sort(key=lambda path: len(path.relative_to(root).parts))
    return matches[0]


def verify_restored_ledger(db_path: Path) -> int:
    """Open a restored file read-only and require a balanced journal."""
    uri = "file:" + quote(str(db_path.resolve()), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        try:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.Error as exc:
            raise HomeBaseError("Restored database has no schema_version table") from exc
        if row is None:
            raise HomeBaseError("Restored database has no schema_version row")
        version = int(row[0])
        try:
            bad = conn.execute(
                """
                SELECT transaction_id, COALESCE(SUM(amount), 0)
                FROM journal_entries
                GROUP BY transaction_id
                HAVING COALESCE(SUM(amount), 0) != 0
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise HomeBaseError("Restored database is missing journal_entries") from exc
        if bad:
            raise LedgerImbalanceError(int(bad[0][0]), int(bad[0][1]))
        return version
    finally:
        conn.close()


def backup_test_restore(local: bool = False) -> int:
    """Restore the newest snapshot to a temp dir, verify it, then delete the temp dir."""
    label, repository = selected_repository(local)
    staging = Path(tempfile.mkdtemp(prefix="homebase-restore-"))
    os.chmod(staging, 0o700)
    try:
        try:
            run_restic(["restore", "latest", "--target", str(staging)], repository=repository)
            version = verify_restored_ledger(find_restored_ledger(staging))
        except (HomeBaseError, OSError):
            _safe_audit("test_restore", "failed", "latest", label)
            notify_backup_failure("Backup test restore failed")
            raise
        _record_audit("test_restore", "ok", "latest", label)
        print(f"Test restore ok (schema {version}). Temporary files removed.")
        return version
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def dispatch(argv: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="homebase backup")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="create the restic repository")
    sub.add_parser("run", help="snapshot the ledger, back it up, and prune")
    list_parser = sub.add_parser("list", help="list snapshots")
    list_parser.add_argument("--local", action="store_true", help="use the optional local repository")
    restore_parser = sub.add_parser("restore", help="restore a snapshot into an empty directory")
    restore_parser.add_argument("--snapshot", required=True)
    restore_parser.add_argument("--target", required=True, type=Path)
    restore_parser.add_argument("--local", action="store_true")
    check_parser = sub.add_parser("check", help="restic check with a read-data subset")
    check_parser.add_argument("--local", action="store_true")
    test_parser = sub.add_parser("test-restore", help="restore the latest snapshot, verify, and delete it")
    test_parser.add_argument("--local", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "init":
        backup_init()
    elif args.action == "run":
        backup_run()
    elif args.action == "list":
        backup_list(local=args.local)
    elif args.action == "restore":
        backup_restore(args.snapshot, args.target, local=args.local)
    elif args.action == "check":
        backup_check(local=args.local)
    elif args.action == "test-restore":
        backup_test_restore(local=args.local)
    else:
        raise HomeBaseError("Unknown backup command")
