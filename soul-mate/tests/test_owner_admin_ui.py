from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "web" / "frontend"


class OwnerAdminUiTests(unittest.TestCase):
    def test_navigation_is_reduced_to_five_owner_areas_without_pwa_or_roles(self) -> None:
        app = (FRONTEND_ROOT / "src" / "App.tsx").read_text(encoding="utf-8")
        shell = (FRONTEND_ROOT / "src" / "components" / "Shell.tsx").read_text(encoding="utf-8")
        for label in ("运行总览", "人设与素材", "白名单联系人", "微信与媒体", "日志与设置"):
            self.assertIn(label, shell)
        self.assertNotIn("角色工作台", shell)
        self.assertNotIn("PWA 对话", shell)
        self.assertNotIn("workspace/:characterId", app)
        self.assertIn('path="/chat" element={<PwaChatPage />} />', app)
        self.assertIn('admin-app-shell', shell)
        self.assertIn('admin-bottom-nav', shell)
        self.assertIn('aria-label="移动端主导航"', shell)

    def test_admin_shell_uses_mobile_safe_height_and_motion_contract(self) -> None:
        shell_style = (FRONTEND_ROOT / "src" / "styles" / "shell.css").read_text(encoding="utf-8")
        global_style = (FRONTEND_ROOT / "src" / "styles" / "global.css").read_text(encoding="utf-8")
        shell = (FRONTEND_ROOT / "src" / "components" / "Shell.tsx").read_text(encoding="utf-8")
        self.assertIn("admin-app-shell", shell_style)
        self.assertIn("admin-bottom-nav", shell_style)
        self.assertIn("100vh", shell_style)
        self.assertIn("safe-area-inset-bottom", shell_style)
        self.assertNotIn("100dvh", shell_style)
        self.assertIn("prefers-reduced-motion", global_style)
        self.assertNotIn("100dvh", global_style)
        self.assertIn("admin-route", shell)
        self.assertIn("theme-color", shell)

    def test_admin_pages_share_glass_hierarchy_without_changing_pwa_shell(self) -> None:
        pages_style = (FRONTEND_ROOT / "src" / "styles" / "pages.css").read_text(encoding="utf-8")
        for name in ("OverviewPage.tsx", "PersonaPage.tsx", "ContactsPage.tsx", "WechatPage.tsx", "SettingsPage.tsx"):
            page = (FRONTEND_ROOT / "src" / "pages" / name).read_text(encoding="utf-8")
            self.assertIn("admin-page", page)
        for token in ("admin-route .page-header", "backdrop-filter", "admin-route .card", "prefers-reduced-motion"):
            self.assertIn(token, pages_style)
        dialog_style = (FRONTEND_ROOT / "src" / "styles" / "dialog.css").read_text(encoding="utf-8")
        self.assertNotIn("100dvh", dialog_style)
        self.assertIn("admin-route .dialog-panel", dialog_style)
        pwa_style = (FRONTEND_ROOT / "src" / "styles" / "pwa-chat.css").read_text(encoding="utf-8")
        self.assertIn("html.pwa-route", pwa_style)

    def test_material_page_uses_real_review_contract_and_channel_separation(self) -> None:
        page = (FRONTEND_ROOT / "src" / "pages" / "PersonaPage.tsx").read_text(encoding="utf-8")
        endpoint = (FRONTEND_ROOT / "src" / "api" / "endpoints" / "owner.ts").read_text(encoding="utf-8")
        for token in (
            "微信聊天",
            "朋友圈",
            "保留",
            "排除",
            "修改后保留",
            "拆成多条",
            "你要审核的内容",
            "系统理解（仅供参考）",
            "内容分类",
        ):
            self.assertIn(token, page)
        self.assertNotIn("原始字段", page)
        self.assertNotIn("value.replaceAll('_', ' ')", page)
        for decision in ("corrected", "split", "keep", "exclude"):
            self.assertIn(decision, endpoint)
        self.assertIn("/materials/batch", endpoint)

    def test_persona_baseline_requires_diff_preview_before_activation(self) -> None:
        page = (FRONTEND_ROOT / "src" / "pages" / "PersonaPage.tsx").read_text(encoding="utf-8")
        endpoint = (FRONTEND_ROOT / "src" / "api" / "endpoints" / "owner.ts").read_text(encoding="utf-8")
        for token in ("审核后人格基线", "人格事实变化", "聊天与朋友圈风格变化", "当前联系人画像变化"):
            self.assertIn(token, page)
        self.assertIn("我已看完，确认激活", page)
        self.assertIn("/persona/baseline/drafts/", endpoint)
        self.assertIn("persona.baseline.activate", endpoint)

    def test_owner_console_exposes_character_scoped_core_controls(self) -> None:
        persona = (FRONTEND_ROOT / "src" / "pages" / "PersonaPage.tsx").read_text(encoding="utf-8")
        wechat = (FRONTEND_ROOT / "src" / "pages" / "WechatPage.tsx").read_text(encoding="utf-8")
        settings = (FRONTEND_ROOT / "src" / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
        runtime_panel = (FRONTEND_ROOT / "src" / "features" / "settings" / "RuntimeConfigurationPanel.tsx").read_text(encoding="utf-8")
        for token in ("知道的关于你的事", "系统怎样更自然地回应你", "真人感审核", "像真人", "人机感明显"):
            self.assertIn(token, persona)
        for token in ("正在编辑人物", "characters.map", "保存${selected?.displayName", "真人浏览器"):
            self.assertIn(token, runtime_panel)
        self.assertIn("item.note.includes('真人感')", persona)
        self.assertIn("item.note.includes('候选')", persona)
        self.assertIn(".sort((left, right) => right.version - left.version)", persona)
        self.assertIn("passesStrictReview", persona)
        self.assertIn("“人机感明显”为 0", persona)
        self.assertIn("两个人物无法区分", persona)
        self.assertNotIn("const v6Candidate", persona)
        self.assertNotIn("<span>纪雨候选</span>", persona)
        for token in ("敏感内容已在当前卡片中临时展开", "场景评价已保存", "人物知道这条资料"):
            self.assertIn(token, persona)
        for token in ("微信窗口顶部昵称", "这个机器人的微信号", "确认账号身份正确"):
            self.assertIn(token, wechat)
        self.assertIn("只给", settings)
        self.assertIn("补充提示词", settings)
        self.assertIn("selectedId", runtime_panel)
        self.assertIn("setConnectionResults({})", runtime_panel)
        self.assertIn("variables.characterId !== selectedId", runtime_panel)
        for token in ("测试文字模型连接", "测试识图连接", "测试语音服务连接", "测试外界信息读取", "测试备用 VPS"):
            self.assertIn(token, runtime_panel)

    def test_pwa_uses_independent_100vh_shell_and_static_manifest(self) -> None:
        page = (FRONTEND_ROOT / "src" / "pages" / "PwaChatPage.tsx").read_text(encoding="utf-8")
        style = (FRONTEND_ROOT / "src" / "styles" / "pwa-chat.css").read_text(encoding="utf-8")
        html = (FRONTEND_ROOT / "index.html").read_text(encoding="utf-8")
        for selector in ("html.pwa-route", ".pwa-chat-page", ".pwa-chat", ".pwa-token-page"):
            self.assertIn(selector, style)
        self.assertIn("height: 100vh", style)
        self.assertNotIn("100dvh", style)
        self.assertNotIn("position: fixed", style)
        self.assertIn("visualViewport", page)
        self.assertIn("pwa-keyboard-offset", page)
        self.assertIn("发送中", page)
        self.assertIn("重试", page)
        self.assertIn('href="/static/manifest.webmanifest"', html)

    @unittest.skipUnless(
        shutil.which("npm.cmd" if os.name == "nt" else "npm"),
        "npm is required for React type validation",
    )
    def test_owner_console_typechecks(self) -> None:
        npm_command = "npm.cmd" if os.name == "nt" else "npm"
        result = subprocess.run(
            [npm_command, "run", "typecheck"],
            cwd=FRONTEND_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
