#!/usr/bin/env python3
"""
voice-relay one-server deployment helper

This script keeps the same deployment inputs as deploy_pipeline.py, but deploys
to a single server directly:

1. Create build zip locally.
2. Create runtime zip locally.
3. Upload both zips to the target server.
4. Build the Docker image on that server.
5. Start the container on that same server.

It does not docker save, download an image tar to local, or upload that tar to a
second server.

Examples:
python deploy_one.py 1.2.3.4 "admin@123" /home/test
python deploy_one.py 1.2.3.4 "admin@123" /home/test --user root
"""

from __future__ import annotations

import argparse
import getpass
import posixpath
import shlex
import sys
import time
import zipfile
from dataclasses import dataclass
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
DOCKER_NETWORK = "data_aiops-net"
DEPLOY_ZIP_NAME = "voice-relay-deploy.zip"
RUNTIME_ZIP_NAME = "voice-relay.zip"
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


@dataclass(frozen=True)
class TargetServer:
    label: str
    host: str
    port: int
    user: str
    password: str
    remote_dir: str


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

    def ensure_remote_dir(self, remote_dir: str) -> None:
        if remote_dir:
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


def create_runtime_zip(
    output_zip: Path,
    env_path: Path,
    keywords_path: Path,
    model_dir: Path,
) -> Path:
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


def build_one_server_commands(remote_dir: str, network: str) -> Sequence[str]:
    project_dir = posixpath.join(remote_dir, PROJECT_DIR_NAME)
    deploy_zip = posixpath.join(remote_dir, DEPLOY_ZIP_NAME)
    runtime_zip = posixpath.join(remote_dir, RUNTIME_ZIP_NAME)
    env_file = posixpath.join(project_dir, ".env")
    keywords_file = posixpath.join(project_dir, "keywords.txt")
    model_dir = posixpath.join(project_dir, MODEL_DIR_NAME)
    return [
        "command -v unzip >/dev/null 2>&1 || (echo 'Missing command: unzip' >&2; exit 1)",
        "command -v docker >/dev/null 2>&1 || (echo 'Missing command: docker' >&2; exit 1)",
        f"mkdir -p {quote_remote(remote_dir)}",
        f"rm -rf {quote_remote(project_dir)}",
        f"cd {quote_remote(remote_dir)}",
        f"unzip -oq {quote_remote(deploy_zip)} -d {quote_remote(remote_dir)}",
        f"rm -f {quote_remote(deploy_zip)}",
        f"cd {quote_remote(project_dir)}",
        f"docker rm -f {CONTAINER_NAME} >/dev/null 2>&1 || true",
        f"docker rmi -f {IMAGE_NAME} >/dev/null 2>&1 || true",
        f"docker build -t {IMAGE_NAME} .",
        f"cd {quote_remote(remote_dir)}",
        f"unzip -oq {quote_remote(runtime_zip)} -d {quote_remote(remote_dir)}",
        f"rm -f {quote_remote(runtime_zip)}",
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


def has_a_args(args: argparse.Namespace) -> bool:
    return any([args.ahost, args.adir])


def has_b_args(args: argparse.Namespace) -> bool:
    return any([args.bhost, args.bdir])


def same_when_supplied(left: object, right: object) -> bool:
    return left is None or right is None or left == right


def resolve_target(args: argparse.Namespace) -> TargetServer:
    positional_values = (args.host, args.password, args.remote_dir)
    has_positional = any(positional_values)
    has_a = has_a_args(args)
    has_b = has_b_args(args)

    if args.part:
        raise SystemExit("--part is not used by deploy_one.py; run without --part")

    if has_positional:
        if not all(positional_values):
            raise SystemExit(
                'Need positional args: host password remote_dir. '
                'Example: python deploy_one.py 192.168.10.122 "admin@123" /home/test'
            )
        if has_a or has_b:
            raise SystemExit(
                "Do not mix positional target args with --ahost/--adir or --bhost/--bdir."
            )
        return TargetServer(
            label="server",
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            remote_dir=normalize_remote_dir(args.remote_dir),
        )

    if not has_a and not has_b:
        raise SystemExit(
            'Need one target server. Example: python deploy_one.py 192.168.10.122 "admin@123" /home/test'
        )

    if has_a:
        missing = [name for name in ("ahost", "adir") if not getattr(args, name)]
        if missing:
            raise SystemExit(f"Target missing args: {', '.join('--' + item for item in missing)}")

    if has_b:
        missing = [name for name in ("bhost", "bdir") if not getattr(args, name)]
        if missing:
            raise SystemExit(f"Target missing args: {', '.join('--' + item for item in missing)}")

    if has_a and has_b:
        compatible = (
            args.ahost == args.bhost
            and args.aport == args.bport
            and same_when_supplied(args.apwd, args.bpwd)
            and args.adir == args.bdir
            and same_when_supplied(args.auser, args.buser)
        )
        if not compatible:
            raise SystemExit(
                "deploy_one.py deploys to one server only. Use either A args or B args, "
                "or make both sets point to the same server."
            )

    if has_a:
        password = maybe_prompt_password("target server", args.apwd or args.bpwd)
        return TargetServer(
            label="server",
            host=args.ahost,
            port=args.aport,
            user=args.auser,
            password=password,
            remote_dir=normalize_remote_dir(args.adir),
        )

    password = maybe_prompt_password("target server", args.bpwd)
    return TargetServer(
        label="server",
        host=args.bhost,
        port=args.bport,
        user=args.buser,
        password=password,
        remote_dir=normalize_remote_dir(args.bdir),
    )


def run_one_server_deploy(
    target: TargetServer,
    work_dir: Path,
    env_path: Path,
    keywords_path: Path,
    model_dir: Path,
    network: str,
) -> None:
    build_zip = create_build_zip(work_dir / DEPLOY_ZIP_NAME)
    runtime_zip = create_runtime_zip(work_dir / RUNTIME_ZIP_NAME, env_path, keywords_path, model_dir)
    remote_build_zip = posixpath.join(target.remote_dir, DEPLOY_ZIP_NAME)
    remote_runtime_zip = posixpath.join(target.remote_dir, RUNTIME_ZIP_NAME)

    with RemoteSession(
        target.label,
        target.host,
        target.port,
        target.user,
        target.password,
    ) as server:
        server.upload_file(build_zip, remote_build_zip)
        server.upload_file(runtime_zip, remote_runtime_zip)
        remove_local_file(build_zip, "local build zip after upload")
        remove_local_file(runtime_zip, "local runtime zip after upload")
        run_remote_script(server, build_one_server_commands(target.remote_dir, network))


def parse_args() -> argparse.Namespace:
    root = repo_root()
    vr = project_root()
    parser = argparse.ArgumentParser(
        description="voice-relay one-server deployment helper",
        epilog='Example: python deploy_one.py 192.168.10.122 "admin@123" /home/test',
    )

    parser.add_argument("host", nargs="?", help="target server host or IP")
    parser.add_argument("password", nargs="?", help="target SSH password")
    parser.add_argument("remote_dir", nargs="?", help="target deploy dir, e.g. /home/test")
    parser.add_argument("--port", type=int, default=22, help="target SSH port, default 22")
    parser.add_argument("--user", default="root", help="target SSH user, default root")

    parser.add_argument("--ahost", help="target server host or IP, compatible with server A arg")
    parser.add_argument("--aport", type=int, default=22, help="target SSH port, default 22")
    parser.add_argument("--auser", default="root", help="target SSH user, default root")
    parser.add_argument("--apwd", help="target SSH password")
    parser.add_argument("--adir", help="target deploy dir, e.g. /home/test")
    parser.add_argument("--part", choices=A_FLOW_PARTS, help="accepted for compatibility; not used")

    parser.add_argument("--bhost", help="target server host or IP, compatible with server B arg")
    parser.add_argument("--bport", type=int, default=22, help="target SSH port, default 22")
    parser.add_argument("--buser", default="root", help="target SSH user, default root")
    parser.add_argument("--bpwd", help="target SSH password")
    parser.add_argument("--bdir", help="target deploy dir, e.g. /home/test")

    parser.add_argument("--env", default=str(vr / ".env"), help="local .env for runtime package")
    parser.add_argument("--keywords", default=str(vr / "keywords.txt"), help="local keywords.txt for runtime package")
    parser.add_argument("--modeldir", default=str(vr / MODEL_DIR_NAME), help="local model dir for runtime package")
    parser.add_argument("--workdir", default=str(root), help="local work dir for generated zips")
    parser.add_argument("--network", default=DOCKER_NETWORK, help=f"docker network for container, default {DOCKER_NETWORK}")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target = resolve_target(args)

    work_dir = Path(args.workdir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    env_path = Path(args.env).resolve()
    keywords_path = Path(args.keywords).resolve()
    model_dir = Path(args.modeldir).resolve()

    run_one_server_deploy(
        target=target,
        work_dir=work_dir,
        env_path=env_path,
        keywords_path=keywords_path,
        model_dir=model_dir,
        network=args.network,
    )

    print("[done] one-server deployment completed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[abort] interrupted by user", file=sys.stderr)
        raise SystemExit(130)
