#!/usr/bin/env bash
# Run one Mythos-isolated verify *session* (the model turn only) in a
# fail-closed gVisor container.
#
# Unlike the scan and fix launchers, this script is not the top-level entry
# point for a Mythos run: ``python -m agent --mode=verify`` runs first, as an
# ordinary trusted-host process, and does the issue/comment/timeline fetch
# plus the target/additional-repo clones with a real GitHub credential
# exactly as it does for every other model — none of that can move into a
# gVisor container without reimplementing GitHub API access in bash. Once
# that trusted work is done, ``agent/verify_mythos.py`` invokes this script
# to run only the actual SDK/model turn, over already-fetched,
# already-cloned, credential-free inputs. No GitHub token, GitHub host, or
# route to GitHub is ever passed to this script or the container it builds.
set -euo pipefail

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
  MYTHOS_REPO_ROOT \
  MYTHOS_TARGET_REPO_CHECKOUT \
  MYTHOS_REPORT_DIR \
  MYTHOS_PROMPT_FILE \
  MYTHOS_OUT_DIR \
  MYTHOS_LOG_PATH \
  VULNHUNT_ANTHROPIC_MODEL \
  VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID \
  VULNHUNT_ANTHROPIC_AWS_API_KEY; do
  require_env "${required}"
done

[[ "${VULNHUNT_ANTHROPIC_MODEL}" == "claude-mythos-5" ]] \
  || die "run_mythos_verify_sandbox.sh is Mythos-only; got model=${VULNHUNT_ANTHROPIC_MODEL}"

target_checkout="$(cd "${MYTHOS_TARGET_REPO_CHECKOUT}" && pwd -P)"
report_dir="$(cd "${MYTHOS_REPORT_DIR}" && pwd -P)"
[[ -f "${MYTHOS_PROMPT_FILE}" && ! -L "${MYTHOS_PROMPT_FILE}" ]] \
  || die "MYTHOS_PROMPT_FILE must be a regular file"

# Additional cross-repo checkouts are optional and best-effort — a missing
# manifest or an empty one just means the skill sees no additional_repos,
# the same non-fatal degradation the in-process pre-flight already accepts
# when it can't resolve a hint.
additional_repos=()
if [[ -n "${MYTHOS_ADDITIONAL_REPOS_MANIFEST:-}" && -f "${MYTHOS_ADDITIONAL_REPOS_MANIFEST}" ]]; then
  mapfile -t additional_repos < "${MYTHOS_ADDITIONAL_REPOS_MANIFEST}"
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
network="vulnhunt-mythos-verify-${run_key}"
egress_network="vulnhunt-mythos-verify-egress-${run_key}"
proxy_container="vulnhunt-mythos-verify-proxy-${run_key}"
agent_container="vulnhunt-mythos-verify-agent-${run_key}"
agent_image="vulnhunt-mythos-verify-agent:${run_key}"
proxy_image="vulnhunt-mythos-verify-proxy:${run_key}"
control_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/vulnhunt-mythos-verify.XXXXXX")"
env_file="${control_dir}/agent.env"

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if (( rc != 0 )); then
    echo "::group::Squid diagnostics captured before failed-verify cleanup" >&2
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
VULNHUNT_ANTHROPIC_MODEL=${VULNHUNT_ANTHROPIC_MODEL}
VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID=${VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID}
VULNHUNT_ANTHROPIC_AWS_API_KEY=${VULNHUNT_ANTHROPIC_AWS_API_KEY}
VULNHUNT_ANTHROPIC_AWS_REGION=${MYTHOS_REGION}
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
# root filesystem enter this container. Verify never uses Bash (no tool in its
# allow-list needs it), so — unlike fix — this profile has no code-execution
# surface at all beyond the model's Read/Write/Edit/Glob/Grep tools.
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
  --memory "${MYTHOS_MEMORY_LIMIT:-4g}" \
  --cpus "${MYTHOS_CPU_LIMIT:-2}" \
  --ulimit nofile=1024:1024 \
  --ipc none \
  --hostname mythos-verify-agent \
  --tmpfs /workspace:rw,nosuid,nodev,noexec,size=${MYTHOS_WORKSPACE_LIMIT:-4g},uid=65532,gid=65532,mode=0700 \
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

# Populate ephemeral storage as the unprivileged runtime UID.
tar -C "${target_checkout}" -cf - . \
  | docker exec --user 65532:65532 --interactive "${agent_container}" sh -c \
      'mkdir -p /workspace/repo && tar -xf - -C /workspace/repo'
tar -C "${report_dir}" -cf - . \
  | docker exec --user 65532:65532 --interactive "${agent_container}" sh -c \
      'mkdir -p /workspace/report && tar -xf - -C /workspace/report'
index=0
for additional_repo in "${additional_repos[@]}"; do
  [[ -n "${additional_repo}" && -d "${additional_repo}" ]] || continue
  additional_repo="$(cd "${additional_repo}" && pwd -P)"
  tar -C "${additional_repo}" -cf - . \
    | docker exec --user 65532:65532 --interactive "${agent_container}" sh -c \
        'mkdir -p "/workspace/additional_repos/$1" && tar -xf - -C "/workspace/additional_repos/$1"' _ "${index}"
  index=$((index + 1))
done
docker cp "${MYTHOS_PROMPT_FILE}" "${agent_container}:/workspace/kickoff-prompt.txt"
docker exec --user 0:0 "${agent_container}" chown 65532:65532 /workspace/kickoff-prompt.txt
docker exec --user 65532:65532 "${agent_container}" sh -c \
  'mkdir -p "$HOME/.claude" "$XDG_RUNTIME_DIR" /workspace/out/iter-1 && chmod 0700 "$XDG_RUNTIME_DIR" && cp -R /opt/claude/skills "$HOME/.claude/skills"'

# Mechanical egress assertions, identical shape to the scan Mythos launcher.
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
  python -m agent._mythos_verify_entry \
    --config /opt/vulnhunter-agent/agent/config.example.toml \
    --model "${VULNHUNT_ANTHROPIC_MODEL}" \
    --cwd /workspace \
    --out-dir /workspace/out/iter-1 \
    --prompt-file /workspace/kickoff-prompt.txt \
    --log-path /workspace/agent.log
entry_rc=$?
set -e

# Stream results back regardless of exit code — a schema-invalid or empty
# disposition still needs to reach the host's classify_output() call for a
# proper (not silently-empty) VerifySessionResult.
mkdir -p "${MYTHOS_OUT_DIR}"
if docker exec "${agent_container}" test -e /workspace/out/iter-1/verify_disposition.json; then
  docker exec "${agent_container}" test ! -L /workspace/out/iter-1/verify_disposition.json \
    || die "Refusing to export a symlinked disposition"
  docker exec --user 65532:65532 "${agent_container}" tar \
      --hard-dereference -C /workspace/out/iter-1 -cf - verify_disposition.json \
    | tar --no-same-owner --no-same-permissions -C "${MYTHOS_OUT_DIR}" -xf -
fi
if docker exec "${agent_container}" test -e /workspace/agent.log; then
  docker cp "${agent_container}:/workspace/agent.log" "${MYTHOS_LOG_PATH}" 2>/dev/null || true
fi

exit "${entry_rc}"
