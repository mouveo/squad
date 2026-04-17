#!/usr/bin/env bash
# Install (or refresh) the repo-local Claude skills into a target directory.
#
# Default target: ~/.claude/skills (the location the Claude CLI scans).
# Override either with the first positional argument or with $SQUAD_SKILLS_TARGET.
# The script is idempotent: each skill directory is replaced atomically so
# repeated runs converge to the repo-local source of truth.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_DIR="${REPO_ROOT}/skills"

TARGET="${1:-${SQUAD_SKILLS_TARGET:-${HOME}/.claude/skills}}"

if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "error: skills source not found at ${SOURCE_DIR}" >&2
  exit 1
fi

mkdir -p "${TARGET}"

installed=0
for skill_path in "${SOURCE_DIR}"/*/; do
  [[ -d "${skill_path}" ]] || continue
  skill_name="$(basename "${skill_path}")"
  dest="${TARGET}/${skill_name}"
  echo "installing skill: ${skill_name} -> ${dest}"
  rm -rf "${dest}"
  cp -R "${skill_path}" "${dest}"
  installed=$((installed + 1))
done

if [[ "${installed}" -eq 0 ]]; then
  echo "warning: no skills found under ${SOURCE_DIR}" >&2
  exit 0
fi

echo "done — ${installed} skill(s) installed to ${TARGET}"
