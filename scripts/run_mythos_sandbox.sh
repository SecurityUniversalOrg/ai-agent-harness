#!/usr/bin/env bash
# Launch one Mythos scan in a fail-closed gVisor container. GitHub access and
# image construction happen in this trusted control plane; the model container
# receives only a sanitized checkout and Claude Platform on AWS credentials.
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
  VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID \
  VULNHUNT_ANTHROPIC_AWS_API_KEY \
  VULNHUNT_GITHUB_SCAN_TOKEN \
  MYTHOS_REPO_ROOT \
  MYTHOS_OUTPUT_DIR; do
  require_env "${required}"
done

[[ "${MYTHOS_RETENTION_ACKNOWLEDGED:-}" == "true" ]] \
  || die "Mythos requires explicit acknowledgement of mandatory 30-day retention"

if [[ ! "${TARGET_REPOSITORY}" =~ ^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(\.git)?$ ]]; then
  die "Mythos GitHub Actions profile accepts only https://github.com/OWNER/REPO URLs"
fi
repo_name="${BASH_REMATCH[2]}"
repo_name="${repo_name%.git}"
[[ "${repo_name}" != "." && "${repo_name}" != ".." ]] || die "Unsafe repository name"

clone_branch_args=()
agent_branch_args=()
if [[ -n "${TARGET_BRANCH:-}" ]]; then
  [[ "${TARGET_BRANCH}" != *$'\n'* && "${TARGET_BRANCH}" != *$'\r'* ]] \
    || die "TARGET_BRANCH contains a newline"
  [[ "${TARGET_BRANCH}" =~ ^[A-Za-z0-9._/-]+$ && "${TARGET_BRANCH}" != -* ]] \
    || die "TARGET_BRANCH is not a safe Git branch name"
  git check-ref-format --branch "${TARGET_BRANCH}" >/dev/null 2>&1 \
    || die "TARGET_BRANCH is not a valid Git branch name"
  clone_branch_args=(--branch "${TARGET_BRANCH}" --single-branch)
  agent_branch_args=(--branch "${TARGET_BRANCH}")
fi

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
network="vulnhunt-mythos-${run_key}"
egress_network="vulnhunt-mythos-egress-${run_key}"
proxy_container="vulnhunt-mythos-proxy-${run_key}"
agent_container="vulnhunt-mythos-agent-${run_key}"
agent_image="vulnhunt-mythos-agent:${run_key}"
proxy_image="vulnhunt-mythos-proxy:${run_key}"
control_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/vulnhunt-mythos.XXXXXX")"
checkout_dir="${control_dir}/checkout"
env_file="${control_dir}/agent.env"

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if (( rc != 0 )); then
    echo "::group::Squid diagnostics captured before failed-scan cleanup" >&2
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
# process environment, never argv or the stored origin URL.
basic_auth="$(printf 'x-access-token:%s' "${VULNHUNT_GITHUB_SCAN_TOKEN}" | base64 | tr -d '\r\n')"
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${basic_auth}" \
GIT_TERMINAL_PROMPT=0 \
  git clone --depth 1 --no-tags "${clone_branch_args[@]}" -- \
    "${TARGET_REPOSITORY}" "${checkout_dir}"
unset basic_auth
source_commit="$(git -C "${checkout_dir}" rev-parse HEAD)"
[[ "${source_commit}" =~ ^[0-9a-fA-F]{7,64}$ ]] \
  || die "Trusted checkout returned an invalid source commit"

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

# The proxy is not exposed on the host. It has ordinary outbound networking,
# then joins the agent's internal network under one fixed alias. Its Squid ACL
# accepts only CONNECT aws-external-anthropic.us-east-1.api.aws:443.
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
VULNHUNT_SANDBOX_ENABLED=true
VULNHUNT_SANDBOX_FAIL_IF_UNAVAILABLE=true
VULNHUNT_SANDBOX_ALLOW_UNSANDBOXED_COMMANDS=false
VULNHUNT_TELEMETRY_ENABLED=false
VULNHUNT_MYTHOS_DATA_RETENTION_ACKNOWLEDGED=true
VULNHUNT_MYTHOS_HTTPS_PROXY=http://${PROXY_ALIAS}:3128
VULNHUNT_AUDIT_EVENTS_PATH=/home/appuser/.vulnhunter/audit_events.jsonl
VULNHUNT_AUDIT_FINDINGS_PATH=/home/appuser/.vulnhunter/findings_events.jsonl
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
  --hostname mythos-agent \
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
    | grep -Eq '^(GH_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_TOKEN|VULNHUNT_GITHUB_SCAN_TOKEN|VULNHUNT_GITHUB_REPORTS_TOKEN)$'; then
  die "A GitHub credential variable reached the Mythos container"
fi

# Populate ephemeral storage as the unprivileged runtime UID. The target's
# .git/config contains the original clean URL and no credential helper/token.
tar -C "${checkout_dir}" -cf - . \
  | docker exec --user 65532:65532 --interactive "${agent_container}" sh -c \
      'mkdir -p "/workspace/clones/$1" && tar -xf - -C "/workspace/clones/$1"' _ "${repo_name}"
docker exec --user 65532:65532 "${agent_container}" sh -c \
  'mkdir -p "$HOME/.claude" "$XDG_RUNTIME_DIR" && chmod 0700 "$XDG_RUNTIME_DIR" && cp -R /opt/claude/skills "$HOME/.claude/skills"'

# Mechanical egress assertions. Direct Internet routing must fail, a denied
# CONNECT must return 403, and the exact Mythos CONNECT must return 200.
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
    --mode=scan \
    --model "${MYTHOS_MODEL}" \
    --config /opt/vulnhunter-agent/agent/config.example.toml \
    --clone-dir /workspace/clones \
    "${agent_branch_args[@]}" \
    --no-publish \
    --no-issues \
    "${TARGET_REPOSITORY}"
scan_rc=$?
set -e

report_output="${MYTHOS_OUTPUT_DIR}/${repo_name}"
[[ ! -L "${MYTHOS_OUTPUT_DIR}" && ! -L "${report_output}" ]] \
  || die "Refusing to export through a symlinked output directory"
mkdir -p "${report_output}"
mapfile -t result_dirs < <(
  docker exec "${agent_container}" find "/workspace/clones/${repo_name}" \
    -mindepth 1 -maxdepth 1 -type d -name '*_VULNHUNT_RESULTS_*' -print
)
for result_dir in "${result_dirs[@]}"; do
  result_name="${result_dir##*/}"
  [[ "${result_name}" =~ ^[A-Za-z0-9._-]+_VULNHUNT_RESULTS_[A-Za-z0-9._-]+$ ]] \
    || die "Refusing to export unexpected result directory name: ${result_name}"
  docker exec "${agent_container}" test ! -L "${result_dir}" \
    || die "Refusing to export a symlinked result directory"
  unexpected_entry="$(docker exec "${agent_container}" find "${result_dir}" \
    -mindepth 1 \( -type l -o \( ! -type f ! -type d \) \) -print -quit)"
  [[ -z "${unexpected_entry}" ]] \
    || die "Refusing to export a result tree containing a symlink or special file: ${unexpected_entry}"
  [[ ! -e "${report_output}/${result_name}" ]] \
    || die "Refusing to overwrite an existing exported result: ${result_name}"

  # Docker's archive-based `cp` cannot reliably see tmpfs mounted by runsc.
  # Stream from a process inside the sandbox instead. The source basename is
  # strictly validated above, special files/symlinks are rejected, hard links
  # are dereferenced, and host ownership/permissions are never restored.
  docker exec --user 65532:65532 "${agent_container}" tar \
      --hard-dereference -C "/workspace/clones/${repo_name}" -cf - -- "${result_name}" \
    | tar --no-same-owner --no-same-permissions -C "${report_output}" -xf -
  [[ -d "${report_output}/${result_name}" ]] \
    || die "Streamed result export did not create ${result_name}"
done
if (( ${#result_dirs[@]} == 1 )); then
  exported_results_dir="${report_output}/${result_dirs[0]##*/}"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
      echo "results_dir=${exported_results_dir}"
      echo "source_commit=${source_commit}"
    } >> "${GITHUB_OUTPUT}"
  fi
elif (( scan_rc == 0 )); then
  die "Successful Mythos scan produced ${#result_dirs[@]} results directories; expected exactly one"
else
  echo "::warning::Failed Mythos scan produced ${#result_dirs[@]} results directories; delivery will not run"
fi

audit_output="${report_output}/audit"
if docker exec "${agent_container}" test -d /home/appuser/.vulnhunter; then
  unexpected_audit_entry="$(docker exec "${agent_container}" find /home/appuser/.vulnhunter \
    -mindepth 1 \( -type l -o \( ! -type f ! -type d \) \) -print -quit)"
  [[ -z "${unexpected_audit_entry}" ]] \
    || die "Refusing to export an audit tree containing a symlink or special file: ${unexpected_audit_entry}"
  [[ ! -e "${audit_output}" ]] || die "Refusing to overwrite existing audit export"
  mkdir -p "${audit_output}"
  docker exec --user 65532:65532 "${agent_container}" tar \
      --hard-dereference -C /home/appuser/.vulnhunter -cf - . \
    | tar --no-same-owner --no-same-permissions -C "${audit_output}" -xf -
fi
echo "Mythos scan reports copied to ${report_output}"
exit "${scan_rc}"
