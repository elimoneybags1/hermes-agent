#!/bin/sh
# Send each subtree's Team files to its GitHub fork, so the forks carry the
# gateway handshake on their own. Run from anywhere after committing; it pushes
# only to the forks, never to origin. See PROVENANCE.md, "Remotes and GitHub forks".
#
#   portkey/   full-history subtree with [team] patches to upstream files:
#              `git subtree push` splits this repo's history for the prefix
#              and pushes it as the fork's main (minutes: it walks every commit).
#   litellm/   squashed subtree where Team only adds files (team/, TEAM.md):
#              one commit on top of the fork's current main, whose tree is the
#              fork's tree with those paths replaced by HEAD's. Upstream never
#              touches them, so GitHub's "Sync fork" merges cleanly afterwards.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HEAD_SHA="$(git -C "$REPO" rev-parse --short HEAD)"
only="${1:-all}"

if [ "$only" = all ] || [ "$only" = portkey ]; then
  echo "portkey: splitting portkey/ and pushing to the gateway fork's main ..."
  git -C "$REPO" subtree push --prefix=portkey portkey main
fi

if [ "$only" = all ] || [ "$only" = litellm ]; then
  echo "litellm: fetching the fork's main ..."
  git -C "$REPO" fetch -q --depth=1 litellm main
  base="$(git -C "$REPO" rev-parse FETCH_HEAD)"
  export GIT_INDEX_FILE="$REPO/.git/index.push-forks"
  rm -f "$GIT_INDEX_FILE"
  git -C "$REPO" read-tree "$base"
  git -C "$REPO" rm -q -r --cached --ignore-unmatch team TEAM.md
  git -C "$REPO" read-tree --prefix=team/ HEAD:litellm/team
  git -C "$REPO" update-index --add --cacheinfo 100644,"$(git -C "$REPO" rev-parse HEAD:litellm/TEAM.md)",TEAM.md
  tree="$(git -C "$REPO" write-tree)"
  rm -f "$GIT_INDEX_FILE"; unset GIT_INDEX_FILE
  if [ "$tree" = "$(git -C "$REPO" rev-parse "${base}^{tree}")" ]; then
    echo "litellm: fork main already has HEAD's team/ and TEAM.md"
  else
    commit="$(git -C "$REPO" commit-tree "$tree" -p "$base" -m "[team] team/ and TEAM.md from elimoneybags1/hermes-agent $HEAD_SHA (litellm/)")"
    git -C "$REPO" push litellm "${commit}:refs/heads/main"
    echo "litellm: pushed $(git -C "$REPO" rev-parse --short "$commit") on top of $(git -C "$REPO" rev-parse --short "$base")"
  fi
fi
