# Developer convenience targets. The actual logic lives in scripts/.

.PHONY: run stop logs install test lint check

# One-command dev startup:
#   - kills any leftover ngrok/uvicorn on port 8000
#   - starts ngrok in background
#   - writes the new public URL into .env (BASE_URL)
#   - prints the URL to paste into Twilio sandbox
#   - starts uvicorn --reload in the foreground
# Ctrl-C kills both cleanly.
run:
	@./scripts/dev_run.sh

# Kill stray ngrok / uvicorn (if a previous `make run` exited messily).
stop:
	@pkill -f "ngrok http 8000" 2>/dev/null || true
	@pkill -f "uvicorn main:app" 2>/dev/null || true
	@echo "✓ Stopped (if running)."

# Tail ngrok's log — useful if `make run` reports a problem.
logs:
	@tail -f /tmp/medagent_ngrok.log

# One-time setup after cloning: install deps + Playwright browser.
install:
	uv sync
	uv run playwright install chromium

# Run the full test suite.
test:
	uv run pytest -q

# Lint.
lint:
	uv run ruff check .

# Tests + lint + migration checks (what CI runs).
check:
	uv run ruff check .
	uv run pytest -q
	uv run alembic upgrade head
	uv run alembic check
