# ===== iPhone 18 Pro Max 店舗受け取り監視（Apple 梅田・心斎橋 → LINE通知）GitHub Actions版 =====
# 使い方: python monitor.py once     … 1回だけ確認（5分ごとの実行）
#         python monitor.py morning  … 08:15まで確認し続ける（朝の実行）
#         python monitor.py test     … LINEにテスト通知を送る
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

LINE_TOKEN = os.environ.get("LINE_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

PARTS = {
    "MJX54J/A": "iPhone 18 Pro Max 256GB ブラック",
    "MJX84J/A": "iPhone 18 Pro Max 256GB グレイシャー",
}
STORES = ["梅田", "心斎橋"]
POSTAL_CODE = "530-0011"
# 通知から機種・容量・色・SIMフリーが選ばれた状態のページを直接開く
BUY_BASE = "https://www.apple.com/jp/shop/buy-iphone/iphone-18-pro/"
BUY_URLS = {
    "MJX54J/A": BUY_BASE + "6.9%E3%82%A4%E3%83%B3%E3%83%81%E3%83%87%E3%82%A3%E3%82%B9%E3%83%97%E3%83%AC%E3%82%A4-256gb-%E3%83%96%E3%83%A9%E3%83%83%E3%82%AF-sim%E3%83%95%E3%83%AA%E3%83%BC",
    "MJX84J/A": BUY_BASE + "6.9%E3%82%A4%E3%83%B3%E3%83%81%E3%83%87%E3%82%A3%E3%82%B9%E3%83%97%E3%83%AC%E3%82%A4-256gb-%E3%82%B0%E3%83%AC%E3%82%A4%E3%82%B7%E3%83%A3%E3%83%BC-sim%E3%83%95%E3%83%AA%E3%83%BC",
}

JST = ZoneInfo("Asia/Tokyo")
FAST_START, FAST_END = dtime(5, 55), dtime(8, 15)
FAST_INTERVAL, SLOW_INTERVAL = 30, 300
ERROR_ALERT_COUNT = 10
STATE_FILE = "state.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")


def now_jst():
    return datetime.now(JST)


def log(msg):
    print(f"[{now_jst():%m/%d %H:%M:%S}] {msg}", flush=True)


def send_line(text):
    body = json.dumps({"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(LINE_PUSH_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + LINE_TOKEN.strip(),
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            log(f"LINE送信 {res.status}")
    except urllib.error.HTTPError as e:
        log(f"LINE送信失敗 {e.code} {e.read().decode('utf-8', 'replace')}")
    except Exception as e:
        log(f"LINE送信失敗 {e!r}")


# --- 状態の保存（実行のたびにリセットされるので、リポジトリの state.json に残す） ---
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("notified", {})   # "店舗|型番" → 最後に通知した受け取り日 (YYYY-MM-DD)
    s.setdefault("errors", 0)
    return s


def save_state(state):
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            if f.read() == text:
                return
    except FileNotFoundError:
        pass
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        subprocess.run(["git", "add", STATE_FILE], check=True)
        subprocess.run(["git", "commit", "-m", "update state"], check=True)
        subprocess.run(["git", "push"], check=True)


def pickup_date(quote):
    """「受け取れる日 本日 / 明日 / 9月18日」を日付に変換。読めなければ None"""
    today = now_jst().date()
    if "本日" in quote or "今日" in quote:
        return today
    if "明日" in quote:
        return today + timedelta(days=1)
    m = re.search(r"(\d{1,2})月(\d{1,2})日", quote)
    if not m:
        return None
    d = date(today.year, int(m.group(1)), int(m.group(2)))
    return d if d >= today else date(today.year + 1, d.month, d.day)


def check_once():
    """{(店舗, 型番): (受け取り日 or None, 表示文言)} を返す"""
    params = "&".join(f"parts.{i}={pn}" for i, pn in enumerate(PARTS))
    url = (f"https://www.apple.com/jp/shop/retail/pickup-message?pl=true&{params}"
           f"&location={POSTAL_CODE}&_={int(time.time())}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as res:
        data = json.loads(res.read().decode("utf-8"))

    stores = data.get("body", {}).get("stores")
    if not stores:
        raise RuntimeError("店舗情報が空です（型番か郵便番号を確認）")

    result = {}
    for s in stores:
        name = s.get("storeName", "")
        if name not in STORES:
            continue
        for pn, a in s.get("partsAvailability", {}).items():
            quote = a.get("pickupSearchQuote", "")
            d = pickup_date(quote) if a.get("pickupDisplay") == "available" else None
            result[(name, pn)] = (d, quote)
    if not result:
        raise RuntimeError("梅田・心斎橋の情報が見つかりません")
    return result


def stock_message(head, store, pn, quote):
    return (f"{head}\n{PARTS[pn]}\nApple {store}（{quote}）\n"
            f"▼Apple Storeアプリで購入 → 受け取り方法で「Apple {store}」を選択\n{BUY_URLS[pn]}")


def run_check(state):
    try:
        result = check_once()
    except Exception as e:
        state["errors"] += 1
        log(f"エラー({state['errors']}回連続) {e!r}")
        if state["errors"] == ERROR_ALERT_COUNT:
            send_line(f"⚠️ 在庫チェックが{ERROR_ALERT_COUNT}回連続で失敗しています\n{e!r}")
        save_state(state)
        return

    if state["errors"] >= ERROR_ALERT_COUNT:
        send_line("✅ 在庫チェックが復旧しました")
    state["errors"] = 0

    notified = state["notified"]
    summary = []
    for (store, pn), (d, quote) in sorted(result.items()):
        color = PARTS[pn].split()[-1]
        summary.append(f"{store}/{color}:{d:%m/%d}" if d else f"{store}/{color}:×")
        key = f"{store}|{pn}"
        last = date.fromisoformat(notified[key]) if key in notified else None
        if d and (last is None or d < last):
            head = "🚨 本日受け取りできます！" if d == now_jst().date() else "🔔 店舗受け取りの予約ができます"
            if last:
                head += f"（{last:%m/%d} → {d:%m/%d} に早まりました）"
            send_line(stock_message(head, store, pn, quote))
            notified[key] = d.isoformat()
        elif not d and key in notified:
            del notified[key]   # 受け取り不可になったら、次に出たときまた通知する
    log(" ".join(summary))
    save_state(state)


def interval_now():
    t = now_jst().time()
    return FAST_INTERVAL if FAST_START <= t <= FAST_END else SLOW_INTERVAL


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "test":
        send_line("✅ テスト通知：GitHub Actions から送信しています")
        return
    if mode == "demo":
        send_line("【練習】ここから下は在庫があると仮定した練習用の通知です")
        send_line(stock_message("🔔 店舗受け取りの予約ができます", "梅田", "MJX84J/A", "受け取れる日 9月18日"))
        return

    state = load_state()
    if mode == "morning":
        if now_jst().time() > FAST_END:
            log("08:15を過ぎているので朝の監視はスキップ")
            return
        send_line("🌅 朝の監視を開始しました（〜08:15、30秒ごと）")
        while now_jst().time() <= FAST_END:
            run_check(state)
            time.sleep(interval_now())
        log("08:15になったので朝の監視を終了")
    else:
        run_check(state)


if __name__ == "__main__":
    main()
