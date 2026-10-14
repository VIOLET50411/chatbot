"""在不打印明文的前提下，为本地核心和宿主桥接生成共享令牌。"""
from __future__ import annotations

import secrets
from pathlib import Path


def ensure_bridge_token(env_path: Path) -> bool:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    found_index: int | None = None
    existing = ""
    for index, line in enumerate(lines):
        if line.startswith("WECHAT_BRIDGE_TOKEN="):
            found_index = index
            existing = line.partition("=")[2].strip()
            break
    if existing:
        return False
    replacement = f"WECHAT_BRIDGE_TOKEN={secrets.token_urlsafe(36)}"
    if found_index is None:
        lines.append(replacement)
    else:
        lines[found_index] = replacement
    temporary = env_path.with_suffix(".env.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(env_path)
    return True


if __name__ == "__main__":
    changed = ensure_bridge_token(Path(__file__).resolve().parents[1] / ".env")
    print("WECHAT_BRIDGE_TOKEN_CREATED" if changed else "WECHAT_BRIDGE_TOKEN_ALREADY_SET")
