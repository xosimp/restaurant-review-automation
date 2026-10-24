#!/bin/bash
# Is this Mac ready to work on Cavnar AI? Prints what is present and what is
# missing; never prints a secret (only fingerprints). Run it on a new or
# migrated Mac before wiping the old one:  bash scripts/check_workstation.sh
cd "$(dirname "$0")/.." || exit 1
ok()   { printf "  ok    %s\n" "$1"; }
bad()  { printf "  MISSING  %s\n" "$1"; }
echo "== machine: $(hostname -s) · $(sysctl -n machdep.cpu.brand_string 2>/dev/null) · macOS $(sw_vers -productVersion)"

echo "== code"
git fetch -q origin 2>/dev/null
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main 2>/dev/null)" ] && ok "repo at origin/main $(git rev-parse --short HEAD)" \
  || echo "  note  repo at $(git rev-parse --short HEAD), origin/main is $(git rev-parse --short origin/main 2>/dev/null) — git pull"
[ -f .env ] && ok ".env present (sha256 $(shasum -a 256 .env | cut -c1-16))" || bad ".env (production keys) — copy it from the old Mac"
M="$HOME/.claude/projects/-Users-simp-review-automation/memory"
[ -d "$M/.git" ] && ok "Claude memory repo ($(git -C "$M" status -sb | head -1))" || bad "Claude memory repo at $M"

echo "== access"
[ -f ~/.ssh/id_ed25519.pub ] && ok "ssh key $(ssh-keygen -lf ~/.ssh/id_ed25519.pub | awk '{print $2, $3}')" || bad "~/.ssh/id_ed25519 (railway ssh)"
command -v railway >/dev/null && { railway whoami 2>&1 | grep -q "Logged in" && ok "railway: $(railway whoami 2>&1 | tail -1)" || bad "railway login (run: railway login)"; } || bad "railway CLI (brew install railway)"
command -v npx >/dev/null && { npx --yes wrangler whoami 2>/dev/null | grep -qi "logged in" && ok "wrangler logged in" || bad "wrangler login (run: npx wrangler login)"; } || bad "node/npx (brew install node)"
[ -f "$HOME/Library/Application Support/ngrok/ngrok.yml" ] && ok "ngrok config" || bad "ngrok config (ngrok config add-authtoken …)"
security find-generic-password -s "Cavnar AI Resend - Gmail send-as" >/dev/null 2>&1 && ok "Keychain: Resend Gmail send-as key" || bad "Keychain: Resend Gmail send-as key"

echo "== tools"
# The app and the suite run on .venv: Python 3.12.7 (.python-version, what
# Railway and CI run) with the lock installed. A migrated uv interpreter
# crashes in ensurepip, so build it with uv itself (10/24/26).
PY=.venv/bin/python
[ -x "$PY" ] && [ "$($PY --version 2>&1 | cut -d' ' -f2)" = "$(cat .python-version)" ] \
  && ok ".venv python $($PY --version 2>&1 | cut -d' ' -f2) (matches .python-version)" \
  || bad ".venv on $(cat .python-version) (brew install uv && uv venv --python $(cat .python-version) .venv && VIRTUAL_ENV=.venv uv pip install -r requirements.txt -r requirements-dev.txt)"
[ -x "$PY" ] && $PY -c "import flask, anthropic, resend, requests, stripe, psycopg, ortools, pytest, xdist" 2>/dev/null \
  && ok ".venv has the lock and the test packages" || bad ".venv packages (VIRTUAL_ENV=.venv uv pip install -r requirements.txt -r requirements-dev.txt)"
command -v gh >/dev/null && { gh auth status >/dev/null 2>&1 && ok "gh logged in" || echo "  note  gh installed, not logged in (gh auth login)"; } || echo "  note  gh not installed (brew install gh) — CI runs and PRs"
curl -s -o /dev/null -w "%{http_code}" localhost:5050/health 2>/dev/null | grep -q 200 && ok "local backend on :5050" \
  || echo "  note  local backend not running (nohup env PORT=5050 .venv/bin/python hosted_dashboard.py &)"
command -v xcodebuild >/dev/null && ok "$(xcodebuild -version 2>/dev/null | head -1)" || bad "Xcode"
security find-identity -v -p codesigning 2>/dev/null | grep -q "Apple Development" && ok "Apple Development signing identity" || bad "Apple Development signing certificate (Xcode → Settings → Accounts)"
[ -d ios/CavnarAI/CavnarAI.xcodeproj ] && ok "iOS project generated" || echo "  note  iOS project not generated yet — run ios/CavnarAI/generate.sh"
