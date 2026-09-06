#!/bin/bash
# Small Jobs – Mac Launcher

cd "$(dirname "$0")"

echo ""
echo " =============================="
echo "  Small Jobs Server Launcher"
echo " =============================="
echo ""

# Kill anything already on port 5001
echo " Checking port 5001 for existing server..."
lsof -ti tcp:5001 | xargs kill -9 2>/dev/null && echo " Stopped old server." || echo " Port was clear."

sleep 1

# Activate venv
source venv/bin/activate

# Open browser after Flask has had time to boot
(sleep 3 && open http://127.0.0.1:5001) &

echo " Starting Flask server (Ctrl+C to stop)..."
echo ""
python scripts/small_jobs.py
