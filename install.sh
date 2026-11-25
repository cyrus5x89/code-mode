#!/usr/bin/env bash
# Install the code-mode skill on this Mac the way studio-styles does it: the store ~/.agents/skills holds one link
# to this repo, Codex and agy link to the store, and cm goes on PATH. Claude is left alone (it has its own Code Mode).
# Re-running is safe; an existing real folder is never replaced.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/skills/code-mode"
link() {   # link <target> <path>
  if [ -L "$2" ]; then ln -sfn "$1" "$2"; elif [ -e "$2" ]; then echo "skip $2 (real folder/file, left alone)"; return; else ln -s "$1" "$2"; fi
  echo "$2 -> $1"
}
mkdir -p ~/.agents/skills ~/.codex/skills ~/.gemini/config/skills ~/.local/bin
link "$src" ~/.agents/skills/code-mode
link ~/.agents/skills/code-mode ~/.codex/skills/code-mode
link ~/.agents/skills/code-mode ~/.gemini/config/skills/code-mode
link "$src/scripts/cm.py" ~/.local/bin/cm
echo "CC Switch: add the row by importing ~/.agents/skills/code-mode in the app (tick Codex + agy, not Claude),"
echo "then: python3 ~/Documents/git/studio-styles/tools/ccswitch_skills.py check"
