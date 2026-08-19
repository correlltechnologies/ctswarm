#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vendor_dir="$repo_root/vendor/SWE-AF"

if [[ ! -d "$vendor_dir/.git" ]]; then
  echo "vendor/SWE-AF is missing or is not a git checkout" >&2
  exit 1
fi

patches=(
  "$repo_root/infra/patches/swe-af-fail-closed.patch"
  "$repo_root/infra/patches/swe-af-review-and-commit-gates.patch"
  "$repo_root/infra/patches/swe-af-issue-build-base.patch"
  "$repo_root/infra/patches/swe-af-autonomous-ui.patch"
  "$repo_root/infra/patches/swe-af-hybrid-runtime.patch"
  "$repo_root/infra/patches/swe-af-plan-integrity.patch"
  "$repo_root/infra/patches/swe-af-codex-wrapper.patch"
  "$repo_root/infra/patches/swe-af-prd-artifact.patch"
  "$repo_root/infra/patches/swe-af-codex-docker.patch"
  "$repo_root/infra/patches/swe-af-architecture-artifact.patch"
  "$repo_root/infra/patches/swe-af-planning-source-timeout.patch"
  "$repo_root/infra/patches/swe-af-preserve-architecture.patch"
  "$repo_root/infra/patches/swe-af-harness-availability.patch"
  # Applies after swe-af-autonomous-ui.patch, which owns the same npm line.
  "$repo_root/infra/patches/swe-af-claude-cli.patch"
  # Last: it edits the verify loop that swe-af-hybrid-runtime.patch introduces
  # continuous_repair into, so it cannot apply before that patch has run.
  "$repo_root/infra/patches/swe-af-verify-convergence.patch"
  "$repo_root/infra/patches/swe-af-fast-clone.patch"
)

# Later patches can intentionally modify lines introduced by earlier patches.
# In that case `git apply --reverse --check` cannot identify the earlier patch
# as applied while the later one remains on top. Build the complete expected
# tree from the pinned HEAD and compare only the paths touched by our patches.
temp_root="$(mktemp -d)"
expected_dir="$temp_root/SWE-AF"
cleanup() {
  if [[ -d "$expected_dir" ]]; then
    git -C "$vendor_dir" worktree remove --force "$expected_dir" >/dev/null 2>&1 || true
  fi
  rmdir "$temp_root" >/dev/null 2>&1 || true
}
trap cleanup EXIT

git -C "$vendor_dir" worktree add --detach "$expected_dir" HEAD >/dev/null 2>&1
for patch_path in "${patches[@]}"; do
  git -C "$expected_dir" apply "$patch_path"
done

touched_files="$(
  for patch_path in "${patches[@]}"; do
    git -C "$expected_dir" apply --numstat "$patch_path"
  done | sed $'s/^[^\\t]*\\t[^\\t]*\\t//' | sort -u
)"

matches_expected=true
while IFS= read -r relative_path; do
  [[ -n "$relative_path" ]] || continue
  if ! cmp -s "$vendor_dir/$relative_path" "$expected_dir/$relative_path"; then
    matches_expected=false
    break
  fi
done <<<"$touched_files"

# Which paths the last successful run wrote. Without this, a tree ctswarm
# patched itself is indistinguishable from one an operator edited by hand, so
# *updating* a patch stranded every existing checkout: the vendor tree no longer
# matched the new expected tree, it was dirty because the old patches were
# applied, and the only way forward was deleting vendor/ and re-cloning.
stamp="$vendor_dir/.ctswarm-applied-paths"

if [[ "$matches_expected" == true ]]; then
  printf '%s\n' "$touched_files" > "$stamp"
  exit 0
fi

# Modifications this script is allowed to discard: the paths it wrote last time
# and the paths it is about to write. Anything else dirty is the operator's.
owned="$(sort -u <(cat "$stamp" 2>/dev/null) <(printf '%s\n' "$touched_files"))"
foreign=""
while IFS= read -r status_line; do
  [[ -n "$status_line" ]] || continue
  dirty_path="${status_line:3}"
  # This script's own bookkeeping, not part of anyone's source tree.
  case "$dirty_path" in
    .ctswarm-applied-paths|.ctswarm-pre-rebuild.diff) continue ;;
  esac
  if ! grep -Fxq "$dirty_path" <<<"$owned"; then
    foreign+="  $dirty_path"$'\n'
  fi
done < <(git -C "$vendor_dir" status --porcelain)

if [[ -n "$foreign" ]]; then
  echo "vendor/SWE-AF has changes ctswarm did not make:" >&2
  printf '%s' "$foreign" >&2
  echo "refusing to overwrite a user-modified vendor tree" >&2
  exit 1
fi

# Every discarded byte is recoverable. The ownership check above says these
# paths are ctswarm's to rewrite, but "ctswarm wrote it" is inferred from a
# stamp file that a first upgrade does not have yet, and being wrong about that
# would destroy work with no way back.
backup="$vendor_dir/.ctswarm-pre-rebuild.diff"
if git -C "$vendor_dir" diff HEAD > "$backup" && [[ -s "$backup" ]]; then
  echo "saved the previous vendor state to $backup"
else
  rm -f "$backup"
fi

# Return the tree to the pinned commit before applying, so a patch that changed
# since last time does not have to apply on top of its own older self.
while IFS= read -r relative_path; do
  [[ -n "$relative_path" ]] || continue
  if git -C "$vendor_dir" ls-files --error-unmatch "$relative_path" >/dev/null 2>&1; then
    git -C "$vendor_dir" checkout --force HEAD -- "$relative_path"
  else
    rm -f "$vendor_dir/$relative_path"
  fi
done <<<"$owned"

for patch_path in "${patches[@]}"; do
  git -C "$vendor_dir" apply "$patch_path"
  echo "applied SWE-AF patch: $(basename "$patch_path")"
done
printf '%s\n' "$touched_files" > "$stamp"
