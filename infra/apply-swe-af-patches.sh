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

if [[ "$matches_expected" == true ]]; then
  exit 0
fi

if [[ -n "$(git -C "$vendor_dir" status --porcelain)" ]]; then
  echo "vendor/SWE-AF has changes that do not match the ctswarm patch set" >&2
  echo "refusing to overwrite a partial or user-modified vendor tree" >&2
  exit 1
fi

for patch_path in "${patches[@]}"; do
  git -C "$vendor_dir" apply "$patch_path"
  echo "applied SWE-AF patch: $(basename "$patch_path")"
done
