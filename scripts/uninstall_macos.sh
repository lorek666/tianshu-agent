#!/bin/sh
set -eu

prefix="${HOME}/Applications"
data_root="${HOME}/Library/Application Support/AgentOS"
purge_data=0

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
    *)
      echo "未知参数：$1" >&2
      exit 2
      ;;
  esac
done

if [ -d "$prefix" ]; then
  prefix=$(cd "$prefix" && pwd -P)
fi
target="$prefix/Tianshu Agent.app"
if [ -d "$target" ]; then
  rm -rf "$target"
  echo "已删除程序：$target"
else
  echo "程序未安装：$target"
fi

if [ "$purge_data" -eq 1 ]; then
  case "$data_root" in
    ""|"/"|"$HOME")
      echo "拒绝清空不安全的数据根：$data_root" >&2
      exit 2
      ;;
  esac
  if [ -e "$data_root" ]; then
    rm -rf "$data_root"
    echo "已按显式请求删除用户数据：$data_root"
  fi
else
  echo "用户数据保留：$data_root"
fi
