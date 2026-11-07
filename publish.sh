#!/usr/bin/env bash
# Publish using the account explicitly selected for this project.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
repository="Mifune-Shioriko/agent-memo"
account="$(gh api user --jq .login)"
if [[ "$account" != "Mifune-Shioriko" ]]; then
  echo "Expected Mifune-Shioriko; GitHub CLI is logged in as $account." >&2
  exit 1
fi
if gh repo view "$repository" --json nameWithOwner >/dev/null 2>&1; then
  expected="https://github.com/$repository.git"
  if git remote get-url origin >/dev/null 2>&1; then
    current="$(git remote get-url origin)"
    if [[ "$current" != "$expected" && "$current" != "git@github.com:$repository.git" ]]; then
      echo "Unexpected origin: $current. Refusing to change it." >&2
      exit 1
    fi
  else
    git remote add origin "$expected"
  fi
  git push -u origin main
else
  gh repo create "$repository" --public --source . --remote origin --push \
    --description "Opt-in project memory for AI coding agents: Markdown + SQLite + a small CLI and skill. No embeddings."
fi
