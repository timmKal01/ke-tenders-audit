#!/usr/bin/env bash
# Push a one-commit snapshot of the current code to a Hugging Face Space.
#   deploy/push_space.sh <hf-username>/<space-name>
# Git asks for your Hugging Face login the first time (use an access token with write access).
# The Space needs one secret, KTA_LLM_API_KEY, added in its Settings by you.
set -euo pipefail
SPACE="$1"
ROOT="$(git rev-parse --show-toplevel)"
TMP="$(mktemp -d)"
git -C "$ROOT" worktree add --detach "$TMP" HEAD >/dev/null
cd "$TMP"
git checkout -q --orphan space-snapshot
# Spaces reject binary files without LFS: drop the PNG and point the README at GitHub's copy.
git rm -q --cached docs/architecture.png && rm docs/architecture.png
IMG="https://raw.githubusercontent.com/timmKal01/ke-tenders-audit/main/docs/architecture.png"
{ cat deploy/space-header.md; sed "s#](docs/architecture.png)#]($IMG)#" README.md; } > README.space && mv README.space README.md
git add -A
git -c user.name="ke-tenders-audit deploy" -c user.email="deploy@users.noreply.github.com" \
    commit -q -m "Deploy snapshot of $(git -C "$ROOT" rev-parse --short HEAD)"
git push --force "https://huggingface.co/spaces/$SPACE" HEAD:main
cd "$ROOT"
git worktree remove --force "$TMP"
echo "Pushed to https://huggingface.co/spaces/$SPACE"
