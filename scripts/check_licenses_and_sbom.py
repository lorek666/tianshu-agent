#!/usr/bin/env python3
"""开源许可证合规校验与 CycloneDX 1.5 SBOM 生成（PRD §7 F15 / §13.3）。

纯标准库实现（零外部依赖）：
1. 校验根 `LICENSE` 为 Apache License 2.0；
2. 校验 `pyproject.toml` 保持零运行时依赖（`dependencies = []`），无闭源运行时依赖；
3. 对 `uv.lock` 全量包、`ui/package.json` 直接依赖及 `desktop/src-tauri/Cargo.toml`
   全部依赖节（含 `workspace.dependencies` 与 `target.*.dependencies`）执行已审宽松许可
   白名单与禁止传染性/闭源许可（GPL / LGPL / AGPL / SSPL / BUSL / Proprietary）检查，
   新增未登记依赖立即 fail-closed 报错；
4. 从 `uv.lock`、`ui/pnpm-lock.yaml`（回退 `ui/package.json`）与 `desktop/src-tauri/Cargo.lock`
   导出覆盖直接与传递锁定组件的确定性 CycloneDX 1.5 JSON SBOM。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: 已逐一核实的 Python 开发锁文件包（运行时 dependencies 必须为空）。
ALLOWED_PYTHON_LOCK_LICENSES: dict[str, str] = {
    "colorama": "BSD-3-Clause",
    "iniconfig": "MIT",
    "packaging": "Apache-2.0 OR BSD-2-Clause",
    "pluggy": "MIT",
    "pygments": "BSD-2-Clause",
    "pytest": "MIT",
    "ruff": "MIT",
}

#: 已逐一核实的前端直接依赖许可证。
ALLOWED_NPM_LICENSES: dict[str, str] = {
    "react": "MIT",
    "react-dom": "MIT",
    "@types/react": "MIT",
    "@types/react-dom": "MIT",
    "@vitejs/plugin-react": "MIT",
    "jsdom": "MIT",
    "typescript": "Apache-2.0",
    "vite": "MIT",
    "vitest": "MIT",
}

#: 已逐一核实的 Tauri 壳直接 Rust 依赖许可证。
ALLOWED_CARGO_DIRECT_LICENSES: dict[str, str] = {
    "serde": "MIT OR Apache-2.0",
    "serde_json": "MIT OR Apache-2.0",
    "sha2": "MIT OR Apache-2.0",
    "tauri": "Apache-2.0 OR MIT",
    "tauri-plugin-single-instance": "Apache-2.0 OR MIT",
    "tempfile": "MIT OR Apache-2.0",
    "tauri-build": "Apache-2.0 OR MIT",
}

FORBIDDEN_LICENSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bAGPL\b", re.IGNORECASE),
    re.compile(r"\bLGPL\b", re.IGNORECASE),
    re.compile(r"\bGPL\b", re.IGNORECASE),
    re.compile(r"\bSSPL\b", re.IGNORECASE),
    re.compile(r"\bBUSL\b", re.IGNORECASE),
    re.compile(r"\bProprietary\b", re.IGNORECASE),
    re.compile(r"\bUNLICENSED\b", re.IGNORECASE),
)

_CARGO_DEP_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")
_PNPM_PKG_HEADER_RE = re.compile(r"^  '?(@?[^@'\s]+@[0-9][^:'\s]*)'?:\s*$")


class ComplianceError(RuntimeError):
    """许可证合规或零运行时依赖约束失败。"""


def _check_not_forbidden(name: str, license_expr: str) -> None:
    for pat in FORBIDDEN_LICENSE_PATTERNS:
        if pat.search(license_expr):
            raise ComplianceError(f"组件 {name} 含禁止的许可证声明：{license_expr}")


def _collect_cargo_direct_crates(cargo_toml: dict[str, Any]) -> list[tuple[str, str]]:
    """收集 Cargo.toml 顶层、workspace 与 target.<cfg> 下的全部直接依赖声明。"""
    found: list[tuple[str, str]] = []
    for sec in _CARGO_DEP_SECTIONS:
        for crate in sorted((cargo_toml.get(sec) or {}).keys()):
            found.append((sec, crate))
    ws_deps = (cargo_toml.get("workspace") or {}).get("dependencies") or {}
    for crate in sorted(ws_deps.keys()):
        found.append(("workspace.dependencies", crate))
    targets = cargo_toml.get("target") or {}
    if isinstance(targets, dict):
        for cfg_name in sorted(targets.keys()):
            cfg_tbl = targets.get(cfg_name) or {}
            if not isinstance(cfg_tbl, dict):
                continue
            for sec in _CARGO_DEP_SECTIONS:
                for crate in sorted((cfg_tbl.get(sec) or {}).keys()):
                    found.append((f"target.{cfg_name}.{sec}", crate))
    return found


def _parse_pnpm_lock_packages(lock_text: str) -> list[tuple[str, str]]:
    """从 pnpm-lock.yaml 的 packages: 段提取全部锁定包 (name, version)。"""
    in_packages = False
    seen: set[tuple[str, str]] = set()
    for line in lock_text.splitlines():
        if not line or line.startswith("#"):
            continue
        if not line.startswith(" "):
            in_packages = line.strip() == "packages:"
            continue
        if not in_packages:
            continue
        m = _PNPM_PKG_HEADER_RE.match(line)
        if not m:
            continue
        spec = m.group(1)
        at_idx = spec.rfind("@")
        if at_idx <= 0:
            continue
        pkg_name = spec[:at_idx]
        pkg_ver = spec[at_idx + 1 :].split("(", 1)[0]
        seen.add((pkg_name, pkg_ver))
    return sorted(seen)


def verify_and_build_sbom(repo_root: Path = ROOT) -> dict[str, Any]:
    license_file = repo_root / "LICENSE"
    if not license_file.is_file():
        raise ComplianceError("缺少根目录 LICENSE 文件")
    license_text = license_file.read_text(encoding="utf-8")
    if "Apache License" not in license_text or "Version 2.0" not in license_text:
        raise ComplianceError("根目录 LICENSE 必须是 Apache License, Version 2.0")

    pyproject = tomllib.loads(
        (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject.get("project", {})
    runtime_deps = project.get("dependencies", [])
    if runtime_deps != []:
        raise ComplianceError(
            f"违反零第三方运行时依赖红线：pyproject.toml project.dependencies = {runtime_deps!r}"
        )

    components: list[dict[str, Any]] = []

    # 1. Python lockfile (uv.lock)
    uv_lock = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    for pkg in sorted(uv_lock.get("package", []), key=lambda item: item["name"]):
        name = pkg["name"]
        version = pkg.get("version", "0.0.0")
        if name == "tianshu":
            continue
        spdx = ALLOWED_PYTHON_LOCK_LICENSES.get(name)
        if spdx is None:
            raise ComplianceError(
                f"uv.lock 出现未完成许可证准入登记的包：{name}@{version}"
            )
        _check_not_forbidden(name, spdx)
        sdist = pkg.get("sdist") or {}
        hashes = []
        if isinstance(sdist.get("hash"), str) and sdist["hash"].startswith("sha256:"):
            hashes.append(
                {"alg": "SHA-256", "content": sdist["hash"].removeprefix("sha256:")}
            )
        comp: dict[str, Any] = {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name}@{version}",
            "scope": "excluded",  # 仅开发/测试依赖，不进运行时
            "licenses": [{"license": {"name": spdx}}],
        }
        if hashes:
            comp["hashes"] = hashes
        components.append(comp)

    # 2. UI direct license gate (ui/package.json) + full lockfile SBOM (ui/pnpm-lock.yaml)
    ui_pkg_path = repo_root / "ui" / "package.json"
    pnpm_lock_path = repo_root / "ui" / "pnpm-lock.yaml"
    if ui_pkg_path.is_file():
        ui_pkg = json.loads(ui_pkg_path.read_text(encoding="utf-8"))
        pkg_lic = str(ui_pkg.get("license") or "Apache-2.0")
        _check_not_forbidden("tianshu-ui", pkg_lic)
        direct_npm_scopes: dict[str, str] = {}
        direct_npm_versions: dict[str, str] = {}
        for section, scope in (
            ("dependencies", "required"),
            ("devDependencies", "excluded"),
        ):
            deps = ui_pkg.get(section) or {}
            for name in sorted(deps):
                ver = str(deps[name])
                spdx = ALLOWED_NPM_LICENSES.get(name)
                if spdx is None:
                    raise ComplianceError(
                        f"ui/package.json ({section}) 出现未完成许可证准入登记的包：{name}@{ver}"
                    )
                _check_not_forbidden(name, spdx)
                direct_npm_scopes[name] = scope
                direct_npm_versions[name] = ver.lstrip("^~=")

        locked_npm = (
            _parse_pnpm_lock_packages(pnpm_lock_path.read_text(encoding="utf-8"))
            if pnpm_lock_path.is_file()
            else [(name, direct_npm_versions[name]) for name in sorted(direct_npm_scopes)]
        )
        for name, clean_ver in locked_npm:
            comp = {
                "type": "library",
                "name": name,
                "version": clean_ver,
                "purl": f"pkg:npm/{name}@{clean_ver}",
            }
            if name in direct_npm_scopes:
                comp["scope"] = direct_npm_scopes[name]
            if name in ALLOWED_NPM_LICENSES:
                comp["licenses"] = [{"license": {"name": ALLOWED_NPM_LICENSES[name]}}]
            components.append(comp)

    # 3. Desktop Rust direct license gate (Cargo.toml, including target.*) + Cargo.lock SBOM
    cargo_toml_path = repo_root / "desktop" / "src-tauri" / "Cargo.toml"
    cargo_lock_path = repo_root / "desktop" / "src-tauri" / "Cargo.lock"
    if cargo_toml_path.is_file() and cargo_lock_path.is_file():
        cargo_toml = tomllib.loads(cargo_toml_path.read_text(encoding="utf-8"))
        for sec_label, crate in _collect_cargo_direct_crates(cargo_toml):
            spdx = ALLOWED_CARGO_DIRECT_LICENSES.get(crate)
            if spdx is None:
                raise ComplianceError(
                    f"Cargo.toml [{sec_label}] 出现未完成许可证准入登记的直接依赖：{crate}"
                )
            _check_not_forbidden(crate, spdx)

        cargo_lock = tomllib.loads(cargo_lock_path.read_text(encoding="utf-8"))
        seen_cargo: set[tuple[str, str]] = set()
        for pkg in sorted(
            cargo_lock.get("package", []),
            key=lambda item: (item["name"], item["version"]),
        ):
            name = pkg["name"]
            version = pkg["version"]
            if name == "tianshu-desktop" or (name, version) in seen_cargo:
                continue
            seen_cargo.add((name, version))
            comp = {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:cargo/{name}@{version}",
            }
            if name in ALLOWED_CARGO_DIRECT_LICENSES:
                comp["licenses"] = [
                    {"license": {"name": ALLOWED_CARGO_DIRECT_LICENSES[name]}}
                ]
            checksum = pkg.get("checksum")
            if isinstance(checksum, str) and len(checksum) == 64:
                comp["hashes"] = [{"alg": "SHA-256", "content": checksum}]
            components.append(comp)

    license_sha = hashlib.sha256(license_file.read_bytes()).hexdigest()
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": str(project.get("name", "tianshu")),
                "version": str(project.get("version", "0.1.0")),
                "licenses": [{"license": {"id": "Apache-2.0"}}],
                "hashes": [{"alg": "SHA-256", "content": license_sha}],
            }
        },
        "components": components,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="许可证合规校验与 CycloneDX SBOM 生成"
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根目录")
    parser.add_argument("--out", type=Path, default=None, help="SBOM JSON 输出路径")
    args = parser.parse_args(argv)

    try:
        sbom = verify_and_build_sbom(args.root.resolve())
    except ComplianceError as exc:
        print(f"许可证合规检查失败：{exc}", file=sys.stderr)
        return 1

    encoded = json.dumps(sbom, ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    count = len(sbom["components"])
    print(
        f"许可证合规通过：Apache-2.0，零 Python 运行时依赖，SBOM 收录 {count} 个构建/锁定组件。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
