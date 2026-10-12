#!/bin/bash
# Daily run: refresh the tender page and publish it to GitHub Pages.
# The portal only answers ordinary networks, not cloud servers, so this runs
# on a Mac (via launchd) rather than in GitHub Actions.
set -u
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
git pull --quiet --ff-only

# The network can take a moment to come back after the Mac wakes.
for attempt in 1 2 3; do
  python3 tender_alerts.py && break
  [ "$attempt" = 3 ] && exit 1
  sleep 120
done

git add docs/index.html state.json
git diff --cached --quiet || git commit --quiet -m "Digest for $(date +%F)"
git push --quiet
