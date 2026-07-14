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
        if v.endswith(('_result', '_fukusho', '_sanrenpuku')):
            continue
        if venue and v != venue:
            continue
        with open(f, encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                try:
                    row['競馬場'] = v
                    _z2h_d = str.maketrans('０１２３４５６７８９', '0123456789')
                    ninki_str = row.get('人気', '').translate(_z2h_d).strip()
                    pay_str   = re.sub(r'[^\d]', '', row.get('配当円', '').translate(_z2h_d))
                    if not ninki_str.isdigit() or not pay_str:
                        continue
                    row['競馬場'] = v
                    row['人気'] = int(ninki_str)
                    row['配当円'] = int(pay_str)
                    rows.append(row)
                except Exception:
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
    filtered = [s for s in stats if s['人気'] <= 20 and s['出現数'] >= 2]
    return sorted(filtered, key=lambda x: (-x['回収率'], x['人気']))[:8]


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
                    # 全角→半角正規化して着順を取得
                    chakujun_raw = tds[0].get_text(strip=True)
                    chakujun = chakujun_raw.translate(str.maketrans('０１２３４５６７８９', '0123456789')).strip()
                    # 着順が "1" "2" "3" のみ対象
                    m_cj = re.match(r'^(\d+)', chakujun)
                    if not m_cj or m_cj.group(1) not in ("1", "2", "3"):
                        continue
                    pos = m_cj.group(1)
                    # 枠番は1列目、ただし列構成が異なる場合のフォールバック
                    waku_raw = tds[1].get_text(strip=True)
                    waku = waku_raw.translate(str.maketrans('０１２３４５６７８９', '0123456789')).strip()
                    if re.match(r'^\d+$', waku) and 1 <= int(waku) <= 8:
                        waku_list.append((pos, waku))
            if horse_rows:
                break

        fuku_entries = []
        sanrenpuku_pay = ""
        for table in div.find_all("table", class_="refund"):
            in_wide = False
            in_fuku = False
            in_sanrenpuku = False
            for tr in table.find_all("tr"):
                th = tr.find("th")
                tds = tr.find_all("td")
                if th:
                    th_txt = th.get_text(strip=True)
                    if th_txt:  # 空th行ではフラグをリセットしない（続きの行が消える）
                        in_wide = "ワイド" in th_txt
                        in_fuku = "複勝" in th_txt
                        in_sanrenpuku = "三連複" in th_txt

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

                # 複勝（1〜3位馬番＋人気＋配当）
                if in_fuku and len(tds) >= 3:
                    _zen2han = str.maketrans('０１２３４５６７８９', '0123456789')
                    umaban = tds[0].get_text(strip=True).translate(_zen2han).strip()
                    pay_raw = re.sub(r'[^\d]', '', tds[1].get_text())
                    nm = re.search(r'(\d+)', tds[2].get_text())
                    ninki = nm.group(1) if nm else ""
                    if umaban and ninki:
                        fuku_entries.append({"umaban": umaban, "ninki": ninki, "pay": pay_raw})

                # 三連複
                if in_sanrenpuku and len(tds) >= 2:
                    combo = tds[0].get_text(strip=True)
                    pay_raw = re.sub(r'[^\d]', '', tds[1].get_text())
                    if combo and pay_raw:
                        sanrenpuku_pay = pay_raw  # 三連複は1レース1件

        if fuku_entries:
            entry = {"race_no": race_no, "sanrenpuku_pay": sanrenpuku_pay}
            waku_map = {c: w for c, w in waku_list}
            for i, fe in enumerate(fuku_entries[:3], 1):
                entry[f"馬番{i}"] = fe["umaban"]
                entry[f"人気{i}"] = fe["ninki"]
                entry[f"枠{i}"] = waku_map.get(str(i), "")
                entry[f"配当{i}"] = fe["pay"]
            # raceResultの全行数が実際の出走頭数
            entry["頭数"] = total_horses if total_horses > 0 else max(
                (int(fe["umaban"]) for fe in fuku_entries if fe["umaban"].isdigit()), default=0
            )
            entry["fuku_entries"] = fuku_entries[:3]
            entry["waku_map"] = waku_map
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
            # 複勝データ保存 (_fukusho.csv) と 結果データ保存 (_result.csv)
            if result.get("results"):
                # _fukusho.csv: 馬別配当データ
                fuku_path = os.path.join(out_dir, f"{venue_name}_fukusho.csv")
                with open(fuku_path, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["日付", "競馬場", "R", "馬番", "人気", "配当円", "枠"])
                    for r in result["results"]:
                        waku_map = r.get("waku_map", {})
                        for fe in r.get("fuku_entries", []):
                            # 枠はwaku_mapから着順ではなく馬番で引く必要があるが、
                            # waku_mapは着順→枠のマップ。枠情報はentry[f"枠{i}"]から取得
                            pass
                        # entry の枠{i}を使って馬別に書き出す
                        for i in range(1, 4):
                            umaban = r.get(f"馬番{i}", "")
                            ninki = r.get(f"人気{i}", "")
                            pay = r.get(f"配当{i}", "")
                            waku = r.get(f"枠{i}", "")
                            if umaban and ninki:
                                writer.writerow([
                                    target_date, venue_name, r["race_no"],
                                    umaban, ninki, pay, waku,
                                ])

                # _result.csv: 着順結果データ（三連複配当を追加）
                res_path = os.path.join(out_dir, f"{venue_name}_result.csv")
                with open(res_path, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["日付", "競馬場", "R", "馬番1", "人気1", "枠1", "馬番2", "人気2", "枠2", "馬番3", "人気3", "枠3", "頭数", "三連複配当"])
                    for r in result["results"]:
                        writer.writerow([
                            target_date, venue_name, r["race_no"],
                            r.get("馬番1",""), r.get("人気1",""), r.get("枠1",""),
                            r.get("馬番2",""), r.get("人気2",""), r.get("枠2",""),
                            r.get("馬番3",""), r.get("人気3",""), r.get("枠3",""),
                            r.get("頭数",""), r.get("sanrenpuku_pay",""),
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
    today_only = request.args.get("today") == "1"
    meetings = request.args.get("meetings", type=int)
    rows = load_all_data(venue=venue or None, days=days, today_only=today_only)
    if meetings:
        dates = sorted(set(r['日付'] for r in rows), reverse=True)[:meetings]
        rows = [r for r in rows if r['日付'] in set(dates)]
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


@app.route("/api/collect/backfill_sanrenpuku", methods=["POST"])
def api_collect_backfill_sanrenpuku():
    """過去30日間のうち_fukusho.csvがない日を再収集（三連複・複勝データ補完）"""
    targets = []
    for i in range(1, 31):
        d = (date.today() - timedelta(days=i)).isoformat()
        fuku_files = glob.glob(f"{DATA_DIR}/{d}/*_fukusho.csv")
        if not fuku_files:
            targets.append(d)

    if not targets:
        return jsonify({"status": "none", "message": "補完不要（全日付にデータあり）", "targets": []})

    def run_backfill():
        for d in sorted(targets):
            run_collect(d)

    thread = threading.Thread(target=run_backfill, daemon=True)
    thread.start()
    return jsonify({"status": "started", "targets": targets, "count": len(targets)})


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
    race_nums = sorted(set(r['R'] for r in rows), key=lambda x: int(x) if str(x).isdigit() else 0)

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
    target_pops = [int(p) for p in pops_param.split(",") if p.strip().isdigit()] if pops_param else None
    return jsonify(calc_trend(rows, target_pops))


def _safe_int(val, default=0):
    """文字列を安全にintに変換する。空文字・Noneはdefaultを返す"""
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def load_fukusho_data(venue=None, days=None, today_only=False, meetings=None):
    """_fukusho.csv を読み込む"""
    rows = []
    cutoff = None
    today_str = date.today().isoformat()
    if today_only:
        cutoff = today_str
    elif days:
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()

    for f in sorted(glob.glob(f"{DATA_DIR}/**/*_fukusho.csv", recursive=True)):
        parent = os.path.basename(os.path.dirname(f))
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', parent):
            continue
        if today_only and parent != today_str:
            continue
        if cutoff and not today_only and parent < cutoff:
            continue
        fname = os.path.basename(f)
        v = fname.replace("_fukusho.csv", "")
        if venue and v != venue:
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                for row in csv.DictReader(fp):
                    try:
                        row['人気'] = _safe_int(row.get('人気'))
                        row['配当円'] = _safe_int(row.get('配当円'))
                        rows.append(row)
                    except Exception:
                        pass
        except Exception:
            pass

    if meetings:
        dates = sorted(set(r['日付'] for r in rows), reverse=True)[:meetings]
        rows = [r for r in rows if r['日付'] in set(dates)]
    return rows


def calc_fukusho_stats(rows):
    """複勝データから人気別・枠別・馬番別の出現率・回収率を計算"""
    race_keys = set()
    pop_count = defaultdict(int)
    pop_pay_sum = defaultdict(int)
    waku_count = defaultdict(int)
    waku_pay_sum = defaultdict(int)
    umaban_count = defaultdict(int)
    umaban_pay_sum = defaultdict(int)
    for row in rows:
        race_keys.add((row.get('日付', ''), row.get('競馬場', ''), row.get('R', '')))
        pop = row.get('人気', 0)
        if pop:
            pop_count[pop] += 1
            pop_pay_sum[pop] += row.get('配当円', 0)
        waku = _safe_int(row.get('枠', 0))
        if waku:
            waku_count[waku] += 1
            waku_pay_sum[waku] += row.get('配当円', 0)
        umaban = _safe_int(row.get('馬番', 0))
        if umaban:
            umaban_count[umaban] += 1
            umaban_pay_sum[umaban] += row.get('配当円', 0)
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
            "平均配当": round(pop_pay_sum[p] / cnt) if cnt else 0,
            "回収率": round(total_ret / total_bet * 100, 1) if total_bet else 0,
            "total_races": total
        })
    waku_stats = []
    for w in sorted(waku_count):
        cnt = waku_count[w]
        total_ret = waku_pay_sum[w]
        waku_stats.append({
            "枠": w,
            "出現数": cnt,
            "出現率": round(cnt / total * 100, 1) if total else 0,
            "平均配当": round(waku_pay_sum[w] / cnt) if cnt else 0,
            "回収率": round(total_ret / (total * 100) * 100, 1) if total else 0,
        })
    umaban_stats = []
    for u in sorted(umaban_count):
        cnt = umaban_count[u]
        total_ret = umaban_pay_sum[u]
        umaban_stats.append({
            "馬番": u,
            "出現数": cnt,
            "出現率": round(cnt / total * 100, 1) if total else 0,
            "平均配当": round(umaban_pay_sum[u] / cnt) if cnt else 0,
            "回収率": round(total_ret / (total * 100) * 100, 1) if total else 0,
        })
    return {"ninki": stats, "waku": waku_stats, "umaban": umaban_stats, "total": total}


def load_sanrenpuku_data(venue=None, days=None, today_only=False, meetings=None):
    """_result.csv を読み込んで三連複分析用データを返す"""
    rows = []
    cutoff = None
    today_str = date.today().isoformat()
    if today_only:
        cutoff = today_str
    elif days:
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()

    for f in sorted(glob.glob(f"{DATA_DIR}/**/*_result.csv", recursive=True)):
        parent = os.path.basename(os.path.dirname(f))
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', parent):
            continue
        if today_only and parent != today_str:
            continue
        if cutoff and not today_only and parent < cutoff:
            continue
        fname = os.path.basename(f)
        v = fname.replace("_result.csv", "")
        if venue and v != venue:
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                for row in csv.DictReader(fp):
                    try:
                        for i in range(1, 4):
                            row[f'人気{i}'] = _safe_int(row.get(f'人気{i}'))
                            row[f'枠{i}'] = _safe_int(row.get(f'枠{i}'))
                        row['三連複配当'] = _safe_int(row.get('三連複配当'))
                        row['頭数'] = _safe_int(row.get('頭数'))
                        rows.append(row)
                    except Exception:
                        pass
        except Exception:
            pass

    if meetings:
        dates = sorted(set(r['日付'] for r in rows), reverse=True)[:meetings]
        rows = [r for r in rows if r['日付'] in set(dates)]
    return rows


def calc_sanrenpuku_stats(rows):
    """三連複データから人気組み合わせ・枠組み合わせ別集計"""
    total = len(set((r.get('日付', ''), r.get('競馬場', ''), r.get('R', '')) for r in rows))

    combo_count = defaultdict(int)
    combo_pay = defaultdict(int)
    waku_combo_count = defaultdict(int)
    waku_combo_pay = defaultdict(int)

    for r in rows:
        p1, p2, p3 = r.get('人気1'), r.get('人気2'), r.get('人気3')
        if p1 and p2 and p3:
            key = '-'.join(map(str, sorted([p1, p2, p3])))
            combo_count[key] += 1
            combo_pay[key] += r.get('三連複配当', 0)

        w1, w2, w3 = r.get('枠1'), r.get('枠2'), r.get('枠3')
        if w1 and w2 and w3:
            wkey = '-'.join(map(str, sorted([w1, w2, w3])))
            waku_combo_count[wkey] += 1
            waku_combo_pay[wkey] += r.get('三連複配当', 0)

    ninki_combos = sorted(
        [
            {
                "combo": k,
                "count": combo_count[k],
                "rate": round(combo_count[k] / total * 100, 1) if total else 0,
                "avg_pay": round(combo_pay[k] / combo_count[k]) if combo_count[k] else 0,
                "roi": round(combo_pay[k] / (total * 100) * 100, 1) if total else 0,
            }
            for k in combo_count
        ],
        key=lambda x: -x['count']
    )[:20]

    waku_combos = sorted(
        [
            {
                "combo": k,
                "count": waku_combo_count[k],
                "rate": round(waku_combo_count[k] / total * 100, 1) if total else 0,
                "avg_pay": round(waku_combo_pay[k] / waku_combo_count[k]) if waku_combo_count[k] else 0,
                "roi": round(waku_combo_pay[k] / (total * 100) * 100, 1) if total else 0,
            }
            for k in waku_combo_count
        ],
        key=lambda x: -x['count']
    )[:20]

    return {"ninki_combos": ninki_combos, "waku_combos": waku_combos, "total": total}


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


def _calc_venue_patterns(venue_name, days=90):
    """
    過去データから「穴馬(4番人気以降)が来やすい枠・馬番」と
    「人気×枠の特に強い組み合わせ」を集計して返す。
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = []
    for f in sorted(glob.glob(f"{DATA_DIR}/**/{venue_name}_result.csv", recursive=True)):
        parent = os.path.basename(os.path.dirname(f))
        if parent < cutoff:
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                rows.extend(list(csv.DictReader(fp)))
        except Exception:
            pass

    if not rows:
        return None

    total = len(rows)

    # 枠・馬番・人気別の入着カウント（全体）
    waku_cnt   = defaultdict(int)
    umaban_cnt = defaultdict(int)
    ninki_cnt  = defaultdict(int)

    # 穴馬(4番人気以降)の入着カウント
    ana_waku_cnt   = defaultdict(int)
    ana_umaban_cnt = defaultdict(int)
    ana_total = 0

    # 人気×枠の組み合わせカウント
    ninki_waku_cnt = defaultdict(int)
    ninki_waku_total = defaultdict(int)  # その人気帯の出走総数（近似）

    for row in rows:
        for i in range(1, 4):
            wk  = row.get(f"枠{i}", "").strip()
            ub  = row.get(f"馬番{i}", "").strip()
            nk  = row.get(f"人気{i}", "").strip()
            if wk:  waku_cnt[wk]   += 1
            if ub:  umaban_cnt[ub] += 1
            if nk:  ninki_cnt[nk]  += 1

            # 穴馬判定（人気が4以上）
            try:
                nk_int = int(float(nk))
            except (ValueError, TypeError):
                nk_int = 0
            if nk_int >= 4:
                ana_total += 1
                if wk: ana_waku_cnt[wk]   += 1
                if ub: ana_umaban_cnt[ub] += 1

            # 人気×枠の組み合わせ
            if nk and wk:
                ninki_waku_cnt[(nk, wk)] += 1

    # 各人気帯の総入着数
    for (nk, wk), cnt in ninki_waku_cnt.items():
        ninki_waku_total[nk] += cnt

    # 人気×枠の入着率（分子:その組み合わせの入着数 / 分母:その人気帯の入着数）
    combo_rates = []
    for (nk, wk), cnt in ninki_waku_cnt.items():
        denom = ninki_waku_total.get(nk, 0)
        if denom < 5:
            continue
        rate = round(cnt / denom * 100)
        combo_rates.append({"ninki": nk, "waku": wk, "count": cnt, "rate": rate})

    # 特に強い組み合わせ（入着率上位5、ただし4番人気以降に絞る）
    def _safe_int(v):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return 0

    ana_combos = sorted(
        [c for c in combo_rates if _safe_int(c["ninki"]) >= 4],
        key=lambda x: -x["rate"]
    )[:5]

    # 穴馬の枠・馬番ランキング
    if ana_total > 0:
        ana_waku_rank   = sorted(ana_waku_cnt.items(),   key=lambda x: -x[1])[:4]
        ana_umaban_rank = sorted(ana_umaban_cnt.items(), key=lambda x: -x[1])[:5]
    else:
        ana_waku_rank   = []
        ana_umaban_rank = []

    return {
        "total_races": total,
        "ana_waku":   [{"waku": w, "count": c, "rate": round(c / ana_total * 100)} for w, c in ana_waku_rank] if ana_total else [],
        "ana_umaban": [{"umaban": u, "count": c, "rate": round(c / ana_total * 100)} for u, c in ana_umaban_rank] if ana_total else [],
        "hot_combos": ana_combos,
    }


def _calc_today_trend(venue_name):
    """今日の完了レースから枠・馬番・人気の入着傾向を集計して返す"""
    today = date.today().isoformat()
    result_path = os.path.join(DATA_DIR, today, f"{venue_name}_result.csv")
    if not os.path.exists(result_path):
        return None
    try:
        with open(result_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return None
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
        return {
            "completed_races": total,
            "waku": {k: {"count": c, "rate": round(c / total * 100)} for k, c in wk_count.items()},
            "umaban": {k: {"count": c, "rate": round(c / total * 100)} for k, c in um_count.items()},
            "ninki": {k: {"count": c, "rate": round(c / total * 100)} for k, c in nk_count.items()},
            "hot_waku":   sorted(wk_count.keys(), key=lambda x: -wk_count[x])[:3],
            "hot_umaban": sorted(um_count.keys(), key=lambda x: -um_count[x])[:3],
            "hot_ninki":  sorted(nk_count.keys(), key=lambda x: -nk_count[x])[:3],
        }
    except Exception:
        return None


@app.route("/api/today_live")
def api_today_live():
    """今日の進行状況をリアルタイムで返す"""
    today = date.today().isoformat()
    venues_data = []
    for venue_name in sorted(VENUE_CODES.keys()):
        trend = _calc_today_trend(venue_name)
        if not trend:
            continue
        total = trend["completed_races"]
        venues_data.append({
            "venue": venue_name,
            "completed_races": total,
            "hot_umaban": [{"val": k, "count": v["count"], "rate": v["rate"]} for k, v in sorted(trend["umaban"].items(), key=lambda x: -x[1]["count"])[:5]],
            "hot_ninki":  [{"val": k, "count": v["count"], "rate": v["rate"]} for k, v in sorted(trend["ninki"].items(),  key=lambda x: -x[1]["count"])[:5]],
            "hot_waku":   [{"val": k, "count": v["count"], "rate": v["rate"]} for k, v in sorted(trend["waku"].items(),   key=lambda x: -x[1]["count"])[:5]],
        })
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


@app.route("/api/fukusho")
def api_fukusho():
    venue = request.args.get("venue")
    days = request.args.get("days", type=int)
    today_only = request.args.get("today") == "1"
    meetings = request.args.get("meetings", type=int)
    rows = load_fukusho_data(venue=venue, days=days, today_only=today_only)
    if meetings:
        dates = sorted(set(r['日付'] for r in rows), reverse=True)[:meetings]
        rows = [r for r in rows if r['日付'] in set(dates)]
    fk = calc_fukusho_stats(rows)
    rec = recommend(fk["ninki"])
    return jsonify({"stats": fk["ninki"], "waku": fk["waku"], "total": fk["total"], "recommend": rec, "venue": venue or "全競馬場"})


@app.route("/api/sanrenpuku")
def api_sanrenpuku():
    venue = request.args.get("venue")
    days = request.args.get("days", type=int)
    today_only = request.args.get("today") == "1"
    meetings = request.args.get("meetings", type=int)
    rows = load_sanrenpuku_data(venue=venue, days=days, today_only=today_only)
    if meetings:
        dates = sorted(set(r['日付'] for r in rows), reverse=True)[:meetings]
        rows = [r for r in rows if r['日付'] in set(dates)]
    stats = calc_sanrenpuku_stats(rows)
    return jsonify({**stats, "venue": venue or "全競馬場"})


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
            if not os.path.basename(f).endswith(("_result.csv", "_fukusho.csv", "_sanrenpuku.csv"))
        ])
        result_count = sum(1 for f in all_csvs if os.path.basename(f).endswith("_result.csv"))
        result.append({"date": d, "venues": venues, "result_count": result_count})
    return jsonify(result)


# ── オッズ取得・レース予想 ────────────────────────────

def fetch_race_entries(venue_name, race_no):
    """keiba.go.jp の DebaTable から馬番・枠番・馬名・単勝・複勝オッズを取得"""
    horses, _ = fetch_race_entries_debug(venue_name, race_no)
    return horses


def fetch_race_entries_debug(venue_name, race_no):
    """fetch_race_entriesのデバッグ情報付き版。(horses, debug_code) を返す。"""
    code = VENUE_CODES.get(venue_name)
    if not code:
        return [], "no_venue_code"
    today = date.today().isoformat()
    date_fmt = today.replace("-", "%2F")
    url = f"https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/DebaTable?k_raceDate={date_fmt}&k_raceNo={race_no}&k_babaCode={code}"
    try:
        html = fetch_html(url, timeout=10)
    except Exception:
        return [], "fetch_error"
    # テーブルの有無を先に確認
    soup_check = BeautifulSoup(html, "html.parser")
    tbl = soup_check.find("table")
    if not tbl:
        return [], "no_table"
    trs = tbl.find_all("tr")
    if len(trs) <= 1:
        return [], "empty_table"
    horses = parse_deba_table(html)
    if not horses:
        return [], "all_filtered"
    return horses, "ok"


def parse_deba_table(html):
    """
    DebaTable HTML をパース。
    各馬の先頭行から 枠番・馬番・馬名・オッズ を取得。

    注意: 同じ枠に複数馬がいる場合（例: 9頭立てで枠8に馬番8・9）、
    2頭目以降は枠番セルが rowspan で省略される。
    その場合は先頭セルが umaban になるので current_waku を引き継ぐ。
    """
    soup = BeautifulSoup(html, "html.parser")
    _z2h = str.maketrans('０１２３４５６７８９', '0123456789')

    table = soup.find("table")
    if not table:
        return []

    horses = []
    current_waku = ""
    rows = table.find_all("tr")

    seen_umabans = set()

    for tr in rows:
        tds = tr.find_all("td")
        if not tds:
            continue

        # ── 枠番セル検出 ──────────────────────────────────────────
        # 方法A: waku[1-8] クラスを持つtdを探す（最も確実）
        waku_td = None
        for td in tds:
            td_cls = " ".join(td.get("class") or [])
            if re.search(r'waku[1-8]', td_cls):
                waku_td = td
                break

        # 方法B: クラスなし・rowspanあり・内容1-8（フォールバック）
        if not waku_td:
            ft = tds[0]
            ft_text = ft.get_text(strip=True).translate(_z2h)
            if ft.get("rowspan") and re.match(r'^[1-8]$', ft_text):
                waku_td = ft

        if waku_td:
            waku = waku_td.get_text(strip=True).translate(_z2h)
            if not re.match(r'^[1-8]$', waku):
                current_waku = ""   # 無効な枠番行はリセット（次行への引き継ぎ防止）
                continue
            current_waku = waku
            try:
                umaban_idx = tds.index(waku_td) + 1
            except ValueError:
                continue
        elif current_waku:
            # 枠番セルなし → 同枠2頭目以降
            waku = current_waku
            umaban_idx = 0
        else:
            continue

        # ── 馬番取得 ──────────────────────────────────────────────
        if len(tds) <= umaban_idx:
            continue
        umaban_raw = tds[umaban_idx].get_text(strip=True).translate(_z2h)
        if not re.match(r'^\d+$', umaban_raw):
            continue
        umaban_int = int(umaban_raw)
        if not (1 <= umaban_int <= 99):
            continue

        # ── 馬名取得 ──────────────────────────────────────────────
        name = ""
        if len(tds) > umaban_idx + 1:
            name_td = tds[umaban_idx + 1]
            # <br>より前のテキストノードのみ取得
            name = next((s.strip() for s in name_td.strings if s.strip()), "")

        # 馬名バリデーション（数字・記号・空のみは統計行とみなす）
        # 日本語必須は廃止し、2文字以上かつ数字のみでないことを確認
        if not name or len(name) < 2 or re.match(r'^[\d\s\-\.\+\/\\]+$', name):
            continue

        # 同じ馬番の重複登録を防ぐ
        if umaban_raw in seen_umabans:
            continue
        seen_umabans.add(umaban_raw)

        # オッズ列 (class=odds_weight)
        odds_td = tr.find("td", class_="odds_weight")
        tan_odds = None
        fuku_odds = ""
        if odds_td:
            raw = odds_td.get_text(" ", strip=True).translate(_z2h)
            m_tan = re.search(r'(\d+\.\d+|\d{2,})', raw)
            if m_tan:
                try:
                    tan_odds = float(m_tan.group(1))
                except ValueError:
                    pass
            # 複勝: "1.1-1.8" または "1.1 - 1.8"（スペースあり）形式
            m_fuku = re.search(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', raw)
            if m_fuku:
                fuku_odds = f"{m_fuku.group(1)}-{m_fuku.group(2)}"

        horses.append({
            "umaban": umaban_raw,
            "waku": waku,
            "name": name,
            "tan_odds": tan_odds,
            "fuku_odds": fuku_odds,
            "ninki": 0,
        })

    # 単勝オッズが出ていれば人気順を付与
    has_odds = [h for h in horses if h["tan_odds"] is not None]
    if has_odds:
        for i, h in enumerate(sorted(has_odds, key=lambda x: x["tan_odds"]), 1):
            h["ninki"] = i

    return sorted(horses, key=lambda x: int(x["umaban"]) if x["umaban"].isdigit() else 99)


def calc_prerace_score(horses, venue_name, days=90, min_odds=None, today_trend=None):
    """
    各馬にスコアを付ける。
    オッズあり: 枠入着率×0.30 + 人気入着率×0.45 + 馬番入着率×0.25
    オッズなし: 枠入着率×0.45 + 馬番入着率×0.55
    today_trend: 当日完了レースの傾向（枠・馬番ホットリスト）。完了数に応じてスコアに加算。
    min_odds: この値未満の単勝オッズの馬は eligible=False とする
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    # _result.csv から枠別・馬番別入着率
    result_rows = []
    for f in sorted(glob.glob(f"{DATA_DIR}/**/{venue_name}_result.csv", recursive=True)):
        parent = os.path.basename(os.path.dirname(f))
        if parent < cutoff:
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                result_rows.extend(list(csv.DictReader(fp)))
        except Exception:
            pass

    total_races = len(result_rows)
    waku_in = defaultdict(int)
    umaban_in = defaultdict(int)
    for row in result_rows:
        for i in range(1, 4):
            w = row.get(f"枠{i}", "")
            ub = row.get(f"馬番{i}", "")
            if w: waku_in[w] += 1
            if ub: umaban_in[ub] += 1

    waku_rate = {w: cnt / total_races * 100 for w, cnt in waku_in.items()} if total_races else {}
    umaban_rate = {ub: cnt / total_races * 100 for ub, cnt in umaban_in.items()} if total_races else {}

    # _fukusho.csv から人気別入着率（days分だけ読む）
    fuku_rows = load_fukusho_data(venue=venue_name, days=days)
    ninki_in = defaultdict(int)
    fuku_races = len(set((r.get('日付',''), r.get('競馬場',''), r.get('R','')) for r in fuku_rows))
    for row in fuku_rows:
        nk = str(row.get('人気', ''))
        if nk: ninki_in[nk] += 1
    ninki_rate = {nk: cnt / fuku_races * 100 for nk, cnt in ninki_in.items()} if fuku_races else {}

    has_odds = any(h["tan_odds"] is not None for h in horses)

    # 今日の傾向バイアス（完了レースが増えるほど信頼度UP、5レースで最大）
    trend_waku   = today_trend.get("hot_waku",   [])[:3] if today_trend else []
    trend_umaban = today_trend.get("hot_umaban", [])[:3] if today_trend else []
    trend_races  = today_trend.get("completed_races", 0) if today_trend else 0
    reliability  = min(trend_races / 5.0, 1.0)  # 0〜1.0（5R完了で100%）

    for h in horses:
        wr = waku_rate.get(h["waku"], 0)
        ur = umaban_rate.get(h["umaban"], 0)
        nr = ninki_rate.get(str(h["ninki"]), 0) if h["ninki"] else 0

        if has_odds and h["ninki"]:
            score = wr * 0.30 + nr * 0.45 + ur * 0.25
        else:
            score = wr * 0.45 + ur * 0.55

        # 今日バイアス補正（枠・馬番のホットランクに応じて最大+8点、信頼度で按分）
        waku_bonus   = [4, 2, 1][trend_waku.index(h["waku"])]     if h["waku"]   in trend_waku   else 0
        umaban_bonus = [4, 2, 1][trend_umaban.index(h["umaban"])] if h["umaban"] in trend_umaban else 0
        trend_bonus  = round((waku_bonus + umaban_bonus) * reliability, 1)

        h["score"]        = round(score + trend_bonus, 1)
        h["base_score"]   = round(score, 1)
        h["trend_bonus"]  = trend_bonus
        h["waku_rate"]    = round(wr, 1)
        h["ninki_rate"]   = round(nr, 1)
        h["umaban_rate"]  = round(ur, 1)

        # 最低オッズフィルター: tan_oddsがあり閾値未満ならeligible=False
        if min_odds and h["tan_odds"] is not None:
            h["eligible"] = h["tan_odds"] >= min_odds
        else:
            h["eligible"] = True

    # スコアランキングはeligible馬の中だけで付ける
    eligible = [h for h in horses if h["eligible"]]
    for rank, h in enumerate(sorted(eligible, key=lambda x: -x["score"]), 1):
        h["score_rank"] = rank
    for h in horses:
        if not h["eligible"]:
            h["score_rank"] = None

    return sorted(horses, key=lambda x: x.get("umaban_int", int(x["umaban"]) if x["umaban"].isdigit() else 99))


@app.route("/api/race_predict")
def api_race_predict():
    """出走表取得（オッズあれば付与）＋過去データでおすすめ馬を返す"""
    venue = request.args.get("venue", "")
    race_no = request.args.get("race", type=int, default=1)
    days = request.args.get("days", type=int, default=90)
    min_odds = request.args.get("min_odds", type=float, default=None)

    if not venue or venue not in VENUE_CODES:
        return jsonify({"error": "競馬場名が不正です"}), 400

    try:
        horses, parse_debug = fetch_race_entries_debug(venue, race_no)
    except Exception as e:
        import traceback
        return jsonify({"error": f"出走表取得エラー: {e}", "horses": [], "debug": traceback.format_exc()}), 200
    if not horses:
        msg = "出走表を取得できませんでした"
        if parse_debug == "no_table":
            msg = "出走表ページにテーブルがありません（レース終了後または開催なし）"
        elif parse_debug == "fetch_error":
            msg = "keiba.go.jpへの接続に失敗しました（時間をおいて再試行してください）"
        elif parse_debug == "all_filtered":
            msg = "馬情報のパースに失敗しました（運営にお知らせください）"
        elif parse_debug == "empty_table":
            msg = "出走表が空です（レース前または開催なし）"
        return jsonify({"error": msg, "horses": [], "debug": parse_debug}), 200

    try:
        has_odds    = any(h["tan_odds"] is not None for h in horses)
        today_trend = _calc_today_trend(venue)
        scored      = calc_prerace_score(horses, venue, days=days, min_odds=min_odds, today_trend=today_trend)
        patterns    = _calc_venue_patterns(venue, days=days)
    except Exception as e:
        import traceback
        return jsonify({"error": f"スコア計算エラー: {e}", "horses": [], "debug": traceback.format_exc()}), 200

    return jsonify({
        "venue": venue,
        "race": race_no,
        "horses": scored,
        "has_odds": has_odds,
        "data_days": days,
        "today_trend": today_trend,
        "patterns": patterns,
    })


PREDICT_LOG_FILE = os.path.join(DATA_DIR, "predict_log.csv")
PREDICT_LOG_FIELDS = [
    "date", "venue", "race",
    "rank1", "rank2", "rank3",
    "pair1_a", "pair1_b", "pair2_a", "pair2_b", "pair3_a", "pair3_b",
    "ana_pick", "logged_at",
]
_predict_log_lock = threading.Lock()


def _append_predict_log(date_str, venue, race, rank1, rank2, rank3,
                        ana_pick="", pairs=None):
    """予想ログをCSVに追記する。同日同レースは上書き。"""
    rows = []
    if os.path.exists(PREDICT_LOG_FILE):
        try:
            with open(PREDICT_LOG_FILE, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            rows = []
    rows = [r for r in rows if not (r["date"] == date_str and r["venue"] == venue and r["race"] == str(race))]
    pairs = pairs or []
    import datetime as _dt
    row = {
        "date": date_str, "venue": venue, "race": str(race),
        "rank1": rank1, "rank2": rank2, "rank3": rank3,
        "pair1_a": pairs[0][0] if len(pairs) > 0 else "",
        "pair1_b": pairs[0][1] if len(pairs) > 0 else "",
        "pair2_a": pairs[1][0] if len(pairs) > 1 else "",
        "pair2_b": pairs[1][1] if len(pairs) > 1 else "",
        "pair3_a": pairs[2][0] if len(pairs) > 2 else "",
        "pair3_b": pairs[2][1] if len(pairs) > 2 else "",
        "ana_pick": ana_pick,
        "logged_at": _dt.datetime.now().strftime("%H:%M"),
    }
    # 旧フォーマットの行にはペア列がない場合があるのでデフォルト補完
    for old in rows:
        for f in PREDICT_LOG_FIELDS:
            old.setdefault(f, "")
    rows.append(row)
    with open(PREDICT_LOG_FILE, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PREDICT_LOG_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


@app.route("/api/log_prediction", methods=["POST"])
def api_log_prediction():
    """JSから予想ペア生成時に呼ばれ、スコア上位3頭＋推奨ペア3組を記録する。"""
    data = request.get_json(force=True) or {}
    venue    = data.get("venue", "")
    race     = data.get("race", 0)
    top3     = data.get("top3", [])
    ana_pick = data.get("ana_pick", "")
    # pairs: [[a_umaban, b_umaban], ...] 最大3組
    pairs    = data.get("pairs", [])
    if not venue or not race or not top3:
        return jsonify({"ok": False}), 400
    r1 = top3[0] if len(top3) > 0 else ""
    r2 = top3[1] if len(top3) > 1 else ""
    r3 = top3[2] if len(top3) > 2 else ""
    with _predict_log_lock:
        try:
            _append_predict_log(date.today().isoformat(), venue, race, r1, r2, r3,
                                ana_pick=ana_pick, pairs=pairs)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/predict_results")
def api_predict_results():
    """
    予想ログと実際の結果を照合して的中率を返す。
    ワイド的中 = ログのrank1〜3のうち2頭以上が結果の馬番1〜3に含まれる。
    """
    days = request.args.get("days", type=int, default=30)
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    if not os.path.exists(PREDICT_LOG_FILE):
        return jsonify({"records": [], "hit_rate": None, "total": 0, "hit": 0})

    try:
        with open(PREDICT_LOG_FILE, encoding="utf-8") as f:
            logs = [r for r in csv.DictReader(f) if r.get("date", "") >= cutoff]
    except Exception:
        return jsonify({"records": [], "hit_rate": None, "total": 0, "hit": 0})

    records = []
    total = hit = 0
    for log in sorted(logs, key=lambda x: (x["date"], x["venue"], int(x["race"] or 0)), reverse=True):
        d_str   = log["date"]
        venue   = log["venue"]
        race    = log["race"]
        pred    = {log["rank1"], log["rank2"], log["rank3"]} - {""}

        # 結果CSVから当該レースを探す
        result_path = os.path.join(DATA_DIR, d_str, f"{venue}_result.csv")
        result_row  = None
        if os.path.exists(result_path):
            try:
                with open(result_path, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if row.get("R", "") == race:
                            result_row = row
                            break
            except Exception:
                pass

        if result_row:
            actual    = {result_row.get("馬番1",""), result_row.get("馬番2",""), result_row.get("馬番3","")} - {""}
            matched   = pred & actual
            is_hit    = len(matched) >= 2
            ana_pick  = log.get("ana_pick", "")
            ana_hit   = (ana_pick in actual) if ana_pick else None
            total    += 1
            if is_hit: hit += 1

            # ワイドCSV読み込み（配当参照用）
            wide_rows = {}  # "a-b" → {"pay": str, "ninki": str}
            wide_csv = os.path.join(DATA_DIR, d_str, f"{venue}.csv")
            if os.path.exists(wide_csv):
                try:
                    with open(wide_csv, encoding="utf-8") as f:
                        for wr in csv.DictReader(f):
                            if wr.get("R", "") == race:
                                combo = wr.get("組み合わせ", "").strip()
                                # 組み合わせを正規化（小→大の順にソート）
                                nums = sorted(re.findall(r'\d+', combo), key=int)
                                if len(nums) == 2:
                                    wide_rows[f"{nums[0]}-{nums[1]}"] = {
                                        "pay": wr.get("配当円", ""),
                                        "ninki": wr.get("人気", ""),
                                    }
                except Exception:
                    pass

            # ペア別的中判定＋配当取得
            pair_hits = []
            for i in range(1, 4):
                pa = log.get(f"pair{i}_a", "")
                pb = log.get(f"pair{i}_b", "")
                if pa and pb:
                    is_pair_hit = pa in actual and pb in actual
                    wide_pay = wide_rate = ""
                    if is_pair_hit:
                        nums = sorted([pa, pb], key=lambda x: int(x) if str(x).isdigit() else 99)
                        key = f"{nums[0]}-{nums[1]}"
                        wd = wide_rows.get(key, {})
                        if wd.get("pay"):
                            try:
                                pay_int = int(wd["pay"])
                                wide_pay = f"{pay_int:,}円"
                                wide_rate = f"{pay_int / 100:.1f}倍"
                            except (ValueError, TypeError):
                                pass
                    pair_hits.append({
                        "no": i, "a": pa, "b": pb,
                        "hit": is_pair_hit,
                        "wide_pay": wide_pay,
                        "wide_rate": wide_rate,
                    })
            # matchedが2頭以上の場合、一致馬番からワイド配当を逆引き（過去ログ対応）
            matched_wide = []
            if len(matched) >= 2:
                from itertools import combinations
                for a, b in combinations(sorted(matched, key=lambda x: int(x) if str(x).isdigit() else 99), 2):
                    nums = sorted([a, b], key=lambda x: int(x) if str(x).isdigit() else 99)
                    key = f"{nums[0]}-{nums[1]}"
                    wd = wide_rows.get(key, {})
                    if wd.get("pay"):
                        try:
                            pay_int = int(wd["pay"])
                            matched_wide.append({
                                "a": a, "b": b,
                                "wide_pay": f"{pay_int:,}円",
                                "wide_rate": f"{pay_int / 100:.1f}倍",
                            })
                        except (ValueError, TypeError):
                            pass

            records.append({
                "date": d_str, "venue": venue, "race": race,
                "pred": sorted(pred), "actual": sorted(actual),
                "matched": sorted(matched), "hit": is_hit,
                "ana_pick": ana_pick, "ana_hit": ana_hit,
                "pair_hits": pair_hits,
                "matched_wide": matched_wide,
                "ninki1": result_row.get("人気1",""), "ninki2": result_row.get("人気2",""), "ninki3": result_row.get("人気3",""),
            })
        else:
            records.append({
                "date": d_str, "venue": venue, "race": race,
                "pred": sorted(pred), "actual": None, "matched": [], "hit": None,
            })

    hit_rate = round(hit / total * 100) if total > 0 else None
    return jsonify({"records": records, "hit_rate": hit_rate, "total": total, "hit": hit})


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
