#!/usr/bin/env python3
"""
Server Health Monitor
تنبيهات فورية + تقرير صباحي يومي + أوامر تفاعلية
"""
import os, time, json, threading, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

# ── Event log (in-memory، يُمسح بعد كل تقرير يومي) ──────────────────────────
_event_log: list = []
_log_lock        = threading.Lock()

# ── Resource samples (لحساب متوسط اليوم) ─────────────────────────────────────
_samples: list = []   # [{"cpu": float, "ram": float}]
_smp_lock = threading.Lock()

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID     = os.environ["TELEGRAM_CHAT_ID"]
CHECK_EVERY = int(os.environ.get("CHECK_INTERVAL", "60"))
REPORT_HOUR = int(os.environ.get("REPORT_HOUR_UTC", "5"))

CPU_WARN  = float(os.environ.get("CPU_WARN",  "80"))
RAM_WARN  = float(os.environ.get("RAM_WARN",  "85"))
DISK_WARN = float(os.environ.get("DISK_WARN", "70"))   # تحذير مبكر
DISK_CRIT = float(os.environ.get("DISK_CRIT", "85"))   # حرج

ENDPOINTS = [
    ("n8n",    "https://n8n.junkpro.duckdns.org/healthz"),
    ("uptime", "https://uptime.junkpro.duckdns.org/"),
]

# ── Telegram ──────────────────────────────────────────────────────────────────
_rate_times: list = []
_rate_lock  = threading.Lock()
MAX_PER_MIN  = 4
MAX_PER_HOUR = 25

def tg(method: str, payload: dict, timeout: int = 15) -> dict:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data,
                                   headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        print(f"[tg error] {method}: {e}")
        return {}

def send(text: str, chat_id: str = CHAT_ID):
    with _rate_lock:
        now = time.time()
        _rate_times[:] = [t for t in _rate_times if now - t < 3600]
        per_min  = sum(1 for t in _rate_times if now - t < 60)
        per_hour = len(_rate_times)
        if per_min >= MAX_PER_MIN or per_hour >= MAX_PER_HOUR:
            print(f"[rate limit] dropped: {text[:60]}")
            return
        _rate_times.append(now)
    tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

# ── Metrics ───────────────────────────────────────────────────────────────────
def cpu_percent() -> float:
    def read_stat():
        with open("/proc/stat") as f:
            p = f.readline().split()
        idle = int(p[4]); total = sum(int(x) for x in p[1:])
        return idle, total
    i1, t1 = read_stat(); time.sleep(0.5); i2, t2 = read_stat()
    dt = t2 - t1
    return round((1 - (i2 - i1) / dt) * 100, 1) if dt else 0.0

def ram_info() -> tuple[float, str, str]:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.split()[0])
    total = info["MemTotal"]; free = info.get("MemAvailable", info["MemFree"])
    used  = total - free
    return round(used / total * 100, 1), _hum(used), _hum(total)

def disk_info(path="/") -> tuple[float, str, str]:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free  = st.f_bavail * st.f_frsize
    used  = total - free
    return round(used / total * 100, 1), _hum(used), _hum(total)

def uptime_str() -> str:
    with open("/proc/uptime") as f:
        secs = float(f.read().split()[0])
    d, r = divmod(int(secs), 86400)
    h, r = divmod(r, 3600)
    m    = r // 60
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)

def _hum(kb: int) -> str:
    b = kb * 1024
    for unit in ("B","KB","MB","GB"):
        if b < 1024: return f"{b:.0f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"

# ── Docker ────────────────────────────────────────────────────────────────────
def docker_containers() -> list[dict]:
    import http.client, socket as _socket
    class UnixConn(http.client.HTTPConnection):
        def connect(self):
            self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            self.sock.connect("/var/run/docker.sock")
    try:
        conn = UnixConn("localhost")
        conn.request("GET", "/containers/json?all=false")
        data = json.loads(conn.getresponse().read())
        result = []
        for c in data:
            name   = c["Names"][0].lstrip("/")
            status = c["Status"]
            health = ("unhealthy" if "unhealthy" in status
                      else "starting" if "starting" in status
                      else "healthy")
            result.append({"name": name, "status": status, "health": health})
        return result
    except Exception as e:
        print(f"[docker error] {e}"); return []

# ── HTTP ──────────────────────────────────────────────────────────────────────
def check_endpoint(url: str, timeout=8) -> tuple[bool, int]:
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        req  = urllib.request.Request(url, headers={"User-Agent": "HealthBot/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        return True, resp.status
    except urllib.error.HTTPError as e:
        return e.code < 500, e.code
    except Exception:
        return False, 0

# ── Helpers ───────────────────────────────────────────────────────────────────
def ksa_time() -> str:
    return datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")

def log_event(icon: str, msg: str):
    with _log_lock:
        _event_log.append({"t": ksa_time(), "icon": icon, "msg": msg})

def pop_event_log() -> list:
    with _log_lock:
        events = list(_event_log)
        _event_log.clear()
    return events

def resource_icon(val, warn): return "🟡" if val > warn else "🟢"
def health_icon(h): return "🟢" if h == "healthy" else ("🟡" if h == "starting" else "🔴")

# ── Status snapshot ───────────────────────────────────────────────────────────
def build_status() -> str:
    cpu          = cpu_percent()
    rp, ru, rt   = ram_info()
    dp, du, dt   = disk_info()
    containers   = docker_containers()
    up           = uptime_str()

    lines = [f"<b>📊 حالة السيرفر</b>",
             f"🕐 {ksa_time()} (KSA)\n"]

    lines.append(f"<b>الموارد:</b>")
    lines.append(f"{resource_icon(cpu, CPU_WARN)} CPU:   {cpu}%")
    lines.append(f"{resource_icon(rp,  RAM_WARN)} RAM:   {rp}% ({ru} / {rt})")
    lines.append(f"{resource_icon(dp, DISK_WARN)} Disk:  {dp}% ({du} / {dt})")
    lines.append(f"⏱ Uptime: {up}")

    lines.append(f"\n<b>الـ Containers ({len(containers)}):</b>")
    for c in containers:
        lines.append(f"{health_icon(c['health'])} {c['name']}")

    lines.append(f"\n<b>الـ Endpoints:</b>")
    for name, url in ENDPOINTS:
        ok, code = check_endpoint(url)
        lines.append(f"{'🟢' if ok else '🔴'} {name}  ({code or 'timeout'})")

    return "\n".join(lines)

# ── Commands ──────────────────────────────────────────────────────────────────
COMMANDS = {
    "/status": "الوضع الحالي للسيرفر",
    "/update": "تحديث Docker images الآن",
    "/help":   "قائمة الأوامر",
}

def run_update(chat_id: str):
    import subprocess
    send("⏳ جاري تحديث المكوّنات...", chat_id)
    try:
        r = subprocess.run(
            ["bash", "/opt/scripts/daily-update.sh", "--docker-only"],
            timeout=300, capture_output=True, text=True
        )
        if r.returncode != 0 and r.stderr:
            send(f"⚠️ خطأ أثناء التحديث:\n<code>{r.stderr[:400]}</code>", chat_id)
    except subprocess.TimeoutExpired:
        send("⚠️ انتهى الوقت (5 دقائق) — قد يكون التحديث مستمراً", chat_id)
    except Exception as e:
        send(f"⚠️ {e}", chat_id)

def handle_command(cmd: str, chat_id: str):
    cmd = cmd.split("@")[0].strip().lower()
    if cmd == "/status":
        send("⏳ جاري الفحص...", chat_id)
        send(build_status(), chat_id)
    elif cmd == "/update":
        threading.Thread(target=run_update, args=(chat_id,), daemon=True).start()
    elif cmd == "/help":
        lines = ["<b>الأوامر المتاحة:</b>"]
        for c, desc in COMMANDS.items():
            lines.append(f"<code>{c}</code> — {desc}")
        send("\n".join(lines), chat_id)
    else:
        send(f"أمر غير معروف: <code>{cmd}</code>\nاكتب /help لقائمة الأوامر.", chat_id)

# ── Polling thread ────────────────────────────────────────────────────────────
def polling_loop():
    offset = 0
    print("[polling] started")
    while True:
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message"]}, timeout=35)
            for update in res.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/"):
                    print(f"[cmd] {text} from {chat_id}")
                    threading.Thread(target=handle_command,
                                     args=(text, chat_id), daemon=True).start()
        except Exception as e:
            print(f"[polling error] {e}")
            notify_error("polling_loop", e)
            time.sleep(5)

# ── State ─────────────────────────────────────────────────────────────────────
STATE_FILE = "/tmp/health_state.json"
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except: return {}
def save_state(s: dict):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

# ── Alert loop ────────────────────────────────────────────────────────────────
def check_once(state: dict) -> dict:
    alerts = []

    cpu = cpu_percent()
    rp, ru, rt = ram_info()
    with _smp_lock:
        _samples.append({"cpu": cpu, "ram": rp})

    was = state.get("cpu_warn", False)
    if cpu > CPU_WARN and not was:
        m = f"CPU مرتفع: {cpu}%"
        alerts.append(f"🔴 <b>{m}</b>"); state["cpu_warn"] = True; log_event("🔴", m)
    elif cpu <= CPU_WARN and was:
        m = f"CPU عاد لطبيعي: {cpu}%"
        alerts.append(f"✅ <b>{m}</b>"); state["cpu_warn"] = False; log_event("✅", m)

    was = state.get("ram_warn", False)
    if rp > RAM_WARN and not was:
        m = f"RAM مرتفع: {rp}% ({ru}/{rt})"
        alerts.append(f"🔴 <b>{m}</b>"); state["ram_warn"] = True; log_event("🔴", m)
    elif rp <= RAM_WARN and was:
        m = f"RAM عاد لطبيعي: {rp}%"
        alerts.append(f"✅ <b>{m}</b>"); state["ram_warn"] = False; log_event("✅", m)

    dp, du, dt = disk_info()
    disk_state = state.get("disk_state", "ok")
    if dp > DISK_CRIT and disk_state != "crit":
        m = f"Disk حرج: {dp}% ({du}/{dt})"
        alerts.append(f"🔴 <b>{m}</b>"); state["disk_state"] = "crit"; log_event("🔴", m)
    elif dp > DISK_WARN and disk_state == "ok":
        m = f"Disk تحذير: {dp}% ({du}/{dt})"
        alerts.append(f"🟡 <b>{m}</b>"); state["disk_state"] = "warn"; log_event("🟡", m)
    elif dp <= DISK_WARN and disk_state != "ok":
        m = f"Disk عاد لطبيعي: {dp}%"
        alerts.append(f"✅ <b>{m}</b>"); state["disk_state"] = "ok"; log_event("✅", m)

    containers = docker_containers()
    prev = state.get("containers", {})
    curr = {}
    for c in containers:
        n, h = c["name"], c["health"]
        curr[n] = h
        if h == "unhealthy" and prev.get(n) != "unhealthy":
            m = f"Container unhealthy: {n}"
            alerts.append(f"🔴 <b>{m}</b>"); log_event("🔴", m)
        elif h == "healthy" and prev.get(n) == "unhealthy":
            m = f"Container عاد: {n}"
            alerts.append(f"✅ <b>{m}</b>"); log_event("✅", m)
    state["containers"] = curr

    prev_ep = state.get("endpoints", {})
    curr_ep = {}
    for name, url in ENDPOINTS:
        ok, code = check_endpoint(url)
        curr_ep[name] = ok
        if not ok:
            log_event("🔴", f"موقع معطل: {name} ({code or 'timeout'})")
        elif not prev_ep.get(name, True):
            log_event("✅", f"موقع عاد: {name}")
    state["endpoints"] = curr_ep

    if alerts:
        send(f"⚠️ <b>تنبيه</b> — {ksa_time()}\n\n" + "\n".join(alerts))

    return state

def daily_report():
    snapshot = build_status().replace("حالة السيرفر", "تقرير السيرفر اليومي")

    # متوسط اليوم
    with _smp_lock:
        smp = list(_samples)
        _samples.clear()
    if smp:
        avg_cpu = round(sum(s["cpu"] for s in smp) / len(smp), 1)
        avg_ram = round(sum(s["ram"] for s in smp) / len(smp), 1)
        max_cpu = round(max(s["cpu"] for s in smp), 1)
        max_ram = round(max(s["ram"] for s in smp), 1)
        snapshot += (f"\n\n<b>📈 متوسط الـ 24 ساعة:</b>"
                     f"\nCPU  متوسط {avg_cpu}%  |  ذروة {max_cpu}%"
                     f"\nRAM  متوسط {avg_ram}%  |  ذروة {max_ram}%")

    events = pop_event_log()
    if events:
        lines = ["\n\n<b>📋 أحداث الـ 24 ساعة الماضية:</b>"]
        for e in events:
            lines.append(f"{e['icon']} <code>{e['t']}</code>  {e['msg']}")
        snapshot += "\n".join(lines)
    else:
        snapshot += "\n\n<b>📋 أحداث اليوم:</b>\n✅ يوم هادئ — لا توجد تنبيهات"

    send(snapshot)

# ── Main ──────────────────────────────────────────────────────────────────────
# ── Error notification with per-type cooldown ────────────────────────────────
import traceback as _tb

_err_last: dict = {}
_ERR_COOLDOWN = 300  # 5 min per error type

def notify_error(where: str, exc: Exception):
    etype = type(exc).__name__
    now = time.time()
    if now - _err_last.get(etype, 0) < _ERR_COOLDOWN:
        return
    _err_last[etype] = now
    tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))[-1200:]
    # HTML-escape manually (avoid adding deps)
    safe = (tb.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    msg = f"🚨 <b>Health Bot error</b>\n<b>Where:</b> {where}\n<b>Type:</b> <code>{etype}</code>\n<pre>{safe}</pre>"
    try: send(msg)
    except Exception as e: print(f"[notify_error failed] {e}")


# ── Health-check HTTP server (for Uptime Kuma) ──────────────────────────────
from http.server import BaseHTTPRequestHandler, HTTPServer

HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8089"))
_health_state = {"polling_alive": False, "check_alive": False, "last_check": 0}

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404); self.end_headers(); return
        now = time.time()
        stale = (now - _health_state["last_check"]) > (CHECK_EVERY * 3)
        ok = _health_state["polling_alive"] and _health_state["check_alive"] and not stale
        body = json.dumps({
            "status": "ok" if ok else "degraded",
            "polling_alive": _health_state["polling_alive"],
            "check_alive": _health_state["check_alive"],
            "seconds_since_last_check": int(now - _health_state["last_check"]) if _health_state["last_check"] else -1,
        }).encode()
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a, **k): return

def start_health_server():
    def _serve():
        try:
            HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler).serve_forever()
        except Exception as e:
            print(f"[health server error] {e}")
    threading.Thread(target=_serve, name="health-http", daemon=True).start()
    print(f"[health-bot] health endpoint on 0.0.0.0:{HEALTH_PORT}/health")


def main():
    print(f"[health-bot] check={CHECK_EVERY}s report={REPORT_HOUR}:00UTC")
    send(f"🟢 <b>Health Bot بدأ</b>\nمراقبة نشطة ✅\n{ksa_time()} (KSA)\n\nاكتب /help للأوامر")

    start_health_server()

    def _polling_wrapper():
        _health_state["polling_alive"] = True
        try: polling_loop()
        finally: _health_state["polling_alive"] = False

    threading.Thread(target=_polling_wrapper, daemon=True).start()

    state = load_state()
    last_report_day = -1
    _health_state["check_alive"] = True

    while True:
        now = datetime.now(timezone.utc)
        if now.hour == REPORT_HOUR and now.day != last_report_day:
            try: daily_report(); last_report_day = now.day
            except Exception as e: print(f"[report error] {e}")
        try:
            state = check_once(state)
            save_state(state)
            _health_state["last_check"] = time.time()
        except Exception as e:
            print(f"[check error] {e}")
            notify_error("check_loop", e)
        time.sleep(CHECK_EVERY)

if __name__ == "__main__":
    main()
