#!/usr/bin/env bash
# Builds the Skill Hub frontend and stages the runtime code in app/, the Databricks App source
# directory (resources/app.yml: source_code_path = ../app). The copies are git-ignored.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Vite writes the production bundle to app/backend/static/.
# Skip the build while app/frontend/package.json is absent.
if [ -f "$ROOT/app/frontend/package.json" ]; then
  (cd "$ROOT/app/frontend" && npm ci && npm run build)
else
  echo "app/frontend/package.json not found: skipping the frontend build"
fi

rm -rf "$ROOT/app/skillhub" "$ROOT/app/config"
cp -R "$ROOT/src/skillhub" "$ROOT/app/skillhub"
cp -R "$ROOT/config" "$ROOT/app/config"
# Runtime dependencies, without dev groups or the local project (the code is copied above).
(cd "$ROOT" && uv export --frozen --no-dev --no-hashes --no-emit-project --extra app > "$ROOT/app/requirements.txt")
echo "staged $ROOT/app"
