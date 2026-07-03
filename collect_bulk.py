"""過去N日分のデータを一括収集するスクリプト"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date, timedelta
from app import collect_day, DATA_DIR

days = int(sys.argv[1]) if len(sys.argv) > 1 else 14

print(f"=== 過去{days}日分 一括収集 ===")
print(f"保存先: {DATA_DIR}\n")

for i in range(days, 0, -1):
    target = (date.today() - timedelta(days=i)).isoformat()
    log = []
    print(f"📅 {target} 収集中...")
    collect_day(target, log)
    for line in log:
        print(f"  {line}")
    print()

print("=== 完了 ===")
