import os
import sys
import time
import json
import uuid
import socket

from flask import Flask, request, redirect, url_for, render_template_string, flash
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = os.urandom(24)
# Sits behind nginx at /admin/ (see the "/admin/" location blocks on the
# relay servers) -- without this, url_for() generates paths like "/upload"
# with no prefix, which land on a completely different nginx location that
# has no client_max_body_size override, causing a 413 for any real upload.
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_proto=1, x_host=1)

queue_dir = "/opt/radio/queue"
playing_dir = "/opt/radio/cache"
liquidsoap_socket = "/run/liquidsoap-radio.sock"
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # generous cap for a handful of lossless files at once

ALLOWED_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac"}


def liquidsoap_command(cmd, timeout=5):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(liquidsoap_socket)
        s.sendall((cmd + "\n").encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if data.rstrip().endswith(b"END"):
                break
        return data.decode(errors="replace")
    finally:
        s.close()


def extract_tags(path):
    artist, title = "", ""
    try:
        import mutagen
        from mutagen.easyid3 import EasyID3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4

        f = None
        try:
            f = mutagen.File(path)
        except Exception:
            f = None
        if f is None:
            try:
                f = FLAC(path)
            except Exception:
                f = None
        if isinstance(f, MP4) and f.tags:
            artist = (f.tags.get("\xa9ART") or [""])[0]
            title = (f.tags.get("\xa9nam") or [""])[0]
        elif f is not None and f.tags:
            try:
                artist = (f.tags.get("artist") or [""])[0]
                title = (f.tags.get("title") or [""])[0]
            except Exception:
                pass
        if not artist and not title:
            try:
                easy = EasyID3(path)
                artist = (easy.get("artist") or [""])[0]
                title = (easy.get("title") or [""])[0]
            except Exception:
                pass
    except Exception as e:
        print(f"tag extraction failed: {e}", file=sys.stderr)
    return artist, title


def queue_dir_files():
    if not os.path.isdir(queue_dir):
        return []
    files = [f for f in os.listdir(queue_dir) if f.endswith(".audio")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(queue_dir, f)))
    return files


MAX_PREFETCH_AGE = 600  # seconds; mirrors queue_filler.py's own staleness threshold


def recently_played_ids():
    # A track lingers as a file in playing_dir for up to 2 generations after
    # it finishes (see update_history_and_cleanup in publish_now_playing.py)
    # purely so crossfade has both ends of the transition to work with --
    # it's not "coming up", it already played.
    ids = set()
    try:
        with open(os.path.join(playing_dir, ".history.json")) as f:
            for path in json.load(f):
                ids.add(os.path.basename(path).replace(".audio", ""))
    except Exception:
        pass
    return ids


def cache_dir_files():
    if not os.path.isdir(playing_dir):
        return []
    files = [f for f in os.listdir(playing_dir) if f.endswith(".audio")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(playing_dir, f)))

    played_ids = recently_played_ids()
    now = time.time()
    visible = []
    for f in files:
        track_id = f.replace(".audio", "")
        if track_id in played_ids:
            continue
        # Anything this old was never actually played and never will be --
        # either the "skip current track" button abandoned it mid-prefetch,
        # or it's some other orphan. queue_filler.py sweeps and deletes
        # these from disk on its own poll loop; here we just don't show it.
        try:
            if now - os.path.getmtime(os.path.join(playing_dir, f)) > MAX_PREFETCH_AGE:
                continue
        except OSError:
            continue
        visible.append(f)
    return visible


def read_meta(dir_, track_id):
    meta_path = os.path.join(dir_, f"{track_id}.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MusicmaniA — очередь</title>
<style>
  :root {
    --bg: #171111; --surface: #251B1A; --on-surface: #EDE0DF;
    --on-surface-variant: #D4C3C2; --primary: #FFB3AC; --on-primary: #68000A;
    --primary-container: #930012;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html { color-scheme: dark; }
  body {
    font-family: system-ui, sans-serif; background: var(--bg); color: var(--on-surface);
    padding: 24px; max-width: 640px; margin: 0 auto;
  }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  .sub { color: var(--on-surface-variant); font-size: 0.85rem; margin-bottom: 24px; }
  .cards-row { display: flex; flex-direction: column; gap: 20px; }
  .card { background: var(--surface); border-radius: 20px; padding: 20px; }
  @media (min-width: 900px) {
    body { max-width: 1400px; }
    .cards-row { flex-direction: row; align-items: flex-start; }
    .cards-row .card { flex: 1; min-width: 0; }
  }
  .card h2 { font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--on-surface-variant); }
  .card-header { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; margin-bottom: 14px; }
  .card-header h2 { margin-bottom: 0; }
  .skip-btn {
    flex-shrink: 0; margin: 0; width: 100%; padding: 8px 14px; border-radius: 100px;
    background: rgba(255,255,255,0.08); color: var(--on-surface); font-weight: 600; font-size: 0.75rem;
    border: none; cursor: pointer;
  }
  .skip-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .item { display: flex; gap: 10px; align-items: center; padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .item:last-child { border-bottom: none; }
  .item .num { width: 22px; height: 22px; border-radius: 50%; background: rgba(255,255,255,0.08); display: flex; align-items: center; justify-content: center; font-size: 0.7rem; flex-shrink: 0; }
  .item .txt { min-width: 0; }
  .item .t { font-size: 0.9rem; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .item .a { font-size: 0.78rem; color: var(--on-surface-variant); }
  .locked { opacity: 0.5; }
  .locked .num { background: rgba(255,255,255,0.04); }
  .item .reorder { display: flex; gap: 2px; margin-left: auto; flex-shrink: 0; }
  .item .reorder button {
    width: 26px; height: 26px; margin: 0; padding: 0; border-radius: 8px;
    background: rgba(255,255,255,0.06); color: var(--on-surface); font-size: 0.75rem;
    border: none; cursor: pointer;
  }
  .item .reorder button:disabled { opacity: 0.25; cursor: not-allowed; }
  label { display: block; font-size: 0.85rem; color: var(--on-surface-variant); margin: 14px 0 6px; }
  input[type=file], select, input[type=text] {
    width: 100%; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px; padding: 10px 12px; color: var(--on-surface); font-size: 0.9rem;
  }
  /* color-scheme:dark on <html> makes most browsers render the dropdown
     list natively dark, but that's a hint, not a guarantee -- pin the
     option colors explicitly too so it can't fall back to a light native
     popup with light-on-white-on-white text. */
  select option {
    background: var(--surface); color: var(--on-surface);
  }
  button {
    margin-top: 18px; width: 100%; padding: 13px; border: none; border-radius: 100px;
    background: var(--primary); color: var(--on-primary); font-weight: 700; font-size: 0.95rem; cursor: pointer;
  }
  .flash { padding: 12px 16px; border-radius: 12px; margin-bottom: 16px; font-size: 0.88rem; }
  .flash.success { background: rgba(120, 200, 120, 0.15); color: #9be89b; }
  .flash.error { background: rgba(220, 100, 100, 0.15); color: #ff9d9d; }
  .empty { color: var(--on-surface-variant); font-size: 0.85rem; }
  .upload-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.65);
    display: none; align-items: center; justify-content: center; z-index: 100; padding: 20px;
  }
  .upload-overlay.visible { display: flex; }
  .upload-modal {
    background: var(--surface); border-radius: 20px; padding: 24px; width: 100%; max-width: 360px;
  }
  .upload-modal-title { font-size: 1rem; font-weight: 700; margin-bottom: 16px; }
  .upload-progress-track { width: 100%; height: 8px; border-radius: 4px; background: rgba(255,255,255,0.1); overflow: hidden; }
  .upload-progress-fill { height: 100%; width: 0%; background: var(--primary); border-radius: 4px; transition: width 0.15s ease; }
  .upload-progress-text { margin-top: 8px; font-size: 0.85rem; color: var(--on-surface-variant); text-align: right; }
  .upload-modal-sub { margin-top: 12px; font-size: 0.85rem; color: var(--on-surface-variant); }
  .upload-modal-sub.error { color: #ff9d9d; }
  .upload-modal-close {
    margin-top: 16px; width: 100%; padding: 10px; border-radius: 100px; border: none;
    background: rgba(255,255,255,0.08); color: var(--on-surface); font-weight: 600; cursor: pointer; display: none;
  }
  .upload-modal-close.visible { display: block; }
</style>
</head>
<body>
  <h1>Очередь эфира</h1>
  <div class="sub">Ручное добавление треков (можно выбрать несколько файлов разом)</div>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, message in messages %}
      <div class="flash {{ category }}">{{ message }}</div>
    {% endfor %}
  {% endwith %}

  <div class="cards-row">
  <div class="card">
    <div class="card-header">
      <h2>Сейчас / прямо следом (нельзя переставить)</h2>
      <button type="button" id="skip-btn" class="skip-btn">Пропустить текущий трек</button>
    </div>
    <div id="cache-list">
      {% if cache_items %}
        {% for it in cache_items %}
          <div class="item locked">
            <div class="num">{{ loop.index }}</div>
            <div class="txt"><div class="t">{{ it.title }}</div><div class="a">{{ it.artist }}</div></div>
          </div>
        {% endfor %}
      {% else %}
        <div class="empty">Пусто</div>
      {% endif %}
    </div>
  </div>

  <div class="card">
    <h2>Дальше в очереди</h2>
    <div id="queue-list">
      {% if queue_items %}
        {% for it in queue_items %}
          <div class="item" data-id="{{ it.id }}">
            <div class="num">{{ loop.index }}</div>
            <div class="txt"><div class="t">{{ it.title }}</div><div class="a">{{ it.artist }}</div></div>
            <div class="reorder">
              <button type="button" class="move-up" {% if loop.first %}disabled{% endif %}>&#9650;</button>
              <button type="button" class="move-down" {% if loop.last %}disabled{% endif %}>&#9660;</button>
            </div>
          </div>
        {% endfor %}
      {% else %}
        <div class="empty">Пусто</div>
      {% endif %}
    </div>
  </div>

  <div class="card">
    <h2>Добавить треки</h2>
    <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data" id="upload-form">
      <label>Файлы (можно выбрать несколько)</label>
      <input type="file" name="audio_files" multiple required accept=".mp3,.flac,.m4a,.ogg,.wav,.aac" id="audio-files-input">

      <label>Куда поставить</label>
      <select name="after" id="after-select">
        <option value="__front__">Играть следующим (сразу после того, что уже нельзя переставить)</option>
        {% for it in queue_items %}
          <option value="{{ it.id }}">После «{{ it.title }}» ({{ loop.index }} в очереди)</option>
        {% endfor %}
      </select>

      <button type="submit">Загрузить и поставить в очередь</button>
    </form>
  </div>
  </div>

  <div class="upload-overlay" id="upload-overlay">
    <div class="upload-modal">
      <div class="upload-modal-title" id="upload-modal-title">Загрузка файлов...</div>
      <div class="upload-progress-track"><div class="upload-progress-fill" id="upload-progress-fill"></div></div>
      <div class="upload-progress-text" id="upload-progress-text">0%</div>
      <div class="upload-modal-sub" id="upload-modal-sub"></div>
      <button type="button" class="upload-modal-close" id="upload-modal-close">Закрыть</button>
    </div>
  </div>

  <script>
    const cacheList = document.getElementById('cache-list');
    const queueList = document.getElementById('queue-list');
    const afterSelect = document.getElementById('after-select');

    function renderItems(container, items, locked) {
      if (!items.length) {
        container.innerHTML = '<div class="empty">Пусто</div>';
        return;
      }
      container.innerHTML = items.map((it, i) => `
        <div class="item${locked ? ' locked' : ''}" data-id="${escapeHtml(it.id)}">
          <div class="num">${i + 1}</div>
          <div class="txt"><div class="t">${escapeHtml(it.title)}</div><div class="a">${escapeHtml(it.artist)}</div></div>
          ${locked ? '' : `
          <div class="reorder">
            <button type="button" class="move-up" ${i === 0 ? 'disabled' : ''}>&#9650;</button>
            <button type="button" class="move-down" ${i === items.length - 1 ? 'disabled' : ''}>&#9660;</button>
          </div>`}
        </div>
      `).join('');
    }

    function escapeHtml(s) {
      const d = document.createElement('div');
      d.textContent = s == null ? '' : s;
      return d.innerHTML;
    }

    const skipBtn = document.getElementById('skip-btn');
    skipBtn.addEventListener('click', async () => {
      skipBtn.disabled = true;
      const origText = skipBtn.textContent;
      skipBtn.textContent = 'Пропускаю...';
      try {
        const res = await fetch('skip', { method: 'POST' });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'unknown error');
        await refreshState();
      } catch (e) {
        console.error('Ошибка пропуска трека:', e);
        alert('Не удалось пропустить трек: ' + e.message);
      } finally {
        skipBtn.disabled = false;
        skipBtn.textContent = origText;
      }
    });

    let reordering = false;
    queueList.addEventListener('click', async (e) => {
      const btn = e.target.closest('.move-up, .move-down');
      if (!btn || btn.disabled || reordering) return;
      const item = btn.closest('.item');
      const id = item.dataset.id;
      const direction = btn.classList.contains('move-up') ? 'up' : 'down';
      reordering = true;
      try {
        await fetch('reorder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, direction }),
        });
        await refreshState();
      } catch (e) {
        console.error('Ошибка перестановки:', e);
      } finally {
        reordering = false;
      }
    });

    async function refreshState() {
      try {
        const res = await fetch('state.json', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        renderItems(cacheList, data.cache_items, true);
        renderItems(queueList, data.queue_items, false);

        // Keep whatever the admin currently has picked if it's still a
        // valid choice, so an in-flight selection doesn't get yanked out
        // from under them just because the list refreshed.
        const prevValue = afterSelect.value;
        const options = ['<option value="__front__">Играть следующим (сразу после того, что уже нельзя переставить)</option>']
          .concat(data.queue_items.map((it, i) => `<option value="${escapeHtml(it.id)}">После «${escapeHtml(it.title)}» (${i + 1} в очереди)</option>`));
        afterSelect.innerHTML = options.join('');
        const stillValid = Array.from(afterSelect.options).some(o => o.value === prevValue);
        afterSelect.value = stillValid ? prevValue : '__front__';
      } catch (e) {
        console.error('Ошибка обновления очереди:', e);
      }
    }

    refreshState();
    setInterval(refreshState, 4000);

    const uploadForm = document.getElementById('upload-form');
    const uploadOverlay = document.getElementById('upload-overlay');
    const uploadTitle = document.getElementById('upload-modal-title');
    const uploadFill = document.getElementById('upload-progress-fill');
    const uploadText = document.getElementById('upload-progress-text');
    const uploadSub = document.getElementById('upload-modal-sub');
    const uploadClose = document.getElementById('upload-modal-close');
    const filesInput = document.getElementById('audio-files-input');

    function showUploadModal() {
      uploadTitle.textContent = 'Загрузка файлов...';
      uploadFill.style.width = '0%';
      uploadText.textContent = '0%';
      uploadSub.textContent = '';
      uploadSub.classList.remove('error');
      uploadClose.classList.remove('visible');
      uploadOverlay.classList.add('visible');
    }

    uploadForm.addEventListener('submit', (e) => {
      e.preventDefault();
      if (!filesInput.files.length) return;

      const formData = new FormData(uploadForm);
      showUploadModal();

      const xhr = new XMLHttpRequest();
      xhr.open('POST', uploadForm.action, true);

      xhr.upload.addEventListener('progress', (ev) => {
        if (!ev.lengthComputable) return;
        const pct = Math.round((ev.loaded / ev.total) * 100);
        uploadFill.style.width = pct + '%';
        uploadText.textContent = pct + '%';
        if (pct >= 100) {
          uploadTitle.textContent = 'Обрабатываю на сервере...';
          uploadSub.textContent = 'Файлы уже загружены, идёт распознавание тегов и постановка в очередь. На медленной сети это может занять немного времени.';
        }
      });

      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 400) {
          uploadTitle.textContent = 'Готово';
          uploadFill.style.width = '100%';
          uploadText.textContent = '100%';
          uploadSub.textContent = 'Треки добавлены в очередь.';
          uploadForm.reset();
          refreshState();
          setTimeout(() => uploadOverlay.classList.remove('visible'), 1200);
        } else {
          uploadTitle.textContent = 'Ошибка загрузки';
          uploadSub.textContent = 'Сервер вернул код ' + xhr.status + '. Попробуйте ещё раз.';
          uploadSub.classList.add('error');
          uploadClose.classList.add('visible');
        }
      });

      xhr.addEventListener('error', () => {
        uploadTitle.textContent = 'Ошибка сети';
        uploadSub.textContent = 'Не удалось загрузить файлы — проверьте соединение и попробуйте снова.';
        uploadSub.classList.add('error');
        uploadClose.classList.add('visible');
      });

      xhr.addEventListener('timeout', () => {
        uploadTitle.textContent = 'Истекло время ожидания';
        uploadSub.textContent = 'Загрузка заняла слишком много времени. Попробуйте на более стабильном соединении.';
        uploadSub.classList.add('error');
        uploadClose.classList.add('visible');
      });

      xhr.timeout = 590000; // just under nginx/waitress's 600s ceiling
      xhr.send(formData);
    });

    uploadClose.addEventListener('click', () => uploadOverlay.classList.remove('visible'));
  </script>
</body>
</html>
"""


def current_state():
    cache_items = []
    for f in cache_dir_files():
        tid = f.replace(".audio", "")
        meta = read_meta(playing_dir, tid)
        cache_items.append({"id": tid, "title": meta.get("title") or "Без названия", "artist": meta.get("artist") or ""})

    queue_items = []
    for f in queue_dir_files():
        tid = f.replace(".audio", "")
        meta = read_meta(queue_dir, tid)
        queue_items.append({"id": tid, "title": meta.get("title") or "Без названия", "artist": meta.get("artist") or ""})

    return cache_items, queue_items


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/", methods=["GET"])
def index():
    cache_items, queue_items = current_state()
    return render_template_string(PAGE_TEMPLATE, cache_items=cache_items, queue_items=queue_items)


@app.route("/state.json", methods=["GET"])
def state_json():
    cache_items, queue_items = current_state()
    return {"cache_items": cache_items, "queue_items": queue_items}


@app.route("/reorder", methods=["POST"])
def reorder():
    data = request.get_json(silent=True) or {}
    track_id = str(data.get("id", ""))
    direction = data.get("direction", "")
    if direction not in ("up", "down") or not track_id:
        return {"ok": False, "error": "bad request"}, 400

    files = queue_dir_files()
    idx = None
    for i, f in enumerate(files):
        if f == f"{track_id}.audio":
            idx = i
            break
    if idx is None:
        return {"ok": False, "error": "not found"}, 404

    neighbor_idx = idx - 1 if direction == "up" else idx + 1
    if neighbor_idx < 0 or neighbor_idx >= len(files):
        return {"ok": False, "error": "already at edge"}, 200

    # Swap mtimes with the neighbor -- that's the only thing that determines
    # play order (see pop_from_queue.py), so swapping it is the same as
    # swapping position.
    path_a = os.path.join(queue_dir, files[idx])
    path_b = os.path.join(queue_dir, files[neighbor_idx])
    mtime_a = os.path.getmtime(path_a)
    mtime_b = os.path.getmtime(path_b)
    os.utime(path_a, (mtime_b, mtime_b))
    os.utime(path_b, (mtime_a, mtime_a))
    return {"ok": True}


@app.route("/skip", methods=["POST"])
def skip():
    try:
        # Deliberately MusicmaniA_Radio.skip, not request.dynamic.flush_and_skip
        # -- flush_and_skip's docs say exactly what they do: flush the whole
        # prefetch queue, not just skip the current track. That was silently
        # discarding anything else already prefetched, including
        # admin-uploaded tracks waiting their turn (confirmed: cache file
        # count dropped after using it). Plain .skip only advances past the
        # current track and leaves everything else queued alone (confirmed:
        # cache file count unchanged after skip).
        result = liquidsoap_command("MusicmaniA_Radio.skip")
        return {"ok": True, "result": result.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/upload", methods=["POST"])
def upload():
    files = [f for f in request.files.getlist("audio_files") if f and f.filename]
    if not files:
        flash("Файлы не выбраны", "error")
        return redirect(url_for("index"))

    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            flash(f"Недопустимый формат: {f.filename}", "error")
            return redirect(url_for("index"))

    os.makedirs(queue_dir, exist_ok=True)
    after_id = request.form.get("after", "__front__").strip()

    # Anchor point: everything currently in queue_dir, keyed by its mtime,
    # excluding whatever we're about to add ourselves.
    existing = queue_dir_files()
    mtimes = [os.path.getmtime(os.path.join(queue_dir, f)) for f in existing]

    if not existing:
        base_mtime = time.time()
        gap_before, gap_after = None, None
    elif after_id == "__front__":
        base_mtime = mtimes[0] - 2
        gap_before, gap_after = base_mtime - 2, mtimes[0]
    else:
        match_idx = None
        for i, fn in enumerate(existing):
            if fn == f"{after_id}.audio":
                match_idx = i
                break
        if match_idx is None:
            # Track played or vanished between page load and submit -- fall
            # back to the front rather than losing the upload.
            base_mtime = mtimes[0] - 2
        elif match_idx == len(existing) - 1:
            base_mtime = mtimes[match_idx] + 2
        else:
            base_mtime = (mtimes[match_idx] + mtimes[match_idx + 1]) / 2

    added_titles = []
    # Multiple files land as consecutive mtimes right after each other, in
    # the order they were selected, all starting at base_mtime -- tiny
    # increments so they sort in upload order without colliding with any
    # neighbor's timestamp.
    step = 0.01
    for i, file in enumerate(files):
        new_id = f"admin-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        tmp_path = os.path.join(queue_dir, f"{new_id}.audio.part")
        final_path = os.path.join(queue_dir, f"{new_id}.audio")
        file.save(tmp_path)

        artist, title = extract_tags(tmp_path)
        title = title or os.path.splitext(file.filename)[0]
        artist = artist or "MusicmaniA"

        meta = {"id": new_id, "artist": artist, "title": title, "link": None}
        with open(os.path.join(queue_dir, f"{new_id}.json"), "w") as mf:
            json.dump(meta, mf)
        os.replace(tmp_path, final_path)

        target_mtime = base_mtime + i * step
        os.utime(final_path, (target_mtime, target_mtime))
        added_titles.append(title)

    flash(f"Добавлено треков: {len(added_titles)} — " + ", ".join(added_titles), "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    from waitress import serve
    # Not auth-gated at this layer -- that's enforced by nginx (auth_basic)
    # on the relay servers in front of this. Binding 0.0.0.0 only works
    # safely because ufw restricts port 5055 to the relay's IP (see
    # "admin-app relay-only" rule), same pattern as icecast's port 8000.
    # channel_timeout defaults to 120s, too short for a slow mobile upload
    # of a large lossless file -- that's the leading suspect for uploads
    # that hang forever on phones and never actually complete.
    serve(app, host="0.0.0.0", port=5055, channel_timeout=600)
