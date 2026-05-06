#!/bin/bash

# ExcelProtocol — Quick Start
# Sets up local dev. Production deploys go through GitHub Actions to Fly.io.

set -e

echo "🤖 ExcelProtocol — Quick Start"
echo "=============================="
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Install Python 3.11 or higher."
    exit 1
fi
echo "✅ Python found: $(python3 --version)"
echo ""

# Check .env
if [ ! -f .env ]; then
    echo "📝 No .env file found. Creating a template..."
    cat > .env <<'ENV'
# Required
DISCORD_TOKEN=
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=

# Required for stream notifications (HMAC secret for Twitch EventSub callbacks)
EVENTSUB_SECRET=

# Required for the dashboard
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
DISCORD_REDIRECT_URI=http://localhost:8080/auth/callback

# Optional
BOT_OWNER_ID=
TWITCH_BOT_USERNAME=
TWITCH_BOT_TOKEN=
DASHBOARD_BASE_URL=http://localhost:8080
DEV_TOKEN=
ENV
    echo "✅ Created .env template — fill in the required values before running."
    echo ""
    echo "Press Enter when ready..."
    read
else
    echo "✅ .env exists"
fi

# Install deps
echo ""
echo "📦 Installing dependencies..."
pip3 install -r requirements.txt
echo "✅ Dependencies installed"

echo ""
echo "🚀 Setup complete."
echo ""
echo "To run locally:"
echo "  python3 bot.py"
echo ""
echo "Dashboard (after bot starts):"
echo "  http://localhost:8080/app/"
echo ""
echo "Production deploys happen automatically on push to main via GitHub Actions"
echo "(see .github/workflows/deploy.yml)."
echo ""
