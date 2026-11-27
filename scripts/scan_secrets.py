"""发布前敏感信息扫描：检查仓库/发布副本里是否有 API Key、令牌、私钥等疑似泄露。

用法::

    python scripts/scan_secrets.py <目录>
    python scripts/scan_secrets.py .            # 扫描主仓库（跳过 .git/.venv/node_modules/backups 等）
    python scripts/scan_secrets.py ../TracePilot-release

退出码：0 = 未发现；1 = 发现疑似敏感信息（只打印掩码片段，不打印完整值）。

注意：本脚本检查工作区文件，不替代 `git log -S`/`git grep` 的历史检查；
Git 历史中的密钥扫描建议：`git log --all -p | rg "(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}"`。
"""

import re
import sys
from pathlib import Path

PATTERNS = {
    "deepseek_key": re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    "modelscope_token": re.compile(r"\bms-[A-Za-z0-9]{10,}\b"),
    "admin_token_assignment": re.compile(r"TRACEPILOT_ADMIN_TOKEN\s*=\s*[A-Za-z0-9]{16,}"),
    # 只匹配带真实密钥体的私钥块（测试里单独一行 header 的负例不算泄露）
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,40}?[A-Za-z0-9+/=]{40,}"),
}
SKIP_DIRS = {"node_modules", "__pycache__", ".pytest_cache", "dist", "backups", "data.polluted_backup", "test-results", "playwright-report"}
SKIP_DIRS_RELATIVE = {".git", ".venv"}  # 仅跳过根目录下的版本库/虚拟环境
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".zip", ".pyc", ".db", ".sqlite3", ".map"}

root = Path(sys.argv[1]).resolve()
hits = []
for path in root.rglob("*"):
    if not path.is_file():
        continue
    rel = path.relative_to(root)
    if rel.parts and rel.parts[0] in SKIP_DIRS_RELATIVE:
        continue
    if set(rel.parts) & SKIP_DIRS:
        continue
    # 本地 .env 是 gitignore 的私密配置，不属于可发布内容；由部署流程与历史扫描单独保证
    if path.name == ".env" or path.name.startswith(".env."):
        continue
    if path.suffix.lower() in SKIP_SUFFIX:
        continue
    try:
        if path.stat().st_size > 3_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for name, pat in PATTERNS.items():
        for m in pat.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            snippet = m.group(0)
            masked = snippet[:6] + "***" + snippet[-4:] if len(snippet) > 12 else "***"
            hits.append((str(rel), line_no, name, masked))
print(f"scanned tree: {root}")
if hits:
    for h in hits:
        print("HIT", h)
    sys.exit(1)
print("no secret-like patterns found")
