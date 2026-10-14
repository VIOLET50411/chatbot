from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.export_vps_data import export_data


class VpsExportTests(unittest.TestCase):
    def test_export_uses_valid_sqlite_backup_and_skips_models_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "data"
            source.mkdir()
            connection = sqlite3.connect(source / "soul_mate.db")
            try:
                connection.execute("CREATE TABLE sample(value TEXT)")
                connection.execute("INSERT INTO sample(value) VALUES ('纪雨')")
                connection.commit()
            finally:
                connection.close()
            (source / "attachment.txt").write_text("私有素材", encoding="utf-8")
            (source / ".env.private").write_text("SECRET=never-copy", encoding="utf-8")
            (source / "models").mkdir()
            (source / "models" / "large.bin").write_bytes(b"model")

            target = root / "exported"
            manifest = export_data(source, target)

            check = sqlite3.connect(target / "soul_mate.db")
            try:
                self.assertEqual(check.execute("SELECT value FROM sample").fetchone()[0], "纪雨")
            finally:
                check.close()
            self.assertTrue((target / "attachment.txt").is_file())
            self.assertFalse((target / ".env.private").exists())
            self.assertFalse((target / "models").exists())
            saved = json.loads((target / "EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["format"], "soul-mate-vps-data-v1")
            self.assertEqual(len(saved["files"]), len(manifest["files"]))


if __name__ == "__main__":
    unittest.main()
