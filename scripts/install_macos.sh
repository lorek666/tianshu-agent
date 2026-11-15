#!/bin/sh
set -eu

archive=
checksum_file=
prefix="${HOME}/Applications"

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
    *)
      echo "未知参数：$1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$archive" ] || [ -z "$checksum_file" ]; then
  echo "用法：install_macos.sh --archive PATH --sha256 PATH [--prefix DIR]" >&2
  exit 2
fi
if [ ! -f "$archive" ] || [ ! -f "$checksum_file" ]; then
  echo "安装包或 SHA256 文件不存在" >&2
  exit 2
fi

expected=$(awk 'NR == 1 { print $1 }' "$checksum_file")
case "$expected" in
  *[!0-9a-f]*|'')
    echo "SHA256 文件格式不合法" >&2
    exit 2
    ;;
esac
if [ "${#expected}" -ne 64 ]; then
  echo "SHA256 必须是 64 位小写十六进制" >&2
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
cleanup() {
  rm -rf "$work"
  if [ -n "$backup" ] && [ -d "$backup" ] && [ ! -e "$prefix/Tianshu Agent.app" ]; then
    mv "$backup" "$prefix/Tianshu Agent.app" || true
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
if [ ! -f "$release_json" ] || [ ! -x "$python_bin" ] || [ ! -f "$ui_index" ]; then
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

mkdir -p "$prefix"
# Tauri rejects a macOS executable path that crosses a symlink. Resolve the
# install prefix before moving the app so callers may pass /tmp or a linked
# Applications directory without creating an app that cannot start.
prefix=$(cd "$prefix" && pwd -P)
target="$prefix/Tianshu Agent.app"
if [ -e "$target" ]; then
  backup="${target}.previous-$(date +%Y%m%d%H%M%S)"
  mv "$target" "$backup"
fi
if ! mv "$source_app" "$target"; then
  echo "安装失败；旧版本保持不变" >&2
  exit 1
fi
if ! env PYTHONPATH="$target/Contents/Resources/app" \
  "$target/Contents/Resources/python/bin/python3" \
  -c 'import tianshu, tianshu.host, tianshu.daemon'; then
  rm -rf "$target"
  echo "新版本启动烟测失败；正在恢复旧版本" >&2
  exit 1
fi
if [ -n "$backup" ]; then
  rm -rf "$backup"
fi
backup=
echo "已安装：$target"
echo "用户数据目录未修改。"
