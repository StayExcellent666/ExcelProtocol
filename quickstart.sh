#!/bin/bash

# Quick Start Script for Twitch Notifier Bot
# This script helps you set up the bot quickly

echo "🎮 Twitch Notifier Bot - Quick Start"
echo "===================================="
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.8 or higher."
    exit 1
fi

echo "✅ Python found: $(python3 --version)"
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "📝 Creating .env file from template..."
    cp .env.example .env
    echo "✅ Created .env file"
    echo ""
    echo "⚠️  IMPORTANT: Edit .env file with your credentials:"
    echo "   - DISCORD_TOKEN"
    echo "   - TWITCH_CLIENT_ID"
    echo "   - TWITCH_CLIENT_SECRET"
    echo ""
    echo "Press Enter when you've added your credentials..."
    read
else
    echo "✅ .env file already exists"
fi

# Install dependencies
echo ""
echo "📦 Installing dependencies..."
pip3 install -r requirements.txt

if [ $? -eq 0 ]; then
    echo "✅ Dependencies installed"
else
    echo "❌ Failed to install dependencies"
    exit 1
fi

echo ""
echo "🚀 Setup complete!"
echo ""
echo "To start the bot:"
echo "  python3 bot.py"
echo ""
echo "To deploy to Fly.io:"
echo "  See DEPLOYMENT.md"
echo ""
echo "For testing:"
echo "  See TESTING.md"
echo ""
