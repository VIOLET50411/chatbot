from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"


class WindowsPowerShellScriptTests(unittest.TestCase):
    def test_all_public_powershell_scripts_are_ascii_for_windows_powershell_51(self) -> None:
        scripts = sorted(SCRIPTS.glob("*.ps1"))
        self.assertTrue(scripts)
        for script in scripts:
            content = script.read_bytes()
            self.assertFalse(content.startswith(b"\xef\xbb\xbf"), script.name)
            try:
                content.decode("ascii")
            except UnicodeDecodeError as exc:
                self.fail(f"{script.name} contains non-ASCII source that PowerShell 5.1 may misread: {exc}")

    def test_vps_migration_requires_single_writer_confirmation_and_health_check(self) -> None:
        script = (SCRIPTS / "migrate_to_vps.ps1").read_text(encoding="ascii")
        self.assertIn("IUnderstandOnlyOneCoreMayWrite", script)
        self.assertIn("docker compose", script)
        self.assertIn("/healthz", script)
        self.assertIn("set_core_mode.ps1", script)
        self.assertIn("Restoring the local core", script)

    def test_tailscale_serve_is_private_and_persistent(self) -> None:
        script = (SCRIPTS / "configure_tailscale_serve.ps1").read_text(encoding="ascii")
        self.assertIn("serve --bg --yes 8080", script)
        self.assertIn("serve reset", script)
        self.assertNotIn("funnel", script.lower())

    def test_bridge_launcher_supports_one_account_canary(self) -> None:
        script = (SCRIPTS / "start_wxautox_bridge.ps1").read_text(encoding="ascii")
        self.assertIn("[string]$AccountId", script)
        self.assertIn("'--account-id'", script)
        self.assertIn("$authText.IndexOf('{')", script)
        self.assertIn("$authText.LastIndexOf('}')", script)
        self.assertIn("ConvertFrom-Json -ErrorAction Stop", script)
        self.assertNotIn("Where-Object { $_ -match '^\\{.*\\}$' }", script)

    def test_bridge_preflight_reports_client_and_plus_compatibility(self) -> None:
        from scripts.wxautox_bridge_agent import _window_failure_hint

        hint = _window_failure_hint(
            {
                "windows_found": 2,
                "minimized_windows": 0,
                "client_versions": ["4.1.12.26"],
            },
            "41.1.1.post1",
        )

        self.assertIn("4.1.12.26", hint)
        self.assertIn("41.1.1.post1", hint)
        self.assertIn("4.1.9.x", hint)

        supported_hint = _window_failure_hint(
            {
                "windows_found": 2,
                "minimized_windows": 0,
                "client_versions": ["4.1.8.107"],
            },
            "41.1.1.post1",
        )
        self.assertIn("官方文档支持范围", supported_hint)
        self.assertIn("同一个管理员 PowerShell", supported_hint)
        self.assertNotIn("固定到兼容的 4.1.9.x", supported_hint)


if __name__ == "__main__":
    unittest.main()
