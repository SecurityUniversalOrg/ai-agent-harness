#!/usr/bin/env bash
# Credential-free, auditable preflight for the Mythos gVisor execution profile.
# This launches disposable containers with the same filesystem and network
# boundaries as the real scan, proves the boundaries mechanically, and exits.
set -Eeuo pipefail

readonly PROXY_ALIAS="mythos-egress"
readonly ALLOWED_HOST="aws-external-anthropic.us-east-1.api.aws"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# shellcheck source=scripts/_mythos_docker.sh
source "${SCRIPT_DIR}/_mythos_docker.sh"

die() {
  echo "::error::$*" >&2
  exit 1
}

proof() {
  printf 'ISOLATION_PROOF %-30s %s\n' "$1" "$2"
}

mythos_configure_docker || die "Docker API access preflight failed"
proof "docker-api-access" "${MYTHOS_DOCKER_ACCESS_MODE}; socket is retained only by the trusted host control plane"

[[ -n "${MYTHOS_REPO_ROOT:-}" ]] || die "MYTHOS_REPO_ROOT is required"
repo_root="$(cd "${MYTHOS_REPO_ROOT}" && pwd -P)"
[[ -f "${repo_root}/vulnhunter-agent/Dockerfile.mythos" ]] \
  || die "MYTHOS_REPO_ROOT does not contain vulnhunter-agent/Dockerfile.mythos"

runtime="${MYTHOS_DOCKER_RUNTIME:-runsc}"
[[ "${runtime}" == "runsc" ]] \
  || die "Mythos isolation validation is pinned to runsc; got ${runtime}"

available_runtimes="$(docker info --format '{{range $name, $runtime := .Runtimes}}{{$name}} {{end}}')"
[[ " ${available_runtimes} " == *" runsc "* ]] \
  || die "Docker runtime runsc is unavailable on this runner"
runsc_config="$(docker info --format '{{json .Runtimes.runsc}}')"
case "${runsc_config}" in
  *network=host*|*host-uds=open*|*directfs=true*)
    die "The registered runsc runtime contains a disallowed host-integration flag: ${runsc_config}"
    ;;
esac
proof "docker-runtime" "runsc registered; no network=host, host-uds=open, or directfs=true"

run_key="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-${RANDOM}"
run_key="${run_key//[^A-Za-z0-9_.-]/-}"
network="vulnhunt-mythos-proof-${run_key}"
proxy_container="vulnhunt-mythos-proof-proxy-${run_key}"
agent_container="vulnhunt-mythos-proof-agent-${run_key}"
agent_image="vulnhunt-mythos-proof-agent:${run_key}"
proxy_image="vulnhunt-mythos-proof-proxy:${run_key}"

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  docker rm -f "${agent_container}" >/dev/null 2>&1 || true
  docker rm -f "${proxy_container}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  docker image rm -f "${agent_image}" >/dev/null 2>&1 || true
  docker image rm -f "${proxy_image}" >/dev/null 2>&1 || true
  exit "${rc}"
}
trap cleanup EXIT INT TERM

echo "::group::Build disposable Mythos isolation canary images"
docker build \
  --file "${repo_root}/vulnhunter-agent/Dockerfile.mythos" \
  --tag "${agent_image}" \
  "${repo_root}"
docker build \
  --file "${repo_root}/vulnhunter-agent/docker/mythos-egress/Dockerfile" \
  --tag "${proxy_image}" \
  "${repo_root}"
echo "::endgroup::"

docker network create --internal "${network}" >/dev/null
network_internal="$(docker network inspect --format '{{.Internal}}' "${network}")"
[[ "${network_internal}" == "true" ]] || die "Canary network is not internal"

# The proxy alone receives an ordinary bridge interface. Its second interface
# is the isolated agent network, and Squid denies everything except one CONNECT
# destination. It also runs in gVisor and contains no application credentials.
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
  --network "name=bridge,gw-priority=1" \
  --network "name=${network},alias=${PROXY_ALIAS}" \
  "${proxy_image}" >/dev/null

# Attach the complete proxy topology before gVisor starts. Resolve the proxy's
# internal address in the trusted control plane and add a fixed hosts entry to
# the agent, avoiding any dependency on embedded-DNS timing.
proxy_internal_ip="$(docker inspect --format \
  "{{with index .NetworkSettings.Networks \"${network}\"}}{{.IPAddress}}{{end}}" \
  "${proxy_container}")"
[[ "${proxy_internal_ip}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || die "Could not attest the proxy's internal IPv4 address: ${proxy_internal_ip}"

# This credential-free canary uses the same isolation switches as the real
# Mythos agent. Only deliberately ephemeral tmpfs paths are writable.
docker run --detach \
  --name "${agent_container}" \
  --runtime "${runtime}" \
  --network "${network}" \
  --add-host "${PROXY_ALIAS}:${proxy_internal_ip}" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 512 \
  --memory "${MYTHOS_MEMORY_LIMIT:-8g}" \
  --cpus "${MYTHOS_CPU_LIMIT:-4}" \
  --ulimit nofile=1024:1024 \
  --ipc none \
  --hostname mythos-isolation-proof \
  --tmpfs /workspace:rw,nosuid,nodev,noexec,size=${MYTHOS_WORKSPACE_LIMIT:-8g},uid=65532,gid=65532,mode=0700 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=512m,uid=65532,gid=65532,mode=0700 \
  --tmpfs /home/appuser/.claude:rw,nosuid,nodev,noexec,size=512m,uid=65532,gid=65532,mode=0700 \
  "${agent_image}" >/dev/null

echo "::group::Selected non-secret Docker and gVisor attestations"
actual_runtime="$(docker inspect --format '{{.HostConfig.Runtime}}' "${agent_container}")"
proxy_runtime="$(docker inspect --format '{{.HostConfig.Runtime}}' "${proxy_container}")"
proxy_readonly="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${proxy_container}")"
proxy_privileged="$(docker inspect --format '{{.HostConfig.Privileged}}' "${proxy_container}")"
proxy_mount_count="$(docker inspect --format '{{len .Mounts}}' "${proxy_container}")"
proxy_network_count="$(docker inspect --format '{{len .NetworkSettings.Networks}}' "${proxy_container}")"
proxy_cap_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${proxy_container}")"
proxy_security_opt="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${proxy_container}")"
guest_kernel="$(docker exec "${agent_container}" uname -r)"
actual_user="$(docker inspect --format '{{.Config.User}}' "${agent_container}")"
actual_network="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "${agent_container}")"
agent_network_count="$(docker inspect --format '{{len .NetworkSettings.Networks}}' "${agent_container}")"
actual_readonly="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${agent_container}")"
actual_privileged="$(docker inspect --format '{{.HostConfig.Privileged}}' "${agent_container}")"
actual_pid_mode="$(docker inspect --format '{{.HostConfig.PidMode}}' "${agent_container}")"
actual_ipc_mode="$(docker inspect --format '{{.HostConfig.IpcMode}}' "${agent_container}")"
actual_publish_all="$(docker inspect --format '{{.HostConfig.PublishAllPorts}}' "${agent_container}")"
actual_port_bindings="$(docker inspect --format '{{json .HostConfig.PortBindings}}' "${agent_container}")"
mount_count="$(docker inspect --format '{{len .Mounts}}' "${agent_container}")"
device_count="$(docker inspect --format '{{len .HostConfig.Devices}}' "${agent_container}")"
cap_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${agent_container}")"
security_opt="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${agent_container}")"
tmpfs="$(docker inspect --format '{{json .HostConfig.Tmpfs}}' "${agent_container}")"

[[ "${actual_runtime}" == "runsc" ]] || die "Agent runtime attestation failed: ${actual_runtime}"
[[ "${proxy_runtime}" == "runsc" ]] || die "Proxy runtime attestation failed: ${proxy_runtime}"
[[ "${proxy_readonly}" == "true" ]] || die "Proxy root filesystem is writable"
[[ "${proxy_privileged}" == "false" ]] || die "Proxy container is privileged"
[[ "${proxy_mount_count}" == "0" ]] || die "Proxy has host or volume mounts"
[[ "${proxy_network_count}" == "2" ]] || die "Proxy does not have exactly two network interfaces"
[[ "${proxy_cap_drop}" == *'ALL'* ]] || die "Proxy did not drop all Linux capabilities"
[[ "${proxy_security_opt}" == *'no-new-privileges'* ]] || die "Proxy lacks no-new-privileges"
case "${guest_kernel}" in
  *gvisor*|4.4.0) ;;
  *) die "Unexpected guest kernel '${guest_kernel}'; gVisor attestation failed" ;;
esac
[[ "${actual_user}" == "65532:65532" ]] || die "Unexpected agent user: ${actual_user}"
[[ "${actual_network}" == "${network}" && "${agent_network_count}" == "1" ]] \
  || die "Agent is attached to an unexpected network"
[[ "${actual_readonly}" == "true" ]] || die "Agent root filesystem is writable"
[[ "${actual_privileged}" == "false" ]] || die "Agent container is privileged"
[[ -z "${actual_pid_mode}" ]] || die "Agent shares a PID namespace: ${actual_pid_mode}"
[[ "${actual_ipc_mode}" == "none" ]] || die "Agent IPC mode is not none: ${actual_ipc_mode}"
[[ "${actual_publish_all}" == "false" ]] || die "Agent publishes container ports"
[[ "${actual_port_bindings}" == "{}" || "${actual_port_bindings}" == "null" ]] \
  || die "Agent has host port bindings: ${actual_port_bindings}"
[[ "${mount_count}" == "0" ]] || die "Agent has host or volume mounts"
[[ "${device_count}" == "0" ]] || die "Agent has host devices"
[[ "${cap_drop}" == *'ALL'* ]] || die "Agent did not drop all Linux capabilities"
[[ "${security_opt}" == *'no-new-privileges'* ]] || die "Agent lacks no-new-privileges"

proof "agent-runtime" "${actual_runtime}; guest kernel ${guest_kernel}"
proof "proxy-runtime" "${proxy_runtime}; read-only=${proxy_readonly}; privileged=${proxy_privileged}"
proof "proxy-boundaries" "interfaces=${proxy_network_count}; mounts=${proxy_mount_count}; drop=${proxy_cap_drop}; security-opt=${proxy_security_opt}"
proof "proxy-internal-address" "${PROXY_ALIAS}=${proxy_internal_ip}; injected into agent hosts file"
proof "identity" "uid:gid=${actual_user}; privileged=${actual_privileged}"
proof "root-filesystem" "read-only=${actual_readonly}; host/volume mounts=${mount_count}; devices=${device_count}"
proof "namespaces" "pid=private; ipc=${actual_ipc_mode}; network=${actual_network}; interfaces=${agent_network_count}"
proof "host-ports" "publish-all=${actual_publish_all}; bindings=${actual_port_bindings}"
proof "capabilities" "drop=${cap_drop}; security-opt=${security_opt}"
proof "writable-filesystems" "tmpfs only: ${tmpfs}"
echo "::endgroup::"

echo "::group::Filesystem write-denial canaries"
for protected_path in /mythos-root-write-test /etc/mythos-write-test /opt/vulnhunter-agent/mythos-write-test; do
  set +e
  denial_output="$(docker exec --user 0:0 "${agent_container}" sh -c 'touch "$1"' _ "${protected_path}" 2>&1)"
  denial_rc=$?
  set -e
  [[ ${denial_rc} -ne 0 ]] || die "Write unexpectedly succeeded at ${protected_path}"
  proof "write-denied:${protected_path}" "exit=${denial_rc}; ${denial_output//$'\n'/ }"
done

docker exec --user 65532:65532 "${agent_container}" sh -c \
  'touch /workspace/.write-proof /tmp/.write-proof /home/appuser/.claude/.write-proof && rm -f /workspace/.write-proof /tmp/.write-proof /home/appuser/.claude/.write-proof'
proof "ephemeral-write" "allowed only in /workspace, /tmp, and /home/appuser/.claude tmpfs"

set +e
noexec_output="$(docker exec --user 65532:65532 "${agent_container}" sh -c \
  'printf "#!/bin/sh\nexit 0\n" >/tmp/mythos-exec-proof && chmod 0700 /tmp/mythos-exec-proof && /tmp/mythos-exec-proof' 2>&1)"
noexec_rc=$?
set -e
docker exec --user 65532:65532 "${agent_container}" rm -f /tmp/mythos-exec-proof
[[ ${noexec_rc} -ne 0 ]] || die "Direct execution unexpectedly succeeded from noexec tmpfs"
proof "tmpfs-noexec" "direct executable launch denied; exit=${noexec_rc}; ${noexec_output//$'\n'/ }"

if docker exec "${agent_container}" test -e /var/run/docker.sock; then
  die "Docker socket is visible inside the agent container"
fi
proof "docker-socket" "absent"
echo "::endgroup::"

echo "::group::Network-denial canaries"
docker exec --interactive --env EXPECTED_PROXY_IP="${proxy_internal_ip}" "${agent_container}" python - <<'PY'
import os
import socket
import time


def direct_must_fail(label: str, host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), 3):
            pass
    except OSError as exc:
        print(f"ISOLATION_PROOF {label:<30} BLOCKED ({type(exc).__name__})")
        return
    raise SystemExit(f"direct network access unexpectedly succeeded: {host}:{port}")


def proxy_status(payload: bytes) -> bytes:
    last_error = None
    for _ in range(30):
        try:
            with socket.create_connection((proxy_ip, 3128), 5) as sock:
                sock.sendall(payload)
                return sock.recv(512).split(b"\r\n", 1)[0]
        except OSError as exc:
            last_error = exc
            time.sleep(0.5)
    raise last_error  # type: ignore[misc]


expected_proxy_ip = os.environ["EXPECTED_PROXY_IP"]
proxy_ip = socket.gethostbyname("mythos-egress")
if proxy_ip != expected_proxy_ip:
    raise SystemExit(
        f"proxy host mapping mismatch: resolved={proxy_ip} expected={expected_proxy_ip}"
    )
print(f"ISOLATION_PROOF proxy-host-resolution          mythos-egress={proxy_ip}")

direct_must_fail("direct-http-example.com", "example.com", 80)
direct_must_fail("direct-https-example.com", "example.com", 443)
direct_must_fail("direct-ip-1.1.1.1:443", "1.1.1.1", 443)

http_denied = proxy_status(
    b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n"
)
https_denied = proxy_status(
    b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
)
ip_denied = proxy_status(
    b"CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1:443\r\n\r\n"
)
wrong_port_denied = proxy_status(
    b"CONNECT aws-external-anthropic.us-east-1.api.aws:80 HTTP/1.1\r\n"
    b"Host: aws-external-anthropic.us-east-1.api.aws:80\r\n\r\n"
)
allowed = proxy_status(
    b"CONNECT aws-external-anthropic.us-east-1.api.aws:443 HTTP/1.1\r\n"
    b"Host: aws-external-anthropic.us-east-1.api.aws:443\r\n\r\n"
)

for label, status in (
    ("proxy-http-example.com", http_denied),
    ("proxy-https-example.com", https_denied),
    ("proxy-ip-1.1.1.1", ip_denied),
    ("proxy-allowed-host-port-80", wrong_port_denied),
):
    if b" 403 " not in status:
        raise SystemExit(f"{label} was not denied: {status!r}")
    print(f"ISOLATION_PROOF {label:<30} DENIED ({status.decode('ascii', 'replace')})")

if b" 200 " not in allowed:
    raise SystemExit(f"allowlisted endpoint was not reachable: {allowed!r}")
print(
    "ISOLATION_PROOF allowlisted-aws-endpoint      "
    f"ALLOWED CONNECT only ({allowed.decode('ascii', 'replace')})"
)
PY
echo "::endgroup::"

echo "::group::Sanitized Squid policy decisions"
docker logs "${proxy_container}" 2>&1 | tail -n 20
echo "::endgroup::"

proof "result" "PASS: disposable gVisor profile enforced read-only root, tmpfs-only writes, and single-host CONNECT egress"
