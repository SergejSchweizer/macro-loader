#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
source_hook="$repo_root/.githooks/pre-commit"
target_dir="$repo_root/.git/hooks"
target_hook="$target_dir/pre-commit"
managed_old_hook="$target_dir/pre-push"

mkdir -p "$target_dir"
if [[ -e "$target_hook" ]] && ! cmp -s "$source_hook" "$target_hook" \
  && [[ "$(<"$target_hook")" != $'#!/usr/bin/env bash\nset -euo pipefail\n\nrepo_root="$(git rev-parse --show-toplevel)"\ncd "$repo_root"\nmake quality-gate' ]]; then
  echo "refusing to replace unmanaged hook: $target_hook" >&2
  exit 1
fi
install -m 0755 "$source_hook" "$target_hook"
if [[ -e "$managed_old_hook" ]]; then
  if [[ "$(<"$managed_old_hook")" == $'#!/usr/bin/env bash\nset -euo pipefail\n\nif [[ -n "$(git status --short)" ]]; then\n  echo "pre-push blocked: working tree is dirty" >&2\n  exit 1\nfi\n\nmake quality-gate' ]]; then
    rm "$managed_old_hook"
  else
    echo "leaving unmanaged hook in place: $managed_old_hook" >&2
  fi
fi
printf 'installed %s\n' "$target_hook"
