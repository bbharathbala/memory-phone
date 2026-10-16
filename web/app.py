"""
Memory Phone message server.

Guests open a secret link, record a voice note, tag it with a year, and send.
The phone polls /api/phone/messages and downloads anything new.

Environment (set with `fly secrets set` / `[env]` in fly.toml):
  GUEST_KEY     secret in the recording link:   https://<app>.fly.dev/r/<GUEST_KEY>
  PHONE_KEY     shared with the Pi's sync script
  ADMIN_KEY     secret for the admin page:      https://<app>.fly.dev/admin?key=<ADMIN_KEY>
  AUTO_APPROVE  "1" = messages go straight to the phone, "0" = you approve each one
  DEFAULT_YEAR  year pre-filled on the recording page (e.g. 2026)
  FOR_NAME      name of the person the phone is for, shown on the page
  DATA_DIR      where the database and audio live (default /data, the Fly volume)
"""

import html
import os
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

DATA = Path(os.environ.get("DATA_DIR", "/data"))
AUDIO = DATA / "audio"
DB = DATA / "messages.db"
GUEST_KEY = os.environ.get("GUEST_KEY", "")
PHONE_KEY = os.environ.get("PHONE_KEY", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
AUTO_APPROVE = os.environ.get("AUTO_APPROVE", "1") == "1"
DEFAULT_YEAR = int(os.environ.get("DEFAULT_YEAR", time.strftime("%Y")))
FOR_NAME = os.environ.get("FOR_NAME", "them")
BIRTHDAY = os.environ.get("BIRTHDAY", "01-01")  # MM-DD, offered as a "save it for" date
MAX_BYTES = 20 * 1024 * 1024   # 20 MB is several minutes of audio
MAX_SECONDS = 120              # anything longer is trimmed

for k, v in {"GUEST_KEY": GUEST_KEY, "PHONE_KEY": PHONE_KEY, "ADMIN_KEY": ADMIN_KEY}.items():
    if len(v) < 12:
        raise SystemExit(f"{k} must be set to a long random string (see DEPLOY.md)")

AUDIO.mkdir(parents=True, exist_ok=True)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


with db() as con:
    con.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            year INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            unlock_at INTEGER NOT NULL DEFAULT 0,
            approved INTEGER NOT NULL DEFAULT 0,
            seconds REAL NOT NULL DEFAULT 0,
            file TEXT NOT NULL
        )"""
    )


def convert(src: Path, dst: Path) -> None:
    """Any phone/browser audio -> clean mono WAV at an even loudness."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", str(MAX_SECONDS),
            "-af", "silenceremove=start_periods=1:start_threshold=-50dB,"
                   "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ac", "1", "-ar", "44100", str(dst),
        ],
        check=True, timeout=120,
    )


def duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return round(float(out), 1)
    except Exception:
        return 0.0


def check(given: str, real: str) -> None:
    import hmac
    if not hmac.compare_digest(given or "", real):
        raise HTTPException(status_code=404)


# ---------------------------------------------------------------- guests

@app.get("/", response_class=HTMLResponse)
def home():
    return "<!doctype html><title>Memory Phone</title><p style='font-family:sans-serif'>Nothing to see here.</p>"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/r/{key}", response_class=HTMLResponse)
def record_page(key: str):
    check(key, GUEST_KEY)
    return (RECORD_HTML
            .replace("{{KEY}}", html.escape(key))
            .replace("{{NAME}}", html.escape(FOR_NAME))
            .replace("{{YEAR}}", str(DEFAULT_YEAR))
            .replace("{{BDAY}}", html.escape(BIRTHDAY)))


@app.post("/api/upload")
async def upload(
    key: str = Form(""),
    name: str = Form(""),
    year: str = Form(""),
    unlock: str = Form(""),
    audio: UploadFile | None = File(None),
):
    check(key, GUEST_KEY)
    name = " ".join(name.split())[:60]
    if not name:
        raise HTTPException(400, "Please add your name.")
    if not (year.strip().isdigit() and 1900 <= int(year) <= 2100):
        raise HTTPException(400, "That year doesn't look right.")
    year = int(year)
    if audio is None:
        raise HTTPException(400, "Record a message first.")
    unlock_at = 0
    if unlock.strip():
        try:
            unlock_at = int(datetime.strptime(unlock.strip(), "%Y-%m-%d")
                            .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            raise HTTPException(400, "That date doesn't look right.")

    data = await audio.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "That recording is too long.")
    if len(data) < 500:
        raise HTTPException(400, "The recording came through empty. Please try again.")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = AUDIO / f"{stamp}-{os.urandom(3).hex()}.wav"
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "in"
        raw.write_bytes(data)
        try:
            convert(raw, out)
        except Exception:
            out.unlink(missing_ok=True)
            raise HTTPException(400, "We couldn't read that audio. Please record again.")
    secs = duration(out)
    if secs < 0.5:
        out.unlink(missing_ok=True)
        raise HTTPException(400, "The recording was silent. Please try again.")

    with db() as con:
        con.execute(
            "INSERT INTO messages (name, year, created_at, unlock_at, approved, seconds, file) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, year, int(time.time()), unlock_at, 1 if AUTO_APPROVE else 0, secs, out.name),
        )
    return {"ok": True, "year": year}


# ---------------------------------------------------------------- phone

@app.get("/api/phone/messages")
def phone_messages(x_phone_key: str = Header("")):
    check(x_phone_key, PHONE_KEY)
    now = int(time.time())
    with db() as con:
        rows = con.execute(
            "SELECT id, name, year, created_at FROM messages "
            "WHERE approved = 1 AND unlock_at <= ? ORDER BY id", (now,)
        ).fetchall()
    return {"ok": True, "messages": [dict(r) for r in rows]}


@app.get("/api/phone/audio/{mid}")
def phone_audio(mid: int, x_phone_key: str = Header("")):
    check(x_phone_key, PHONE_KEY)
    with db() as con:
        r = con.execute(
            "SELECT file FROM messages WHERE id = ? AND approved = 1 AND unlock_at <= ?",
            (mid, int(time.time())),
        ).fetchone()
    if not r:
        raise HTTPException(404)
    return FileResponse(AUDIO / r["file"], media_type="audio/wav")


# ---------------------------------------------------------------- admin

@app.get("/admin", response_class=HTMLResponse)
def admin(key: str = ""):
    check(key, ADMIN_KEY)
    with db() as con:
        rows = con.execute("SELECT * FROM messages ORDER BY id DESC").fetchall()
    k = html.escape(key)
    items = []
    for r in rows:
        when = datetime.fromtimestamp(r["created_at"], timezone.utc).strftime("%d %b %H:%M UTC")
        status = "on the phone" if r["approved"] else "waiting for you"
        if r["unlock_at"] > time.time():
            status += " · opens " + datetime.fromtimestamp(r["unlock_at"], timezone.utc).strftime("%d %b %Y")
        approve = "" if r["approved"] else (
            f"<form method=post action='/admin/approve/{r['id']}?key={k}'><button>Approve</button></form>")
        items.append(
            f"<li><b>{html.escape(r['name'])}</b> · dial <b>{r['year']}</b> · {r['seconds']}s · {when}"
            f"<br><span class=s>{status}</span>"
            f"<br><audio controls preload=none src='/admin/audio/{r['id']}?key={k}'></audio>"
            f"<div class=row>{approve}"
            f"<form method=post action='/admin/delete/{r['id']}?key={k}' "
            f"onsubmit=\"return confirm('Delete this message?')\"><button class=del>Delete</button></form></div></li>"
        )
    body = "".join(items) or "<li>No messages yet.</li>"
    mode = "Messages go straight to the phone." if AUTO_APPROVE else "You approve each message before it reaches the phone."
    return (ADMIN_HTML.replace("{{ITEMS}}", body).replace("{{COUNT}}", str(len(rows)))
            .replace("{{MODE}}", mode))


@app.get("/admin/audio/{mid}")
def admin_audio(mid: int, key: str = ""):
    check(key, ADMIN_KEY)
    with db() as con:
        r = con.execute("SELECT file FROM messages WHERE id = ?", (mid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return FileResponse(AUDIO / r["file"], media_type="audio/wav")


@app.post("/admin/approve/{mid}")
def admin_approve(mid: int, key: str = ""):
    check(key, ADMIN_KEY)
    with db() as con:
        con.execute("UPDATE messages SET approved = 1 WHERE id = ?", (mid,))
    return RedirectResponse(f"/admin?key={key}", status_code=303)


@app.post("/admin/delete/{mid}")
def admin_delete(mid: int, key: str = ""):
    check(key, ADMIN_KEY)
    with db() as con:
        r = con.execute("SELECT file FROM messages WHERE id = ?", (mid,)).fetchone()
        con.execute("DELETE FROM messages WHERE id = ?", (mid,))
    if r:
        (AUDIO / r["file"]).unlink(missing_ok=True)
    return RedirectResponse(f"/admin?key={key}", status_code=303)


# ---------------------------------------------------------------- pages

RECORD_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#12332A">
<title>A message for {{NAME}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Young+Serif&display=swap" rel="stylesheet">
<style>
  :root{
    --paper:#F6F8F4; --wash:#E6EEE8; --line:#C9D8CF;
    --ink:#12332A; --ink-2:#456459; --red:#C42E1D; --red-deep:#97210F;
    --body:"Atkinson Hyperlegible",system-ui,sans-serif; --display:"Young Serif",Georgia,serif;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--paper);color:var(--ink);font:19px/1.55 var(--body)}
  ::selection{background:var(--ink);color:var(--paper)}
  :focus-visible{outline:3px solid var(--ink);outline-offset:3px}
  input{caret-color:var(--red)}
  main{max-width:500px;margin:0 auto;padding:0 20px 56px;position:relative;overflow:hidden}

  header{padding:44px 0 28px;position:relative}
  .dial{position:absolute;top:-58px;right:-74px;width:210px;height:210px;color:var(--ink);opacity:.13;pointer-events:none}
  h1{font:400 clamp(34px,9vw,44px)/1.08 var(--display);margin:0 0 14px;max-width:12ch;text-wrap:balance;letter-spacing:-.01em;position:relative}
  .lead{margin:0;color:var(--ink-2);max-width:34ch}

  ol.steps{list-style:none;margin:0;padding:0;counter-reset:s}
  .step{counter-increment:s;position:relative;padding:0 0 34px 52px}
  .step::before{content:counter(s);position:absolute;left:0;top:-2px;width:34px;height:34px;border-radius:50%;
    border:2px solid var(--ink);display:grid;place-items:center;font:700 17px/1 var(--body)}
  .step::after{content:"";position:absolute;left:17px;top:38px;bottom:8px;width:2px;background:var(--line)}
  .step:last-child::after{display:none}
  h2{font:700 21px/1.3 var(--body);margin:0 0 12px}
  .hint{color:var(--ink-2);font-size:17px;margin:10px 0 0}

  .field{width:100%;font:400 20px/1.3 var(--body);color:var(--ink);background:#fff;border:2px solid var(--line);
    border-radius:10px;padding:14px 16px;transition:border-color .15s}
  .field:focus{border-color:var(--ink);outline:none}
  .field::placeholder{color:#7B948A}

  .year{display:flex;align-items:center;gap:10px}
  .year input{font:400 54px/1 var(--display);letter-spacing:.06em;text-align:center;width:100%;max-width:230px;
    padding:14px 10px 10px;font-variant-numeric:lining-nums tabular-nums;-moz-appearance:textfield}
  .year input::-webkit-outer-spin-button,.year input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
  .nudge{width:52px;height:52px;border-radius:50%;border:2px solid var(--line);background:#fff;color:var(--ink);
    font:700 26px/1 var(--body);cursor:pointer;display:grid;place-items:center}
  .nudge:active{background:var(--wash)}

  .recorder{display:flex;align-items:center;gap:18px}
  #btn{flex:none;width:96px;height:96px;border-radius:50%;border:none;background:var(--red);color:#fff;cursor:pointer;
    display:grid;place-items:center;box-shadow:0 6px 16px -6px rgba(151,33,15,.55);transition:transform .12s,background .15s}
  #btn:active{transform:scale(.96)} #btn svg{width:38px;height:38px}
  #btn.on{background:var(--red-deep)}
  .rec-text b{display:block;font-size:20px} .rec-text span{display:block;color:var(--ink-2);font-size:17px;line-height:1.4}
  .num{font-family:var(--display);font-variant-numeric:lining-nums;font-size:.92em}
  #meter{display:flex;align-items:center;gap:3px;height:40px;margin-top:6px}
  #meter i{display:block;width:5px;height:4px;border-radius:3px;background:var(--red);transition:height .08s linear}
  #time{font-variant-numeric:tabular-nums}

  .review{background:var(--wash);border-radius:12px;padding:16px}
  .review audio{width:100%;display:block}
  .review button{margin-top:12px}
  .link{background:none;border:none;padding:6px 0;color:var(--ink);font:700 18px var(--body);text-decoration:underline;
    text-underline-offset:4px;text-decoration-thickness:2px;text-decoration-color:var(--line);cursor:pointer}
  .link:hover{text-decoration-color:var(--ink)}

  .when{display:grid;gap:10px;border:0;padding:0;margin:0}
  .when legend{padding:0}
  .opt{display:flex;align-items:center;gap:14px;padding:15px 16px;border:2px solid var(--line);border-radius:10px;background:#fff;cursor:pointer}
  .opt input{appearance:none;flex:none;width:24px;height:24px;border:2px solid var(--ink-2);border-radius:50%;margin:0;display:grid;place-items:center}
  .opt input::after{content:"";width:12px;height:12px;border-radius:50%;background:var(--ink);transform:scale(0);transition:transform .12s}
  .opt input:checked::after{transform:scale(1)}
  .opt:has(input:checked){border-color:var(--ink);background:var(--wash)}
  .opt small{display:block;color:var(--ink-2);font-size:16px}
  #dateWrap{margin-top:2px}

  .send{width:100%;padding:18px;border:none;border-radius:12px;background:var(--ink);color:var(--paper);
    font:700 20px var(--body);cursor:pointer;box-shadow:0 8px 20px -10px rgba(18,51,42,.6)}
  .send:disabled{opacity:.55;cursor:default}
  #msg{min-height:28px;margin:14px 0 0;color:var(--red-deep);font-weight:700}
  .upload{margin-top:18px;color:var(--ink-2);font-size:17px}

  .done{padding:56px 0 20px}
  .done h1{max-width:none}
  .done .big{font:400 72px/1 var(--display);margin:22px 0 4px;letter-spacing:.06em;font-variant-numeric:lining-nums}
  .done p{color:var(--ink-2);margin:0 0 30px;max-width:32ch}
  .hidden{display:none !important}
  @media (prefers-reduced-motion:reduce){*{transition:none !important}}
  @media (min-width:700px){main{overflow:visible} .dial{top:36px;right:-150px;width:190px;height:190px;opacity:.16} header{padding-top:64px}}
</style></head>
<body><main>
  <svg class="dial" viewBox="0 0 200 200" aria-hidden="true">
    <circle cx="100" cy="100" r="96" fill="none" stroke="currentColor" stroke-width="5"/>
    <circle cx="100" cy="100" r="40" fill="none" stroke="currentColor" stroke-width="5"/>
    <g fill="none" stroke="currentColor" stroke-width="5" id="holes"></g>
  </svg>

  <section id="form">
    <header>
      <h1>Leave {{NAME}} a message</h1>
      <p class="lead">They'll pick up an old rotary phone, dial a year, and hear your voice.</p>
    </header>

    <ol class="steps">
      <li class="step">
        <h2><label for="name">Your name</label></h2>
        <input class="field" id="name" autocomplete="name" maxlength="60" placeholder="So they know it's you">
      </li>

      <li class="step">
        <h2><label for="year">The year they'll dial</label></h2>
        <div class="year">
          <button class="nudge" type="button" data-d="-1" aria-label="One year earlier">&minus;</button>
          <input class="field" id="year" type="number" inputmode="numeric" min="1900" max="2100" value="{{YEAR}}">
          <button class="nudge" type="button" data-d="1" aria-label="One year later">+</button>
        </div>
        <p class="hint">Pick the year your memory is from. For a birthday wish, keep <span class="num">{{YEAR}}</span>.</p>
      </li>

      <li class="step">
        <h2 id="recLabel">Record your message</h2>
        <div id="recArea">
          <div class="recorder">
            <button id="btn" type="button" aria-labelledby="recLabel">
              <svg id="icMic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3.5"/></svg>
              <svg id="icStop" class="hidden" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
            </button>
            <div class="rec-text" aria-live="polite">
              <b id="recTitle">Tap to start</b>
              <span id="recSub">Up to a minute. Tap again to stop.</span>
              <div id="meter" class="hidden"></div>
            </div>
          </div>
        </div>
        <div id="review" class="review hidden">
          <audio id="preview" controls></audio>
          <button class="link" id="again" type="button">Record it again</button>
        </div>
      </li>

      <li class="step">
        <fieldset class="when">
          <legend><h2>When should they hear it?</h2></legend>
          <label class="opt"><input type="radio" name="when" value="now" checked><span>Straight away</span></label>
          <label class="opt"><input type="radio" name="when" value="date"><span>On a future date<small>Anniversary, birthday, any day you pick</small></span></label>
          <div id="dateWrap" class="hidden">
            <input class="field" id="unlock" type="date" aria-label="Date they can hear it">
            <p class="hint">It stays hidden on the phone until that morning.</p>
          </div>
        </fieldset>
      </li>
    </ol>

    <button class="send" id="send" type="button">Send message</button>
    <p id="msg" role="alert"></p>
    <p class="upload">Already have a voice note on your phone?
      <label class="link" for="file" style="display:inline">Upload it instead</label>
      <input id="file" class="hidden" type="file" accept="audio/*,.ogg,.opus,.m4a,.mp3,.wav,.aac">
    </p>
  </section>

  <section id="done" class="done hidden" aria-live="polite">
    <h1>Sent. Thank you.</h1>
    <div class="big" id="doneYear"></div>
    <p id="doneText"></p>
    <button class="send" id="another" type="button">Record another message</button>
  </section>
</main>
<script>
const KEY = "{{KEY}}", MAX = 60, BDAY = "{{BDAY}}";
const $ = id => document.getElementById(id);
let rec, chunks = [], blob = null, timer, started, stream, actx, raf;

// finger holes on the decorative dial
(() => { const g = $('holes'); for (let i = 0; i < 10; i++) {
  const a = (-60 + i * 28) * Math.PI / 180, c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  c.setAttribute('cx', 100 + 68 * Math.cos(a)); c.setAttribute('cy', 100 + 68 * Math.sin(a)); c.setAttribute('r', 13); g.appendChild(c); } })();

// their next birthday
const fmt = d => d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const nextBday = (() => { const [m, d] = BDAY.split('-').map(Number), t = new Date(); t.setHours(0, 0, 0, 0);
  let b = new Date(t.getFullYear(), m - 1, d); if (b <= t) b = new Date(t.getFullYear() + 1, m - 1, d); return b; })();
const tmr = new Date(); tmr.setDate(tmr.getDate() + 1); $('unlock').min = iso(tmr);

document.querySelectorAll('input[name=when]').forEach(r => r.onchange = () => {
  $('dateWrap').classList.toggle('hidden', r.value !== 'date' || !r.checked);
  if (r.value === 'date' && r.checked) $('unlock').focus();
});
document.querySelectorAll('.nudge').forEach(b => b.onclick = () => {
  const y = parseInt($('year').value || '{{YEAR}}', 10) + Number(b.dataset.d);
  if (y >= 1900 && y <= 2100) $('year').value = y;
});

function say(t) { $('msg').textContent = t || ''; }
function pickType() {
  if (!window.MediaRecorder) return null;
  for (const t of ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus', 'audio/webm', 'audio/aac'])
    if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) return t;
  return '';
}
function tick() {
  const s = Math.floor((Date.now() - started) / 1000);
  $('recSub').innerHTML = `<span class="num" style="display:inline">0:${String(Math.min(s, MAX)).padStart(2, '0')}</span> of a minute. Tap to stop.`;
  if (s >= MAX) stop();
}
function meter() {
  const m = $('meter'); m.innerHTML = ''; const bars = [];
  for (let i = 0; i < 22; i++) { const b = document.createElement('i'); m.appendChild(b); bars.push(b); }
  m.classList.remove('hidden');
  try {
    actx = new (window.AudioContext || window.webkitAudioContext)();
    const an = actx.createAnalyser(); an.fftSize = 256; actx.createMediaStreamSource(stream).connect(an);
    const data = new Uint8Array(an.fftSize), hist = new Array(22).fill(0);
    const draw = () => { an.getByteTimeDomainData(data); let peak = 0;
      for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
      hist.push(Math.min(1, peak / 60)); hist.shift();
      bars.forEach((b, i) => b.style.height = (4 + hist[i] * 36) + 'px'); raf = requestAnimationFrame(draw); };
    draw();
  } catch (e) {}
}
async function start() {
  say('');
  const type = pickType();
  if (type === null) { say("This browser can't record. Use \"Upload it instead\" below."); return; }
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { say('Allow the microphone, then tap the red button again. If nothing happens, open this link in Safari or Chrome.'); return; }
  chunks = [];
  rec = type ? new MediaRecorder(stream, { mimeType: type }) : new MediaRecorder(stream);
  rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = () => {
    stream.getTracks().forEach(t => t.stop());
    cancelAnimationFrame(raf); try { actx && actx.close(); } catch (e) {}
    showReview(new Blob(chunks, { type: rec.mimeType || type || 'audio/webm' }));
  };
  rec.start(250);
  started = Date.now(); tick(); timer = setInterval(tick, 250);
  $('btn').classList.add('on'); $('icMic').classList.add('hidden'); $('icStop').classList.remove('hidden');
  $('recTitle').textContent = 'Recording'; meter();
}
function stop() {
  clearInterval(timer);
  if (rec && rec.state !== 'inactive') rec.stop();
}
function showReview(b) {
  blob = b;
  $('preview').src = URL.createObjectURL(b);
  $('recArea').classList.add('hidden'); $('review').classList.remove('hidden');
  $('recLabel').textContent = 'Listen back';
}
function reset() {
  blob = null; $('file').value = '';
  $('btn').classList.remove('on'); $('icMic').classList.remove('hidden'); $('icStop').classList.add('hidden');
  $('meter').classList.add('hidden');
  $('recTitle').textContent = 'Tap to start'; $('recSub').textContent = 'Up to a minute. Tap again to stop.';
  $('recLabel').textContent = 'Record your message';
  $('review').classList.add('hidden'); $('recArea').classList.remove('hidden'); say('');
}
$('btn').onclick = () => (rec && rec.state === 'recording') ? stop() : start();
$('again').onclick = reset;
$('file').onchange = () => { const f = $('file').files[0]; if (f) { showReview(f); say(''); } };

$('send').onclick = async () => {
  const name = $('name').value.trim(), year = $('year').value.trim();
  const when = document.querySelector('input[name=when]:checked').value;
  const unlock = when === 'date' ? $('unlock').value : '';
  if (!name) { say('Add your name first.'); $('name').focus(); return; }
  if (!/^\d{4}$/.test(year)) { say('The year needs 4 digits, like 1998.'); $('year').focus(); return; }
  if (!blob) { say('Record your message first. Tap the red button.'); $('btn').focus(); return; }
  if (when === 'date' && !unlock) { say('Pick the date they should hear it.'); $('unlock').focus(); return; }
  const ext = (blob.type || '').includes('mp4') ? 'm4a' : (blob.type || '').includes('ogg') ? 'ogg' : 'webm';
  const fd = new FormData();
  fd.append('key', KEY); fd.append('name', name); fd.append('year', year); fd.append('unlock', unlock);
  fd.append('audio', blob, blob.name || `message.${ext}`);
  $('send').disabled = true; $('send').textContent = 'Sending…'; say('');
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'That didn\'t go through. Check your internet and tap Send again.');
    $('form').classList.add('hidden'); $('done').classList.remove('hidden');
    $('doneYear').textContent = year;
    const n = t => `<span class="num">${t}</span>`;
    $('doneText').innerHTML = unlock
      ? `It's saved. From ${n(fmt(new Date(unlock + 'T00:00')))}, {{NAME}} can dial ${n(year)} to hear it.`
      : `When {{NAME}} dials ${n(year)}, they'll hear you.`;
    window.scrollTo(0, 0);
    try { localStorage.setItem('mp-name', name); } catch (e) {}
  } catch (e) { say(e.message); }
  finally { $('send').disabled = false; $('send').textContent = 'Send message'; }
};
$('another').onclick = () => {
  reset(); document.querySelector('input[value=now]').checked = true; $('dateWrap').classList.add('hidden'); $('unlock').value = '';
  $('done').classList.add('hidden'); $('form').classList.remove('hidden'); window.scrollTo(0, 0);
};
try { const n = localStorage.getItem('mp-name'); if (n) $('name').value = n; } catch (e) {}
</script>
</body></html>"""

ADMIN_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memory Phone admin</title>
<style>
  :root{--bg:#f6f1e9;--card:#fffdf9;--ink:#2b2420;--muted:#7a6d64;--accent:#9c3d2e;--line:#e4dacd}
  @media (prefers-color-scheme: dark){:root{--bg:#1d1916;--card:#26211d;--ink:#f1e9df;--muted:#b3a597;--accent:#e07a5f;--line:#3a332d}}
  body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
  main{max-width:640px;margin:0 auto;padding:24px 16px}
  h1{font-size:22px;margin:0} .s{color:var(--muted);font-size:14px}
  ul{list-style:none;padding:0} li{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}
  audio{width:100%;margin:8px 0} .row{display:flex;gap:8px} form{margin:0}
  button{font:600 15px system-ui;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:none;color:var(--ink);cursor:pointer}
  .del{color:var(--accent)}
</style></head>
<body><main>
  <h1>Messages ({{COUNT}})</h1>
  <p class="s">{{MODE}} The phone checks for new messages every few minutes.</p>
  <ul>{{ITEMS}}</ul>
</main></body></html>"""
