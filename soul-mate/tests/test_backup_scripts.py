from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BackupScriptsTests(unittest.TestCase):
    def _create_db(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
            connection.execute("INSERT INTO sample(value) VALUES (?)", (value,))
            connection.commit()
        finally:
            connection.close()

    def test_backup_covers_all_databases_and_excludes_plaintext_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            data_dir = root / "data"
            backup_dir = root / "backups"
            self._create_db(data_dir / "soul_mate.db", "root")
            self._create_db(data_dir / "platform" / "owner_profile.db", "profile")
            registry = data_dir / "registry" / "characters.json"
            registry.parent.mkdir(parents=True)
            registry.write_text('{"version": 1}', encoding="utf-8")
            corpus = data_dir / "corpus" / "chat.txt"
            corpus.parent.mkdir(parents=True)
            corpus.write_text("审核语料", encoding="utf-8")
            attachment = data_dir / "characters" / "ji-yu" / "attachments" / "image.jpg"
            attachment.parent.mkdir(parents=True)
            attachment.write_bytes(b"image")
            (data_dir / "douyin_cookie.txt").write_text("secret", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "backup_db.py"),
                    "--data-dir",
                    str(data_dir),
                    "--backup-dir",
                    str(backup_dir),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            snapshots = list(backup_dir.glob("soul-mate-*"))
            self.assertEqual(len(snapshots), 1)
            snapshot = snapshots[0]
            self.assertTrue((snapshot / "data" / "soul_mate.db").is_file())
            self.assertTrue((snapshot / "data" / "platform" / "owner_profile.db").is_file())
            self.assertTrue((snapshot / "data" / "registry" / "characters.json").is_file())
            self.assertTrue((snapshot / "data" / "corpus" / "chat.txt").is_file())
            self.assertTrue(
                (snapshot / "data" / "characters" / "ji-yu" / "attachments" / "image.jpg").is_file()
            )
            self.assertFalse((snapshot / "data" / "douyin_cookie.txt").exists())
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["databases"]), 2)
            self.assertEqual(len(manifest["assets"]), 2)

    def test_health_check_covers_nested_databases(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            data_dir = Path(raw_root) / "data"
            self._create_db(data_dir / "soul_mate.db", "root")
            self._create_db(data_dir / "characters" / "ji-yu" / "soul_mate.db", "ji-yu")

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "check_db.py"),
                    "--data-dir",
                    str(data_dir),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("DATABASE_COUNT=2", result.stdout)
            self.assertIn("characters/ji-yu/soul_mate.db", result.stdout)


if __name__ == "__main__":
    unittest.main()
