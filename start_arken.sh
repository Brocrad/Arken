#!/bin/bash

# Arken Bot Auto-Restart Manager (Linux/Mac)
echo "================================"
echo "   Arken Bot Auto-Restart"
echo "================================"
echo ""
echo "Starting bot with automatic restart capability..."
echo "Press Ctrl+C to stop the bot completely"
echo ""

cd "$(dirname "$0")"
python3 restart_bot.py

echo ""
echo "Bot manager has stopped."