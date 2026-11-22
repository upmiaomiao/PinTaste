#!/bin/bash
# Scan the commits that origin/<branch> does not have yet with gitleaks, then push.
# Stops without pushing if gitleaks reports anything.
#
# Usage (inside the repository): ops/git/push_checked.sh [branch]   (default: main)
# Environment overrides: GITLEAKS (scanner binary), GIT_REMOTE (default origin)
set -euo pipefail
BRANCH=${1:-main}
REMOTE=${GIT_REMOTE:-origin}
GITLEAKS=${GITLEAKS:-/opt/wuyu-staging/operations/git-upload-20261115/private/gitleaks}

cd "$(git rev-parse --show-toplevel)"
[ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] || { echo "not on $BRANCH"; exit 1; }
[ -x "$GITLEAKS" ] || { echo "gitleaks not found: $GITLEAKS"; exit 1; }

git fetch -q "$REMOTE" "$BRANCH"
BASE=$(git merge-base "$REMOTE/$BRANCH" HEAD)
[ "$BASE" = "$(git rev-parse "$REMOTE/$BRANCH")" ] || { echo "$REMOTE/$BRANCH has commits that are not here; pull/rebase first"; exit 1; }
COUNT=$(git rev-list --count "$BASE"..HEAD)
[ "$COUNT" -gt 0 ] || { echo "nothing to push"; exit 0; }

echo "scanning $COUNT new commit(s): $BASE..HEAD"
REPORT=$(mktemp)
trap 'rm -f "$REPORT"' EXIT
if ! "$GITLEAKS" git --no-banner --redact --log-opts="$BASE..HEAD" --report-format json --report-path "$REPORT" .; then
  echo "gitleaks reported findings (secrets redacted):"
  python3 -c "import json,sys;[print(' ',f['RuleID'],f['File'],'line',f['StartLine'],f['Commit'][:8]) for f in json.load(open(sys.argv[1]))]" "$REPORT"
  echo "NOT pushed."
  exit 1
fi
git push "$REMOTE" "HEAD:$BRANCH"
