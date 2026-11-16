#!/bin/sh
set -eu

archive=
checksum_file=
prefix="${HOME}/Applications"
data_root="${HOME}/Library/Application Support/AgentOS"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --archive)
      archive=$2
      shift 2
      ;;
    --sha256)
      checksum_file=$2
      shift 2
      ;;
    --prefix)
      prefix=$2
      shift 2
      ;;
    --data-root)
      data_root=$2
      shift 2
      ;;
    *)
      echo "未知参数：$1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$archive" ] || [ -z "$checksum_file" ]; then
  echo "用法：install-macos.sh --archive PATH --sha256 PATH [--prefix DIR]" >&2
  exit 2
fi
if [ ! -f "$archive" ] || [ ! -f "$checksum_file" ]; then
  echo "安装包或 SHA256 文件不存在" >&2
  exit 2
fi

checksum_record=$(awk '
  NR == 1 {
    hash = $1
    archive_name = $2
    if (NF != 2 || length(hash) != 64 || hash !~ /^[0-9a-f]+$/ \
      || archive_name == "" || $0 != hash "  " archive_name) {
      invalid = 1
    }
  }
  END {
    if (NR != 1 || invalid) exit 1
    printf "%s\t%s\n", hash, archive_name
  }
' "$checksum_file") || {
  echo "SHA256 文件格式不合法" >&2
  exit 2
}
tab=$(printf '\t')
expected=${checksum_record%%"$tab"*}
checksum_name=${checksum_record#*"$tab"}
if [ "${#expected}" -ne 64 ] || [ "$checksum_name" = "$checksum_record" ]; then
  echo "SHA256 文件格式不合法" >&2
  exit 2
fi
case "$expected" in
  *[!0-9a-f]*)
    echo "SHA256 文件格式不合法" >&2
    exit 2
    ;;
esac
archive_name=$(basename "$archive")
if [ "$checksum_name" != "$archive_name" ]; then
  echo "SHA256 文件名不匹配" >&2
  exit 2
fi
actual=$(shasum -a 256 "$archive" | awk '{ print $1 }')
if [ "$actual" != "$expected" ]; then
  echo "安装包 SHA256 不匹配" >&2
  exit 2
fi

if ! tar -tzf "$archive" | awk '
  $0 == "" { exit 1 }
  /^\// { exit 1 }
  /(^|\/)\.\.(\/|$)/ { exit 1 }
  /\\/ { exit 1 }
'; then
  echo "安装包包含逃逸路径，拒绝安装" >&2
  exit 2
fi
if ! tar -tvzf "$archive" | awk '
  substr($0, 1, 1) ~ /^[lh]/ {
    separator = index($0, " -> ")
    if (separator == 0) exit 1
    target = substr($0, separator + 4)
    if (target ~ /^\// || target ~ /(^|\/)\.\.(\/|$)/ || target ~ /\\/) exit 1
  }
'; then
  echo "安装包包含不安全链接，拒绝安装" >&2
  exit 2
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/tianshu-install.XXXXXX")
backup=
publish_started=0
cleanup() {
  rm -rf "$work"
  if [ -n "$backup" ] && [ "$publish_started" -eq 0 ]; then
    rm -rf "$backup"
  fi
}
trap cleanup EXIT HUP INT TERM

tar -xzf "$archive" -C "$work"
source_app="$work/Tianshu Agent.app"
if [ ! -d "$source_app" ]; then
  echo "安装包缺少 Tianshu Agent.app" >&2
  exit 2
fi
release_json="$source_app/Contents/Resources/release.json"
python_bin="$source_app/Contents/Resources/python/bin/python3"
ui_index="$source_app/Contents/Resources/ui/dist/index.html"
desktop_bin="$source_app/Contents/MacOS/tianshu-desktop"
if [ ! -f "$release_json" ] || [ ! -x "$python_bin" ] \
  || [ ! -f "$ui_index" ] || [ ! -x "$desktop_bin" ]; then
  echo "安装包缺少受版本约束的应用资源" >&2
  exit 2
fi
if [ "$(plutil -extract schema raw -o - "$release_json")" != "1" ] \
  || [ "$(plutil -extract product raw -o - "$release_json")" != "tianshu-agent" ]; then
  echo "release.json schema 或 product 不合法" >&2
  exit 2
fi
release_version=$(plutil -extract version raw -o - "$release_json")
case "$release_version" in
  0.*.*) ;;
  *)
    echo "release.json version 必须匹配 0.x.y" >&2
    exit 2
    ;;
esac
release_commit=$(plutil -extract commit raw -o - "$release_json")
case "$release_commit" in
  *[!0-9a-f]*|'')
    echo "release.json commit 必须是小写十六进制" >&2
    exit 2
    ;;
esac
if [ "${#release_commit}" -ne 40 ]; then
  echo "release.json commit 必须是 40 位 SHA" >&2
  exit 2
fi
release_target=$(plutil -extract target raw -o - "$release_json")
case "$release_target" in
  macos-arm64|macos-x64) ;;
  *)
    echo "release.json target 不是受支持的 macOS 架构" >&2
    exit 2
    ;;
esac
if ! grep -q "\"target\"[[:space:]]*:[[:space:]]*\"$release_target\"" "$release_json"; then
  echo "安装包 target 不是 macOS" >&2
  exit 2
fi

canonicalize_path() {
  path=$1
  if [ -e "$path" ] || [ -L "$path" ]; then
    (cd "$path" && pwd -P)
    return
  fi
  parent=$(dirname "$path")
  base=$(basename "$path")
  mkdir -p "$parent"
  parent=$(cd "$parent" && pwd -P)
  printf '%s/%s\n' "$parent" "$base"
}

# Tauri rejects a macOS executable path that crosses a symlink. Resolve both
# roots before moving the app and refuse overlapping program/data trees, so a
# rollback or future uninstall cannot delete the other tree.
prefix=$(canonicalize_path "$prefix")
data_root=$(canonicalize_path "$data_root")
case "$prefix/" in "$data_root/"*)
  echo "安装目录不能位于数据根内" >&2
  exit 2
  ;;
esac
case "$data_root/" in "$prefix/"*)
  echo "数据根不能位于安装目录内" >&2
  exit 2
  ;;
esac

if ! "$desktop_bin" --verify-bundle "$source_app/Contents/Resources"; then
  echo "新版本资源清单校验失败，拒绝替换现有程序" >&2
  exit 2
fi

mkdir -p "$prefix"
target="$prefix/Tianshu Agent.app"
if [ -L "$target" ] || { [ -e "$target" ] && [ ! -d "$target" ]; }; then
  echo "现有应用入口必须是目录，不能是链接或文件" >&2
  exit 2
fi
# A previous SIGKILL may leave one prepared/previous generation behind. The
# formal target is still valid; rotate that evidence away only when a new run
# has completed archive, manifest, and path validation.
for stale in "$target".previous-*; do
  if [ -e "$stale" ] || [ -L "$stale" ]; then
    rm -rf "$stale"
  fi
done
backup=$(mktemp -d "${target}.previous-XXXXXX")
staged_app="$backup/Tianshu Agent.app"
if ! mv "$source_app" "$staged_app"; then
  echo "安装失败；旧版本保持不变" >&2
  exit 1
fi
if ! env PYTHONPATH="$staged_app/Contents/Resources/app" PYTHONDONTWRITEBYTECODE=1 \
  "$staged_app/Contents/Resources/python/bin/python3" \
  -P \
  -m tianshu.release_smoke \
  --ui-dir "$staged_app/Contents/Resources/ui/dist" \
  --expected-version "$release_version"; then
  echo "新版本启动烟测失败；旧版本保持不变，无需恢复旧版本" >&2
  exit 1
fi

# The prepared app lives on the destination filesystem. Darwin's RENAME_SWAP
# publishes it in one operation; the old app remains at staged_app, including
# when SIGKILL prevents this shell's cleanup trap from running. No two-rename
# fallback is permitted on filesystems that do not support an atomic swap.
publish_started=1
if ! env PYTHONDONTWRITEBYTECODE=1 \
  "$staged_app/Contents/Resources/python/bin/python3" -I -B - "$staged_app" "$target" <<'PY'
import ctypes
import os
import signal
import sys


def crashpoint(name):
    if os.environ.get("TIANSHU_FAILPOINT") == name:
        # Kill the installer too: a helper-only exit would execute its trap and
        # would not reproduce the abrupt process loss this regression checks.
        os.kill(os.getppid(), signal.SIGKILL)
        os._exit(77)


source, target = sys.argv[1:]
if sys.platform != "darwin":
    raise SystemExit("原子应用切换仅支持 macOS")
libc = ctypes.CDLL(None, use_errno=True)
rename = libc.renamex_np
rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
rename.restype = ctypes.c_int
flags = 2 if os.path.lexists(target) else 4  # RENAME_SWAP / RENAME_EXCL
crashpoint("install_before_switch")
if rename(os.fsencode(source), os.fsencode(target), flags) != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error), target)
crashpoint("install_after_switch")
for directory in (os.path.dirname(source), os.path.dirname(target)):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
crashpoint("install_after_directory_sync")
PY
then
  echo "安装未确认完成；未删除正式入口或暂存/旧版本：$backup" >&2
  exit 1
fi
if [ -d "$staged_app" ]; then
  echo "旧版本保留：$staged_app"
else
  rmdir "$backup"
  backup=
fi
echo "已安装：$target"
echo "用户数据目录未修改。"
