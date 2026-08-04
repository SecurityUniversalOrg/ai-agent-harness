#!/usr/bin/env bash
# Docker control-plane access for the trusted Mythos launcher scripts.
#
# Direct access through the runner service account is preferred. A passwordless
# sudo fallback is supported because many hardened self-hosted runner images
# intentionally leave /var/run/docker.sock owned by root:docker. This helper
# never changes socket permissions or host group membership.

MYTHOS_DOCKER_ACCESS_MODE="unconfigured"
MYTHOS_DOCKER_BIN=""

mythos_configure_docker() {
  local diagnostic_dir direct_error sudo_error socket_path

  # `type -P` deliberately ignores the wrapper function defined below and
  # resolves the real Docker CLI from PATH.
  MYTHOS_DOCKER_BIN="$(type -P docker 2>/dev/null || true)"
  if [[ -z "${MYTHOS_DOCKER_BIN}" ]]; then
    echo "::error::Docker CLI is not installed on the gvisor runner" >&2
    return 1
  fi

  diagnostic_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/mythos-docker-access.XXXXXX")"
  direct_error="${diagnostic_dir}/direct.err"
  sudo_error="${diagnostic_dir}/sudo.err"

  if "${MYTHOS_DOCKER_BIN}" info >/dev/null 2>"${direct_error}"; then
    MYTHOS_DOCKER_ACCESS_MODE="direct"
    rm -rf -- "${diagnostic_dir}"
    echo "Docker API access: direct (${MYTHOS_DOCKER_BIN})"
    return 0
  fi

  if command -v sudo >/dev/null 2>&1 \
      && sudo -n "${MYTHOS_DOCKER_BIN}" info >/dev/null 2>"${sudo_error}"; then
    MYTHOS_DOCKER_ACCESS_MODE="sudo"
    rm -rf -- "${diagnostic_dir}"
    echo "::warning::The runner user cannot open the Docker API directly; using non-interactive sudo for the trusted Docker control plane. Configure the runner service account for direct Docker access to remove this fallback."
    return 0
  fi

  echo "::group::Docker API access diagnostics" >&2
  echo "runner-user=$(id -un) uid=$(id -u) gid=$(id -g) groups=$(id -Gn)" >&2
  socket_path="${DOCKER_HOST:-unix:///var/run/docker.sock}"
  if [[ "${socket_path}" == unix://* ]]; then
    socket_path="${socket_path#unix://}"
    if [[ -S "${socket_path}" ]]; then
      stat -c 'docker-socket=%n owner=%U:%G uid=%u gid=%g mode=%a' "${socket_path}" >&2 \
        || ls -l -- "${socket_path}" >&2
    else
      echo "docker-socket=${socket_path} status=missing-or-not-a-socket" >&2
    fi
  else
    echo "docker-host=non-unix-socket (value suppressed)" >&2
  fi
  echo "direct-docker-error:" >&2
  sed -n '1,8p' "${direct_error}" >&2 || true
  if [[ -s "${sudo_error}" ]]; then
    echo "sudo-docker-error:" >&2
    sed -n '1,8p' "${sudo_error}" >&2 || true
  elif ! command -v sudo >/dev/null 2>&1; then
    echo "sudo-docker-error: sudo is not installed" >&2
  else
    echo "sudo-docker-error: sudo is not authorized non-interactively" >&2
  fi
  echo "::endgroup::" >&2
  rm -rf -- "${diagnostic_dir}"

  echo "::error::The trusted Mythos control plane cannot access the Docker API. Provision this dedicated runner so its service account belongs to the group owning /var/run/docker.sock, then restart the runner service; or provide narrowly controlled passwordless sudo for Docker. Docker access is root-equivalent, so do this only on an ephemeral, single-job gvisor runner." >&2
  return 1
}

# Deliberately shadow the Docker executable only inside the trusted launcher
# process. Model containers never receive this function, sudo, or the socket.
docker() {
  case "${MYTHOS_DOCKER_ACCESS_MODE}" in
    direct)
      "${MYTHOS_DOCKER_BIN}" "$@"
      ;;
    sudo)
      sudo -n "${MYTHOS_DOCKER_BIN}" "$@"
      ;;
    *)
      echo "::error::Docker wrapper used before mythos_configure_docker" >&2
      return 127
      ;;
  esac
}

# Resolve a fixed allowlisted hostname in the trusted host control plane. The
# proxy receives these addresses through /etc/hosts and therefore does not
# depend on container DNS for the allowlisted route. Only canonical IPv4 output
# is accepted.
mythos_resolve_ipv4() {
  local hostname="$1" address octet valid
  local -a octets

  if ! command -v getent >/dev/null 2>&1; then
    echo "::error::getent is required on the gvisor runner for trusted endpoint resolution" >&2
    return 1
  fi

  while IFS= read -r address; do
    [[ "${address}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || continue
    valid=true
    IFS=. read -r -a octets <<<"${address}"
    for octet in "${octets[@]}"; do
      if (( 10#${octet} > 255 )); then
        valid=false
        break
      fi
    done
    [[ "${valid}" == "true" ]] && printf '%s\n' "${address}"
  done < <(getent ahostsv4 "${hostname}" | awk '{print $1}' | sort -u)
}
