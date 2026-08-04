from __future__ import annotations

import os
import shutil
import subprocess
import tempfile


def build(dockerfile_dir: str, tag: str) -> str:
    subprocess.run(
        ["docker", "build", "-t", tag, dockerfile_dir],
        check=True,
    )
    return tag


def run(
    image_tag: str,
    name: str,
    network: str = "none",
    memory: str = "4g",
    shm_size: str | None = None,
    shell: str = "/bin/bash",
    runtime: str | None = None,
    env: dict[str, str] | None = None,
    mounts: list[tuple[str, str]] | None = None,
) -> str:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    runtime = runtime or os.environ.get("VULN_HARNESS_DOCKER_RUNTIME")
    extra: list[str] = []
    if runtime:
        extra += ["--runtime", runtime]
    if shm_size:
        extra += ["--shm-size", shm_size]
    env_file: str | None = None
    if env:
        fd, env_file = tempfile.mkstemp(prefix="vulnhunt-docker-env-")
        try:
            os.chmod(env_file, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for key, value in env.items():
                    if "\n" in key or "\r" in key or "=" in key:
                        raise ValueError(f"invalid container environment key: {key!r}")
                    if "\n" in value or "\r" in value:
                        raise ValueError(
                            f"container environment value for {key!r} contains a newline"
                        )
                    fh.write(f"{key}={value}\n")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            os.unlink(env_file)
            raise
        extra += ["--env-file", env_file]
    for src, dst in (mounts or []):
        extra += ["-v", f"{src}:{dst}:ro"]
    try:
        r = subprocess.run(
            [
                "docker", "run", "-dit",
                *extra,
                "--name", name,
                "--network", network,
                "--memory", memory,
                image_tag, shell,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        if env_file:
            try:
                os.unlink(env_file)
            except FileNotFoundError:
                pass
    if r.returncode != 0:
        raise RuntimeError(
            f"docker run failed (exit {r.returncode}): {r.stderr.strip()}"
        )
    actual_image, actual_runtime = subprocess.run(
        ["docker", "inspect", name, "--format",
         "{{.Config.Image}}\t{{.HostConfig.Runtime}}"],
         capture_output=True, text=True, check=True,
    ).stdout.rstrip("\n").split("\t")
    if actual_image != image_tag:
        raise RuntimeError(
            f"container {name} has wrong image: requested {image_tag!r}, got {actual_image!r}"
        )
    if runtime and actual_runtime != runtime:
        raise RuntimeError(
            f"container {name} runtime mismatch: requested {runtime!r}, "
            f"docker reports {actual_runtime}"
        )
    return name


def read_file(container: str, path: str) -> bytes:
    """Read a file from the container to ensure it exists so agent does lie about POC path"""
    r = subprocess.run(
        ["docker", "exec", container, "cat", path],
        capture_output=True,
    )
    return r.stdout if r.returncode == 0 else b""


def write_file(container: str, path: str, content: bytes) -> None:
    """Write bytes to a path inside a container securely"""
    subprocess.run(
        ["docker", "exec", "-i", container, "sh", "-c", 'cat > "$1"', "_", path],
        input=content,
        check=True,
        capture_output=True,
    )


def copy_dir_in(container: str, host_dir: str, dst: str) -> None:
    """Copy a host directory into a container as container-owned files"""
    if shutil.which("tar") is None:
        raise RuntimeError(
            "host `tar` is required to copy the auth profile into agent "
            "containers - install tar, or authenticate via ANTHROPIC_API_KEY "
            "or WIF instead"
        )
    tar = subprocess.run(
        ["tar", "-C", host_dir, "-cf", "-", "."],
        capture_output=True, check=True,
    ).stdout
    subprocess.run(
        ["docker", "exec", "-i", container, "sh", "-c",
         'mkdir -p "$1" && tar -xf - -C "$1"', "_", dst],
         input=tar, check=True, capture_output=True,
    )


def rm(container: str) -> None:
    """Remove a container, force-killing if running, Idempotent."""
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def image_exists(tag: str) -> bool:
    """Check whether an image tag exists locally"""
    r = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return r.returncode == 0


def exec_sh(
    container: str, command: str, timeout: int | None = None
) -> tuple[int, str, str]:
    """Run a shell command inside a container and return (rc, stdout, stderr)"""
    r = subprocess.run(
        ["docker", "exec", container, "sh", "-c", command],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def commit(container: str, tag: str) -> str:
    """Snapshot a container's filesystem as a new image.  Use by re-attack to run a
    find agent against the patched binary without rebuilding."""
    subprocess.run(["docker", "commit", container, tag], check=True, capture_output=True)
    return tag


def rmi(tag: str) -> None:
    """Remove an image tag.  Idempotent."""
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
