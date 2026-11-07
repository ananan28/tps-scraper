#!/bin/bash
# 在 Mac 上双击本文件即可打包出 .app（若提示无权限，在终端运行: bash 打包Mac.command）
cd "$(dirname "$0")"
python3 build_mac.py
echo ""
echo "完成。按回车关闭本窗口..."
read