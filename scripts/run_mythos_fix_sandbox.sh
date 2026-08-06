#!/usr/bin/env bash
# Launch one Mythos fix run in a fail-closed gVisor container. GitHub access
# (cloning the target) and image construction happen in this trusted control
# plane; the model container receives only a sanitized target checkout and
# already-staged report, plus Claude Platform on AWS credentials. Delivery is
# always disabled: Mythos fix mode is local-dry-run only (enforced both here
# and independently by agent.model_policy.enforce_mythos_mode_policy), so no
# fork/PR/issue credential ever needs to exist anywhere in this flow.
set -euo pipefail

readonly MYTHOS_MODEL="claude-mythos-5"
readonly MYTHOS_REGION="us-east-1"
readonly MYTHOS_INFERENCE_HOST="aws-external-anthropic.us-east-1.api.aws"
readonly PROXY_ALIAS="mythos-egress"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# shellcheck source=scripts/_mythos_docker.sh
source "${SCRIPT_DIR}/_mythos_docker.sh"

die() {
  echo "::error::$*" >&2
  exit 1
}

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "Required environment variable ${name} is empty"
  [[ "${!name}" != *$'\n'* && "${!name}" != *$'\r'* ]] \
    || die "${name} contains a newline"
}

mythos_configure_docker || die "Docker API access preflight failed"

for required in \
  TARGET_REPOSITORY \
  MYTHOS_REPORT_DIR \
  MYTHOS_SCRATCH_BASE \
  VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID \
  VULNHUNT_ANTHROPIC_AWS_API_KEY \
  VULNHUNT_GITHUB_FIX_TOKEN \
  MYTHOS_GITHUB_HOST \
  MYTHOS_REPO_ROOT; do
  require_env "${required}"
done

[[ "${MYTHOS_RETENTION_ACKNOWLEDGED:-}" == "true" ]] \
  || die "Mythos requires explicit acknowledgement of mandatory 30-day retention"

test_policy="${MYTHOS_TEST_POLICY:-best-effort}"
case "${test_policy}" in best-effort|must-pass|skip) ;; *) die "Unsupported test policy: ${test_policy}" ;; esac

if [[ ! "${TARGET_REPOSITORY}" =~ ^https://${MYTHOS_GITHUB_HOST//./\\.}/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(\.git)?$ ]]; then
  die "TARGET_REPOSITORY must be an https://${MYTHOS_GITHUB_HOST}/OWNER/REPO URL"
fi
repo_name="${BASH_REMATCH[2]}"
repo_name="${repo_name%.git}"
[[ "${repo_name}" != "." && "${repo_name}" != ".." ]] || die "Unsafe repository name"

report_dir="$(cd "${MYTHOS_REPORT_DIR}" && pwd -P)"
[[ -f "${report_dir}/README.md" && ! -L "${report_dir}/README.md" ]] \
  || die "MYTHOS_REPORT_DIR does not contain a regular top-level README.md"

runtime="${MYTHOS_DOCKER_RUNTIME:-runsc}"
[[ "${runtime}" == "runsc" ]] \
  || die "Mythos profile is pinned to the gVisor runsc runtime; got ${runtime}"

available_runtimes="$(docker info --format '{{range $name, $runtime := .Runtimes}}{{$name}} {{end}}')"
[[ " ${available_runtimes} " == *" runsc "* ]] \
  || die "Docker runtime runsc is unavailable. Use a hardened self-hosted runner labeled gvisor."
runsc_config="$(docker info --format '{{json .Runtimes.runsc}}')"
case "${runsc_config}" in
  *network=host*|*host-uds=open*|*directfs=true*)
    die "The registered runsc runtime contains a disallowed host-integration flag"
    ;;
esac

repo_root="$(cd "${MYTHOS_REPO_ROOT}" && pwd -P)"
[[ -f "${repo_root}/vulnhunter-agent/Dockerfile.mythos" ]] \
  || die "MYTHOS_REPO_ROOT does not contain vulnhunter-agent/Dockerfile.mythos"

run_key="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-${RANDOM}"
run_key="${run_key//[^A-Za-z0-9_.-]/-}"
network="vulnhunt-mythos-fix-${run_key}"
egress_network="vulnhunt-mythos-fix-egress-${run_key}"
proxy_container="vulnhunt-mythos-fix-proxy-${run_key}"
agent_container="vulnhunt-mythos-fix-agent-${run_key}"
agent_image="vulnhunt-mythos-fix-agent:${run_key}"
proxy_image="vulnhunt-mythos-fix-proxy:${run_key}"
control_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/vulnhunt-mythos-fix.XXXXXX")"
checkout_dir="${control_dir}/checkout"
env_file="${control_dir}/agent.env"

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if (( rc != 0 )); then
    echo "::group::Squid diagnostics captured before failed-fix cleanup" >&2
    docker logs "${proxy_container}" 2>&1 | tail -n 50 >&2 || true
    echo "::endgroup::" >&2
  fi
  docker rm -f "${agent_container}" >/dev/null 2>&1 || true
  docker rm -f "${proxy_container}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  docker network rm "${egress_network}" >/dev/null 2>&1 || true
  docker image rm -f "${agent_image}" >/dev/null 2>&1 || true
  docker image rm -f "${proxy_image}" >/dev/null 2>&1 || true
  rm -rf -- "${control_dir}"
  exit "${rc}"
}
trap cleanup EXIT INT TERM

# Clone in the trusted control plane. The credential is passed through Git's
# process environment, never argv or the stored origin URL, and never enters
# the container: the container-side invocation uses --target-checkout, which
# needs no GitHub identity at all.
basic_auth="$(printf 'x-access-token:%s' "${VULNHUNT_GITHUB_FIX_TOKEN}" | base64 | tr -d '\r\n')"
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0="http.https://${MYTHOS_GITHUB_HOST}/.extraheader" \
GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${basic_auth}" \
GIT_TERMINAL_PROMPT=0 \
  git clone --depth 1 --no-tags -- \
    "${TARGET_REPOSITORY}" "${checkout_dir}"
unset basic_auth
# The token never touched .git/config (the extraheader lived only in this
# process's env), but scrub the origin URL defensively anyway in case a
# future git version changes that behavior.
git -C "${checkout_dir}" remote set-url origin "${TARGET_REPOSITORY}"

echo "Building immutable Mythos runtime images before the restricted container starts"
docker build \
  --file "${repo_root}/vulnhunter-agent/Dockerfile.mythos" \
  --tag "${agent_image}" \
  "${repo_root}"
docker build \
  --file "${repo_root}/vulnhunter-agent/docker/mythos-egress/Dockerfile" \
  --tag "${proxy_image}" \
  "${repo_root}"

docker network create "${egress_network}" >/dev/null
docker network create --internal "${network}" >/dev/null
[[ "$(docker network inspect --format '{{.Internal}}' "${egress_network}")" == "false" ]] \
  || die "Proxy egress network is unexpectedly internal"
[[ "$(docker network inspect --format '{{.Internal}}' "${network}")" == "true" ]] \
  || die "Agent network is not internal"

mapfile -t allowed_endpoint_ips < <(mythos_resolve_ipv4 "${MYTHOS_INFERENCE_HOST}")
(( ${#allowed_endpoint_ips[@]} > 0 )) \
  || die "Trusted control plane could not resolve ${MYTHOS_INFERENCE_HOST} to a validated IPv4 address"
endpoint_host_args=()
for endpoint_ip in "${allowed_endpoint_ips[@]}"; do
  endpoint_host_args+=(--add-host "${MYTHOS_INFERENCE_HOST}:${endpoint_ip}")
done

docker run --detach \
  --name "${proxy_container}" \
  --runtime "${runtime}" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 128 \
  --memory 256m \
  --cpus 0.5 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
  --network "name=${egress_network},gw-priority=1" \
  --network "name=${network},alias=${PROXY_ALIAS}" \
  "${endpoint_host_args[@]}" \
  "${proxy_image}" >/dev/null

proxy_internal_ip="$(docker inspect --format \
  "{{with index .NetworkSettings.Networks \"${network}\"}}{{.IPAddress}}{{end}}" \
  "${proxy_container}")"
[[ "${proxy_internal_ip}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || die "Could not attest the proxy's internal IPv4 address: ${proxy_internal_ip}"

umask 077
cat >"${env_file}" <<EOF
HOME=/home/appuser
XDG_RUNTIME_DIR=/tmp/xdg
CLAUDE_CODE_USE_ANTHROPIC_AWS=1
ANTHROPIC_AWS_WORKSPACE_ID=${VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID}
ANTHROPIC_AWS_API_KEY=${VULNHUNT_ANTHROPIC_AWS_API_KEY}
AWS_REGION=${MYTHOS_REGION}
HTTPS_PROXY=http://${PROXY_ALIAS}:3128
HTTP_PROXY=http://${PROXY_ALIAS}:3128
NO_PROXY=localhost,127.0.0.1
VULNHUNT_ANTHROPIC_AUTH_MODE=anthropic_aws
VULNHUNT_ANTHROPIC_MODEL=${MYTHOS_MODEL}
VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID=${VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID}
VULNHUNT_ANTHROPIC_AWS_API_KEY=${VULNHUNT_ANTHROPIC_AWS_API_KEY}
VULNHUNT_ANTHROPIC_AWS_REGION=${MYTHOS_REGION}
VULNHUNT_GITHUB_HOST=${MYTHOS_GITHUB_HOST}
VULNHUNT_SANDBOX_ENABLED=true
VULNHUNT_SANDBOX_FAIL_IF_UNAVAILABLE=true
VULNHUNT_SANDBOX_ALLOW_UNSANDBOXED_COMMANDS=false
VULNHUNT_TELEMETRY_ENABLED=false
VULNHUNT_MYTHOS_DATA_RETENTION_ACKNOWLEDGED=true
VULNHUNT_MYTHOS_HTTPS_PROXY=http://${PROXY_ALIAS}:3128
VULNHUNT_AUDIT_ENABLED=false
PYTHONUNBUFFERED=1
EOF
chmod 0600 "${env_file}"

# No host bind mounts, Docker socket, devices, GitHub credentials, or writable
# root filesystem enter this container. All mutable state is run-scoped tmpfs.
docker run --detach \
  --name "${agent_container}" \
  --runtime "${runtime}" \
  --network "${network}" \
  --add-host "${PROXY_ALIAS}:${proxy_internal_ip}" \
  --env-file "${env_file}" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 512 \
  --memory "${MYTHOS_MEMORY_LIMIT:-8g}" \
  --cpus "${MYTHOS_CPU_LIMIT:-4}" \
  --ulimit nofile=1024:1024 \
  --ipc none \
  --hostname mythos-fix-agent \
  --tmpfs /workspace:rw,nosuid,nodev,noexec,size=${MYTHOS_WORKSPACE_LIMIT:-8g},uid=65532,gid=65532,mode=0700 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=512m,uid=65532,gid=65532,mode=0700 \
  --tmpfs /home/appuser/.claude:rw,nosuid,nodev,noexec,size=512m,uid=65532,gid=65532,mode=0700 \
  --tmpfs /home/appuser/.vulnhunter:rw,nosuid,nodev,noexec,size=64m,uid=65532,gid=65532,mode=0700 \
  "${agent_image}" >/dev/null
rm -f -- "${env_file}"

actual_runtime="$(docker inspect --format '{{.HostConfig.Runtime}}' "${agent_container}")"
[[ "${actual_runtime}" == "runsc" ]] || die "Agent runtime attestation failed: ${actual_runtime}"
proxy_runtime="$(docker inspect --format '{{.HostConfig.Runtime}}' "${proxy_container}")"
[[ "${proxy_runtime}" == "runsc" ]] || die "Proxy runtime attestation failed: ${proxy_runtime}"
proxy_readonly="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${proxy_container}")"
[[ "${proxy_readonly}" == "true" ]] || die "Proxy root filesystem is not read-only"
proxy_privileged="$(docker inspect --format '{{.HostConfig.Privileged}}' "${proxy_container}")"
[[ "${proxy_privileged}" == "false" ]] || die "Proxy container is privileged"
proxy_mount_count="$(docker inspect --format '{{len .Mounts}}' "${proxy_container}")"
[[ "${proxy_mount_count}" == "0" ]] || die "Proxy unexpectedly has host/volume mounts"
proxy_network_count="$(docker inspect --format '{{len .NetworkSettings.Networks}}' "${proxy_container}")"
[[ "${proxy_network_count}" == "2" ]] || die "Proxy does not have exactly two network interfaces"
proxy_cap_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${proxy_container}")"
[[ "${proxy_cap_drop}" == *'ALL'* ]] || die "Proxy did not drop all Linux capabilities"
proxy_security_opt="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${proxy_container}")"
[[ "${proxy_security_opt}" == *'no-new-privileges'* ]] \
  || die "Proxy is missing no-new-privileges"
guest_kernel="$(docker exec "${agent_container}" uname -r)"
case "${guest_kernel}" in
  *gvisor*|4.4.0) ;;
  *) die "Unexpected guest kernel '${guest_kernel}'; gVisor attestation failed" ;;
esac
actual_network="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "${agent_container}")"
[[ "${actual_network}" == "${network}" ]] || die "Agent network attestation failed: ${actual_network}"
actual_readonly="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${agent_container}")"
[[ "${actual_readonly}" == "true" ]] || die "Agent root filesystem is not read-only"
actual_privileged="$(docker inspect --format '{{.HostConfig.Privileged}}' "${agent_container}")"
[[ "${actual_privileged}" == "false" ]] || die "Agent container is privileged"
mount_count="$(docker inspect --format '{{len .Mounts}}' "${agent_container}")"
[[ "${mount_count}" == "0" ]] || die "Agent unexpectedly has host/volume mounts"
cap_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${agent_container}")"
[[ "${cap_drop}" == *'ALL'* ]] || die "Agent did not drop all Linux capabilities"
security_opt="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${agent_container}")"
[[ "${security_opt}" == *'no-new-privileges'* ]] \
  || die "Agent is missing no-new-privileges"
if docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${agent_container}" \
    | sed 's/=.*//' \
    | grep -Eq '^(GH_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_TOKEN|VULNHUNT_GITHUB_SCAN_TOKEN|VULNHUNT_GITHUB_REPORTS_TOKEN|VULNHUNT_GITHUB_FIX_TOKEN|VULNHUNT_GITHUB_VERIFY_TOKEN)$'; then
  die "A GitHub credential variable reached the Mythos container"
fi

# Populate ephemeral storage as the unprivileged runtime UID. The target's
# .git/config contains the original clean URL and no credential helper/token.
tar -C "${checkout_dir}" -cf - . \
  | docker exec --user 65532:65532 --interactive "${agent_container}" sh -c \
      'mkdir -p /workspace/repo && tar -xf - -C /workspace/repo'
tar -C "${report_dir}" -cf - . \
  | docker exec --user 65532:65532 --interactive "${agent_container}" sh -c \
      'mkdir -p /workspace/report && tar -xf - -C /workspace/report'
docker exec --user 65532:65532 "${agent_container}" sh -c \
  'mkdir -p "$HOME/.claude" "$XDG_RUNTIME_DIR" /workspace/fix_runs && chmod 0700 "$XDG_RUNTIME_DIR" && cp -R /opt/claude/skills "$HOME/.claude/skills"'

# Mechanical egress assertions, identical shape to the scan Mythos launcher:
# direct Internet routing must fail, a denied CONNECT must return 403, and the
# exact Mythos CONNECT must return 200.
resolved_proxy_ip="$(docker exec "${agent_container}" python -c \
  'import socket; print(socket.gethostbyname("mythos-egress"))')"
[[ "${resolved_proxy_ip}" == "${proxy_internal_ip}" ]] \
  || die "Agent proxy host mapping failed: resolved=${resolved_proxy_ip} expected=${proxy_internal_ip}"
if docker exec "${agent_container}" python -c \
  'import socket; s=socket.create_connection(("1.1.1.1",443),2); s.close()' \
  >/dev/null 2>&1; then
  die "Direct egress unexpectedly succeeded from the Mythos container"
fi
docker exec "${agent_container}" python -c '
import socket, time
def status(host):
    last_error = None
    for _ in range(20):
        try:
            with socket.create_connection(("mythos-egress", 3128), 5) as sock:
                request = f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n"
                sock.sendall(request.encode("ascii"))
                return sock.recv(256).split(b"\r\n", 1)[0]
        except OSError as exc:
            last_error = exc
            time.sleep(0.5)
    raise last_error
denied = status("example.com")
allowed = status("aws-external-anthropic.us-east-1.api.aws")
assert b" 403 " in denied, denied
assert b" 200 " in allowed, allowed
'

set +e
docker exec \
  --user 65532:65532 \
  --workdir /opt/vulnhunter-agent \
  --env VULNHUNT_MYTHOS_HARDENED_RUNTIME=1 \
  "${agent_container}" \
  python -m agent \
    --mode=fix \
    --model "${MYTHOS_MODEL}" \
    --config /opt/vulnhunter-agent/agent/config.example.toml \
    --no-post \
    --test-policy "${test_policy}" \
    --scratch-dir /workspace/fix_runs \
    --target-checkout /workspace/repo \
    "${TARGET_REPOSITORY}" \
    /workspace/report
agent_rc=$?
set -e

# Discover the single contained run directory, exactly like run-vulnhunter-fix's
# non-Mythos path does, but inside the container first.
mapfile -t run_dir_names < <(
  docker exec "${agent_container}" find /workspace/fix_runs -mindepth 1 -maxdepth 1 -type d -printf '%f\n'
)
run_dir=""
disposition=""
if (( ${#run_dir_names[@]} == 1 )); then
  run_dir_name="${run_dir_names[0]}"
  docker exec "${agent_container}" test ! -L "/workspace/fix_runs/${run_dir_name}" \
    || die "Refusing to export a symlinked run directory"
  host_run_dir="${MYTHOS_SCRATCH_BASE}/${run_dir_name}"
  [[ ! -e "${host_run_dir}" ]] || die "Refusing to overwrite an existing exported run directory"
  mkdir -p "${host_run_dir}"
  docker exec --user 65532:65532 "${agent_container}" tar \
      --hard-dereference -C /workspace/fix_runs -cf - -- "${run_dir_name}" \
    | tar --no-same-owner --no-same-permissions -C "${MYTHOS_SCRATCH_BASE}" -xf -
  run_dir="$(realpath -e "${host_run_dir}")"
  case "${run_dir}" in "$(realpath -e "${MYTHOS_SCRATCH_BASE}")"/*) ;; *) die "Exported fix run escaped its scratch base" ;; esac
  candidate_disposition="${run_dir}/out/fix_disposition.json"
  [[ -f "${candidate_disposition}" && ! -L "${candidate_disposition}" ]] && disposition="${candidate_disposition}"
elif (( ${#run_dir_names[@]} > 1 )); then
  echo "::error::Mythos fix execution created multiple run directories."
  agent_rc=1
fi

{
  echo "agent-exit-code=${agent_rc}"
  echo "run-directory=${run_dir}"
  echo "disposition-path=${disposition}"
} >> "${GITHUB_OUTPUT}"
exit "${agent_rc}"
