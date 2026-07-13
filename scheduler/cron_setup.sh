#!/bin/bash
# 三品王每日品牌动态日报 — Cron 安装脚本
# 用法: bash scheduler/cron_setup.sh

set -e

PROJECT_DIR="/workspace/sanpin-daily"
PYTHON_BIN=$(which python3.11 || which python3)
JOB_SCRIPT="$PROJECT_DIR/scheduler/daily_job.py"
LOG_DIR="$PROJECT_DIR/data/logs"

# 创建日志目录
mkdir -p "$LOG_DIR"

# Cron 任务：每天 8:00 执行
CRON_JOB="0 8 * * * cd $PROJECT_DIR && $PYTHON_BIN $JOB_SCRIPT >> $LOG_DIR/daily_\$(date +\%Y\%m\%d).log 2>&1"

echo "============================================"
echo "三品王每日品牌动态日报 — Cron 安装"
echo "============================================"
echo ""
echo "项目目录: $PROJECT_DIR"
echo "Python:    $PYTHON_BIN"
echo "任务脚本:  $JOB_SCRIPT"
echo "日志目录:  $LOG_DIR"
echo ""
echo "Cron 配置:"
echo "  $CRON_JOB"
echo ""

# 检查是否已存在相同的cron任务
if crontab -l 2>/dev/null | grep -q "daily_job.py"; then
    echo "⚠️  检测到已有 cron 任务，将替换..."
    # 删除旧任务
    crontab -l 2>/dev/null | grep -v "daily_job.py" | crontab -
fi

# 添加新任务
(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "✅ Cron 任务已安装"
echo ""
echo "当前 crontab:"
crontab -l | grep "daily_job"
echo ""
echo "============================================"
echo "手动测试: cd $PROJECT_DIR && $PYTHON_BIN $JOB_SCRIPT"
echo "查看日志: tail -f $LOG_DIR/daily_\$(date +%Y%m%d).log"
echo "============================================"
