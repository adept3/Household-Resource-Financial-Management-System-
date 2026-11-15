import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from homebase.backup import (
    assert_password_file_mode,
    assert_restore_target_safe,
    backup_restore,
    backup_run,
    backup_test_restore,
    prepare_staging,
    primary_repository,
    require_tool,
    retention_args,
    run_restic,
    snapshot_database,
    verify_restored_ledger,
)
from homebase.db import open_db
from homebase.ledger import ingest_receipt
from homebase.util import HomeBaseError, LedgerImbalanceError

RECEIPT = {
    "date": "2026-09-20",
    "merchant": "Walmart",
    "payment_method": "Debit Card",
    "items": [{"name": "Milk", "qty": 1, "price": 3}],
    "tax": 0,
    "total": 3,
}

SECRET = "restic-secret-value"


def _password_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "restic-password"
    path.write_text(SECRET + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _use_data_dir(monkeypatch, data: Path, password: Path) -> None:
    monkeypatch.setenv("HOMEBASE_DATA_DIR", str(data))
    monkeypatch.delenv("HOMEBASE_DB", raising=False)
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(password))
    monkeypatch.setenv("RESTIC_PASSWORD", SECRET)
    monkeypatch.setenv("RESTIC_REPOSITORY", "rclone:gdrive:homebase-backups")
    monkeypatch.delenv("HOMEBASE_BACKUP_LOCAL_REPOSITORY", raising=False)
    monkeypatch.setattr("homebase.backup.shutil.which", lambda name: "/usr/bin/" + name)


def _completed(cmd, stdout="", code=0):
    return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="" if code == 0 else "Fatal: repository does not exist")


def test_snapshot_stays_consistent_when_the_live_db_changes(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _use_data_dir(monkeypatch, data, _password_file(tmp_path))
    conn = open_db()
    ingest_receipt(conn, RECEIPT)
    conn.close()
    source = data / "homebase.db"
    previous = os.umask(0o022)
    try:
        staging = prepare_staging(source)
    finally:
        os.umask(previous)
    try:
        snap = staging / "ledger.db"
        assert stat.S_IMODE(staging.stat().st_mode) == 0o700
        assert stat.S_IMODE(snap.stat().st_mode) == 0o600
        assert snap.stat().st_ino != source.stat().st_ino
        assert verify_restored_ledger(snap) == 1
        assert _txn_count(snap) == 1
        conn = open_db()
        ingest_receipt(conn, {**RECEIPT, "date": "2026-09-21", "merchant": "Other"})
        conn.close()
        assert _txn_count(source) == 2
        assert _txn_count(snap) == 1
    finally:
        import shutil

        shutil.rmtree(staging)


def test_snapshot_includes_receipts_and_rejects_a_symlink(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _use_data_dir(monkeypatch, data, _password_file(tmp_path))
    conn = open_db()
    conn.close()
    receipts = data / "receipts"
    receipts.mkdir()
    (receipts / "note.txt").write_text("milk", encoding="utf-8")
    staging = prepare_staging(data / "homebase.db")
    try:
        assert (staging / "receipts" / "note.txt").read_text(encoding="utf-8") == "milk"
    finally:
        import shutil

        shutil.rmtree(staging)

    link = tmp_path / "linked-receipts"
    link.symlink_to(receipts, target_is_directory=True)
    monkeypatch.setenv("HOMEBASE_RECEIPTS_DIR", str(link))
    held = {}

    def fake_mkdtemp(prefix):
        path = tmp_path / "staging-held"
        path.mkdir()
        held["path"] = path
        return str(path)

    monkeypatch.setattr("homebase.backup.tempfile.mkdtemp", fake_mkdtemp)
    with pytest.raises(HomeBaseError, match="symlink"):
        prepare_staging(data / "homebase.db")
    assert not held["path"].exists()


def test_password_file_must_not_be_looser_than_0600(tmp_path):
    loose = _password_file(tmp_path, 0o644)
    with pytest.raises(HomeBaseError, match="0600"):
        assert_password_file_mode(loose)
    loose.chmod(0o640)
    with pytest.raises(HomeBaseError, match="0600"):
        assert_password_file_mode(loose)
    loose.chmod(0o600)
    assert_password_file_mode(loose)
    loose.chmod(0o400)
    assert_password_file_mode(loose)
    missing = tmp_path / "missing"
    with pytest.raises(HomeBaseError, match="does not exist"):
        assert_password_file_mode(missing)


def test_retention_args_default_and_env(monkeypatch):
    for name in (
        "HOMEBASE_BACKUP_KEEP_DAILY",
        "HOMEBASE_BACKUP_KEEP_WEEKLY",
        "HOMEBASE_BACKUP_KEEP_MONTHLY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert retention_args() == [
        "--keep-daily",
        "7",
        "--keep-weekly",
        "4",
        "--keep-monthly",
        "12",
    ]
    monkeypatch.setenv("HOMEBASE_BACKUP_KEEP_DAILY", "3")
    monkeypatch.setenv("HOMEBASE_BACKUP_KEEP_WEEKLY", "2")
    monkeypatch.setenv("HOMEBASE_BACKUP_KEEP_MONTHLY", "6")
    assert retention_args() == [
        "--keep-daily",
        "3",
        "--keep-weekly",
        "2",
        "--keep-monthly",
        "6",
    ]
    monkeypatch.setenv("HOMEBASE_BACKUP_KEEP_DAILY", "0")
    with pytest.raises(HomeBaseError):
        retention_args()


def test_default_repository_is_google_drive(monkeypatch):
    monkeypatch.delenv("RESTIC_REPOSITORY", raising=False)
    monkeypatch.delenv("HOMEBASE_RCLONE_REMOTE", raising=False)
    assert primary_repository() == "rclone:gdrive:homebase-backups"
    monkeypatch.setenv("HOMEBASE_RCLONE_REMOTE", "familydrive")
    assert primary_repository() == "rclone:familydrive:homebase-backups"


def test_run_restic_keeps_the_password_off_the_command_line(tmp_path, monkeypatch):
    password = _password_file(tmp_path)
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(password))
    monkeypatch.setenv("RESTIC_PASSWORD", SECRET)
    monkeypatch.setattr("homebase.backup.shutil.which", lambda name: "/usr/bin/" + name)
    seen = {}

    def fake_spawn(cmd, env, cwd):
        seen["cmd"] = cmd
        seen["env"] = env
        return _completed(cmd, stdout="[]")

    monkeypatch.setattr("homebase.backup._spawn", fake_spawn)
    run_restic(["snapshots", "--json"], repository="rclone:gdrive:homebase-backups")
    assert SECRET not in seen["cmd"]
    assert "--password" not in seen["cmd"]
    assert "password-command" not in " ".join(seen["cmd"])
    assert seen["env"].get("RESTIC_PASSWORD") is None
    assert seen["env"]["RESTIC_PASSWORD_FILE"] == str(password)
    assert SECRET not in json.dumps(seen["env"])


def test_missing_restic_is_a_clear_error(monkeypatch):
    monkeypatch.setattr("homebase.backup.shutil.which", lambda name: None)
    with pytest.raises(HomeBaseError, match="sudo apt install restic"):
        require_tool("restic")


def test_backup_run_prunes_with_retention_and_writes_audit(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _use_data_dir(monkeypatch, data, _password_file(tmp_path))
    conn = open_db()
    ingest_receipt(conn, RECEIPT)
    conn.close()
    calls = []
    cwds = []

    def fake_spawn(cmd, env, cwd):
        calls.append(list(cmd))
        if cwd:
            cwds.append(Path(cwd))
        assert SECRET not in cmd
        assert env.get("RESTIC_PASSWORD") is None
        if "snapshots" in cmd:
            return _completed(cmd, stdout='[{"id":"abcdef1234567890","short_id":"abcdef12"}]')
        return _completed(cmd)

    monkeypatch.setattr("homebase.backup._spawn", fake_spawn)
    backup_run()
    assert cwds and not cwds[0].exists()
    forget = [cmd for cmd in calls if "forget" in cmd][0]
    assert forget[forget.index("--keep-daily") + 1] == "7"
    assert forget[forget.index("--keep-weekly") + 1] == "4"
    assert forget[forget.index("--keep-monthly") + 1] == "12"
    conn = open_db()
    try:
        detail = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'backup'"
        ).fetchone()[0]
    finally:
        conn.close()
    payload = json.loads(detail)
    assert payload["status"] == "ok"
    assert payload["snapshot_id"] == "abcdef1234567890"
    assert SECRET not in detail


def test_backup_run_failure_notifies_without_amounts(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _use_data_dir(monkeypatch, data, _password_file(tmp_path))
    conn = open_db()
    conn.close()
    notes = []
    cwds = []

    def fake_spawn(cmd, env, cwd):
        if cwd:
            cwds.append(Path(cwd))
        return _completed(cmd, code=1)

    monkeypatch.setattr("homebase.backup._spawn", fake_spawn)
    monkeypatch.setattr(
        "homebase.backup.deliver_notifications",
        lambda messages, include_amounts=False: notes.append((messages, include_amounts)) or False,
    )
    with pytest.raises(HomeBaseError, match="repository does not exist"):
        backup_run()
    assert cwds and not cwds[0].exists()
    assert notes and notes[0][1] is False
    assert "$" not in notes[0][0][0]
    assert SECRET not in notes[0][0][0]
    conn = open_db()
    try:
        detail = json.loads(
            conn.execute("SELECT detail FROM audit_log WHERE action = 'backup'").fetchone()[0]
        )
    finally:
        conn.close()
    assert detail["status"] == "failed"
    assert detail["snapshot_id"] is None


def test_restore_never_writes_the_live_database(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _use_data_dir(monkeypatch, data, _password_file(tmp_path))
    conn = open_db()
    ingest_receipt(conn, RECEIPT)
    conn.close()
    live = data / "homebase.db"
    inode = live.stat().st_ino
    before = _txn_count(live)
    called = []
    monkeypatch.setattr(
        "homebase.backup._spawn",
        lambda cmd, env, cwd: called.append(cmd) or (_ for _ in ()).throw(AssertionError("restic should not run")),
    )
    with pytest.raises(HomeBaseError):
        backup_restore("abcdef1234567890", data)
    with pytest.raises(HomeBaseError):
        backup_restore("abcdef1234567890", data / "nested")
    with pytest.raises(HomeBaseError):
        backup_restore("abcdef1234567890", tmp_path)
    with pytest.raises(HomeBaseError):
        backup_restore("not a snapshot", tmp_path / "out")
    assert called == []
    assert live.stat().st_ino == inode
    assert _txn_count(live) == before
    assert not (data / "nested").exists()

    calls = []

    def fake_spawn(cmd, env, cwd):
        calls.append(cmd)
        return _completed(cmd)

    monkeypatch.setattr("homebase.backup._spawn", fake_spawn)
    target = tmp_path / "restored"
    backup_restore("abcdef1234567890", target)
    assert live.stat().st_ino == inode
    assert _txn_count(live) == before
    assert target.resolve() != data.resolve()
    assert any("restore" in cmd and str(target.resolve()) in cmd for cmd in calls)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert_restore_target_safe(target, live)


def test_test_restore_verifies_a_balanced_snapshot_then_deletes_it(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _use_data_dir(monkeypatch, data, _password_file(tmp_path))
    conn = open_db()
    ingest_receipt(conn, RECEIPT)
    conn.close()
    good = (data / "homebase.db").read_bytes()
    targets = []

    def fake_spawn(cmd, env, cwd):
        target = Path(cmd[cmd.index("--target") + 1])
        targets.append(target)
        (target / "ledger.db").write_bytes(good)
        return _completed(cmd)

    monkeypatch.setattr("homebase.backup._spawn", fake_spawn)
    version = backup_test_restore()
    assert version == 1
    assert targets and not targets[0].exists()
    conn = open_db()
    try:
        detail = json.loads(
            conn.execute("SELECT detail FROM audit_log WHERE action = 'test_restore'").fetchone()[0]
        )
    finally:
        conn.close()
    assert detail == {"snapshot_id": "latest", "status": "ok", "target": "primary"}


def test_test_restore_rejects_an_unbalanced_journal(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _use_data_dir(monkeypatch, data, _password_file(tmp_path))
    conn = open_db()
    conn.close()
    bad = tmp_path / "bad.db"
    broken = open_db(bad)
    broken.execute("INSERT INTO merchants (name) VALUES ('X')")
    merchant_id = broken.execute("SELECT merchant_id FROM merchants").fetchone()[0]
    broken.execute(
        """
        INSERT INTO transactions
            (date, merchant_id, payment_method, raw_subtotal, calculated_tax, total_amount)
        VALUES ('2026-01-01', ?, 'Cash', 100, 0, 100)
        """,
        (merchant_id,),
    )
    txn_id = broken.execute("SELECT transaction_id FROM transactions").fetchone()[0]
    groceries = broken.execute("SELECT account_id FROM accounts WHERE account_name = 'Groceries'").fetchone()[0]
    checking = broken.execute(
        "SELECT account_id FROM accounts WHERE account_name = 'Checking Account'"
    ).fetchone()[0]
    broken.execute(
        "INSERT INTO journal_entries (transaction_id, account_id, amount) VALUES (?, ?, 100)",
        (txn_id, groceries),
    )
    broken.execute(
        "INSERT INTO journal_entries (transaction_id, account_id, amount) VALUES (?, ?, -40)",
        (txn_id, checking),
    )
    broken.close()
    payload = bad.read_bytes()
    targets = []
    notes = []

    def fake_spawn(cmd, env, cwd):
        target = Path(cmd[cmd.index("--target") + 1])
        targets.append(target)
        (target / "ledger.db").write_bytes(payload)
        return _completed(cmd)

    monkeypatch.setattr("homebase.backup._spawn", fake_spawn)
    monkeypatch.setattr(
        "homebase.backup.deliver_notifications",
        lambda messages, include_amounts=False: notes.append(messages) or False,
    )
    with pytest.raises(LedgerImbalanceError) as caught:
        backup_test_restore()
    assert caught.value.balance_cents == 60
    assert targets and not targets[0].exists()
    assert notes and "$" not in notes[0][0]
    conn = open_db()
    try:
        detail = json.loads(
            conn.execute("SELECT detail FROM audit_log WHERE action = 'test_restore'").fetchone()[0]
        )
    finally:
        conn.close()
    assert detail["status"] == "failed"


def test_check_reads_a_data_subset_and_local_copy_is_optional(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _use_data_dir(monkeypatch, data, _password_file(tmp_path))
    conn = open_db()
    conn.close()
    calls = []

    def fake_spawn(cmd, env, cwd):
        calls.append(list(cmd))
        if "snapshots" in cmd:
            return _completed(cmd, stdout='[{"id":"abcdef1234567890"}]')
        return _completed(cmd)

    monkeypatch.setattr("homebase.backup._spawn", fake_spawn)
    from homebase.backup import backup_check, local_repository

    assert local_repository() is None
    backup_check()
    assert any("--read-data-subset=10%" in arg for cmd in calls for arg in cmd)

    monkeypatch.setenv("HOMEBASE_BACKUP_LOCAL_REPOSITORY", str(tmp_path / "usb-repo"))
    seen = []

    def fake_spawn_both(cmd, env, cwd):
        seen.append(env["RESTIC_REPOSITORY"])
        if "snapshots" in cmd:
            return _completed(cmd, stdout='[{"id":"abcdef1234567890"}]')
        return _completed(cmd)

    monkeypatch.setattr("homebase.backup._spawn", fake_spawn_both)
    backup_run()
    assert seen.count("rclone:gdrive:homebase-backups") == 3
    assert seen.count(str(tmp_path / "usb-repo")) == 3


def _txn_count(path: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()
