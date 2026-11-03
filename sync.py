#!/usr/bin/env python3
"""SatAir match/channel synchronizer.

Sources are intentionally configurable because upstream sites can change or block CI.
All persisted data is JSON so GitHub Pages/other consumers can read it directly.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CAIRO = ZoneInfo("Africa/Cairo")
UA = os.getenv("HTTP_USER_AGENT", "SatAir-GitHub-Sync/1.0 (+https://github.com/)" )
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "ar,en;q=0.8"})

YS_PROXY = os.getenv("YS_PROXY_URL", "https://satairimage.naderelalmy.workers.dev").rstrip("/")
LST_PROXY = os.getenv("LST_PROXY_URL", "https://lst-proxy.naderelalmy.workers.dev").rstrip("/")
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY_SECONDS", "0.8"))
FIREBASE_PROJECT = os.getenv("FIREBASE_PROJECT_ID", "satair-6983b")
PUBLIC_DATA_BASE = os.getenv("PUBLIC_DATA_BASE", "https://satair-6983b.web.app/data").rstrip("/")
CATALOG_FILES = ("channels.json", "satellites.json", "teams.json", "competitions.json")


def now_cairo() -> datetime:
    return datetime.now(CAIRO)


def read_json(name: str, default: Any) -> Any:
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.warning("تعذر قراءة %s: %s", p, exc)
        return default


def write_json(name: str, value: Any) -> None:
    # GitHub Actions أو نسخة المستودع الجديدة قد لا تحتوي مجلد data بعد.
    # أنشئه قبل إنشاء الملف المؤقت حتى لا يفشل التشغيل بـ FileNotFoundError.
    DATA.mkdir(parents=True, exist_ok=True)
    p = DATA / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def ensure_catalog_files() -> None:
    """إنشاء كتالوج البيانات الثابتة تلقائيا في أول تشغيل فقط.

    لا نعيد تنزيل ملف موجود حتى لا نمسح تعديلات المستخدم المحلية.
    عند فشل الشبكة يستمر السكربت، وستظل بيانات المباراة والكاش قابلة للحفظ.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    for name in CATALOG_FILES:
        target = DATA / name
        if target.exists() and target.stat().st_size > 2:
            continue
        url = f"{PUBLIC_DATA_BASE}/{name}"
        try:
            response = SESSION.get(url, timeout=45)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, (list, dict)):
                raise ValueError("صيغة الكتالوج غير صحيحة")
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            logging.info("تم إنشاء data/%s تلقائيا", name)
        except Exception as exc:
            logging.warning("تعذر إنشاء data/%s تلقائيا: %s", name, exc)


def clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def norm(s: Any) -> str:
    s = clean(s).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    return re.sub(r"[^\w\u0600-\u06ff]+", "", s)


def get(url: str, **kwargs) -> requests.Response | None:
    try:
        r = SESSION.get(url, timeout=35, **kwargs)
        r.raise_for_status()
        return r
    except Exception as exc:
        logging.warning("GET فشل %s: %s", url, exc)
        return None


def ymd(dt: datetime) -> str:
    return dt.astimezone(CAIRO).strftime("%Y-%m-%d")


def ysscores_day(date: str) -> str | None:
    dt = datetime.strptime(date, "%Y-%m-%d")
    english = dt.strftime("%B")
    body = {"get_date": f"{english} {dt.day},{dt.year}"}
    try:
        r = SESSION.post(f"{YS_PROXY}/?url={quote('https://www.ysscores.com/ar/match_date_to', safe='')}",
                         data=body, timeout=45)
        r.raise_for_status()
        text = r.text
        if "matches-wrapper" not in text:
            logging.warning("مصدر YSScores لم يعط صفحة مباريات لـ %s", date)
            return None
        return text
    except Exception as exc:
        logging.warning("فشل جلب مباريات %s: %s", date, exc)
        return None


def firestore_value(value: dict[str, Any]) -> Any:
    """تحويل Firestore REST typed values إلى Python عادي."""
    if "stringValue" in value: return value["stringValue"]
    if "integerValue" in value: return int(value["integerValue"])
    if "doubleValue" in value: return value["doubleValue"]
    if "booleanValue" in value: return value["booleanValue"]
    if "nullValue" in value: return None
    if "timestampValue" in value: return value["timestampValue"]
    if "arrayValue" in value:
        return [firestore_value(x) for x in value.get("arrayValue", {}).get("values", [])]
    if "mapValue" in value:
        return {k: firestore_value(v) for k, v in value.get("mapValue", {}).get("fields", {}).items()}
    return value


def firestore_day(date: str) -> list[dict[str, Any]] | None:
    """قراءة matchDays المنشور للعامة في مشروع التطبيق، بلا Firebase SDK أو مفتاح سري."""
    url = (f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
           f"/databases/(default)/documents/matchDays/{date}")
    r = get(url)
    if not r: return None
    try:
        doc = r.json()
        values = doc.get("fields", {}).get("matches", {}).get("arrayValue", {}).get("values", [])
        return [firestore_value(x) for x in values]
    except Exception as exc:
        logging.warning("تعذر تحليل Firebase ليوم %s: %s", date, exc)
        return None


def normalize_firestore_matches(items: list[dict[str, Any]], date: str,
                                catalog_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for raw in items:
        if not raw.get("team1") or not raw.get("team2"): continue
        channel_details = []
        commentators = raw.get("channelCommentators") or {}
        for cid in raw.get("channelIds") or []:
            ch = dict(catalog_by_id.get(cid) or {"id": cid, "name": cid})
            cm = commentators.get(cid) or {}
            ch["commentator"] = clean(cm.get("name") if isinstance(cm, dict) else cm)
            channel_details.append(ch)
        out.append({
            "id": str(raw.get("id") or f"{date}-{norm(raw['team1'])}-{norm(raw['team2'])}"),
            "date": date, "team1": raw.get("team1", ""), "team2": raw.get("team2", ""),
            "logo1": raw.get("logo1", ""), "logo2": raw.get("logo2", ""),
            "time": raw.get("time", "--:--"), "competition": raw.get("competition", "غير محدد"),
            "competitionLogo": raw.get("competitionLogo", "🏆"),
            "round": raw.get("competitionStage", ""), "url": raw.get("lstUrl", ""),
            "ysMatchId": raw.get("ysMatchId", ""), "stadium": raw.get("stadium", ""),
            "status": raw.get("status", "NS"), "score": raw.get("score", {"home": None, "away": None}),
            "channels": [x.get("name", x.get("id")) for x in channel_details],
            "channel_details": channel_details, "channelIds": raw.get("channelIds", []),
            "commentator": raw.get("commentator", ""),
            "lastUpdated": now_cairo().isoformat(),
        })
    return out


def parse_ysscores_day(html: str, date: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for wrap in soup.select(".matches-wrapper"):
        competition = clean(wrap.get("champ_title")) or "غير محدد"
        comp_logo = clean(wrap.get("champ_img"))
        for a in wrap.select("a.ajax-match-item"):
            home, away = clean(a.get("home_name")), clean(a.get("away_name"))
            if not home or not away:
                continue
            md = a.select_one(".match-date")
            mer = clean(md.select_one(".mb-meridiem").get_text(" ", strip=True)) if md and md.select_one(".mb-meridiem") else ""
            hhmm = clean(md.get_text(" ", strip=True)).replace(mer, "").strip() if md else ""
            match_id = clean(a.get("match_id"))
            href = urljoin("https://www.ysscores.com", clean(a.get("href")))
            item = {
                "id": match_id or f"{date}-{norm(home)}-{norm(away)}",
                "date": date, "team1": home, "team2": away,
                "logo1": clean(a.get("home_image")), "logo2": clean(a.get("away_image")),
                "time": f"{hhmm} {mer}".strip() or "--:--",
                "competition": competition, "competitionLogo": comp_logo or "🏆",
                "round": clean(a.get("title")), "url": href,
                "status": "NS", "score": {"home": None, "away": None},
                "channels": [], "channel_details": [], "commentator": "",
                "lastUpdated": now_cairo().isoformat(),
            }
            out.append(item)
    return out


def parse_detail(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    pairs: list[dict[str, str]] = []
    for row in soup.select(".match-info-collapse .collapse-content .match-info-item"):
        t, c = row.select_one(".title"), row.select_one(".content")
        ch = clean(t.select_one("a.channel_info").get_text(" ", strip=True) if t and t.select_one("a.channel_info") else t.get_text(" ", strip=True) if t else "")
        cm = clean(c.select_one("a").get_text(" ", strip=True) if c and c.select_one("a") else c.get_text(" ", strip=True) if c else "")
        if ch and ch != "-":
            pairs.append({"channel": ch, "commentator": "" if cm == "-" else cm})
    unique = []
    seen = set()
    for p in pairs:
        k = norm(p["channel"])
        if k and k not in seen:
            seen.add(k); unique.append(p)
    if not unique:
        titles = soup.select(".title")
        for t in titles:
            if clean(t.get_text()) != "القناة": continue
            box = t.parent.select_one(".content") if t.parent else None
            if box:
                for a in box.select("a.channel_info"):
                    unique.append({"channel": clean(a.get_text()), "commentator": ""})
    start_date = ""
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            val = json.loads(script.string or script.get_text())
            vals = val if isinstance(val, list) else [val]
            for x in vals:
                if isinstance(x, dict) and x.get("startDate"):
                    start_date = x["startDate"]; break
        except Exception:
            pass
    text = clean(soup.get_text(" ", strip=True))
    status = "NS"
    if re.search(r"جارية|مباشر|الشوط الأول|الشوط الثاني|live", text, re.I): status = "LIVE"
    if re.search(r"انتهت|نهاية المباراة|full time", text, re.I): status = "FT"
    score = {"home": None, "away": None}
    for pat in [r"(\d+)\s*[-:–]\s*(\d+)", r"النتيجة\s*(\d+)\s*[-:–]\s*(\d+)"]:
        m = re.search(pat, text)
        if m:
            score = {"home": int(m.group(1)), "away": int(m.group(2))}; break
    return {"channel_details": unique, "commentator": next((x["commentator"] for x in unique if x["commentator"]), ""),
            "startDate": start_date, "status": status, "score": score}


def channel_catalog() -> dict[str, dict[str, Any]]:
    items = read_json("channels.json", [])
    return {norm(x.get("name") or x.get("displayName")): x for x in items if isinstance(x, dict)}


def channel_catalog_by_id() -> dict[str, dict[str, Any]]:
    items = read_json("channels.json", [])
    return {str(x.get("id")): x for x in items if isinstance(x, dict) and x.get("id")}


def enrich_channels(details: list[dict[str, str]], catalog: dict[str, dict[str, Any]], cache: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for d in details:
        name = clean(d.get("channel"))
        key = norm(name)
        known = catalog.get(key) or cache.get(key) or {}
        merged = {"name": name, "commentator": clean(d.get("commentator")), **known}
        merged["name"] = name
        if merged.get("id") is None: merged["id"] = f"ch-{key}"
        result.append(merged)
        cache[key] = {k: v for k, v in merged.items() if k != "commentator"}
    return result


def fetch_detail(match: dict[str, Any]) -> dict[str, Any]:
    url = match.get("url")
    if not url: return {}
    r = get(url)
    if not r: return {}
    time.sleep(REQUEST_DELAY)
    return parse_detail(r.text)


def optional_api_live(matches: list[dict[str, Any]]) -> None:
    if not API_KEY: return
    active_window = now_cairo()
    for m in matches:
        if m.get("status") in {"FT", "AET", "PEN"}: continue
        # API-Football search by date is one request per day; kept optional and bounded.
        break
    # Deliberately do not spend quota here unless configured for a future adapter.


def should_refresh_details(m: dict[str, Any], now: datetime) -> bool:
    # المباراة القادمة التي لديها قنوات كاملة لا تحتاج أي طلب إضافي.
    # المباراة الجارية فقط يعاد فحصها للحصول على النتيجة/الحالة الحية.
    if m.get("status") == "LIVE":
        return True
    if m.get("channels"):
        return False
    if not m.get("url"):
        return False
    try:
        return (now - datetime.fromisoformat(m.get("detailUpdated")).astimezone(CAIRO)).total_seconds() > 86400
    except Exception:
        return True


def sync(days: int = 1) -> tuple[int, int]:
    ensure_catalog_files()
    now = now_cairo()
    matches = read_json("matches.json", [])
    if not isinstance(matches, list): matches = []
    by_id = {str(x.get("id")): x for x in matches if isinstance(x, dict)}
    cache = read_json("channel_cache.json", {})
    catalog = channel_catalog()
    catalog_by_id = channel_catalog_by_id()
    added = updated = 0

    for offset in range(days):
        date = ymd(now + timedelta(days=offset))
        firebase_items = firestore_day(date)
        if firebase_items is not None:
            daily = normalize_firestore_matches(firebase_items, date, catalog_by_id)
        else:
            html = ysscores_day(date)
            if not html: continue
            daily = parse_ysscores_day(html, date)
        logging.info("%s: %d مباراة", date, len(daily))
        for item in daily:
            # تحديث كاش القناة من الكتالوج/المصدر مرة واحدة، مع إبقاء بيانات المباراة خفيفة.
            for channel in item.get("channel_details", []):
                key = norm(channel.get("name") or channel.get("displayName"))
                if key:
                    cache[key] = {k: v for k, v in channel.items() if k != "commentator"}
            old = by_id.get(str(item["id"]))
            if old:
                # Keep cached channels until a detail refresh is successful.
                for k in ("channels", "channel_details", "commentator", "score", "status"):
                    if k in old: item[k] = old[k]
                by_id[str(item["id"])] = {**old, **item}
                updated += 1
            else:
                by_id[str(item["id"])] = item; added += 1

    # Details are fetched only for new/near/live matches; frequencies come from local cache/catalog.
    for m in by_id.values():
        if m.get("date") not in {ymd(now + timedelta(days=i)) for i in range(days)}: continue
        if not should_refresh_details(m, now): continue
        detail = fetch_detail(m)
        if detail:
            if detail.get("channel_details"):
                enriched = enrich_channels(detail["channel_details"], catalog, cache)
                m["channel_details"] = enriched
                m["channels"] = [x.get("name") for x in enriched]
            for k in ("commentator", "status", "score"):
                if detail.get(k) not in (None, "", {"home": None, "away": None}): m[k] = detail[k]
            m["detailUpdated"] = now.isoformat()
            m["lastUpdated"] = now.isoformat()

    optional_api_live(list(by_id.values()))
    ordered = sorted(by_id.values(), key=lambda x: (x.get("date", ""), x.get("time", ""), x.get("team1", "")))
    write_json("matches.json", ordered)
    write_json("channel_cache.json", cache)
    write_json("last_run.json", {"utc": datetime.now(timezone.utc).isoformat(), "cairo": now.isoformat(),
                                  "matches": len(ordered), "added": added, "updated": updated})
    return added, updated


def main() -> int:
    ap = argparse.ArgumentParser(description="مزامنة مباريات SatAir مع كاش القنوات والترددات")
    ap.add_argument("--days", type=int, default=2, help="عدد الأيام التي يتم جلبها بدءا من اليوم")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    try:
        a, u = sync(max(1, min(args.days, 3)))
        logging.info("اكتملت المزامنة: جديد=%d محدث=%d", a, u)
        return 0
    except Exception:
        logging.exception("فشلت المزامنة")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
