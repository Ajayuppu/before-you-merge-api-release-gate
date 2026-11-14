#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/build/lambda"
rm -rf "$DEST"
mkdir -p "$DEST/fixtures/mocks"
cp -a "$ROOT/src/apireview" "$DEST/apireview"
cp -a "$ROOT/fixtures/mocks/." "$DEST/fixtures/mocks/"
find "$DEST" -type d -name __pycache__ -prune -exec rm -rf {} +
(
  cd "$DEST"
  zip -qr "$ROOT/build/lambda.zip" apireview fixtures
)
python3 - "$ROOT/build/lambda.zip" <<'PY'
import sys
import zipfile
archive = zipfile.ZipFile(sys.argv[1])
names = set(archive.namelist())
required = {
    "apireview/handlers/lambda_handlers.py",
    "apireview/signed_mock.py",
    "fixtures/mocks/orders-v1.json",
    "fixtures/mocks/orders-v2-broken.json",
    "fixtures/mocks/orders-v2-corrected.json",
}
missing = required - names
if missing:
    raise SystemExit(f"lambda zip is missing {sorted(missing)}")
print(f"wrote {sys.argv[1]} with {len(names)} entries")
PY
