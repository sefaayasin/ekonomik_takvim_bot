# requirements: cloudscraper, lxml, pandas, tzdata, requests
import os, re, sys, requests, pandas as pd, datetime as dt
from zoneinfo import ZoneInfo
from lxml import html
import cloudscraper

# =================== AYARLAR ===================
TZ_TR = ZoneInfo("Europe/Istanbul")
IMPORTANCE = (2, 3)                 # yalnız 2★ ve 3★
QUIET_START = 0                     # 00:00
QUIET_END   = 9                     # 09:00 (09 dahil değil)
TELEGRAM_API = "https://api.telegram.org"

# Env ile opsiyonel test/force
DRY_RUN   = (os.environ.get("DRY_RUN","").lower() in {"1","true","yes"})
FORCE_RUN = (os.environ.get("FORCE_RUN","").lower() in {"1","true","yes"})

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# =================== GENEL ===================
def now_tr():
    return dt.datetime.now(TZ_TR)

def in_quiet_hours(t: dt.datetime) -> bool:
    """00:00 <= saat < 09:00 arası sessiz saatler."""
    return QUIET_START <= t.hour < QUIET_END

def fmt_val(x):
    return x if (x and str(x).strip() and str(x).strip() != "\xa0") else "-"

# =================== TELEGRAM ===================
def tg_send(text: str, disable_preview=True, prefix:str=""):
    """DRY_RUN modunda göndermeyip stdout'a basar."""
    msg = (prefix + text) if prefix else text
    if DRY_RUN:
        print("\n--- DRY RUN ---\n" + msg + "\n---------------\n")
        return {"ok": True, "dry_run": True}
    if not (BOT_TOKEN and CHAT_ID):
        raise RuntimeError("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID yok.")
    url = f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": msg,
        "disable_web_page_preview": disable_preview
    }, timeout=30)
    # debug istersen aç:
    # print("TG resp:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()

# =================== INVESTING ÇEKİCİLER ===================
def td_value(tr, td_xpath: str) -> str:
    """TD metni → (data-value | data-real-value | title) fallback"""
    val = tr.xpath(f"normalize-space(string({td_xpath}))")
    if val and val != "\xa0":
        return val
    for attr in ("data-value", "data-real-value", "title"):
        v = tr.xpath(f"{td_xpath}/@{attr}")
        if v and v[0].strip():
            return v[0].strip()
    for attr in ("data-value", "data-real-value", "title"):
        v = tr.xpath(f"{td_xpath}//@{attr}")
        if v and v[0].strip():
            return v[0].strip()
    return ""

def parse_rows_from_html(fragment: str):
    tree = html.fromstring(f"<table><tbody>{fragment}</tbody></table>")
    rows = tree.xpath("//tr[starts-with(@id,'eventRowId_') and contains(@class,'js-event-item')]")
    out = []
    for tr in rows:
        try:
            row_id  = (tr.get("id") or "").replace("eventRowId_","")
            dt_attr = tr.get("data-event-datetime")  # 'YYYY/MM/DD HH:MM:SS' UTC

            time_txt = td_value(tr, "./td[contains(@class,'js-time')]")
            country  = (tr.xpath("./td[contains(@class,'flagCur')]/*/@title") or [""])[0].strip()
            event    = tr.xpath("normalize-space(./td[contains(@class,' event')])")

            imp_key  = (tr.xpath("./td[contains(@class,'sentiment')]/@data-img_key") or [""])[0]
            m = re.search(r"bull(\d+)", imp_key or ""); importance = int(m.group(1)) if m else 0

            actual   = td_value(tr, "./td[contains(@class,' act ') or contains(@class,' act') or contains(@class,'act ')]")
            forecast = td_value(tr, "./td[contains(@class,' fore ') or contains(@class,' fore') or contains(@class,'fore ')]")
            previous = td_value(tr, "./td[contains(@class,' prev ') or contains(@class,' prev') or contains(@class,'prev ')]")

            dt_TR = None
            if dt_attr:
                try:
                    dt_TR = dt.datetime.strptime(dt_attr, "%Y/%m/%d %H:%M:%S") \
                                    .replace(tzinfo=dt.timezone.utc).astimezone(TZ_TR)
                except Exception:
                    pass

            out.append({
                "row_id": row_id,
                "dt_TR": dt_TR,
                "date_TR": (dt_TR.date().isoformat() if dt_TR else None),
                "time_TR": (dt_TR.strftime("%H:%M") if dt_TR else time_txt),
                "country": country,
                "event": event,
                "importance": importance,
                "actual": actual,
                "forecast": forecast,
                "previous": previous
            })
        except:
            continue
    return out

def fetch_day_fragments(day: dt.date, *, importance=(2,3), countries=None, max_pages=60):
    s = cloudscraper.create_scraper(browser={"browser":"chrome","platform":"windows","desktop":True})
    url = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
    headers = {"x-requested-with":"XMLHttpRequest", "referer":"https://www.investing.com/economic-calendar/"}
    countries = countries or []

    frags = []
    dateFrom = day.strftime("%Y-%m-%d")
    dateTo   = (day + dt.timedelta(days=1)).strftime("%Y-%m-%d")

    for offset in range(0, max_pages*50, 50):
        payload = {
            "country[]": countries,
            "importance[]": list(importance),
            "timeZone": 55,                 # Europe/Istanbul
            "timeFilter": "timeRemain",
            "dateFrom": dateFrom,
            "dateTo": dateTo,
            "limit_from": offset
        }
        r = s.post(url, data=payload, headers=headers, timeout=30)
        r.raise_for_status()
        frag = (r.json().get("data") or "").strip()
        if not frag or "js-event-item" not in frag:
            break
        frags.append(frag)
    return frags

def get_today_df(importance=(2,3), countries=None):
    today = now_tr().date()
    frags = fetch_day_fragments(today, importance=importance, countries=countries, max_pages=60)
    rows  = parse_rows_from_html("".join(frags))
    df = pd.DataFrame(rows)
    if not df.empty and "row_id" in df.columns:
        df = df.drop_duplicates("row_id")
    return df.sort_values(["date_TR","time_TR","country"], na_position="last").reset_index(drop=True)

# =================== MESAJLAR ===================
def build_summary_message(df: pd.DataFrame) -> str:
    today = now_tr().strftime("%Y-%m-%d %A")
    lines = [f"📅 Ekonomik Takvim — {today} (TR)", "Önem: 2★ ve 3★", ""]
    if df.empty:
        lines.append("Bugün önemli (2★/3★) olay bulunamadı.")
        return "\n".join(lines)
    for _, r in df.iterrows():
        star = "★★★" if r["importance"] == 3 else "★★"
        line = f"{r['time_TR']} — {r['country']} — {r['event']} ({star})"
        if fmt_val(r['forecast']) != "-": line += f" | Bekl: {r['forecast']}"
        if fmt_val(r['previous']) != "-": line += f" | Önceki: {r['previous']}"
        lines.append(line)
    return "\n".join(lines)

def build_alert_message(r) -> str:
    t = r['dt_TR'].strftime('%H:%M') if pd.notna(r['dt_TR']) else r['time_TR']
    star = "★★★" if r["importance"] == 3 else "★★"
    msg = [
        "⏰ Yaklaşan Etkinlik (30 dk sonra)",
        f"Saat: {t} (TR)",
        f"Ülke: {r['country']}",
        f"Olay: {r['event']}  {star}",
    ]
    if fmt_val(r['forecast']) != "-": msg.append(f"Beklenti: {r['forecast']}")
    if fmt_val(r['previous']) != "-": msg.append(f"Önceki: {r['previous']}")
    return "\n".join(msg)

# =================== ÇALIŞTIRMA MODLARI ===================
def run_daily_summary(prefix=""):
    now = now_tr()
    if in_quiet_hours(now) and not FORCE_RUN:
        print("Sessiz saat: özet gönderilmedi.")
        return
    df = get_today_df(importance=IMPORTANCE)
    tg_send(build_summary_message(df), prefix=prefix)

def run_half_hour_alerts(prefix=""):
    now = now_tr()
    if in_quiet_hours(now) and not FORCE_RUN:
        print("Sessiz saat: uyarılar kapalı.")
        return
    df = get_today_df(importance=IMPORTANCE)
    if df.empty or "dt_TR" not in df.columns:
        return
    win_start = now + dt.timedelta(minutes=30)
    win_end   = win_start + dt.timedelta(minutes=5)  # GH Actions 5 dk'da bir
    upcoming = df[df["dt_TR"].notna() & (df["dt_TR"] >= win_start) & (df["dt_TR"] < win_end)].copy()
    for _, r in upcoming.iterrows():
        tg_send(build_alert_message(r), prefix=prefix)

# ======== TEST MODLARI ========
def run_test_summary():
    # DRY_RUN zorlaması yok; --dry-run kullanırsan yazdırır, yoksa gerçekten gönderir
    run_daily_summary(prefix="[TEST] ")

def run_test_alert():
    # En yakın ileri tarihli bir olayı seçip örnek uyarı gönder
    df = get_today_df(importance=IMPORTANCE)
    now = now_tr()
    cand = df[df["dt_TR"].notna() & (df["dt_TR"] > now)].sort_values("dt_TR").head(1)
    if cand.empty:
        tg_send("[TEST] Uyarı: bugün ileri tarihli bir olay bulunamadı (sadece test).")
        return
    r = cand.iloc[0]
    tg_send(build_alert_message(r), prefix="[TEST] ")

# basit argüman ayrıştırma
def parse_args(argv):
    mode = (argv[1] if len(argv) > 1 else "summary").lower()
    flags = set(a.lower() for a in argv[2:])
    global DRY_RUN, FORCE_RUN
    if "--dry-run" in flags: DRY_RUN = True
    if "--force" in flags:   FORCE_RUN = True
    return mode

if __name__ == "__main__":
    mode = parse_args(sys.argv)
    if mode == "summary":
        run_daily_summary()
    elif mode == "alerts":
        run_half_hour_alerts()
    elif mode in {"test","test-summary"}:
        run_test_summary()
    elif mode in {"test-alert","test-alerts"}:
        run_test_alert()
    else:
        print("Kullanım: python send_calendar_bot.py [summary|alerts|test-summary|test-alerts] [--dry-run] [--force]")
