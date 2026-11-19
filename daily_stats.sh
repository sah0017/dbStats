#!/bin/bash
# daily_stats.sh - run update_commit_stats.py on every class folder.
# Started each morning by the LaunchAgent
# ~/Library/LaunchAgents/edu.lipscomb.sahammond.dbstats.plist.
# After END_DATE it removes that LaunchAgent and stops running.

END_DATE="2026-12-11"
FOLDERS=(
  "/Users/sahammond/Fall26DB"
  "/Users/sahammond/SoftwareStudio26"
)
LABEL="edu.lipscomb.sahammond.dbstats"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

# launchd doesn't load the login shell, so set the tools' locations here.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [[ "$(date +%Y-%m-%d)" > "$END_DATE" ]]; then
  echo "$(date): past $END_DATE; removing the daily schedule." >> "$LOG_DIR/schedule.log"
  rm -f "$PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  exit 0
fi

{
  echo "=== $(date)"
  status=0
  for folder in "${FOLDERS[@]}"; do
    echo "--- $folder"
    python3 "$DIR/update_commit_stats.py" "$folder" || status=1
  done
  echo "=== finished $(date) (exit $status)"
} >> "$LOG" 2>&1

# Keep about two months of logs.
find "$LOG_DIR" -name '20*.log' -mtime +60 -delete
exit $status
