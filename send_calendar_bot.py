import os, re, sys, json, requests, pandas as pd, datetime as dt
from zoneinfo import ZoneInfo
from lxml import html
import cloudscraper

# ==== Ayarlar ====
TZ_TR = ZoneInfo("Europe/Istanbul")
IMPORTANCE = (2, 3)           # sadece 2★ ve 3★
TELEGRAM_API = "https://api.telegram.org"

# ==== Telegram yardımcıları ====
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

def tg_send(text: str, disable_preview=True):
    if not (BOT_TOKEN and CHAT_ID):
        raise RuntimeError("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID tanımlı değil.")
    url = f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_preview
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()

# ==== Investing -> HTML çekim ====
def td_value(tr, td_xpath: str) -> str:
    # 1) tüm metin
    val = tr.xpath(f"normalize-space(string({td_xpath}))")
    if val and val != "\xa0":
        return val
    # 2) TD üzerindeki attribute fallback'leri
    for attr in ("data-value", "data-real-value", "title"):
        v = tr.xpath(f"{td_xpath}/@{attr}")
        if v and v[0].strip():
            return v[0].strip()
    # 3) çocuk nodelar
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
            m = re.search(r"bull(\d+)", imp_key or "")
            importance = int(m.group(1)) if m else 0

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
    today = dt.datetime.now(TZ_TR).date()
    frags = fetch_day_fragments(today, importance=importance, countries=countries, max_pages=60)
    rows  = parse_rows_from_html("".join(frags))
    df = pd.DataFrame(rows)
    if not df.empty and "row_id" in df.columns:
        df = df.drop_duplicates("row_id")
    df = df.sort_values(["date_TR","time_TR","country"], na_position="last").reset_index(drop=True)
    return df

# ==== Mesaj biçimleyiciler ====
def fmt_val(x): return x if (x and str(x).strip() and str(x).strip() != "\xa0") else "-"

def build_summary_message(df: pd.DataFrame) -> str:
    today = dt.datetime.now(TZ_TR).strftime("%Y-%m-%d %A")
    lines = [f"📅 *Ekonomik Takvim* — {today} (TR)", "Önem: 2★ ve 3★", ""]
    if df.empty:
        lines.append("Bugün önemli (2★/3★) olay bulunamadı.")
        return "\n".join(lines)
    # 2★ ve 3★
    for _, r in df.iterrows():
        star = "★★★" if r["importance"] == 3 else "★★"
        lines.append(
            f"{r['time_TR']} — {r['country']} — {r['event']}  ({star})"
            + (f" | Bekl: {fmt_val(r['forecast'])}" if fmt_val(r['forecast'])!="-"
               else "")
            + (f" | Önceki: {fmt_val(r['previous'])}" if fmt_val(r['previous'])!="-"
               else "")
        )
    text = "\n".join(lines)
    # Telegram Markdown değil düz metin gönderelim (kaçış derdi olmasın)
    return text

def build_alert_message(r) -> str:
    t = r['dt_TR'].strftime('%H:%M') if pd.notna(r['dt_TR']) else r['time_TR']
    star = "★★★" if r["importance"] == 3 else "★★"
    msg = [
        "⏰ *Yaklaşan Etkinlik* (30 dk sonra)",
        f"Saat: {t} (TR)",
        f"Ülke: {r['country']}",
        f"Olay: {r['event']}  {star}",
    ]
    if fmt_val(r['forecast']) != "-": msg.append(f"Beklenti: {r['forecast']}")
    if fmt_val(r['previous']) != "-": msg.append(f"Önceki: {r['previous']}")
    return "\n".join(msg)

# ==== Çalıştırma Modları ====
def run_daily_summary():
    df = get_today_df(importance=IMPORTANCE)
    msg = build_summary_message(df)
    tg_send(msg)

def run_half_hour_alerts():
    df = get_today_df(importance=IMPORTANCE)
    if df.empty or "dt_TR" not in df.columns:
        return
    now = dt.datetime.now(TZ_TR)
    win_start = now + dt.timedelta(minutes=30)
    win_end   = win_start + dt.timedelta(minutes=5)  # GitHub Actions 5 dk'da bir
    upcoming = df[df["dt_TR"].notna() & (df["dt_TR"] >= win_start) & (df["dt_TR"] < win_end)].copy()
    if upcoming.empty:
        return
    # Aynı 5 dakikalık pencerede birden fazla olabilir; hepsi için gönder
    for _, r in upcoming.iterrows():
        tg_send(build_alert_message(r))

if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "summary").lower()
    if mode == "summary":
        run_daily_summary()
    elif mode == "alerts":
        run_half_hour_alerts()
    else:
        print("Kullanım: python send_calendar_bot.py [summary|alerts]")
