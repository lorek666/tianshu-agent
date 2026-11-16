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

prefix=$(canonicalize_path "$prefix")
data_root=$(canonicalize_path "$data_root")
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
  case "$data_root" in
    ""|"/"|"$HOME"|"/Users"|"/Volumes"|"/private"|"/tmp")
      echo "拒绝清空不安全的数据根：$data_root" >&2
      exit 2
      ;;
  esac
  if [ "$confirm_data_root" != "$data_root" ]; then
    echo "清理用户数据还必须传入完全匹配的 --confirm-data-root：$data_root" >&2
    exit 2
  fi
fi

target="$prefix/Tianshu Agent.app"
if [ -d "$target" ]; then
  rm -rf "$target"
  echo "已删除程序：$target"
else
  echo "程序未安装：$target"
fi

if [ "$purge_data" -eq 1 ]; then
  if [ -e "$data_root" ]; then
    rm -rf "$data_root"
    echo "已按显式请求删除用户数据：$data_root"
  fi
else
  echo "用户数据保留：$data_root"
fi
