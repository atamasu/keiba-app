#!/bin/bash
# 既存のCSVデータをRailwayボリュームにアップロードするスクリプト
# 使い方: RAILWAY_URL=https://your-app.railway.app bash migrate_data.sh

RAILWAY_URL="${RAILWAY_URL:-http://localhost:5050}"
DATA_DIR="/Users/hagiharadaiki/Desktop/地方競馬まとめ"

echo "=== 地方競馬データ マイグレーション ==="
echo "送信先: $RAILWAY_URL"
echo ""

for folder in "$DATA_DIR"/*/; do
  date_dir=$(basename "$folder")
  if [[ ! "$date_dir" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    continue
  fi
  echo "📅 $date_dir"
  for csv_file in "$folder"*.csv; do
    [ -f "$csv_file" ] || continue
    venue=$(basename "$csv_file" .csv)
    echo "  📤 $venue ..."
    curl -s -X POST "$RAILWAY_URL/api/upload_csv" \
      -H "Content-Type: application/json" \
      -d "{\"date\":\"$date_dir\",\"venue\":\"$venue\",\"csv\":$(python3 -c "
import json, sys
with open('$csv_file', encoding='utf-8') as f:
    print(json.dumps(f.read()))
")}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('    ✅' if d.get('ok') else '    ❌ ' + str(d))"
  done
done

echo ""
echo "=== 完了 ==="
