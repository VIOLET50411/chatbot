"""导出可迁移的数据目录：SQLite 使用在线备份，其余私有资产逐文件复制。"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


SKIP_DIRS = {"models", ".pytest_cache", "__pycache__"}
SKIP_SUFFIXES = {"-wal", "-shm"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_sqlite(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        source_connection.backup(target_connection)
        result = target_connection.execute("PRAGMA integrity_check").fetchall()
        if any(str(row[0]) != "ok" for row in result):
            raise RuntimeError(f"数据库完整性检查失败：{source}")
    finally:
        target_connection.close()
        source_connection.close()


def export_data(source: Path, target: Path) -> dict[str, object]:
    source = source.resolve()
    target = target.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"数据目录不存在：{source}")
    if source == target or source in target.parents:
        raise ValueError("导出目录不能位于源数据目录内部")
    target.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, object]] = []
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if item.is_symlink() or item.is_dir() or item.name.startswith(".env") or any(item.name.endswith(suffix) for suffix in SKIP_SUFFIXES):
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if item.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            _backup_sqlite(item, destination)
        else:
            shutil.copy2(item, destination)
        files.append({"path": relative.as_posix(), "size": destination.stat().st_size, "sha256": _sha256(destination)})
    manifest = {"format": "soul-mate-vps-data-v1", "files": files}
    (target / "EXPORT_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data")
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    manifest = export_data(Path(args.source), Path(args.target))
    print(json.dumps({"status": "ok", "file_count": len(manifest["files"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
