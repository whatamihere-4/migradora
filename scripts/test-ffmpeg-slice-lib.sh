# Shared setup for test-ffmpeg-slice*.sh — source from those scripts, do not run directly.
set -euo pipefail

slice_resolve_input() {
  CONTAINER="${MIGRADORA_CONTAINER:-migradora-orchestrator}"
  INPUT="${1:-/data/downloads/test/test.mp4}"
  PART_LIMIT="${2:-10200547328}"
  OUT_DIR="${OUT_DIR:-/data/downloads/test/slice-test-$$}"

  if [[ ! -f "$INPUT" && -f "./data/downloads/${INPUT#./data/downloads/}" ]]; then
    INPUT="./data/downloads/${INPUT#./data/downloads/}"
  fi

  if [[ -f "$INPUT" ]]; then
    HOST_INPUT="$INPUT"
    case "$INPUT" in
      /data/downloads/*) CONTAINER_INPUT="$INPUT" ;;
      ./data/downloads/*|data/downloads/*)
        rel="${INPUT#./}"
        rel="${rel#data/downloads/}"
        CONTAINER_INPUT="/data/downloads/$rel"
        ;;
      *)
        echo "Put test files under ./data/downloads/ or pass a /data/downloads/... container path." >&2
        return 1
        ;;
    esac
  else
    CONTAINER_INPUT="$INPUT"
    HOST_INPUT=""
  fi

  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "Container $CONTAINER is not running." >&2
    return 1
  fi

  docker exec "$CONTAINER" mkdir -p "$OUT_DIR"

  FILE_SIZE="$(docker exec "$CONTAINER" stat -c%s "$CONTAINER_INPUT")"
  SPLIT_LIMIT="$PART_LIMIT"
  if (( FILE_SIZE <= PART_LIMIT )); then
    SPLIT_LIMIT=$(( FILE_SIZE / 3 ))
    echo "NOTE: file ($FILE_SIZE bytes) fits in one part at cap $PART_LIMIT."
    echo "      Using split part limit $SPLIT_LIMIT bytes (~3 parts) for testing."
    echo "      Pass an explicit second arg to override."
    echo
  fi

  echo "==> Container: $CONTAINER"
  echo "==> Input:     $CONTAINER_INPUT"
  echo "==> Part cap:  $PART_LIMIT bytes (planning)"
  echo "==> Split cap: $SPLIT_LIMIT bytes (actual split)"
  echo "==> Output:    $OUT_DIR"
  echo
}

slice_print_done() {
  echo
  echo "==> Done. Artifacts in $OUT_DIR (remove manually when finished)."
  if [[ -n "${HOST_INPUT:-}" ]]; then
    echo "Host path: ./data/downloads/${OUT_DIR#/data/downloads/}"
  fi
}
