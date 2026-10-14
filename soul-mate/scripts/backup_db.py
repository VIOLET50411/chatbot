#!/usr/bin/env python3
"""Create a consistent backup of every Soul Mate SQLite database and safe metadata."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _notify(title: str, detail: str) -> None:
    webhook = os.getenv("OPS_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    try:
        request = urllib.request.Request(
            webhook,
            data=json.dumps({"title": title, "text": detail}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=10)
    except Exception as exc:
        print(f"WEBHOOK_ERROR {type(exc).__name__}")


def _sqlite_backup(source_path: Path, target_path: Path) -> list[tuple[str]]:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path, timeout=30)
    destination = sqlite3.connect(target_path, timeout=30)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    check = sqlite3.connect(target_path, timeout=10)
    try:
        return check.execute("PRAGMA integrity_check").fetchall()
    finally:
        check.close()


def _prune(backup_dir: Path, keep: int) -> None:
    snapshots = sorted(
        (path for path in backup_dir.glob("soul-mate-*") if path.is_dir()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in snapshots[keep:]:
        shutil.rmtree(old)
        print(f"PRUNED {old}")


def _safe_metadata_files(data_dir: Path) -> list[Path]:
    registry = data_dir / "registry"
    if not registry.is_dir():
        return []
    return sorted(path for path in registry.rglob("*.json") if path.is_file())


def _safe_asset_files(data_dir: Path) -> list[Path]:
    """备份用户素材与已落盘附件，不复制日志、Cookie 或可重新下载的模型缓存。"""
    roots = [data_dir / "corpus"]
    characters = data_dir / "characters"
    if characters.is_dir():
        for character_dir in characters.iterdir():
            if not character_dir.is_dir():
                continue
            roots.extend((character_dir / "attachments", character_dir / "corpus"))
    result: list[Path] = []
    for root in roots:
        if root.is_dir():
            result.extend(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    return sorted(set(result))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        default=os.getenv("DATA_DIR", "./data"),
        help="Soul Mate data directory; defaults to DATA_DIR or ./data",
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="Backup directory; defaults to a sibling backups directory",
    )
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    database_paths = sorted(path for path in data_dir.rglob("*.db") if path.is_file())
    if not database_paths:
        print(f"DB_NOT_FOUND {data_dir}")
        return 2

    backup_dir = (
        Path(args.backup_dir).resolve()
        if args.backup_dir
        else (data_dir.parent / "backups").resolve()
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d%H%M%S")
    snapshot_dir = backup_dir / f"soul-mate-{timestamp}"
    partial_dir = backup_dir / f".soul-mate-{timestamp}.partial"
    if snapshot_dir.exists() or partial_dir.exists():
        print(f"BACKUP_EXISTS {snapshot_dir}")
        return 2

    manifest: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format_version": 2,
        "databases": [],
        "metadata": [],
        "assets": [],
        "excluded": [".env", "cookies", "logs", "model caches", "plaintext secrets"],
    }
    try:
        partial_dir.mkdir(parents=True)
        for source_path in database_paths:
            relative = source_path.relative_to(data_dir)
            target_path = partial_dir / "data" / relative
            integrity = _sqlite_backup(source_path, target_path)
            if not all(str(row[0]) == "ok" for row in integrity):
                raise RuntimeError(f"integrity check failed for {relative}")
            manifest["databases"].append(relative.as_posix())  # type: ignore[union-attr]
            print(f"DATABASE_OK {relative.as_posix()}")

        for source_path in _safe_metadata_files(data_dir):
            relative = source_path.relative_to(data_dir)
            target_path = partial_dir / "data" / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            manifest["metadata"].append(relative.as_posix())  # type: ignore[union-attr]

        for source_path in _safe_asset_files(data_dir):
            relative = source_path.relative_to(data_dir)
            target_path = partial_dir / "data" / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            manifest["assets"].append(relative.as_posix())  # type: ignore[union-attr]

        (partial_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        partial_dir.rename(snapshot_dir)
    except Exception as exc:
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        _notify("Soul Mate backup failed", type(exc).__name__)
        print(f"BACKUP_ERROR {type(exc).__name__}: {exc}")
        return 1

    print(f"BACKUP_PATH={snapshot_dir}")
    print(f"DATABASE_COUNT={len(database_paths)}")
    print(f"METADATA_COUNT={len(manifest['metadata'])}")
    print(f"ASSET_COUNT={len(manifest['assets'])}")
    _prune(backup_dir, max(1, args.keep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
