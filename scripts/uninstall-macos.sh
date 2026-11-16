#!/bin/sh
set -eu

prefix="${HOME}/Applications"
data_root="${HOME}/Library/Application Support/AgentOS"
purge_data=0
confirm_data_root=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix)
      prefix=$2
      shift 2
      ;;
    --data-root)
      data_root=$2
      shift 2
      ;;
    --purge-data)
      purge_data=1
      shift
      ;;
    --confirm-data-root)
      confirm_data_root=$2
      shift 2
      ;;
    *)
      echo "未知参数：$1" >&2
      exit 2
      ;;
  esac
done

canonicalize_path() {
  path=$1
  if [ -e "$path" ] || [ -L "$path" ]; then
    (cd "$path" && pwd -P)
    return
  fi
  parent=$(dirname "$path")
  base=$(basename "$path")
  if [ ! -d "$parent" ]; then
    printf '%s/%s\n' "$parent" "$base"
    return
  fi
  parent=$(cd "$parent" && pwd -P)
  printf '%s/%s\n' "$parent" "$base"
}

remove_tree() {
  path=$1
  parent=$(dirname "$path")
  base=$(basename "$path")
  # Hold the canonical parent itself, then remove one relative entry. A
  # concurrent replacement of an ancestor cannot redirect rm to another tree;
  # a replacement of the final entry with a symlink removes only that link.
  (
    cd "$parent"
    if [ -L "$base" ]; then
      rm -f -- "$base"
    else
      rm -rf -- "$base"
    fi
  )
}

prefix=$(canonicalize_path "$prefix")
data_root=$(canonicalize_path "$data_root")
home_root=$(cd "$HOME" && pwd -P)
case "$prefix/" in "$data_root/"*)
  echo "拒绝卸载：程序目录位于数据根内" >&2
  exit 2
  ;;
esac
case "$data_root/" in "$prefix/"*)
  echo "拒绝卸载：数据根位于程序目录内" >&2
  exit 2
  ;;
esac

if [ "$purge_data" -eq 1 ]; then
  reject_unsafe_root() {
    echo "拒绝清空不安全的数据根：$data_root" >&2
    exit 2
  }
  same_or_descendant() {
    candidate=$1
    protected=$2
    case "$candidate/" in
      "$protected/"*) return 0 ;;
      *) return 1 ;;
    esac
  }
  same_or_ancestor() {
    candidate=$1
    protected=$2
    case "$protected/" in
      "$candidate/"*) return 0 ;;
      *) return 1 ;;
    esac
  }

  case "$data_root" in
    ""|"/"|"/Users"|"/Volumes"|"/private"|"/tmp"|"/private/tmp")
      reject_unsafe_root
      ;;
  esac
  if same_or_ancestor "$data_root" "$home_root"; then
    reject_unsafe_root
  fi
  for protected_root in \
    "/Applications" \
    "/Library" \
    "/System" \
    "/Users/Shared" \
    "/private/etc"
  do
    if same_or_descendant "$data_root" "$protected_root"; then
      reject_unsafe_root
    fi
  done
  for protected_root in \
    "$home_root/Applications" \
    "$home_root/Desktop" \
    "$home_root/Documents" \
    "$home_root/Downloads" \
    "$home_root/Movies" \
    "$home_root/Music" \
    "$home_root/Pictures" \
    "$home_root/Public"
  do
    if same_or_descendant "$data_root" "$protected_root"; then
      reject_unsafe_root
    fi
  done
  for protected_root in \
    "$home_root" \
    "$home_root/Library" \
    "$home_root/Library/Application Support"
  do
    if [ "$data_root" = "$protected_root" ]; then
      reject_unsafe_root
    fi
  done
  if [ "$confirm_data_root" != "$data_root" ]; then
    echo "清理用户数据还必须传入完全匹配的 --confirm-data-root：$data_root" >&2
    exit 2
  fi
fi

target="$prefix/Tianshu Agent.app"
if [ -d "$target" ]; then
  remove_tree "$target"
  echo "已删除程序：$target"
else
  echo "程序未安装：$target"
fi

if [ "$purge_data" -eq 1 ]; then
  if [ -e "$data_root" ]; then
    remove_tree "$data_root"
    echo "已按显式请求删除用户数据：$data_root"
  fi
else
  echo "用户数据保留：$data_root"
fi
