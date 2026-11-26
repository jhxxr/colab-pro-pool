#!/usr/bin/env bash
# 在无图形界面的 Linux 服务器上初始化单个 Colab Pro / Pro+ 账号槽位
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "用法: $0 <账号槽位名, 如 pro_01> [套餐等级: pro | pro+ | free, 默认 pro]"
    exit 1
fi

ACCT_NAME="$1"
TIER="${2:-pro}"
DATA_DIR="${COLAB_POOL_DATA_DIR:-/var/lib/colab-pool}"
ACCT_HOME="${COLAB_POOL_ACCOUNTS_DIR:-${DATA_DIR}/accounts}/${ACCT_NAME}"
COLAB_BIN="${COLAB_POOL_CLI_BIN:-colab}"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! "${ACCT_NAME}" =~ ^[a-zA-Z0-9_-]+$ ]] || [[ ! "${TIER}" =~ ^(pro|pro\+|free)$ ]]; then
    echo "账号名仅允许字母、数字、下划线和连字符；套餐为 pro、pro+ 或 free。"
    exit 1
fi

mkdir -p "${ACCT_HOME}/.config/colab-cli"
echo "${TIER}" > "${ACCT_HOME}/tier.txt"

echo "=================================================================="
echo "正在初始化 Colab Pro 账号槽位: ${ACCT_NAME} (等级: ${TIER})"
echo "隔离目录 HOME=${ACCT_HOME}"
echo ""
echo "【重要防坑提示】"
echo "1. 终端即将打印 Google OAuth 授权链接，请复制到你本地电脑浏览器中打开并登录该 Pro 账号。"
echo "2. 在授权同意页面上，【必须勾选全部 4 个权限复选框】！漏勾会导致每小时掉线重登 (Issue #116)。"
echo "3. 授权完成后将授权码粘贴回下方提示处。"
echo "=================================================================="

HOME="${ACCT_HOME}" XDG_CONFIG_HOME="${ACCT_HOME}/.config" \
    COLAB_POOL_ACCOUNT_HOME="${ACCT_HOME}" \
    PYTHON_KEYRING_BACKEND="colab_pool.keyring_backend.AccountKeyring" \
    PYTHONPATH="${PROJECT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${COLAB_BIN}" --auth oauth2 sessions

echo ""
echo "[OK] 账号槽位 '${ACCT_NAME}' 授权完成！服务端将自动发现并纳入空闲调度池。"
