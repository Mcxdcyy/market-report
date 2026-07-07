#!/bin/bash
# 推送 GitHub Pages（docs/ 文件夹发布，无需 workflow 权限）
# 仓库: https://github.com/Mcxdcyy/market-report
# 访问: https://mcxdcyy.github.io/market-report/

set -e
cd "/Users/machengxiang/Desktop/增长2026"

echo "-> 生成最新报表并同步 docs/ ..."
python3 generate_report.py

if [ -d .git ] && [ ! -f .git/HEAD ]; then
  echo "-> 清理损坏的 .git ..."
  rm -rf .git
fi

if [ ! -d .git ]; then
  echo "-> 初始化 Git ..."
  git init -b main
  git remote add origin https://github.com/Mcxdcyy/market-report.git
else
  git remote set-url origin https://github.com/Mcxdcyy/market-report.git 2>/dev/null || \
    git remote add origin https://github.com/Mcxdcyy/market-report.git
fi

git add .gitignore generate_report.py market_news.json event_catalog.json serve_mobile.py docs .cursor/rules/ push_github.sh
git status --short

if git diff --cached --quiet; then
  echo "无变更，跳过 commit"
else
  git commit -m "update report $(date +%Y-%m-%d)"
fi

echo ""
echo "-> 推送到 GitHub ..."
git push -u origin main

echo ""
echo "完成。首次请在 GitHub 设置 Pages:"
echo "  Settings -> Pages -> Deploy from a branch"
echo "  Branch: main  Folder: /docs"
echo "访问: https://mcxdcyy.github.io/market-report/"
