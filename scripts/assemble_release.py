from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from release_manifest import (  # noqa: E402
    ReleaseManifestError,
    validate_manifest_document,
)

TARGETS = ("macos-arm64", "macos-x64", "windows-x64", "windows-arm64")


class AssembleError(ValueError):
    """四目标产物不完整或彼此身份不一致。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_index(path: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def assemble_release(
    *,
    artifacts_root: Path,
    out_dir: Path,
    version: str,
    commit: str,
    build_run_url: str,
) -> Path:
    artifacts_root = Path(artifacts_root).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for target in TARGETS:
        target_dir = artifacts_root / target
        suffix = ".app.tar.gz" if target.startswith("macos") else "-setup.exe"
        stem = f"Tianshu-Agent-{version}-{target}"
        artifact = target_dir / f"{stem}{suffix}"
        checksum = target_dir / f"{stem}.sha256"
        manifest_path = target_dir / "release-manifest.json"
        verification_path = target_dir / "release-verification.json"
        if (
            not artifact.is_file()
            or not checksum.is_file()
            or not manifest_path.is_file()
            or not verification_path.is_file()
        ):
            raise AssembleError(f"{target} 缺少产物、SHA256、发布清单或验证报告")
        fields = checksum.read_text(encoding="ascii").split()
        actual = _sha256(artifact)
        if len(fields) != 2 or fields[0] != actual or fields[1] != artifact.name:
            raise AssembleError(f"{target} 的 SHA256 与产物不一致")
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = validate_manifest_document(
                json.loads(manifest_bytes.decode("utf-8"))
            )
        except (UnicodeDecodeError, ValueError, ReleaseManifestError) as exc:
            raise AssembleError(f"{target} 发布清单不合法：{exc}") from exc
        expected = {"target": target, "version": version, "commit": commit}
        for field, value in expected.items():
            if manifest.get(field) != value:
                raise AssembleError(
                    f"{target} 发布清单 {field} 不匹配："
                    f"期望 {value!r}，实际 {manifest.get(field)!r}"
                )
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if (
            verification.get("target") != target
            or verification.get("version") != version
            or verification.get("commit") != commit
            or verification.get("sha256") != actual
            or verification.get("artifact") != artifact.name
            or verification.get("manifest_sha256") != manifest_sha256
        ):
            raise AssembleError(f"{target} 的验证报告与产物身份不一致")
        runtime_smoke = verification.get("runtime_smoke")
        if (
            not isinstance(runtime_smoke, dict)
            or runtime_smoke.get("status") != "pass"
            or runtime_smoke.get("host_target") != target
        ):
            raise AssembleError(f"{target} 的 runtime smoke 未在原生 target 通过")
        shutil.copyfile(artifact, out_dir / artifact.name)
        shutil.copyfile(checksum, out_dir / checksum.name)
        shutil.copyfile(
            manifest_path,
            out_dir / f"release-manifest-{target}.json",
        )
        records.append(
            {
                "target": target,
                "artifact": artifact.name,
                "sha256": actual,
                "size": artifact.stat().st_size,
                "device_verification": "unknown",
                "runtime_smoke": runtime_smoke,
            }
        )

    for script in ("install_macos.sh", "uninstall_macos.sh"):
        source = Path(__file__).resolve().parent / script
        shutil.copyfile(source, out_dir / script)
    index = {
        "schema": 1,
        "product": "tianshu-agent",
        "version": version,
        "commit": commit,
        "build_run_url": build_run_url,
        "signed": False,
        "artifacts": records,
    }
    path = out_dir / "release-index.json"
    _write_index(path, index)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--build-run-url", required=True)
    args = parser.parse_args(argv)
    try:
        path = assemble_release(
            artifacts_root=args.artifacts_root,
            out_dir=args.out_dir,
            version=args.version,
            commit=args.commit,
            build_run_url=args.build_run_url,
        )
    except (AssembleError, OSError) as exc:
        print(f"发行汇总失败：{exc}")
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
