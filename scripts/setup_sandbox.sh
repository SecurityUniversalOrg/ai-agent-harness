#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

step() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m  %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31m fail\033[0m  %s\n' "$*" >&2; exit 1; }

DAEMON_JSON="etc/docker/daemon.json"
RUNSC_BIN="/usr/local/bin/runsc"
RUNSC_RELEASE=${RUNSC_RELEASE:-20260420}
NET=vsa-sandbox
PROXY_NAME=vsa-egress-proxy
PROXY_TAG=vuln-scan-harness-egress-proxy:latest

# --- 1+2. gVisor (runsc) install + register ----------------

# Used only with macOS path
install_runsc_linux() {
    if [ -x "$RUNSC_BIN" ]; then
        echo "have $("$RUNSC_BIN" --version | head -1)"
    else
        case "$(unname -m)" in x86_64|aarch64) arch=$(uname -m) ;;
            *) echo "gVisor ships for x86_64 and aarch64 only; please install runsc manually" >&2; exit 1 ;;
        esac
        base="https://storage.googleapis.com/gvisor/releases/release/${RUNSC_RELEASE}/${arch}"
        tmp=$(mktemp -d)
        echo "  fetching runsc (~30 MB) from ${base}/"
        curl -fL --retry 3 --retry-all-errors -# "${base}/runsc" -o "$tmp/runsc"
        curl -fsSL --retry 3 "${base}/runsc.sha512" -o "$tmp/runsc.sha512"
        ( cd "$tmp" && sha512sum -c runsc.sha512 )
        sudo install -m 0755 "$tmp/runsc" "$RUNSC_BIN"
        rm -rf "$tmp"
    fi
    args="--overlay2=none"
    cg=/sys/fs/cgroup; [ -f "$cg/cgroup.controllers" ] || cg="$cg/memory"
    if ! sudo sh -c 'd="$1/runsc-probe-$$" && mkdir "$d" 2>/dev/null && rmdir "$d"' _ "$cg"; then
        echo "  warn: cgroups not writable; --memory cap not enforced under runsc" >&2
        args="--ignore-cgroups $args"
    fi
    sudo "$RUNSC_BIN" install --runtime=runsc -- $args
    live_restore=$(docker info --format '{{.LiveRestoreEnabled}}' 2>/dev/null || true)
    running=$( { docker ps -q 2>/dev/null || true; } | wc -l | tr -d ' ')
    if [ "${running:-0}" -gt 0 ] && [ "$live_restore" != "true" ] \
            && [ "${VULN_HARNESS_ALLOW_DOCKERD_RESTART:-}" != "1" ]; then
        {
            echo " **********************************************************************"
            echo " refusing to restart dockerd: $running container(s) are running and"
            echo " dockerd live-restore is OFF - a restart would KILL every running"
            echo " container in this host.  Stop them, enable live-restore in"
            echo " /etc/docker/daemon.json, or re-run with"
            echo " VULN_HARNESS_ALLOW_DOCKERD_RESTART=1 to restart anyway."
            echo " **********************************************************************"
        } >&2
        return 1
    fi
    sudo systemctl restart docker 2>/dev/null \
        || sudo service docker restart 2>/dev/null \
        || sudo kill -HUP "$(pgrep -xo dockerd)"
}

# Linux-host registration
RUNSC_ARGS=(--overlay2=none)
if [ -f "$DAEMON_JSON" ] && grep -q 'ignore-cgroups' "$DAEMON_JSON"; then
    RUNSC_ARGS=(--ignore-cgroups "${RUNSC_ARGS[@]}")
fi

register_runsc() {
    rc=0
    sudo python3 - "$DAEMON_JSON" "$RUNSC_BIN" "${RUNSC_ARGS[@]}" <<'PY' || rc=$?
import json, pathlib, shutil, sys, time
path, runsc = pathlib.Path(sys.argv[1]), sys.argv[2]
want = {"path": runsc, "runtimeArgs": sys.argv[3:]}
cfg = json.loads(path.read_text()) if path.exists() else {}
if cfg.get("runtimes", {}).get("runsc") == want:
    sys.exit(0)
if path.exists():
    shutil.copy(path, f"{path}.bak.{int(time.time())}")
path.parent.mkdir(parents=True, exist_ok=True)
cfg.setdefault("runtimes", {})["runsc"] = want
path.write_text(json.dumps(cfg, indent=4) + "\n")
sys.exit(10)
PY
    case "$rc" in
        0)  ok "runsc already registered (${RUNSC_ARGS[*]})" ;;
        10) sudo kill -HUP "$(pgrep -xo dockerd)" || die "dockerd not running"
            for _ in $(seq 10); do
                docker info 2>/dev/null | grep -q 'runsc' && break
                sleep 1
            done
            docker info 2>/dev/null | grep -q 'runsc' || die "runtime reload failed"
            ok "runsc registered + reloaded (${RUNSC_ARGS[*]})" ;;
        *)  die "daemon.json update failed (exit $rc)" ;;
    esac
}

step "1. gVisor (runsc) install + register"
if docker info --format '{{json .Runtimes.runsc}}' 2>/dev/null \
        | grep -q 'overlay2=none'; then
    ok "runsc already installed + registered"
else
    case "$(uname -s)" in
        Linux)
          if [ ! -x "$RUNSC_BIN" ]; then
              case "$(uname -m)" in x86_64|aarch64) ARCH=$(uname -m) ;;
                  *) die "gVisor ships for x86_64 and aarch64 only; please install runsc manually" ;;
              esac
              base="https://storage.googleapis.com/gvisor/releases/release/${RUNSC_RELEASE}/${ARCH}"
              tmp=$(mktemp -d)
              echo "  fetching runsc (~30 MB) from ${base}/"
              curl -fL --retry 3 --retry-all-errors -# "${base}/runsc" -o "$tmp/runsc"
              curl -fsSL --retry 3 "${base}/runsc.sha512" -o "$tmp/runsc.sha512"
              ( cd "$tmp" && sha512sum -c runsc.sha512 )
              sudo install -m 0755 "$tmp/runsc" "$RUNSC_BIN"
              rm -rf "$tmp"
              ok "installed $("$RUNSC_BIN" --version | head -1)"
          fi
          register_runsc ;;
        Darwin)
          command -v colima >/dev/null \
              || die "colima not found; please install it."
          if ! colima status >/dev/null 2>&1; then
              warn "colima not running - starting it (first start can take ~1 min)"
              colima start
          fi
          ok "routing runsc install through colima VM"
          colima ssh -- sh -s <<EOF || die "runsc install inside colima failed"
set -e
RUNSC_BIN="$RUNSC_BIN"; RUNSC_RELEASE="$RUNSC_RELEASE"
VULN_HARNESS_ALLOW_DOCKERD_RESTART="${VULN_HARNESS_ALLOW_DOCKERD_RESTART:-}"
$(declare -f install_runsc_linux)
install_runsc_linux
EOF
          ;;
      *) die "unsupported host OS $(uname -s); please install runsc manually" ;;
    esac
    for _ in $(seq 30); do
        docker info --format '{{range $k,$v := .Runtimes}}{{k}} {{end}}' 2>/dev/null \
            | grep -qw runsc && break
        sleep 1
    done
    docker info --format '{{range $k,$v := .Runtimes}}{{k}} {{end}}' 2>/dev/null \
        | grep -qw runsc \
        || die "runsc not found in docker info after install"
    ok "runsc registered + reloaded"
fi

step "2. egress-only network (${NET}) + proxy ($PROXY_NAME)"
docker network inspect "$NET" >/dev/null 2>&1 \
    || docker network create --internal "$NET" >/dev/null
docker build -q -t "$PROXY_TAG" -f scripts/Dockerfile.proxy scripts >/dev/null
docker rm -f "$PROXY_NAME" >/dev/null 2>&1 || true
docker run -d --name "$PROXY_NAME" --restart=unless-stopped \
    -e VH_EGRESS_ALLOW="${VSA_EGRESS_ALLOW:-}" \
    --network bridge "$PROXY_TAG" >/dev/null
docker network connect "$NET" "$PROXY_NAME"
proxy_ip=$(docker inspect "$PROXY_NAME" --format \
    '{{(index .NetworkSettings.Networks "'$NET'").IPAddress}}')
allow=""
for _ in $(seq 30); do
    allow=$( { docker logs "$PROXY_NAME" 2>&1 || true; } \
        | sed -n 's/^\[egress\] listening on :[0-9]*, allow=//p' | head -1)
    [ -n "$allow" ] && break
    sleep 0.5
done
[ -n "$allow" ] || die "proxy up but no allowlist in: docker logs ${PROXY_NAME}"
ok "proxy ${PROXY_NAME} up on ${NET} (${proxy_ip}:3128, allow: ${allow})"
api_allowed=1
case "$allow" in
    *"api.anthropic.com:443"*) ;;
    *) api_allowed=0 
       warn "allowlist omits api.anthropic.com:443 - agents on the first-party API will be blocked" ;;
esac

step "3. virtualenv setup and entry points"
[ -x .venv/bin/vuln-harness ] || { python3 -m venv .venv; .venv/bin/pip install -q -e .; }
ok "entry points registered (.venv/bin/{vuln-harness})"

step "4. verification"
probe_err=$(mktemp)
if ! guest_kver=$(docker run --rm --runtime=runsc alpine uname -r 2>"$probe_err"); then
    if [ "$(uname -s)" != "Linux" ] || \
      [[ " ${RUNSC_ARGS[*]} " != *"--ignore-cgroups"* ]] || \
      ! grep -qi cgroup "$probe_err"; then
        die "runsc probe failed: $(cat "$probe_err")"
    fi
    warn "runsc can't manage cgroups on this host; --memory cap not enforced under runsc"
    orig_args=("${RUNSC_ARGS[@]}")
    RUNSC_ARGS=(--ignore-cgroups "${RUNSC_ARGS[@]}")
    register_runsc
    recovered=
    for _ in $(seq 10); do
        if guest_kver=$(docker run --rm --runtime=runsc alpine uname -r 2>"$probe_err"); then
            recovered=1;break
        fi
        sleep 1
    done
    if [ -z "$recovered" ]; then
        err=$(cat "$probe_err")
        warn "--ignore-cgroups did not help; restoring previous runsc registration"
        RUNSC_ARGS=("${orig_args[@]}")
        register_runsc
        die "runsc probe failed: $err"
    fi
fi
rm -f "$probe_err"
case "$guest_kver" in
    *gvisor*|4.4.0) ok "gVisor active (guest kernel $guest_kver)" ;;
    *) die "unexpected guest kernel '$guest_kver' -- gVisor may not be active" ;;
esac

docker run --rm -i --runtime=runsc --networks="$NET" \
    -e HTTPS_PROXY="http://${proxy_ip}:3128" -e VH_PROBE_API="$api_allowed" \
    python:3-alpine python3 - <<'PY' \
    || die "egress check failed"
import os, urllib.request, socket, sys
if os.environ.get("VH_PROBE_API") == "1":
    try:
        urllib.request.urlopen("https://api.anthropic.com/", timeout=10).read(1)
    except urllib.error.HTTPError:
        pass
try:
    urllib.request.urlopen("https://example.com/", timeout=5); sys.exit("example.com reachable")
except Exception:
    pass
try:
    socket.create_connection(("8.8.8.8", 53), timeout=3); sys.exit("direct egress reachable")
except OSError:
    pass
PY
if [ "$api_allowed" = 1 ]; then
    ok "egress: api.anthropic.com reachable; example.com + direct egress blocked"
else
    ok "egress: example.com + direct egress blocked (api.anthropic.com probe skipped - not in allowlist)"
fi

sentinel=/tmp/host-sentinel-$$
echo host > "$sentinel"
out=$(docker run --rm --runtime=runsc alpine cat "$sentinel" 2>&1 || true)
rm -f "$sentinel"
echo "$out" | grep -qi 'no such file' || die "agent container can read host /tmp"
ok "host filesystem unreachable from agent container"

step "Done - sandbox ready"
echo "  vuln-harness: scripts/vuln_harness/build_vuln_targets.sh (builds target images + full egress check)"
        


