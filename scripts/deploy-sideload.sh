#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-sideload.sh — one-command side-load deploy for this Sage plugin on Thor.
#
# WHY THIS EXISTS
#   The ECR portal "Register and Build" path cannot build this NVIDIA/CUDA-base
#   plugin yet: the pipeline cross-builds linux/arm64 under QEMU on x86 and
#   crashes on the CUDA base (qemu signal 6 / exit 134). That's Infra #3, still
#   open (the /proc/acpi runc bug, Infra #2, IS fixed). Until a native arm64
#   build node lands, the WORKING deploy path is: build natively on Thor (arm64,
#   no QEMU) → import into k3s containerd → register catalog metadata so SES
#   validates → (optionally) create+submit the SES job. SES pods use
#   imagePullPolicy=IfNotPresent, so a locally-imported image under the exact
#   registry-qualified tag is used without any registry pull.
#
#   This script wraps that chore. It reads name/namespace/version straight from
#   sage.yaml — nothing is hardcoded, so a version bump needs no edit here.
#
# USAGE
#   Run from the repo root, ON the Thor node (needs docker + k3s + sudo):
#
#     scripts/deploy-sideload.sh                 # build → import → register
#     scripts/deploy-sideload.sh --submit jobs/yolo-hummingcam-h00f.yaml
#     scripts/deploy-sideload.sh --version 0.3.2 # override the sage.yaml version
#     scripts/deploy-sideload.sh --dry-run       # print the plan, run nothing
#     scripts/deploy-sideload.sh --skip-build    # image already imported; just register
#     scripts/deploy-sideload.sh --skip-register # catalog record already exists
#     scripts/deploy-sideload.sh -h              # full help
#
# TOKENS (only needed for the steps that use them)
#   SAGE_TOKEN      — Sage portal token, for the ECR catalog register step.
#   SES_USER_TOKEN  — write-scoped SES token, only for --submit.
#
# STEPS (all idempotent; safe to re-run)
#   1 build     sudo docker build -t <registry-tag> .
#   2 import    sudo docker save <registry-tag> | sudo k3s ctr images import -
#   3 register  scripts/register-ecr-version.py  (catalog metadata only)
#   4 submit    sesctl create -f <job> && sesctl submit -j <id>   (opt-in only)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── locate repo root (script lives in <root>/scripts/) ───────────────────────
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

REGISTRY="registry.sagecontinuum.org"
ECR_API="https://ecr.sagecontinuum.org/api"
SES_SERVER="${SES_SERVER:-https://es.sagecontinuum.org}"

# ── colors (fall back to plain if not a tty) ─────────────────────────────────
if [ -t 1 ]; then
  B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; Z=$'\e[0m'
else
  B=""; G=""; Y=""; R=""; C=""; Z=""
fi
say()  { printf '%s\n' "${C}${B}==>${Z} ${B}$*${Z}"; }
ok()   { printf '%s\n' "${G}  ✓ $*${Z}"; }
warn() { printf '%s\n' "${Y}  ! $*${Z}" >&2; }
die()  { printf '%s\n' "${R}ERROR: $*${Z}" >&2; exit 1; }

# ── args ─────────────────────────────────────────────────────────────────────
VERSION_OVERRIDE=""
FROM_VERSION=""
SUBMIT_JOB=""
DRY_RUN=0
DO_BUILD=1
DO_REGISTER=1

usage() { sed -n '2,52p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --version)       VERSION_OVERRIDE="${2:?--version needs a value}"; shift 2;;
    --from-version)  FROM_VERSION="${2:?--from-version needs a value}"; shift 2;;
    --submit)        SUBMIT_JOB="${2:?--submit needs a job YAML path}"; shift 2;;
    --dry-run)       DRY_RUN=1; shift;;
    --skip-build)    DO_BUILD=0; shift;;
    --skip-register) DO_REGISTER=0; shift;;
    -h|--help)       usage;;
    *) die "unknown argument: $1  (try -h)";;
  esac
done

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s\n' "${Y}  [dry-run] $*${Z}"
  else
    eval "$@"
  fi
}

# ── parse sage.yaml (top-level scalars + source.url) ─────────────────────────
[ -f sage.yaml ] || die "sage.yaml not found in $REPO_ROOT — run from the repo root."

yfield() {  # yfield <key> — first top-level "key: value", quotes stripped
  sed -nE "s/^$1:[[:space:]]*\"?([^\"#]+)\"?.*/\1/p" sage.yaml | head -n1 \
    | sed -E 's/[[:space:]]+$//'
}
yurl() {    # nested source.url (indented)
  sed -nE 's/^[[:space:]]+url:[[:space:]]*"?([^"#]+)"?.*/\1/p' sage.yaml | head -n1 \
    | sed -E 's/[[:space:]]+$//'
}

NAME="$(yfield name)"
NAMESPACE="$(yfield namespace)"
VERSION="${VERSION_OVERRIDE:-$(yfield version)}"
GIT_URL="$(yurl)"

[ -n "$NAME" ]      || die "could not parse 'name' from sage.yaml"
[ -n "$NAMESPACE" ] || die "could not parse 'namespace' from sage.yaml"
[ -n "$VERSION" ]   || die "could not parse 'version' from sage.yaml"
[ -n "$GIT_URL" ]   || die "could not parse source.url from sage.yaml"

TAG="${REGISTRY}/${NAMESPACE}/${NAME}:${VERSION}"

say "Plugin:   ${NAMESPACE}/${NAME}"
say "Version:  ${VERSION}${VERSION_OVERRIDE:+  (override)}"
say "Image:    ${TAG}"
say "Git:      ${GIT_URL}"
[ "$DRY_RUN" -eq 1 ] && warn "DRY RUN — nothing will actually execute."
echo

# ── drift check: do the job YAMLs point at this exact tag? ───────────────────
drift=0
for j in jobs/*.yaml; do
  [ -e "$j" ] || continue
  if grep -q 'image:' "$j"; then
    jt="$(sed -nE 's/^[[:space:]]*image:[[:space:]]*(.+)$/\1/p' "$j" | head -n1)"
    if [ -n "$jt" ] && [ "$jt" != "$TAG" ]; then
      warn "job $j image: $jt  ≠  $TAG"
      drift=1
    fi
  fi
done
[ "$drift" -eq 1 ] && warn "Job YAML image tag(s) differ from sage.yaml version — bump them before --submit." || ok "Job YAML image tags match ${VERSION}."
echo

# ── preflight ────────────────────────────────────────────────────────────────
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found on PATH"; }
if [ "$DRY_RUN" -eq 0 ]; then
  need sudo; need docker; need python3
  [ "$DO_BUILD" -eq 1 ] && need k3s || true
fi

# ── Step 1: build natively on Thor (arm64, no QEMU) ──────────────────────────
if [ "$DO_BUILD" -eq 1 ]; then
  say "Step 1/4 — build ${TAG} natively (arm64)"
  run "sudo docker build -t '$TAG' ."
  ok "built"
else
  warn "Step 1 skipped (--skip-build)"
fi

# ── Step 2: import into k3s containerd ───────────────────────────────────────
if [ "$DO_BUILD" -eq 1 ]; then
  say "Step 2/4 — import into k3s containerd (~a few min)"
  run "sudo docker save '$TAG' | sudo k3s ctr images import -"
  if [ "$DRY_RUN" -eq 0 ]; then
    # Capture first, then match in pure bash. Do NOT pipe `ctr images ls` into
    # `grep -q`: grep -q exits on first match and SIGPIPEs the still-writing ls;
    # under `set -o pipefail` that 141 propagates and false-fails on success.
    imgs="$(sudo k3s ctr images ls)"
    if [[ "$imgs" == *"$TAG"* ]]; then
      ok "present in k3s: $TAG"
    else
      die "image not found in k3s after import — check the build/import output"
    fi
  fi
else
  warn "Step 2 skipped (--skip-build)"
fi

# ── Step 3: register ECR catalog metadata (so SES validation passes) ─────────
if [ "$DO_REGISTER" -eq 1 ] && [ "$DRY_RUN" -eq 1 ]; then
  say "Step 3/4 — register catalog metadata for ${NAMESPACE}/${NAME}:${VERSION}"
  printf '%s\n' "${Y}  [dry-run] python3 scripts/register-ecr-version.py --namespace $NAMESPACE --name $NAME --from-version ${FROM_VERSION:-<auto-detect>} --version $VERSION --git-url $GIT_URL --token \$SAGE_TOKEN${Z}"
elif [ "$DO_REGISTER" -eq 1 ]; then
  say "Step 3/4 — register catalog metadata for ${NAMESPACE}/${NAME}:${VERSION}"
  [ -n "${SAGE_TOKEN:-}" ] || die "SAGE_TOKEN not set — needed to register the ECR catalog record. export SAGE_TOKEN=... (or use --skip-register if the record already exists)."

  # Auto-detect a prior version to clone metadata from, unless given.
  if [ -z "$FROM_VERSION" ]; then
    FROM_VERSION="$(curl -fsS -H "Authorization: Sage ${SAGE_TOKEN}" \
        "${ECR_API}/apps/${NAMESPACE}/${NAME}" 2>/dev/null \
      | python3 -c "import sys,json
try: d=json.load(sys.stdin).get('data',[])
except Exception: d=[]
vs=sorted({i.get('version') for i in d if i.get('version') and i.get('version')!='$VERSION'})
print(vs[-1] if vs else '')" 2>/dev/null || true)"
    [ -n "$FROM_VERSION" ] && ok "cloning catalog metadata from prior version ${FROM_VERSION}" \
      || die "no prior catalog version found to clone from. Either the app has never been registered (do the first version via the ECR portal once), or pass --from-version <ver>."
  fi

  run "python3 scripts/register-ecr-version.py \
        --namespace '$NAMESPACE' --name '$NAME' \
        --from-version '$FROM_VERSION' --version '$VERSION' \
        --git-url '$GIT_URL' --token \"\$SAGE_TOKEN\""
  ok "catalog record ensured for ${VERSION}"
else
  warn "Step 3 skipped (--skip-register)"
fi

# ── Step 4: create + submit the SES job (opt-in) ─────────────────────────────
if [ -n "$SUBMIT_JOB" ]; then
  say "Step 4/4 — submit SES job ${SUBMIT_JOB}"
  [ -f "$SUBMIT_JOB" ] || die "job file not found: $SUBMIT_JOB"

  # Guard against submitting a job whose image tag doesn't match what we deployed.
  if [ "$drift" -eq 1 ]; then
    jt="$(sed -nE 's/^[[:space:]]*image:[[:space:]]*(.+)$/\1/p' "$SUBMIT_JOB" | head -n1)"
    [ "$jt" = "$TAG" ] || die "refusing to submit: $SUBMIT_JOB image ($jt) ≠ deployed tag ($TAG). Fix the job's image: line first."
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s\n' "${Y}  [dry-run] sesctl --server $SES_SERVER create -f $SUBMIT_JOB ; sesctl submit -j <id>${Z}"
  else
    [ -n "${SES_USER_TOKEN:-}" ] || die "SES_USER_TOKEN not set — needed to create/submit the SES job. export SES_USER_TOKEN=..."
    need sesctl
    create_out="$(sesctl --server "$SES_SERVER" --token "$SES_USER_TOKEN" create -f "$SUBMIT_JOB")"
    printf '%s\n' "$create_out"
    JOB_ID="$(printf '%s' "$create_out" | grep -oE '[0-9]+' | head -n1 || true)"
    [ -n "$JOB_ID" ] || die "could not parse job id from sesctl create output above."
    ok "created job id ${JOB_ID}"
    sesctl --server "$SES_SERVER" --token "$SES_USER_TOKEN" submit -j "$JOB_ID"
    ok "submitted job ${JOB_ID}"
  fi
else
  say "Step 4/4 — submit skipped (no --submit). To run the job:"
  printf '%s\n' "    scripts/deploy-sideload.sh --submit jobs/yolo-hummingcam-h00f.yaml"
fi

echo
ok "Done: ${TAG} is built + imported + catalog-registered${SUBMIT_JOB:+ + job submitted}."
if [ "$drift" -eq 1 ]; then
  warn "Reminder: some job YAML image tags still differ from ${VERSION}."
fi
exit 0
