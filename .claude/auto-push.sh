#!/bin/sh
# Push the project to GitHub at the end of every work round (Stop hook).
#
# Claude is expected to make its own commits with real messages during the
# turn; this is the safety net that (a) catches anything left uncommitted and
# (b) pushes. It must never fail the turn, so every step ends in `|| true`.

REPO="/Users/omermaoz/trading bot"
cd "$REPO" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

# never touch a repo mid-rebase/merge
git_dir=$(git rev-parse --git-dir)
if [ -d "$git_dir/rebase-merge" ] || [ -d "$git_dir/rebase-apply" ] || [ -f "$git_dir/MERGE_HEAD" ]; then
    exit 0
fi

dirty=""
git diff --quiet || dirty=1
git diff --cached --quiet || dirty=1
[ -n "$(git ls-files --others --exclude-standard)" ] && dirty=1

if [ -n "$dirty" ]; then
    git add -A
    # the pre-commit guard can refuse this (secrets) — that must not kill the turn
    git commit -q -m "Auto-commit: leftover changes from the last session

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" || true
fi

# nothing to push is not an error
ahead=$(git rev-list --count @{u}..HEAD 2>/dev/null || echo 0)
if [ "$ahead" -gt 0 ] 2>/dev/null; then
    if git push -q origin HEAD 2>/dev/null; then
        printf '{"systemMessage":"נדחפו %s commits ל-GitHub"}\n' "$ahead"
    else
        printf '{"systemMessage":"דחיפה ל-GitHub נכשלה — %s commits ממתינים מקומית"}\n' "$ahead"
    fi
fi
exit 0
