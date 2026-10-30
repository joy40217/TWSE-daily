#!/usr/bin/env bash
# 依暫存區（git add 之後）變動的檔名產生 commit 訊息，日期取自檔名，也就是資料本身的日期，不是執行當天
# 用法：bash scripts/commit_message.sh <前綴> <路徑>
#   bash scripts/commit_message.sh data data               -> data: 2026-10-29
#   bash scripts/commit_message.sh report site/data/days   -> report: 2024-01-02 ~ 2024-12-31（242 天）
# 檔名沒有日期時（例如休市日程、月營收），改列出變動的資料集名稱
prefix=$1
path=$2
files=$(git diff --cached --name-only -- "$path")
days=$(grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' <<<"$files" | sort -u)
first=$(head -n1 <<<"$days")
last=$(tail -n1 <<<"$days")
if [ -z "$first" ]; then
  echo "$prefix: 更新 $(sed "s|^$path/||" <<<"$files" | cut -d/ -f1 | sort -u | paste -sd, -)"
elif [ "$first" = "$last" ]; then
  echo "$prefix: $first"
else
  echo "$prefix: $first ~ $last（$(wc -l <<<"$days") 天）"
fi
