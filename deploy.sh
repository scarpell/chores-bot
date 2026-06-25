#!/bin/bash

# Navigate to the repo directory
cd ~/chores-bot || { echo "Failed to navigate to ~/chores-bot"; exit 1; }

echo "Pulling latest changes from git..."
git pull

echo "Killing any existing instances of the bot..."
# The pkill command matches against the full command line (-f)
pkill -f "python src/bot.py" || echo "No existing process found."

echo "Activating virtual environment..."
source venv-chores/bin/activate

echo "Updating dependencies..."
pip install -r requirements.txt

echo "Starting the bot in the background..."
# Using nohup to ensure it stays running after the SSH session closes
nohup python src/bot.py > nohup.out 2>&1 &

echo "Bot updated and started! Checking process:"
ps aux | grep "[p]ython src/bot.py"
