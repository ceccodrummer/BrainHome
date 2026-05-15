import re
from pathlib import Path

MAIN_PATH = Path(__file__).resolve().parent.parent / "services" / "fastapi" / "main.py"
VERSION_PATTERN = re.compile(r'^(MOBILE_UI_VERSION\s*=\s*")(\d+)\.(\d+)\.(\d+)(")$', re.MULTILINE)

if not MAIN_PATH.exists():
    raise FileNotFoundError(MAIN_PATH)

content = MAIN_PATH.read_text(encoding="utf-8")
match = VERSION_PATTERN.search(content)
if not match:
    raise ValueError("MOBILE_UI_VERSION not found in main.py")

prefix, major, minor, patch, suffix = match.groups()
new_patch = int(patch) + 1
new_version = f"{major}.{minor}.{new_patch}"
new_content = VERSION_PATTERN.sub(f"\1{new_version}\5", content, count=1)
MAIN_PATH.write_text(new_content, encoding="utf-8")
print(f"Bumped frontend version: {patch} -> {new_version}")
