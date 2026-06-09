#!/usr/bin/env python3
"""
voice-relay deployment pipeline

Modes:
1. A flow part one: upload/unzip build package on server A, then delete remote zip.
2. A flow part two: build image on server A and download voice-relay_latest.tar to local.
3. B flow: upload local image tar and runtime files to server B, then load/start.
4. Full flow: if both A and B arguments are provided without --part, run full A then B.

Examples:
python deploy_pipeline.py --ahost 1.2.3.4 --apwd 123 --adir /home/test --part one
python deploy_pipeline.py --ahost 1.2.3.4 --apwd 123 --adir /home/test --part two
python deploy_pipeline.py --ahost 1.2.3.4 --apwd 123 --adir /home/test
python deploy_pipeline.py --bhost 5.6.7.8 --bpwd 456 --bdir /home/test
python deploy_pipeline.py --ahost 1.2.3.4 --apwd 123 --adir /home/test --bhost 5.6.7.8 --bpwd 456 --bdir /home/test
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
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: paramiko\n"
        "Install it with: python -m pip install paramiko"
    ) from exc


IMAGE_NAME = "voice-relay:latest"
CONTAINER_NAME = "voice-relay"
DOCKER_NETWORK = "data-aiops-net"
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
A_FLOW_PARTS = ("one", "two")


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


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def project_root() -> Path:
    return repo_root() / PROJECT_DIR_NAME


def ensure_exists(path: Path, description: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Missing {description}: {path}")
    return path


def maybe_prompt_password(label: str, supplied: str | None) -> str:
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
    print(f"[local] created {output_zip.name}")
    return output_zip


def create_runtime_zip(output_zip: Path, env_path: Path, keywords_path: Path, model_dir: Path) -> Path:
    ensure_exists(env_path, ".env file")
    ensure_exists(keywords_path, "keywords.txt")
    ensure_exists(model_dir, f"model directory {MODEL_DIR_NAME}")
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
    print(f"[local] created {output_zip.name}")
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


def build_server_a_part_one_commands(remote_dir: str) -> Sequence[str]:
    deploy_zip = posixpath.join(remote_dir, DEPLOY_ZIP_NAME)
    project_dir = posixpath.join(remote_dir, PROJECT_DIR_NAME)
    return [
        f"mkdir -p {quote_remote(remote_dir)}",
        f"rm -rf {quote_remote(project_dir)}",
        f"cd {quote_remote(remote_dir)}",
        f"unzip -oq {quote_remote(deploy_zip)} -d {quote_remote(remote_dir)}",
        f"rm -f {quote_remote(deploy_zip)}",
    ]


def build_server_a_part_two_commands(remote_dir: str) -> Sequence[str]:
    project_dir = posixpath.join(remote_dir, PROJECT_DIR_NAME)
    image_tar = posixpath.join(remote_dir, IMAGE_TAR_NAME)
    return [
        (
            f"test -d {quote_remote(project_dir)} || "
            f"(echo 'Missing extracted project dir: {project_dir}. Run --part one first.' >&2; exit 1)"
        ),
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


def build_server_b_commands(remote_dir: str, network: str) -> Sequence[str]:
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
            f"docker network inspect {shlex.quote(network)} >/dev/null 2>&1 "
            f"|| docker network create {shlex.quote(network)}"
        ),
        (
            "docker run -d "
            f"--name {CONTAINER_NAME} "
            f"--network {shlex.quote(network)} "
            "--restart unless-stopped "
            f"--env-file {quote_remote(env_file)} "
            "-p 9000:9000 "
            f"-v {quote_remote(model_dir)}:/app/{MODEL_DIR_NAME}:ro "
            f"-v {quote_remote(keywords_file)}:/app/keywords.txt:ro "
            f"{IMAGE_NAME}"
        ),
    ]


def run_remote_script(session: RemoteSession, commands: Sequence[str]) -> None:
    session.run("set -e\n" + "\n".join(commands))


def run_a_flow(
    host: str,
    port: int,
    user: str,
    password: str,
    remote_dir: str,
    work_dir: Path,
    part: str | None = None,
) -> Path:
    local_image_tar = work_dir / IMAGE_TAR_NAME
    remote_tar = posixpath.join(remote_dir, IMAGE_TAR_NAME)

    with RemoteSession("server-a", host, port, user, password) as server_a:
        if part in (None, "one"):
            build_zip = create_build_zip(work_dir / DEPLOY_ZIP_NAME)
            remote_zip = posixpath.join(remote_dir, DEPLOY_ZIP_NAME)
            server_a.upload_file(build_zip, remote_zip)
            remove_local_file(build_zip, "local build zip after upload")
            run_remote_script(server_a, build_server_a_part_one_commands(remote_dir))
            if part == "one":
                print(f"[server-a] prepared project files in {remote_dir}/{PROJECT_DIR_NAME}")
                return local_image_tar

        run_remote_script(server_a, build_server_a_part_two_commands(remote_dir))
        server_a.download_file(remote_tar, local_image_tar)
        run_remote_script(server_a, cleanup_server_a_commands(remote_dir))

    print(f"[local] downloaded image tar: {local_image_tar}")
    return local_image_tar


def run_b_flow(
    host: str,
    port: int,
    user: str,
    password: str,
    remote_dir: str,
    work_dir: Path,
    env_path: Path,
    keywords_path: Path,
    model_dir: Path,
    network: str,
) -> None:
    local_image_tar = ensure_exists(work_dir / IMAGE_TAR_NAME, IMAGE_TAR_NAME)
    runtime_zip = create_runtime_zip(work_dir / RUNTIME_ZIP_NAME, env_path, keywords_path, model_dir)
    remote_tar = posixpath.join(remote_dir, IMAGE_TAR_NAME)
    remote_zip = posixpath.join(remote_dir, RUNTIME_ZIP_NAME)

    with RemoteSession("server-b", host, port, user, password) as server_b:
        server_b.upload_file(local_image_tar, remote_tar)
        server_b.upload_file(runtime_zip, remote_zip)
        remove_local_file(local_image_tar, "local image tar after upload")
        remove_local_file(runtime_zip, "local runtime zip after upload")
        run_remote_script(server_b, build_server_b_commands(remote_dir, network))


def parse_args() -> argparse.Namespace:
    root = repo_root()
    vr = project_root()
    parser = argparse.ArgumentParser(description="voice-relay deployment helper")

    parser.add_argument("--ahost", help="server A host or IP")
    parser.add_argument("--aport", type=int, default=22, help="server A SSH port")
    parser.add_argument("--auser", default="root", help="server A SSH user, default root")
    parser.add_argument("--apwd", help="server A SSH password")
    parser.add_argument("--adir", help="server A target dir, e.g. /home/test")
    parser.add_argument("--part", choices=A_FLOW_PARTS, help="run only one part of A flow: one or two")

    parser.add_argument("--bhost", help="server B host or IP")
    parser.add_argument("--bport", type=int, default=22, help="server B SSH port")
    parser.add_argument("--buser", default="root", help="server B SSH user, default root")
    parser.add_argument("--bpwd", help="server B SSH password")
    parser.add_argument("--bdir", help="server B target dir, e.g. /home/test")

    parser.add_argument("--env", default=str(vr / ".env"), help="local .env for server B package")
    parser.add_argument("--keywords", default=str(vr / "keywords.txt"), help="local keywords.txt for server B package")
    parser.add_argument("--modeldir", default=str(vr / MODEL_DIR_NAME), help="local model dir for server B package")
    parser.add_argument("--workdir", default=str(root), help="local work dir for generated zip/tar")
    parser.add_argument("--network", default=DOCKER_NETWORK, help=f"docker network for container, default {DOCKER_NETWORK}")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> tuple[bool, bool]:
    has_a = any([args.ahost, args.apwd, args.adir])
    has_b = any([args.bhost, args.bpwd, args.bdir])

    if not has_a and not has_b:
        raise SystemExit("Need A flow args or B flow args. Example: --ahost ... --adir ...")

    if has_a:
        missing = [name for name in ("ahost", "adir") if not getattr(args, name)]
        if missing:
            raise SystemExit(f"A flow missing args: {', '.join('--' + item for item in missing)}")

    if has_b:
        missing = [name for name in ("bhost", "bdir") if not getattr(args, name)]
        if missing:
            raise SystemExit(f"B flow missing args: {', '.join('--' + item for item in missing)}")

    if args.part and not has_a:
        raise SystemExit("--part can only be used with A flow args")

    if args.part and has_b:
        raise SystemExit("--part cannot be used together with B flow args")

    return has_a, has_b


def main() -> int:
    args = parse_args()
    has_a, has_b = validate_args(args)

    work_dir = Path(args.workdir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    env_path = Path(args.env).resolve()
    keywords_path = Path(args.keywords).resolve()
    model_dir = Path(args.modeldir).resolve()

    if has_a:
        a_password = maybe_prompt_password("server A", args.apwd)
        run_a_flow(
            host=args.ahost,
            port=args.aport,
            user=args.auser,
            password=a_password,
            remote_dir=normalize_remote_dir(args.adir),
            work_dir=work_dir,
            part=args.part,
        )

    if has_b:
        b_password = maybe_prompt_password("server B", args.bpwd)
        run_b_flow(
            host=args.bhost,
            port=args.bport,
            user=args.buser,
            password=b_password,
            remote_dir=normalize_remote_dir(args.bdir),
            work_dir=work_dir,
            env_path=env_path,
            keywords_path=keywords_path,
            model_dir=model_dir,
            network=args.network,
        )

    print("[done] deployment completed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[abort] interrupted by user", file=sys.stderr)
        raise SystemExit(130)
