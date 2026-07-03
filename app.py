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
    "帯広": "3", "盛岡": "10", "水沢": "11", "浦和": "18",
    "船橋": "19", "大井": "20", "川崎": "21", "金沢": "22",
    "笠松": "23", "名古屋": "24", "園田": "27", "高知": "31",
    "佐賀": "32", "門別": "36"
}

BABA_MAP = {"良": "良", "稍重": "稍重", "重": "重", "不良": "不良"}

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

def fetch_html(url, timeout=10):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://www.keiba.go.jp/",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja",
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="replace")


def parse_venue_day(html):
    """keiba.go.jp の RefundMoneyList ページを解析し、全レースのワイドデータを返す"""
    soup = BeautifulSoup(html, "html.parser")

    # 馬場状態・天候をh3から取得
    baba = "不明"
    weather = "不明"
    h3 = soup.find("h3", class_="refund")
    if h3:
        txt = h3.get_text()
        for b in ["不良", "稍重", "重", "良"]:
            if b in txt:
                baba = b
                break
        for w in ["小雨", "雨", "曇", "晴", "雪"]:
            if w in txt:
                weather = w
                break

    races = []    # ワイド払戻データ
    results = []  # 複勝データ（1〜3位馬番＋人気）

    # div.roundWrapper ごとにレースが入っている
    for div in soup.find_all("div", class_="roundWrapper"):
        txt = div.get_text()
        m = re.match(r'\s*(\d+)R', txt)
        if not m:
            continue
        race_no = int(m.group(1))

        # raceResultから枠番・総頭数を取得
        waku_list = []
        total_horses = 0
        for table in div.find_all("table", class_="raceResult"):
            horse_rows = [tr for tr in table.find_all("tr") if tr.find_all("td")]
            total_horses = len(horse_rows)
            for tr in horse_rows:
                tds = tr.find_all("td")
                if len(tds) >= 3:
                    chakujun = tds[0].get_text(strip=True)
                    waku = tds[1].get_text(strip=True)
                    if chakujun in ("1", "2", "3") and re.match(r'^\d+$', waku):
                        waku_list.append((chakujun, waku))
            if horse_rows:
                break

        fuku_entries = []
        for table in div.find_all("table", class_="refund"):
            in_wide = False
            in_fuku = False
            for tr in table.find_all("tr"):
                th = tr.find("th")
                tds = tr.find_all("td")
                if th:
                    th_txt = th.get_text(strip=True)
                    in_wide = "ワイド" in th_txt
                    in_fuku = "複勝" in th_txt

                # ワイド
                if in_wide and len(tds) >= 2:
                    combo = tds[0].get_text(strip=True)
                    pay = re.sub(r'[^\d]', '', tds[1].get_text())
                    ninki = ""
                    if len(tds) >= 3:
                        nm = re.search(r'(\d+)', tds[2].get_text())
                        if nm:
                            ninki = nm.group(1)
                    if combo and pay:
                        races.append({"race_no": race_no, "combo": combo, "ninki": ninki, "pay": pay})

                # 複勝（1〜3位馬番＋人気）
                if in_fuku and len(tds) >= 3:
                    umaban = tds[0].get_text(strip=True)
                    nm = re.search(r'(\d+)', tds[2].get_text())
                    ninki = nm.group(1) if nm else ""
                    if umaban and ninki:
                        fuku_entries.append({"umaban": umaban, "ninki": ninki})

        if fuku_entries:
            entry = {"race_no": race_no}
            for i, fe in enumerate(fuku_entries[:3], 1):
                entry[f"馬番{i}"] = fe["umaban"]
                entry[f"人気{i}"] = fe["ninki"]
                # 枠番を着順から対応付け
                waku_map = {c: w for c, w in waku_list}
                entry[f"枠{i}"] = waku_map.get(str(i), "")
            entry["頭数"] = total_horses
            results.append(entry)

    return {"baba": baba, "weather": weather, "races": races, "results": results}


def collect_day(target_date, log):
    date_fmt = target_date.replace("-", "%2F")  # keiba.go.jp形式 2026%2F07%2F03
    out_dir = os.path.join(DATA_DIR, target_date)
    os.makedirs(out_dir, exist_ok=True)

    for venue_name, code in VENUE_CODES.items():
        url = f"https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/RefundMoneyList?k_raceDate={date_fmt}&k_babaCode={code}"
        try:
            html = fetch_html(url)
            result = parse_venue_day(html)
            if not result["races"]:
                log.append(f"⏭ {venue_name}: 開催なし")
                continue
            baba = result["baba"]
            weather = result["weather"]
            race_rows = [
                [target_date, baba, weather, str(r["race_no"]), r["combo"], r["ninki"], r["pay"]]
                for r in result["races"] if r["ninki"] and r["pay"]
            ]
            if race_rows:
                csv_path = os.path.join(out_dir, f"{venue_name}.csv")
                with open(csv_path, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["日付", "馬場状態", "天候", "R", "組み合わせ", "人気", "配当円"])
                    writer.writerows(race_rows)
                num_races = len(set(r["race_no"] for r in result["races"]))
                log.append(f"✅ {venue_name}: {num_races}R 保存")
            else:
                log.append(f"⏭ {venue_name}: データなし")
            # 複勝データ保存
            if result.get("results"):
                res_path = os.path.join(out_dir, f"{venue_name}_result.csv")
                with open(res_path, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["日付", "競馬場", "R", "馬番1", "人気1", "枠1", "馬番2", "人気2", "枠2", "馬番3", "人気3", "枠3", "頭数"])
                    for r in result["results"]:
                        writer.writerow([
                            target_date, venue_name, r["race_no"],
                            r.get("馬番1",""), r.get("人気1",""), r.get("枠1",""),
                            r.get("馬番2",""), r.get("人気2",""), r.get("枠2",""),
                            r.get("馬番3",""), r.get("人気3",""), r.get("枠3",""),
                            r.get("頭数",""),
                        ])
        except Exception as e:
            log.append(f"⏭ {venue_name}: {e}")


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
    meetings = request.args.get("meetings", type=int)  # 競馬場ごとの直近N開催日
    rows = load_all_data(days=days, today_only=today_only)
    if not rows:
        return jsonify({"error": "データなし"})
    # meetingsモード: 全体は全データの直近N開催日、競馬場別は各場の直近N日
    if meetings:
        all_dates = sorted(set(r['日付'] for r in rows), reverse=True)[:meetings]
        rows = [r for r in rows if r['日付'] in set(all_dates)]

    # 全体おすすめ
    overall_stats = calc_stats(rows)
    overall_rec = recommend(overall_stats)

    # 競馬場別おすすめ（上位3場のみ）
    venues = set(r['競馬場'] for r in rows)
    venue_recs = []
    for v in sorted(venues):
        vrows = [r for r in rows if r['競馬場'] == v]
        if meetings:
            vdates = sorted(set(r['日付'] for r in vrows), reverse=True)[:meetings]
            vrows = [r for r in vrows if r['日付'] in set(vdates)]
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

    total_races = overall_stats[0]['total_races'] if overall_stats else 0
    return jsonify({
        "total_races": total_races,
        "days": days,
        "today_only": today_only,
        "overall": overall_rec,
        "by_venue": venue_recs,
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


def load_venue_results(venues_filter=None, date_filter=None, field_size=None):
    """_result.csvを読み込んで競馬場別に集計する"""
    result = {}
    for csv_path in sorted(glob.glob(f"{DATA_DIR}/**/*_result.csv", recursive=True)):
        parent = os.path.basename(os.path.dirname(csv_path))
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', parent):
            continue
        if date_filter and parent not in date_filter:
            continue
        fname = os.path.basename(csv_path)
        v = fname.replace("_result.csv", "")
        if venues_filter and v not in venues_filter:
            continue
        try:
            with open(csv_path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    # 頭数フィルター
                    try:
                        heads = int(row.get("頭数") or 0)
                    except:
                        heads = 0
                    if field_size and heads > 0:
                        if field_size == "small" and heads > 8:
                            continue
                        elif field_size == "medium" and (heads < 9 or heads > 12):
                            continue
                        elif field_size == "large" and heads < 13:
                            continue
                    if v not in result:
                        result[v] = {
                            "dates": set(),
                            "umaban": defaultdict(int),
                            "ninki": defaultdict(int),
                            "waku": defaultdict(int),
                            "ninki_pairs": defaultdict(int),
                            "umaban_pairs": defaultdict(int),
                            "waku_pairs": defaultdict(int),
                            "field_dist": defaultdict(int),
                            "total": 0,
                        }
                    result[v]["dates"].add(parent)
                    result[v]["total"] += 1
                    if heads > 0:
                        if heads <= 8:
                            result[v]["field_dist"]["small"] += 1
                        elif heads <= 12:
                            result[v]["field_dist"]["medium"] += 1
                        else:
                            result[v]["field_dist"]["large"] += 1
                    nkl = []
                    for i in range(1, 4):
                        ub = row.get(f"馬番{i}", "")
                        nk = row.get(f"人気{i}", "")
                        if ub: result[v]["umaban"][ub] += 1
                        if nk:
                            result[v]["ninki"][nk] += 1
                            try: nkl.append(int(nk))
                            except: pass
                    nkl_s = sorted(set(nkl))
                    for a in range(len(nkl_s)):
                        for b in range(a+1, len(nkl_s)):
                            result[v]["ninki_pairs"][f"{nkl_s[a]}-{nkl_s[b]}"] += 1
                    ubl = []
                    for i in range(1, 4):
                        ub = row.get(f"馬番{i}", "")
                        if ub:
                            try: ubl.append(int(ub))
                            except: pass
                    ubl_s = sorted(set(ubl))
                    for a in range(len(ubl_s)):
                        for b in range(a+1, len(ubl_s)):
                            result[v]["umaban_pairs"][f"{ubl_s[a]}-{ubl_s[b]}"] += 1
                    wkl = []
                    for i in range(1, 4):
                        wkv = row.get(f"枠{i}", "")
                        if wkv:
                            result[v]["waku"][wkv] += 1
                            try: wkl.append(int(wkv))
                            except: pass
                    wkl_s = sorted(set(wkl))
                    for a in range(len(wkl_s)):
                        for b in range(a+1, len(wkl_s)):
                            result[v]["waku_pairs"][f"{wkl_s[a]}-{wkl_s[b]}"] += 1
        except Exception:
            pass
    return result


@app.route("/api/venue_analysis")
def api_venue_analysis():
    """競馬場別 馬番・人気・枠の出現率（複勝データ使用）"""
    days = request.args.get("days", type=int)
    today_only = request.args.get("today") == "1"
    meetings = request.args.get("meetings", type=int)
    field_size = request.args.get("field_size")  # small/medium/large

    today_str = date.today().isoformat()
    if today_only:
        date_filter = {today_str}
    elif days:
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        date_filter = None  # will filter by cutoff below
    else:
        date_filter = None

    # 全データ読み込み（meetings用に一旦全部取る）
    all_data = load_venue_results(field_size=field_size)

    out = []
    for venue, d in sorted(all_data.items()):
        # 日付絞り込み
        if today_only:
            use_dates = {today_str} & d["dates"]
        elif days:
            cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
            use_dates = {dt for dt in d["dates"] if dt >= cutoff}
        elif meetings:
            use_dates = set(sorted(d["dates"], reverse=True)[:meetings])
        else:
            use_dates = d["dates"]

        if not use_dates:
            continue

        # 絞り込み後の集計
        if use_dates == d["dates"]:
            # 全部使う場合はそのまま
            um = d["umaban"]
            nk = d["ninki"]
            wk = d["waku"]
            np_ = d["ninki_pairs"]
            up_ = d["umaban_pairs"]
            wp_ = d["waku_pairs"]
            fd = d["field_dist"]
            total = d["total"]
        else:
            filtered = load_venue_results(venues_filter={venue}, date_filter=use_dates, field_size=field_size)
            if venue not in filtered:
                continue
            fd2 = filtered[venue]
            um = fd2["umaban"]
            nk = fd2["ninki"]
            wk = fd2["waku"]
            np_ = fd2["ninki_pairs"]
            up_ = fd2["umaban_pairs"]
            wp_ = fd2["waku_pairs"]
            fd = fd2["field_dist"]
            total = fd2["total"]

        if total == 0:
            continue

        top_umaban = sorted(um.items(), key=lambda x: -x[1])[:3]
        top_ninki = sorted(nk.items(), key=lambda x: -x[1])[:3]
        top_waku = sorted(wk.items(), key=lambda x: -x[1])[:3]
        top_ninki_pairs = sorted(np_.items(), key=lambda x: -x[1])[:3]
        top_umaban_pairs = sorted(up_.items(), key=lambda x: -x[1])[:3]
        top_waku_pairs = sorted(wp_.items(), key=lambda x: -x[1])[:3]

        high_uma = sorted([k for k, c in um.items() if c / total >= 0.5], key=lambda x: -um[x])[:2]
        high_nk  = sorted([k for k, c in nk.items() if c / total >= 0.5], key=lambda x: -nk[x])[:2]
        high_wak = sorted([k for k, c in wk.items() if c / total >= 0.5], key=lambda x: -wk[x])[:2]
        star = [f"馬番{u}×{n}人気" for u in high_uma for n in high_nk][:3]
        star += [f"{w}枠×{n}人気" for w in high_wak for n in high_nk if f"{w}枠×{n}人気" not in star][:2]

        out.append({
            "venue": venue,
            "total_races": total,
            "field_dist": dict(fd),
            "top_umaban": [{"umaban": k, "count": c, "rate": round(c / total * 100)} for k, c in top_umaban],
            "top_ninki": [{"ninki": k, "count": c, "rate": round(c / total * 100)} for k, c in top_ninki],
            "top_waku": [{"waku": k, "count": c, "rate": round(c / total * 100)} for k, c in top_waku],
            "top_umaban_pairs": [{"pair": k, "count": c, "rate": round(c / total * 100)} for k, c in top_umaban_pairs],
            "top_ninki_pairs": [{"pair": k, "count": c, "rate": round(c / total * 100)} for k, c in top_ninki_pairs],
            "top_waku_pairs": [{"pair": k, "count": c, "rate": round(c / total * 100)} for k, c in top_waku_pairs],
            "star": star,
        })
    return jsonify(out)


@app.route("/api/today_live")
def api_today_live():
    """今日の進行状況をリアルタイムで返す"""
    today = date.today().isoformat()
    venues_data = []
    for venue_name in sorted(VENUE_CODES.keys()):
        result_path = os.path.join(DATA_DIR, today, f"{venue_name}_result.csv")
        if not os.path.exists(result_path):
            continue
        try:
            with open(result_path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if not rows:
                continue
            um_count = defaultdict(int)
            nk_count = defaultdict(int)
            wk_count = defaultdict(int)
            total = len(rows)
            for row in rows:
                for i in range(1, 4):
                    ub = row.get(f"馬番{i}", "")
                    nk = row.get(f"人気{i}", "")
                    wkv = row.get(f"枠{i}", "")
                    if ub: um_count[ub] += 1
                    if nk: nk_count[nk] += 1
                    if wkv: wk_count[wkv] += 1
            venues_data.append({
                "venue": venue_name,
                "completed_races": total,
                "hot_umaban": [{"val": k, "count": c, "rate": round(c/total*100)} for k, c in sorted(um_count.items(), key=lambda x: -x[1])[:3]],
                "hot_ninki": [{"val": k, "count": c, "rate": round(c/total*100)} for k, c in sorted(nk_count.items(), key=lambda x: -x[1])[:3]],
                "hot_waku": [{"val": k, "count": c, "rate": round(c/total*100)} for k, c in sorted(wk_count.items(), key=lambda x: -x[1])[:3]],
            })
        except Exception:
            pass
    return jsonify({"date": today, "venues": venues_data})


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


@app.route("/api/debug/files")
def api_debug_files():
    import os
    result = {
        "DATA_DIR": DATA_DIR,
        "DATA_DIR_exists": os.path.exists(DATA_DIR),
        "folders": [],
        "total_csv": 0,
    }
    try:
        for entry in sorted(os.listdir(DATA_DIR)):
            full = os.path.join(DATA_DIR, entry)
            if os.path.isdir(full):
                csvs = glob.glob(f"{full}/*.csv")
                result["folders"].append({"name": entry, "csvs": len(csvs)})
                result["total_csv"] += len(csvs)
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result)


@app.route("/api/recent")
def api_recent():
    folders = sorted(glob.glob(f"{DATA_DIR}/202*"), reverse=True)[:10]
    result = []
    for folder in folders:
        d = os.path.basename(folder)
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            continue
        all_csvs = glob.glob(f"{folder}/*.csv")
        venues = sorted([
            os.path.splitext(os.path.basename(f))[0]
            for f in all_csvs
            if not os.path.basename(f).endswith("_result.csv")
        ])
        result_count = sum(1 for f in all_csvs if os.path.basename(f).endswith("_result.csv"))
        result.append({"date": d, "venues": venues, "result_count": result_count})
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
