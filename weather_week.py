"""The 7-day weather window on the job dashboard (accepted 2026-10-09).

One Open-Meteo forecast call (free, no key — CLAUDE.md secrets rule exception for public
APIs) for the job site: per day high/low, conditions, rain chance + amount, wind + gusts,
humidity, sunrise/sunset, and 6 readings through the day (7 AM … 10 PM, every 3 hours).

`build_week(data)` is pure (no network) so the smoke can feed it a canned payload.
Weekdays come from the America/New_York date STRINGS, never a UTC timestamp (CLAUDE.md
dates rule). Cached ~30 min per site; on a provider failure the last good week is served
marked stale, same posture as the Field today weather.
"""
import json
import urllib.parse
import urllib.request
from datetime import date, datetime

HOURS = (7, 10, 13, 16, 19, 22)           # the readings shown through the day
WORK_DAY = (7, 15.5)                      # 07:00–15:30, shaded in the hour row
GUST_HEADS_UP_MPH = 25
RAIN_HEADS_UP_PCT = 50
_TTL = 1800
_CACHE = {}
_WEEKDAY = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"]

DAILY_VARS = ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
              "precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,"
              "sunrise,sunset")
HOURLY_VARS = "temperature_2m,precipitation_probability,wind_speed_10m,weather_code,relative_humidity_2m"


def _at(seq, i):
    return seq[i] if seq is not None and i < len(seq) else None


def _compass(deg):
    return None if deg is None else _COMPASS[int((float(deg) % 360 + 22.5) // 45)]


def _clock(iso):
    """'2026-10-09T07:06' -> '7:06 AM' (the string is already local time)."""
    try:
        t = datetime.strptime(iso, '%Y-%m-%dT%H:%M')
    except (TypeError, ValueError):
        return None
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def build_week(data, labels=None, today=None):
    labels = labels or {}
    today = (today or date.today()).isoformat()
    d = data.get("daily") or {}
    h = data.get("hourly") or {}
    htime = h.get("time") or []
    idx = {t: i for i, t in enumerate(htime)}
    days = []
    for i, ds in enumerate(d.get("time") or []):
        try:
            wd = _WEEKDAY[datetime.strptime(ds, '%Y-%m-%d').weekday()]
        except ValueError:
            continue
        hours, hums = [], []
        for hr in range(24):
            j = idx.get(f"{ds}T{hr:02d}:00")
            if j is None:
                continue
            hum = _at(h.get("relative_humidity_2m"), j)
            if hum is not None:
                hums.append(hum)
            if hr in HOURS:
                code = _at(h.get("weather_code"), j)
                hours.append({"hour": hr, "code": code, "temp": _at(h.get("temperature_2m"), j),
                              "precip_prob": _at(h.get("precipitation_probability"), j),
                              "wind_mph": _at(h.get("wind_speed_10m"), j),
                              "work": WORK_DAY[0] <= hr <= WORK_DAY[1]})
        code = _at(d.get("weather_code"), i)
        gust = _at(d.get("wind_gusts_10m_max"), i)
        rain = _at(d.get("precipitation_probability_max"), i)
        work_rain = max([x["precip_prob"] or 0 for x in hours if x["work"]] or [0])
        heads = []
        if gust is not None and gust >= GUST_HEADS_UP_MPH:
            heads.append(f"Gusts up to {round(gust)} mph, check rigging and loose material")
        if work_rain >= RAIN_HEADS_UP_PCT:
            heads.append(f"{round(work_rain)}% chance of rain during the work day")
        days.append({
            "date": ds, "weekday": wd, "is_today": ds == today,
            "code": code, "label": labels.get(code, "—"),
            "temp_max": _at(d.get("temperature_2m_max"), i), "temp_min": _at(d.get("temperature_2m_min"), i),
            "precip_prob": rain, "precip_in": _at(d.get("precipitation_sum"), i),
            "wind_mph": _at(d.get("wind_speed_10m_max"), i), "gust_mph": gust,
            "wind_dir": _compass(_at(d.get("wind_direction_10m_dominant"), i)),
            "humidity": round(sum(hums) / len(hums)) if hums else None,
            "sunrise": _clock(_at(d.get("sunrise"), i)), "sunset": _clock(_at(d.get("sunset"), i)),
            "hours": hours, "heads_up": heads,
        })
    return {"days": days, "work_day": "7:00 – 3:30"}


def fetch_week(lat, lng, labels=None):
    key = (round(float(lat), 4), round(float(lng), 4), date.today().isoformat())
    now = datetime.utcnow()
    hit = _CACHE.get(key)
    if hit and (now - hit["ts"]).total_seconds() < _TTL:
        return hit["data"]
    params = {"latitude": lat, "longitude": lng, "daily": DAILY_VARS, "hourly": HOURLY_VARS,
              "forecast_days": 7, "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
              "precipitation_unit": "inch", "timezone": "America/New_York"}
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        if hit:
            return dict(hit["data"], stale=True)
        raise
    out = build_week(data, labels)
    out["fetched_at"] = now.isoformat() + "Z"
    _CACHE[key] = {"data": out, "ts": now}
    return out
