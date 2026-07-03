from flask import Flask, render_template, jsonify, request
import csv, glob, os, re, threading
from datetime import date, timedelta
from collections import defaultdict
import urllib.request
from bs4 import BeautifulSoup

app = Flask(__name__)

# クラウド(Railway)では環境変数 DATA_DIR を /data に設定する
# ローカル開発時はデフォルトパスを使用
DATA_DIR = os.environ.get("DATA_DIR", "/Users/hagiharadaiki/Desktop/地方競馬まとめ")

VENUE_CODES = {
    "門別": "30", "盛岡": "35", "水沢": "36", "浦和": "42",
    "船橋": "43", "大井": "44", "川崎": "45", "金沢": "46",
    "笠松": "47", "名古屋": "48", "園田": "50", "姫路": "51",
    "高知": "54", "佐賀": "55"
}

WEATHER_MAP = {1: "晴", 2: "曇", 3: "雨", 4: "小雨", 10: "雪"}
BABA_MAP = {"良": "良", "稍": "稍重", "重": "重", "不": "不良"}

collect_status = {}  # {date: {"status": "running"|"done"|"error", "log": [...]}}


# ── データ読み込み・集計 ──────────────────────────────

def load_all_data(venue=None, days=None, today_only=False):
    rows = []
    cutoff = None
    today_str = date.today().isoformat()
    if today_only:
        cutoff = today_str
    elif days:
        cutoff = (date.today() - timedelta(days=days-1)).isoformat()
    for f in sorted(glob.glob(f"{DATA_DIR}/**/*.csv", recursive=True)):
        parent = os.path.basename(os.path.dirname(f))
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', parent):
            continue
        if today_only and parent != today_str:
            continue
        if cutoff and not today_only and parent < cutoff:
            continue
        v = os.path.splitext(os.path.basename(f))[0]
        if venue and v != venue:
            continue
        with open(f, encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                try:
                    row['競馬場'] = v
                    row['人気'] = int(row['人気'])
                    row['配当円'] = int(row['配当円'])
                    rows.append(row)
                except:
                    pass
    return rows


def calc_stats(rows):
    race_keys = set()
    pop_count = defaultdict(int)
    pop_pay_sum = defaultdict(int)
    for row in rows:
        race_keys.add((row['日付'], row['競馬場'], row['R']))
        pop_count[row['人気']] += 1
        pop_pay_sum[row['人気']] += row['配当円']
    total = len(race_keys)
    stats = []
    for p in sorted(pop_count):
        cnt = pop_count[p]
        total_bet = total * 100
        total_ret = pop_pay_sum[p]
        stats.append({
            "人気": p,
            "出現数": cnt,
            "出現率": round(cnt / total * 100, 1) if total else 0,
            "平均配当": round(pop_pay_sum[p] / cnt),
            "回収率": round(total_ret / total_bet * 100, 1) if total_bet else 0,
            "total_races": total
        })
    return stats


def recommend(stats):
    filtered = [s for s in stats if s['回収率'] >= 100 and s['人気'] <= 20 and s['出現数'] >= 2]
    return sorted(filtered, key=lambda x: (-x['回収率'], x['人気']))[:3]


# ── スクレイピング ────────────────────────────────────

def fetch_html(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="replace")


def parse_race(html):
    soup = BeautifulSoup(html, "html.parser")

    # 天候
    weather_tag = soup.find(class_=re.compile(r'^Icon_Weather'))
    weather = "不明"
    if weather_tag:
        for c in weather_tag.get("class", []):
            m = re.search(r'Weather(\d+)', c)
            if m:
                weather = WEATHER_MAP.get(int(m.group(1)), "不明")

    # 馬場状態
    baba = "不明"
    item04 = soup.find("span", class_="Item04")
    if item04:
        t = item04.get_text()
        m = re.search(r'馬場[:：]\s*(\S)', t)
        if m:
            baba = BABA_MAP.get(m.group(1), m.group(1))

    # ワイド払戻
    wide_rows = []
    for table in soup.find_all("table", class_="Payout_Detail_Table"):
        for tr in table.find_all("tr"):
            th = tr.find("th")
            if not th or "ワイド" not in th.get_text():
                continue
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            horse_spans = [s.get_text(strip=True) for s in tds[0].find_all("span")]
            pays = re.findall(r'[\d,]+(?=円)', tds[1].get_text())
            ninkis = re.findall(r'(\d+)(?=人気)', tds[2].get_text())
            for i in range(0, len(horse_spans) - 1, 2):
                idx = i // 2
                combo = f"{horse_spans[i]}-{horse_spans[i+1]}"
                pay = pays[idx].replace(",", "") if idx < len(pays) else ""
                ninki = ninkis[idx] if idx < len(ninkis) else ""
                wide_rows.append({"combo": combo, "ninki": ninki, "pay": pay})

    return {"weather": weather, "baba": baba, "wide": wide_rows}


def collect_day(target_date, log):
    d = target_date.replace("-", "")
    year, mmdd = d[:4], d[4:]
    out_dir = os.path.join(DATA_DIR, target_date)
    os.makedirs(out_dir, exist_ok=True)

    for venue_name, code in VENUE_CODES.items():
        race_rows = []
        venue_ok = False
        log.append(f"🔍 {venue_name} 確認中...")
        for r in range(1, 13):
            race_id = f"{year}{code}{mmdd}{r:02d}"
            url = f"https://nar.netkeiba.com/race/result.html?race_id={race_id}"
            try:
                html = fetch_html(url)
                result = parse_race(html)
                if not result["wide"]:
                    continue
                venue_ok = True
                baba = result["baba"]
                weather = result["weather"]
                for w in result["wide"]:
                    if w["ninki"] and w["pay"]:
                        race_rows.append([
                            target_date, baba, weather,
                            str(r), w["combo"], w["ninki"], w["pay"]
                        ])
            except Exception as e:
                pass

        if venue_ok and race_rows:
            csv_path = os.path.join(out_dir, f"{venue_name}.csv")
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["日付", "馬場状態", "天候", "R", "組み合わせ", "人気", "配当円"])
                writer.writerows(race_rows)
            log.append(f"✅ {venue_name}: {len(race_rows)//3}R 保存")
        else:
            log.append(f"⏭ {venue_name}: 開催なし")


def run_collect(target_date):
    collect_status[target_date] = {"status": "running", "log": [f"📅 {target_date} 収集開始..."]}
    try:
        collect_day(target_date, collect_status[target_date]["log"])
        collect_status[target_date]["status"] = "done"
        collect_status[target_date]["log"].append("🎉 収集完了！")
    except Exception as e:
        collect_status[target_date]["status"] = "error"
        collect_status[target_date]["log"].append(f"❌ エラー: {e}")


def calc_trend(rows, target_pops=None):
    """日付ごとに指定人気の回収率を集計して返す"""
    from collections import defaultdict
    # 日付×人気 でレース数・配当合計を集計
    date_pop_races = defaultdict(set)
    date_pop_pay = defaultdict(int)
    dates_seen = sorted(set(r['日付'] for r in rows))

    for row in rows:
        key = (row['日付'], row['人気'])
        date_pop_races[(row['日付'], row['人気'])].add((row['日付'], row['競馬場'], row['R']))
        date_pop_pay[(row['日付'], row['人気'])] += row['配当円']

    # 全体での人気別出現数を集計して上位を選ぶ
    if target_pops is None:
        pop_total = defaultdict(int)
        for row in rows:
            pop_total[row['人気']] += 1
        target_pops = sorted(pop_total, key=lambda p: -pop_total[p])[:5]

    result = {}
    for pop in target_pops:
        series = []
        for d in dates_seen:
            races = len(date_pop_races.get((d, pop), set()))
            # その日の総レース数
            all_races = len(set((r['競馬場'], r['R']) for r in rows if r['日付'] == d))
            if all_races == 0:
                continue
            total_bet = all_races * 100
            total_pay = date_pop_pay.get((d, pop), 0)
            roi = round(total_pay / total_bet * 100, 1) if total_bet else 0
            series.append({"date": d, "roi": roi, "races": all_races})
        result[str(pop)] = series
    return {"dates": dates_seen, "series": result, "pops": [str(p) for p in target_pops]}


# ── APIエンドポイント ─────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/venues")
def api_venues():
    rows = load_all_data()
    venues = sorted(set(r['競馬場'] for r in rows))
    return jsonify(venues)


@app.route("/api/summary")
def api_summary():
    days = request.args.get("days", type=int)
    today_only = request.args.get("today") == "1"
    rows = load_all_data(days=days, today_only=today_only)
    if not rows:
        return jsonify({"error": "データなし"})

    # 全体おすすめ
    overall_stats = calc_stats(rows)
    overall_rec = recommend(overall_stats)

    # 競馬場別おすすめ（上位3場のみ）
    venues = set(r['競馬場'] for r in rows)
    venue_recs = []
    for v in sorted(venues):
        vrows = [r for r in rows if r['競馬場'] == v]
        vstats = calc_stats(vrows)
        vrec = recommend(vstats)
        if vrec:
            total = vstats[0]['total_races'] if vstats else 0
            venue_recs.append({
                "venue": v,
                "total_races": total,
                "top": vrec[0]
            })
    venue_recs = sorted(venue_recs, key=lambda x: -x['top']['回収率'])

    # 馬場状態別おすすめ
    baba_order = ['良', '稍重', '重', '不良']
    baba_recs = []
    for b in baba_order:
        brows = [r for r in rows if r.get('馬場状態') == b]
        if not brows:
            continue
        bstats = calc_stats(brows)
        brec = recommend(bstats)
        if brec:
            total = bstats[0]['total_races'] if bstats else 0
            baba_recs.append({
                "baba": b,
                "total_races": total,
                "top": brec[0]
            })

    # 天候別おすすめ
    tenkous = sorted(set(r['天候'] for r in rows if r.get('天候') and r['天候'] != '不明'))
    tenkou_recs = []
    for t in tenkous:
        trows = [r for r in rows if r.get('天候') == t]
        if not trows:
            continue
        tstats = calc_stats(trows)
        trec = recommend(tstats)
        if trec:
            total = tstats[0]['total_races'] if tstats else 0
            tenkou_recs.append({
                "tenkou": t,
                "total_races": total,
                "top": trec[0]
            })

    total_races = overall_stats[0]['total_races'] if overall_stats else 0
    return jsonify({
        "total_races": total_races,
        "days": days,
        "today_only": today_only,
        "overall": overall_rec,
        "by_venue": venue_recs,
        "by_baba": baba_recs,
        "by_tenkou": tenkou_recs
    })


@app.route("/api/filters")
def api_filters():
    rows = load_all_data()
    babas = sorted(set(r['馬場状態'] for r in rows if r.get('馬場状態') and r['馬場状態'] != '不明'))
    tenkous = sorted(set(r['天候'] for r in rows if r.get('天候') and r['天候'] != '不明'))
    baba_order = ['良', '稍重', '重', '不良']
    babas = [b for b in baba_order if b in babas]
    return jsonify({"馬場状態": babas, "天候": tenkous})


@app.route("/api/stats")
def api_stats():
    venue = request.args.get("venue")
    baba = request.args.get("baba")
    tenkou = request.args.get("tenkou")
    days = request.args.get("days", type=int)
    rows = load_all_data(venue=venue or None, days=days)
    if baba:
        rows = [r for r in rows if r.get('馬場状態') == baba]
    if tenkou:
        rows = [r for r in rows if r.get('天候') == tenkou]
    stats = calc_stats(rows)
    label_parts = [venue or "全競馬場"]
    if baba:
        label_parts.append(f"馬場:{baba}")
    if tenkou:
        label_parts.append(f"天候:{tenkou}")
    return jsonify({
        "stats": stats,
        "recommend": recommend(stats),
        "venue": " / ".join(label_parts)
    })


@app.route("/api/collect", methods=["POST"])
def api_collect():
    when = (request.json or {}).get("when", "yesterday")
    if when == "today":
        target = date.today().isoformat()
    else:
        target = (date.today() - timedelta(days=1)).isoformat()

    if collect_status.get(target, {}).get("status") == "running":
        return jsonify({"status": "running", "date": target, "log": collect_status[target]["log"]})

    thread = threading.Thread(target=run_collect, args=(target,), daemon=True)
    thread.start()
    return jsonify({"status": "started", "date": target})


@app.route("/api/collect/status")
def api_collect_status():
    target = request.args.get("date", (date.today() - timedelta(days=1)).isoformat())
    info = collect_status.get(target, {"status": "idle", "log": []})
    return jsonify({"date": target, **info})


@app.route("/api/collect/extend", methods=["POST"])
def api_collect_extend():
    """一番古いデータの1週間前を収集する"""
    folders = sorted(glob.glob(f"{DATA_DIR}/202*"))
    if not folders:
        return jsonify({"status": "error", "message": "既存データなし"})

    oldest = os.path.basename(folders[0])
    oldest_date = date.fromisoformat(oldest)
    targets = [(oldest_date - timedelta(days=i)).isoformat() for i in range(1, 8)]

    def collect_all():
        for d in reversed(targets):
            if collect_status.get(d, {}).get("status") == "running":
                continue
            run_collect(d)

    thread = threading.Thread(target=collect_all, daemon=True)
    thread.start()
    return jsonify({"status": "started", "oldest": oldest, "targets": list(reversed(targets))})


@app.route("/api/collect/missing", methods=["POST"])
def api_collect_missing():
    """過去7日間の未収集日をまとめて収集する"""
    missing = []
    for i in range(1, 8):
        d = (date.today() - timedelta(days=i)).isoformat()
        csv_files = glob.glob(f"{DATA_DIR}/{d}/*.csv")
        if not csv_files:
            missing.append(d)

    if not missing:
        return jsonify({"status": "none", "message": "未収集データなし", "missing": []})

    def collect_all_missing():
        for d in sorted(missing):
            run_collect(d)

    thread = threading.Thread(target=collect_all_missing, daemon=True)
    thread.start()
    return jsonify({"status": "started", "missing": missing})


@app.route("/api/races")
def api_races():
    venue = request.args.get("venue")
    days = request.args.get("days", type=int)
    today_only = request.args.get("today") == "1"
    min_pop = request.args.get("min_pop", type=int)
    max_pop = request.args.get("max_pop", type=int)

    rows = load_all_data(venue=venue or None, days=days, today_only=today_only)
    if min_pop:
        rows = [r for r in rows if r['人気'] >= min_pop]
    if max_pop:
        rows = [r for r in rows if r['人気'] <= max_pop]

    # レース単位でグループ化
    from collections import defaultdict
    race_map = defaultdict(list)
    for r in rows:
        key = (r['日付'], r['競馬場'], r['R'])
        race_map[key].append(r)

    result = []
    for (d, v, rno), rs in sorted(race_map.items(), key=lambda x: x[0], reverse=True):
        for r in sorted(rs, key=lambda x: x['人気']):
            pay = r['配当円']
            result.append({
                "日付": d,
                "競馬場": v,
                "R": rno,
                "組み合わせ": r.get('組み合わせ', ''),
                "人気": r['人気'],
                "倍率": round(pay / 100, 1),
                "配当円": pay,
                "馬場状態": r.get('馬場状態', ''),
                "天候": r.get('天候', ''),
            })

    return jsonify({"races": result, "total": len(result)})


@app.route("/api/race_patterns")
def api_race_patterns():
    venue = request.args.get("venue")
    days = request.args.get("days", type=int)
    recent = request.args.get("recent", type=int)  # 直近N回（開催日数）
    if not venue:
        return jsonify({"error": "venue required"})

    rows = load_all_data(venue=venue, days=days)
    if recent:
        dates = sorted(set(r['日付'] for r in rows), reverse=True)[:recent]
        rows = [r for r in rows if r['日付'] in dates]
    if not rows:
        return jsonify({"patterns": {}, "venue": venue})

    # レース番号ごとに人気別集計
    from collections import defaultdict
    race_nums = sorted(set(r['R'] for r in rows), key=lambda x: int(x))

    patterns = {}
    for rno in race_nums:
        rrows = [r for r in rows if r['R'] == rno]
        # このR番号の総レース数（日付ごとにカウント）
        total_days = len(set(r['日付'] for r in rrows))
        pop_count = defaultdict(int)
        pop_pay = defaultdict(int)
        for r in rrows:
            pop_count[r['人気']] += 1
            pop_pay[r['人気']] += r['配当円']

        stats = []
        for p in sorted(pop_count):
            cnt = pop_count[p]
            total_bet = total_days * 100
            stats.append({
                "人気": p,
                "出現数": cnt,
                "出現率": round(cnt / total_days * 100, 1) if total_days else 0,
                "平均配当": round(pop_pay[p] / cnt),
                "回収率": round(pop_pay[p] / total_bet * 100, 1) if total_bet else 0,
                "total_days": total_days
            })
        stats.sort(key=lambda x: -x['出現率'])
        patterns[rno] = stats

    return jsonify({"patterns": patterns, "venue": venue})


@app.route("/api/trend")
def api_trend():
    days = request.args.get("days", 14, type=int)
    pops_param = request.args.get("pops")
    rows = load_all_data(days=days)
    target_pops = [int(p) for p in pops_param.split(",")] if pops_param else None
    return jsonify(calc_trend(rows, target_pops))


@app.route("/api/upload_csv", methods=["POST"])
def api_upload_csv():
    """既存CSVデータをマイグレーション用に受け取る"""
    data = request.json or {}
    d = data.get("date", "")
    venue = data.get("venue", "")
    csv_content = data.get("csv", "")
    if not (d and venue and csv_content):
        return jsonify({"ok": False, "error": "missing fields"})
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
        return jsonify({"ok": False, "error": "invalid date"})
    out_dir = os.path.join(DATA_DIR, d)
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"{venue}.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(csv_content)
    return jsonify({"ok": True})


@app.route("/api/recent")
def api_recent():
    folders = sorted(glob.glob(f"{DATA_DIR}/202*"), reverse=True)[:10]
    result = []
    for folder in folders:
        d = os.path.basename(folder)
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            continue
        venues = [os.path.splitext(os.path.basename(f))[0]
                  for f in glob.glob(f"{folder}/*.csv")]
        result.append({"date": d, "venues": sorted(venues)})
    return jsonify(result)


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    jst = pytz.timezone("Asia/Tokyo")
    scheduler = BackgroundScheduler(timezone=jst)

    def nightly_collect():
        target = (date.today() - timedelta(days=1)).isoformat()
        if collect_status.get(target, {}).get("status") == "running":
            return
        thread = threading.Thread(target=run_collect, args=(target,), daemon=True)
        thread.start()

    # 毎日JST 0:30に前日分を自動収集
    scheduler.add_job(nightly_collect, CronTrigger(hour=0, minute=30, timezone=jst))

    # 本日のデータをレース時間帯（10〜23時、毎時0分）に自動収集
    def today_collect():
        target = date.today().isoformat()
        if collect_status.get(target, {}).get("status") == "running":
            return
        thread = threading.Thread(target=run_collect, args=(target,), daemon=True)
        thread.start()

    scheduler.add_job(today_collect, CronTrigger(hour='10-23', minute=30, timezone=jst))

    scheduler.start()


if __name__ == "__main__":
    app.run(debug=False, port=5050)
else:
    # gunicorn起動時（Railway本番）にスケジューラを開始
    start_scheduler()
