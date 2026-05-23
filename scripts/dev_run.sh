#!/usr/bin/env bash
# One-command local dev startup.
#
# Starts ngrok against port 8000, polls its local API for the public HTTPS
# URL, writes it into .env as BASE_URL, prints the Twilio webhook URL to
# paste manually, then starts uvicorn in the foreground.
#
# Ctrl-C kills both processes cleanly.

set -euo pipefail

PORT=8000
ENV_FILE=".env"
NGROK_LOG="/tmp/medagent_ngrok.log"

# ---- preflight ----

if ! command -v ngrok >/dev/null 2>&1; then
    echo "✗ ngrok not found in PATH."
    echo "  Install: brew install ngrok"
    echo "  Auth:    ngrok config add-authtoken <your-token-from-ngrok.com>"
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "✗ .env not found. Copy .env.example to .env first."
    exit 1
fi

# ---- kill any leftovers ----

# Only kill ngrok tunnels pointing at our port — preserves any other ngrok
# tunnels you might have for unrelated work.
pkill -f "ngrok http $PORT" 2>/dev/null || true
pkill -f "uvicorn main:app" 2>/dev/null || true
sleep 0.5

# ---- start ngrok in background ----

echo "→ Starting ngrok on port $PORT..."
ngrok http "$PORT" --log=stdout > "$NGROK_LOG" 2>&1 &
NGROK_PID=$!

# ---- clean shutdown ----

cleanup() {
    echo ""
    echo "→ Shutting down ngrok (pid $NGROK_PID)..."
    kill "$NGROK_PID" 2>/dev/null || true
    wait "$NGROK_PID" 2>/dev/null || true
    echo "✓ Stopped."
}
trap cleanup EXIT INT TERM

# ---- poll ngrok's local API for the tunnel URL ----

URL=""
for _ in {1..20}; do
    sleep 0.5
    URL=$(curl -fs http://127.0.0.1:4040/api/tunnels 2>/dev/null \
        | python3 -c "
import sys, json
try:
    tunnels = json.load(sys.stdin).get('tunnels', [])
    for t in tunnels:
        if t.get('public_url', '').startswith('https'):
            print(t['public_url'])
            break
except Exception:
    pass
" 2>/dev/null || true)
    if [[ "$URL" == https://* ]]; then
        break
    fi
done

if [[ -z "$URL" ]]; then
    echo "✗ ngrok didn't expose an HTTPS tunnel in time."
    echo "  Tail of $NGROK_LOG:"
    tail -20 "$NGROK_LOG" 2>/dev/null || true
    exit 1
fi

echo "✓ ngrok tunnel: $URL"

# ---- update .env ----

python3 - <<PY
import re, pathlib
env_path = pathlib.Path("$ENV_FILE")
text = env_path.read_text()
url = "$URL"
if re.search(r"^BASE_URL=", text, re.M):
    new = re.sub(r"^BASE_URL=.*$", f"BASE_URL={url}", text, flags=re.M)
else:
    new = text.rstrip("\n") + f"\nBASE_URL={url}\n"
env_path.write_text(new)
print(f"✓ Updated .env: BASE_URL={url}")
PY

# ---- reminder for the one manual step ----

cat <<EOF

══════════════════════════════════════════════════════════════════════
  Paste this into Twilio's sandbox "When a message comes in" field
  (https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn?frameUrl=%2Fconsole%2Fsms%2Fwhatsapp%2Flearn%3Fx-target-region%3Dus1):

  $URL/webhooks/whatsapp

  (Twilio sandbox config can't be set programmatically — paid numbers can.)
══════════════════════════════════════════════════════════════════════

ngrok web inspector: http://127.0.0.1:4040
Press Ctrl-C to stop both uvicorn and ngrok.

EOF

# ---- start uvicorn (foreground) ----

uv run uvicorn main:app --reload --port "$PORT"
