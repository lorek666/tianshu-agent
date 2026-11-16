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
  if [ -z "$path" ]; then
    printf '\n'
    return
  fi
  suffix=
  while [ ! -e "$path" ] && [ ! -L "$path" ] && [ "$path" != "/" ]; do
    suffix="/$(basename "$path")${suffix}"
    path=$(dirname "$path")
  done
  if [ -d "$path" ]; then
    resolved=$(cd "$path" && pwd -P)
  else
    resolved=$path
  fi
  if [ "$resolved" = "/" ]; then
    if [ -n "$suffix" ]; then
      printf '/%s\n' "${suffix#/}"
    else
      printf '/\n'
    fi
  else
    printf '%s%s\n' "$resolved" "$suffix"
  fi
}

path_identity() {
  if [ ! -e "$1" ] && [ ! -L "$1" ]; then
    return 1
  fi
  /usr/bin/stat -f '%d:%i' "$1" 2>/dev/null
}

same_path_object() {
  candidate_identity=$(path_identity "$1") || return 1
  protected_identity=$(path_identity "$2") || return 1
  [ "$candidate_identity" = "$protected_identity" ]
}

path_is_same_or_descendant() {
  candidate=$1
  protected=$2
  protected_identity=$(path_identity "$protected") || return 1
  current=$candidate
  while :; do
    if [ -e "$current" ] || [ -L "$current" ]; then
      current_identity=$(path_identity "$current") || return 1
      if [ "$current_identity" = "$protected_identity" ]; then
        return 0
      fi
    fi
    if [ "$current" = "/" ]; then
      return 1
    fi
    parent=$(dirname "$current")
    if [ "$parent" = "$current" ]; then
      return 1
    fi
    current=$parent
  done
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
if path_is_same_or_descendant "$prefix" "$data_root"; then
  echo "拒绝卸载：程序目录位于数据根内" >&2
  exit 2
fi
if path_is_same_or_descendant "$data_root" "$prefix"; then
  echo "拒绝卸载：数据根位于程序目录内" >&2
  exit 2
fi

if [ "$purge_data" -eq 1 ]; then
  reject_unsafe_root() {
    echo "拒绝清空不安全的数据根：$data_root" >&2
    exit 2
  }
  if [ -z "$data_root" ]; then
    reject_unsafe_root
  fi
  for unsafe_path in \
    "/" \
    "/Users" \
    "/Volumes" \
    "/private" \
    "/tmp" \
    "/private/tmp"
  do
    if same_path_object "$data_root" "$unsafe_path"; then
      reject_unsafe_root
    fi
  done
  if path_is_same_or_descendant "$home_root" "$data_root"; then
    reject_unsafe_root
  fi
  for protected_root in \
    "/Applications" \
    "/Library" \
    "/System" \
    "/Users/Shared" \
    "/private/etc"
  do
    if path_is_same_or_descendant "$data_root" "$protected_root"; then
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
    if path_is_same_or_descendant "$data_root" "$protected_root"; then
      reject_unsafe_root
    fi
  done
  for protected_root in \
    "$home_root" \
    "$home_root/Library" \
    "$home_root/Library/Application Support"
  do
    if same_path_object "$data_root" "$protected_root"; then
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
