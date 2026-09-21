#!/usr/bin/env bash
# 把 dist/ 同步到 docs/，供 GitHub Pages 用「main 分支 + /docs 目录」发布。
#
# 为什么要复制而不是软链：
#   1. Windows 上建符号链接要管理员权限（本机实测 WinError 5）。
#   2. git 存的是软链本身（一个文本文件），GitHub Pages 不会把它当目录展开。
# 所以老老实实复制。dist/ 是唯一真相，docs/ 是它的产物，别手改 docs/。

set -e
cd "$(dirname "$0")"

rm -rf docs
mkdir -p docs
cp dist/index.html dist/app.css dist/app.js dist/config.js docs/
touch docs/.nojekyll          # 别让 Jekyll 插手，避免下划线开头的文件被忽略

echo "docs/ 已同步："
ls -1 docs/
