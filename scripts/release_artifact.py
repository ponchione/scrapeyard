#!/usr/bin/env python3
"""Retain a scanned/qualified release; verify and load it for offline deployment."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def inventory(directory: Path) -> dict[str, dict[str, str | int]]:
    files = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"release contains a link or special file: {relative}")
        if relative == "manifest.json":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files[relative] = {"sha256": digest.hexdigest(), "mode": stat.S_IMODE(mode)}
    return files


def verify(directory: Path) -> dict:
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("manifest must be a regular file")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or manifest.get("format") != "scrapeyard-release-v1":
        raise ValueError("unsupported or incomplete release")
    if not re.fullmatch(r"[a-f0-9]{40}", manifest.get("revision", "")):
        raise ValueError("release source revision is missing")
    images = manifest.get("images", {})
    if set(images) != {"scrapeyard", "redis", "egress-probe"} or not all(
        re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in images.values()
    ):
        raise ValueError("release must identify all three immutable images")
    if manifest.get("files") != inventory(directory):
        raise ValueError("release checksum, file set, or permissions changed")
    overlay = json.loads((directory / "images.json").read_text())
    if overlay != {"services": {name: {"image": value} for name, value in images.items()}}:
        raise ValueError("release Compose images differ from the manifest")
    return manifest


def create(directory: Path, revision: str) -> None:
    revision = run("git", "rev-parse", "--verify", f"{revision}^{{commit}}", cwd=ROOT)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists():
        raise ValueError("release destination already exists; keep previous releases intact")
    # Includes the image archive plus build/qualification headroom. Do not prune
    # other projects or a previous known-good release to make room.
    if shutil.disk_usage(directory.parent).free < 15 * 1024**3:
        raise ValueError("at least 15 GiB free disk is required to prepare a release")
    with tempfile.TemporaryDirectory(prefix=".release-", dir=directory.parent) as temporary:
        bundle = Path(temporary)
        source = bundle / "source"
        source.mkdir()
        archive = bundle / "source.tar"
        subprocess.run(
            ["git", "archive", "--format=tar", f"--output={archive}", revision],
            cwd=ROOT,
            check=True,
        )
        with tarfile.open(archive) as stream:
            # git archive comes from the explicitly selected local commit. Refuse
            # links/special entries so retained security mounts cannot escape it.
            for member in stream.getmembers():
                if (
                    not (member.isfile() or member.isdir())
                    or ".." in Path(member.name).parts
                    or member.name.startswith("/")
                ):
                    raise ValueError(f"unsafe source archive entry: {member.name}")
            stream.extractall(
                source, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {})
            )
        archive.unlink()
        # The existing qualification lane uses fixed fixture subnets. Refuse a
        # collision before building instead of disturbing another deployment.
        env = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            SCRAPEYARD_API_CREDENTIALS="{}",
            SCRAPEYARD_ENCRYPTION_KEYS="{}",
            SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID="release",
        )
        config = json.loads(
            run(
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker-compose.smoke.yml",
                "config",
                "--format",
                "json",
                cwd=source,
                env=env,
            )
        )
        subnets = [
            ipaddress.ip_network(item["subnet"])
            for network in config["networks"].values()
            for item in network.get("ipam", {}).get("config", [])
            if "subnet" in item
        ]
        network_ids = run("docker", "network", "ls", "-q", cwd=source).split()
        networks = (
            json.loads(run("docker", "network", "inspect", *network_ids, cwd=source))
            if network_ids
            else []
        )
        for network in networks:
            for item in network.get("IPAM", {}).get("Config") or []:
                if item.get("Subnet") and any(
                    ipaddress.ip_network(item["Subnet"]).overlaps(subnet) for subnet in subnets
                ):
                    raise ValueError(
                        f"qualification subnet overlaps existing network {network['Name']}; use an isolated Docker host"
                    )
        project = f"scrapeyard-release-{bundle.name.removeprefix('.release-')}"
        tag = f"{project}-scrapeyard"
        subprocess.run(
            [
                "docker",
                "build",
                "--label",
                f"org.opencontainers.image.revision={revision}",
                "--tag",
                tag,
                ".",
            ],
            cwd=source,
            check=True,
        )
        image = run("docker", "image", "inspect", "--format", "{{.Id}}", tag, cwd=source)
        env["SCRAPEYARD_SECURITY_REPORT_DIR"] = str(bundle / "security-reports")
        subprocess.run(
            ["./scripts/run_container_security_scan.sh", image], cwd=source, env=env, check=True
        )
        env.update(
            SCRAPEYARD_QUALIFICATION_PROJECT=project,
            SCRAPEYARD_QUALIFICATION_DIAGNOSTICS_DIR=str(bundle / "qualification-reports"),
        )
        subprocess.run(
            ["./scripts/run_release_qualification.sh", "--profile", "quick", "--no-build"],
            cwd=source,
            env=env,
            check=True,
        )
        if run("docker", "image", "inspect", "--format", "{{.Id}}", tag, cwd=source) != image:
            raise ValueError("application image changed during qualification")
        # Compose needs placeholders to resolve image references; never retain a
        # rendered deployment environment or its secrets in the release bundle.
        env.update(
            SCRAPEYARD_API_CREDENTIALS="{}",
            SCRAPEYARD_ENCRYPTION_KEYS="{}",
            SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID="release",
        )
        config = json.loads(
            run(
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "config",
                "--format",
                "json",
                cwd=source,
                env=env,
            )
        )
        images = {"scrapeyard": image}
        for name in ("redis", "egress-probe"):
            reference = config["services"][name]["image"]
            images[name] = run(
                "docker", "image", "inspect", "--format", "{{.Id}}", reference, cwd=source
            )
        (bundle / "images.json").write_text(
            json.dumps(
                {"services": {name: {"image": value} for name, value in images.items()}}, indent=2
            )
            + "\n"
        )
        subprocess.run(
            ["docker", "image", "save", "--output", str(bundle / "images.tar"), *images.values()],
            cwd=source,
            check=True,
        )
        manifest = {
            "format": "scrapeyard-release-v1",
            "revision": revision,
            "images": images,
            "files": inventory(bundle),
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        verify(bundle)
        bundle.rename(directory)
    print(f"Retained scanned and qualified release {revision}: {directory}")


def load_images(directory: Path) -> None:
    manifest = verify(directory)
    subprocess.run(
        ["docker", "image", "load", "--input", str(directory / "images.tar")], check=True
    )
    for image in manifest["images"].values():
        if run("docker", "image", "inspect", "--format", "{{.Id}}", image, cwd=directory) != image:
            raise ValueError("loaded image identity differs from the retained release")
    print(f"Verified and loaded release {manifest['revision']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify", "load"))
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--revision", default="HEAD", help="committed source to build (create only)"
    )
    args = parser.parse_args()
    directory = args.directory.resolve()
    try:
        if args.action == "create":
            create(directory, args.revision)
        elif args.action == "load":
            load_images(directory)
        else:
            manifest = verify(directory)
            print(f"Verified release {manifest['revision']}")
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"release artifact: {exc}\n")


if __name__ == "__main__":
    main()
