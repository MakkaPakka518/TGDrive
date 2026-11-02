#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

REPOSITORY="https://github.com/MakkaPakka518/TGDrive.git"
DRY_RUN=0
for argument in "$@"; do
  case "$argument" in
    --dry-run) DRY_RUN=1 ;;
    --help|-h)
      printf '%s\n' 'TGDrive 交互式后端部署（Debian / Ubuntu + systemd）' \
        '用法：bash install.sh [--dry-run]' \
        '正式安装请以 root 运行；--dry-run 只询问配置并显示计划。'
      exit 0 ;;
    *) printf '未知参数：%s\n' "$argument" >&2; exit 2 ;;
  esac
done

if [[ ! -r /dev/tty ]]; then
  printf '%s\n' '需要交互终端，请通过 SSH 登录服务器后运行。' >&2
  exit 1
fi
if [[ "$DRY_RUN" == 0 ]]; then
  if [[ "$EUID" != 0 ]]; then
    printf '%s\n' '请先执行 sudo -i，再运行部署命令。' >&2
    exit 1
  fi
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) ;;
    *) printf '%s\n' '一键脚本目前支持 Debian / Ubuntu，其他系统请参考部署文档。' >&2; exit 1 ;;
  esac
  [[ -d /run/systemd/system ]] || { printf '%s\n' '需要使用 systemd 的服务器。' >&2; exit 1; }
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv nginx certbot
fi
command -v git >/dev/null || { printf '%s\n' '缺少 git。' >&2; exit 1; }
command -v python3 >/dev/null || { printf '%s\n' '缺少 python3。' >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 8), "需要 Python 3.8 或更新版本"'

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/tgdrive-install.XXXXXXXX")
trap 'rm -rf -- "$STAGE"' EXIT
printf '%s\n' '正在从 TGDrive GitHub 仓库获取 main 分支…'
git clone --depth 1 --branch main "$REPOSITORY" "$STAGE/source"
python3 "$STAGE/source/scripts/install_backend.py" --source "$STAGE/source" "$@" </dev/tty
