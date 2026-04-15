#!/usr/bin/env python3
"""
End-to-end deployment helper for voice-relay.

Flow:
1. Package the source files required to build the Docker image on server A
   into voice-relay-deploy.zip.
2. Upload the zip to server A, unpack it, delete any existing image/container,
   build voice-relay:latest, save the image as voice-relay_latest.tar, then
   download that tar back to the local machine.
3. Clean the build artifacts on server A.
4. Package the runtime files for server B into voice-relay.zip, upload both
   voice-relay_latest.tar and voice-relay.zip to server B, then remove the
   local copies after upload.
5. On server B, optionally unpack voice-relay.zip if the voice-relay folder
   does not already exist, remove the old container/image, load the tar, and
   start the container with the requested docker run command.

Passwords:
- The password requested by this script is the SSH login password for the
  remote server account, not the machine boot/startup password.
"""

from __future__ import annotations

import argparse
import getpass
import posixpath
import shlex
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

try:
    import paramiko
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        "Missing dependency: paramiko\n"
        "Install it with: python -m pip install paramiko"
    ) from exc


IMAGE_NAME = "voice-relay:latest"
CONTAINER_NAME = "voice-relay"
DEPLOY_ZIP_NAME = "voice-relay-deploy.zip"
RUNTIME_ZIP_NAME = "voice-relay.zip"
IMAGE_TAR_NAME = "voice-relay_latest.tar"
PROJECT_DIR_NAME = "voice-relay"
MODEL_DIR_NAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
MODEL_RUNTIME_FILES = (
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "tokens.txt",
)
ZIP_DIR_SKIP = {
    "__pycache__",
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "venv",
}
ZIP_FILE_SUFFIX_SKIP = {".pyc", ".pyo", ".pyd"}


class RemoteSession:
    def __init__(
        self,
        label: str,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: int = 30,
    ) -> None:
        self.label = label
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            allow_agent=False,
            look_for_keys=False,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
        )
        self._client = client
        self._sftp = client.open_sftp()
        print(f"[{self.label}] connected to {self.username}@{self.host}:{self.port}")

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "RemoteSession":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise RuntimeError(f"[{self.label}] SSH client is not connected")
        return self._client

    @property
    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise RuntimeError(f"[{self.label}] SFTP client is not connected")
        return self._sftp

    def run(self, command: str, check: bool = True) -> str:
        wrapped = f"bash -lc {shlex.quote(command)}"
        print(f"[{self.label}] $ {command}")
        channel = self.client.get_transport().open_session()
        channel.get_pty()
        channel.exec_command(wrapped)

        output_parts: list[str] = []
        while True:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                output_parts.append(chunk)
                print(chunk, end="")
            if channel.exit_status_ready():
                while channel.recv_ready():
                    chunk = channel.recv(4096).decode("utf-8", errors="replace")
                    output_parts.append(chunk)
                    print(chunk, end="")
                break
            time.sleep(0.1)

        status = channel.recv_exit_status()
        channel.close()
        output = "".join(output_parts)
        if check and status != 0:
            raise RuntimeError(
                f"[{self.label}] command failed with exit code {status}: {command}"
            )
        return output

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.ensure_remote_dir(posixpath.dirname(remote_path))
        print(f"[{self.label}] upload {local_path} -> {remote_path}")
        self.sftp.put(str(local_path), remote_path)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{self.label}] download {remote_path} -> {local_path}")
        self.sftp.get(remote_path, str(local_path))

    def ensure_remote_dir(self, remote_dir: str) -> None:
        if not remote_dir:
            return
        self.run(f"mkdir -p {quote_remote(remote_dir)}")


def quote_remote(path: str) -> str:
    return shlex.quote(path)


def project_root() -> Path:
    return Path(__file__).resolve().parent


def default_work_dir() -> Path:
    return project_root()


def ensure_exists(path: Path, description: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Missing {description}: {path}")
    return path


def prompt_password(label: str, supplied: str | None) -> str:
    if supplied:
        return supplied
    return getpass.getpass(f"Enter SSH password for {label}: ")


def iter_files_for_zip(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(part in ZIP_DIR_SKIP for part in path.parts):
            continue
        if path.suffix.lower() in ZIP_FILE_SUFFIX_SKIP:
            continue
        yield path


def add_file_to_zip(zf: zipfile.ZipFile, source: Path, arcname: str) -> None:
    zf.write(source, arcname)


def add_tree_to_zip(zf: zipfile.ZipFile, source_dir: Path, arc_root: str) -> None:
    for file_path in iter_files_for_zip(source_dir):
        relative = file_path.relative_to(source_dir).as_posix()
        add_file_to_zip(zf, file_path, f"{arc_root}/{relative}")


def create_build_zip(output_zip: Path) -> Path:
    root = project_root()
    items = [
        ("Dockerfile", root / "Dockerfile"),
        ("requirements.txt", root / "requirements.txt"),
        (".dockerignore", root / ".dockerignore"),
        ("app", root / "app"),
    ]

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, source in items:
            ensure_exists(source, name)
            arc_root = f"{PROJECT_DIR_NAME}/{name}"
            if source.is_dir():
                add_tree_to_zip(zf, source, arc_root)
            else:
                add_file_to_zip(zf, source, arc_root)

    print(f"[local] created build package: {output_zip}")
    return output_zip


def create_runtime_zip(
    output_zip: Path,
    env_path: Path,
    keywords_path: Path,
    model_dir: Path,
) -> Path:
    ensure_exists(env_path, ".env file")
    ensure_exists(keywords_path, "keywords.txt")
    ensure_exists(model_dir, f"model directory {MODEL_DIR_NAME}")

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        add_file_to_zip(zf, env_path, f"{PROJECT_DIR_NAME}/.env")
        add_file_to_zip(zf, keywords_path, f"{PROJECT_DIR_NAME}/keywords.txt")
        for file_name in MODEL_RUNTIME_FILES:
            model_file = ensure_exists(model_dir / file_name, f"model file {file_name}")
            add_file_to_zip(
                zf,
                model_file,
                f"{PROJECT_DIR_NAME}/{MODEL_DIR_NAME}/{file_name}",
            )

    print(f"[local] created runtime package: {output_zip}")
    return output_zip


def remove_local_file(path: Path, label: str) -> None:
    if path.exists():
        path.unlink()
        print(f"[local] deleted {label}: {path}")


def normalize_remote_dir(remote_dir: str) -> str:
    cleaned = remote_dir.strip()
    if not cleaned:
        raise ValueError("Remote directory must not be empty")
    return cleaned.rstrip("/")


def build_server_a_commands(remote_dir: str) -> Sequence[str]:
    deploy_zip = posixpath.join(remote_dir, DEPLOY_ZIP_NAME)
    project_dir = posixpath.join(remote_dir, PROJECT_DIR_NAME)
    image_tar = posixpath.join(remote_dir, IMAGE_TAR_NAME)
    return [
        f"mkdir -p {quote_remote(remote_dir)}",
        f"rm -rf {quote_remote(project_dir)}",
        f"cd {quote_remote(remote_dir)}",
        f"unzip -oq {quote_remote(deploy_zip)} -d {quote_remote(remote_dir)}",
        f"rm -f {quote_remote(deploy_zip)}",
        f"docker rm -f {CONTAINER_NAME} >/dev/null 2>&1 || true",
        f"docker rmi -f {IMAGE_NAME} >/dev/null 2>&1 || true",
        f"cd {quote_remote(project_dir)}",
        f"docker build -t {IMAGE_NAME} .",
        f"docker save -o {quote_remote(image_tar)} {IMAGE_NAME}",
    ]


def cleanup_server_a_commands(remote_dir: str) -> Sequence[str]:
    project_dir = posixpath.join(remote_dir, PROJECT_DIR_NAME)
    image_tar = posixpath.join(remote_dir, IMAGE_TAR_NAME)
    return [
        f"rm -f {quote_remote(image_tar)}",
        f"rm -rf {quote_remote(project_dir)}",
    ]


def build_server_b_commands(remote_dir: str) -> Sequence[str]:
    project_dir = posixpath.join(remote_dir, PROJECT_DIR_NAME)
    runtime_zip = posixpath.join(remote_dir, RUNTIME_ZIP_NAME)
    image_tar = posixpath.join(remote_dir, IMAGE_TAR_NAME)
    env_file = posixpath.join(project_dir, ".env")
    keywords_file = posixpath.join(project_dir, "keywords.txt")
    model_dir = posixpath.join(project_dir, MODEL_DIR_NAME)

    return [
        f"mkdir -p {quote_remote(remote_dir)}",
        f"cd {quote_remote(remote_dir)}",
        (
            f"if [ -d {quote_remote(project_dir)} ]; then "
            f"rm -f {quote_remote(runtime_zip)}; "
            f"else unzip -oq {quote_remote(runtime_zip)} -d {quote_remote(remote_dir)}; "
            f"rm -f {quote_remote(runtime_zip)}; "
            f"fi"
        ),
        f"docker rm -f {CONTAINER_NAME} >/dev/null 2>&1 || true",
        f"docker rmi -f {IMAGE_NAME} >/dev/null 2>&1 || true",
        f"docker load -i {quote_remote(image_tar)}",
        (
            "docker run -d "
            f"--name {CONTAINER_NAME} "
            "--restart unless-stopped "
            f"--env-file {quote_remote(env_file)} "
            "-p 9000:9000 "
            f"-v {quote_remote(model_dir)}:/app/{MODEL_DIR_NAME}:ro "
            f"-v {quote_remote(keywords_file)}:/app/keywords.txt:ro "
            f"{IMAGE_NAME}"
        ),
    ]


def run_remote_script(session: RemoteSession, commands: Sequence[str]) -> None:
    script = "set -e\n" + "\n".join(commands)
    session.run(script)


def parse_args() -> argparse.Namespace:
    root = project_root()

    parser = argparse.ArgumentParser(
        description="Package, build, export, transfer, load, and run voice-relay."
    )
    parser.add_argument("--server-a-host", required=True, help="Server A host or IP")
    parser.add_argument("--server-a-port", type=int, default=22, help="Server A SSH port")
    parser.add_argument("--server-a-user", required=True, help="Server A SSH username")
    parser.add_argument(
        "--server-a-password",
        help="Server A SSH password. If omitted, the script will prompt for it.",
    )
    parser.add_argument(
        "--server-a-dir",
        required=True,
        help="Target directory on server A, e.g. /home/test",
    )

    parser.add_argument("--server-b-host", required=True, help="Server B host or IP")
    parser.add_argument("--server-b-port", type=int, default=22, help="Server B SSH port")
    parser.add_argument("--server-b-user", required=True, help="Server B SSH username")
    parser.add_argument(
        "--server-b-password",
        help="Server B SSH password. If omitted, the script will prompt for it.",
    )
    parser.add_argument(
        "--server-b-dir",
        required=True,
        help="Target directory on server B, e.g. /home/test",
    )

    parser.add_argument(
        "--local-env",
        default=str(root / ".env"),
        help="Local .env file used for server B runtime package",
    )
    parser.add_argument(
        "--local-keywords",
        default=str(root / "keywords.txt"),
        help="Local keywords.txt used for server B runtime package",
    )
    parser.add_argument(
        "--local-model-dir",
        default=str(root / MODEL_DIR_NAME),
        help="Local model directory used for server B runtime package",
    )
    parser.add_argument(
        "--work-dir",
        default=str(default_work_dir()),
        help="Local working directory for generated zip/tar files",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    local_env = Path(args.local_env).resolve()
    local_keywords = Path(args.local_keywords).resolve()
    local_model_dir = Path(args.local_model_dir).resolve()

    server_a_password = prompt_password("server A", args.server_a_password)
    server_b_password = prompt_password("server B", args.server_b_password)

    build_zip = work_dir / DEPLOY_ZIP_NAME
    runtime_zip = work_dir / RUNTIME_ZIP_NAME
    local_image_tar = work_dir / IMAGE_TAR_NAME

    server_a_dir = normalize_remote_dir(args.server_a_dir)
    server_b_dir = normalize_remote_dir(args.server_b_dir)
    remote_a_zip = posixpath.join(server_a_dir, DEPLOY_ZIP_NAME)
    remote_a_tar = posixpath.join(server_a_dir, IMAGE_TAR_NAME)
    remote_b_zip = posixpath.join(server_b_dir, RUNTIME_ZIP_NAME)
    remote_b_tar = posixpath.join(server_b_dir, IMAGE_TAR_NAME)

    create_build_zip(build_zip)

    with RemoteSession(
        label="server-a",
        host=args.server_a_host,
        port=args.server_a_port,
        username=args.server_a_user,
        password=server_a_password,
    ) as server_a:
        server_a.upload_file(build_zip, remote_a_zip)
        remove_local_file(build_zip, "local build zip after upload to server A")

        run_remote_script(server_a, build_server_a_commands(server_a_dir))
        server_a.download_file(remote_a_tar, local_image_tar)
        run_remote_script(server_a, cleanup_server_a_commands(server_a_dir))

    create_runtime_zip(runtime_zip, local_env, local_keywords, local_model_dir)

    with RemoteSession(
        label="server-b",
        host=args.server_b_host,
        port=args.server_b_port,
        username=args.server_b_user,
        password=server_b_password,
    ) as server_b:
        server_b.upload_file(local_image_tar, remote_b_tar)
        server_b.upload_file(runtime_zip, remote_b_zip)
        remove_local_file(local_image_tar, "local image tar after upload to server B")
        remove_local_file(runtime_zip, "local runtime zip after upload to server B")

        run_remote_script(server_b, build_server_b_commands(server_b_dir))

    print("[done] deployment pipeline completed successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[abort] interrupted by user", file=sys.stderr)
        raise SystemExit(130)
