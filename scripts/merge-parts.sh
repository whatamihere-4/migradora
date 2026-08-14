#!/usr/bin/env bash
# Merge .PART1 / .PART2 / … in a folder (mkvmerge only — no Python, no ffmpeg).
#
# If you downloaded parts from Filester, use the uploaded script instead:
#   bash *.merge.sh
#
# This script is for folders without *.merge.sh (legacy uploads):
#   ./scripts/merge-parts.sh
#   ./scripts/merge-parts.sh "/path/to/parts/folder"
set -euo pipefail

cd "${1:-.}"

if ls -1 *.merge.sh >/dev/null 2>&1; then
  echo "Found *.merge.sh — running that instead."
  exec bash ./*.merge.sh
fi

mkv="${MKVMERGE_BIN:-mkvmerge}"
shopt -s nullglob
part1=( *.PART1.* )
if (( ${#part1[@]} != 1 )); then
  echo "Need exactly one *.PART1.* file in $(pwd)" >&2
  exit 1
fi

file="${part1[0]}"
if [[ ! "$file" =~ ^(.+)\.PART[0-9]+(\..+)$ ]]; then
  echo "Unexpected part name: $file" >&2
  exit 1
fi
stem="${BASH_REMATCH[1]}"
ext="${BASH_REMATCH[2]}"
out="${stem}${ext}"

trim=1
if [[ -f "${stem}.merge_trim_frames" ]]; then
  trim=$(tr -d '[:space:]' < "${stem}.merge_trim_frames")
fi

parts=("${stem}.PART1${ext}")
i=2
while [[ -f "${stem}.PART${i}${ext}" ]]; do
  parts+=("${stem}.PART${i}${ext}")
  i=$((i + 1))
done
if (( ${#parts[@]} < 2 )); then
  echo "Only one part file found." >&2
  exit 1
fi
if [[ -f "$out" ]]; then
  echo "Already exists: $out" >&2
  exit 1
fi

args=("$mkv" -o "$out" "${parts[0]}")
for (( j=1; j<${#parts[@]}; j++ )); do
  if (( trim > 0 )); then
    args+=(--split "parts-frames:${trim}-")
  fi
  args+=("+${parts[j]}")
done

echo "Merging ${#parts[@]} parts -> $out (trim_frames=${trim})"
"$mkv" "${args[@]}"
echo "Done."
