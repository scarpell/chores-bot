#!/bin/bash

# Navigate to the repo directory
cd ~/chores-bot || { echo "Failed to navigate to ~/chores-bot"; exit 1; }

echo "Pulling latest changes from git..."
git pull

echo "Killing any existing instances of the bot..."
# The pkill command matches against the full command line (-f)
pkill -f "python.*bot.py" || echo "No existing process found."

echo "Checking for virtual environment..."
if [ ! -d "venv-chores" ]; then
    echo "Virtual environment 'venv-chores' not found. Creating it now..."
    python3 -m venv venv-chores
fi

echo "Activating virtual environment..."
source venv-chores/bin/activate

echo "Updating dependencies..."
pip install -r requirements.txt

echo "Starting the bot in the background..."
# Using nohup with stdin redirected from /dev/null to fully detach from the SSH session
nohup python -u src/bot.py > nohup.out 2>&1 < /dev/null &

echo "Bot updated and started! Waiting briefly to check for instant crashes..."
sleep 2

echo "Checking process:"
ps aux | grep "[p]ython -u src/bot.py" || echo "Process is not running!"

echo "Tail of nohup.out (recent logs):"
tail -n 5 nohup.out
