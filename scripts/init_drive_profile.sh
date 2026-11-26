#!/usr/bin/env bash
# 生成或导入个人 Google Drive 的 rclone 配置文件，使任意 Pro 账号分配的实例都能无弹窗挂载你的云盘
set -euo pipefail

PROFILE_NAME="${1:-default}"
DATA_DIR="${COLAB_POOL_DATA_DIR:-/var/lib/colab-pool}"
DRIVES_DIR="${COLAB_POOL_DRIVES_DIR:-${DATA_DIR}/drives}"
TARGET_CONF="${DRIVES_DIR}/${PROFILE_NAME}.conf"

mkdir -p "${DRIVES_DIR}"

echo "=================================================================="
echo "配置个人 Google Drive 挂载档案: ${PROFILE_NAME}"
echo "输出文件: ${TARGET_CONF}"
echo ""
echo "操作指引 (在无浏览器 Linux 服务器上):"
echo "  1. 输入 n 新建 remote，名称可填 gdrive"
echo "  2. Storage 类型输入 drive (Google Drive)"
echo "  3. client_id / client_secret 可直接回车（或填你自己的 GCP OAuth ID 以获更高吞吐）"
echo "  4. scope 选 1 (Full access all files)"
echo "  5. Use auto config? 选 n (无界面服务器模式)，在本地电脑运行 rclone authorize \"drive\" 获取 token 粘回"
echo "  6. 完成后输入 q 退出"
echo "=================================================================="

rclone config --config "${TARGET_CONF}"
chmod 600 "${TARGET_CONF}"

echo ""
echo "[OK] 云盘档案 '${PROFILE_NAME}' 已保存至 ${TARGET_CONF}。"
echo "AI 提交任务时只需传入 drive_profile=\"${PROFILE_NAME}\"，无论分配到哪个 Pro 账号，"
echo "都会自动将你的云盘挂载到 /content/drive/MyDrive 且消耗你自己的云盘配额。"
