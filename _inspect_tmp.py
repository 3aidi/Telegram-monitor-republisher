"""One-off inspection helper (deleted after the audit)."""
import glob
import os
import re

os.chdir(os.path.dirname(os.path.abspath(__file__)))

print("=== all env vars read in py files ===")
pat = re.compile(r"os\.(?:environ\.get|environ\[|getenv)\(\s*[\"']([A-Z_]{3,})[\"']")
found = set()
for f in ("main.py", "admin_bot.py", "parser.py", "db.py", "filters.py",
          "ai_rephraser.py", "publish_guard.py", "countries.py"):
    with open(f, encoding="utf-8") as fh:
        found.update(pat.findall(fh.read()))
for name in sorted(found):
    print(name)

print()
print("=== ps1 encoding check ===")
for f in sorted(glob.glob("*.ps1")):
    b = open(f, "rb").read()
    na = [(i, hex(c)) for i, c in enumerate(b) if c > 127]
    print(f, "BOM" if b[:3] == b"\xef\xbb\xbf" else "no-BOM", "nonascii:", na[:5])

print()
print("=== requirements/env example present? ===")
print("requirements.txt:", os.path.exists("requirements.txt"))
print(".env.example:", os.path.exists(".env.example"))

print()
print("=== deploy.yml present? ===")
print(".github/workflows/deploy.yml:", os.path.exists(".github/workflows/deploy.yml"))

print()
print("=== .env keys (names only, values redacted) ===")
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8", errors="replace"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            print(line.split("=", 1)[0])