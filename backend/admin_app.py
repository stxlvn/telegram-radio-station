import os
import sys
import time
import json
import re
import uuid
import socket
import asyncio

from flask import Flask, request, redirect, url_for, render_template_string, flash
from werkzeug.middleware.proxy_fix import ProxyFix
from telethon import TelegramClient

app = Flask(__name__)
app.secret_key = os.urandom(24)
# Sits behind nginx at /admin/ (see the "/admin/" location blocks on the
# relay servers) -- without this, url_for() generates paths like "/upload"
# with no prefix, which land on a completely different nginx location that
# has no client_max_body_size override, causing a 413 for any real upload.
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1, x_proto=1, x_host=1)

queue_dir = os.environ.get("RADIO_QUEUE_DIR", "/opt/radio/queue")
playing_dir = os.environ.get("RADIO_CACHE_DIR", "/opt/radio/cache")
liquidsoap_socket = os.environ.get("LIQUIDSOAP_SOCKET_PATH", "/run/liquidsoap-radio.sock")
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # generous cap for a handful of lossless files at once

ALLOWED_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac"}

# Optional -- only needed for the "add by channel link" form. Left as
# os.environ.get() rather than the bracket access queue_filler.py uses for
# these same names, so this app still starts fine on a host that hasn't
# been given the systemd EnvironmentFile yet; the route itself checks and
# flashes a clear error instead of the whole app failing to boot.
telegram_api_id = os.environ.get("TELEGRAM_API_ID")
telegram_api_hash = os.environ.get("TELEGRAM_API_HASH")
telegram_channel = os.environ.get("TELEGRAM_CHANNEL")
# A dedicated session file, not queue_filler.py's live one -- confirmed
# directly (earlier incident) that two Telethon clients sharing one
# .session file at the same time is a real, live footgun, not a
# theoretical one. This is a plain file copy of an already-authorized
# session (see the deploy notes), never a fresh interactive login here.
telegram_admin_session_path = os.environ.get("TELEGRAM_ADMIN_SESSION_PATH", "/opt/radio/session_admin")

# Accepts a full message link (https://t.me/<channel>/<id>, with or
# without a leading https://, with or without "www."), or just the bare
# numeric message ID for anyone who already knows it. Validates the
# channel name in a full link matches the configured channel rather than
# silently accepting a link to a different chat entirely.
TELEGRAM_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?t\.me/([A-Za-z0-9_]+)/(\d+)")


def parse_telegram_link(text):
    text = (text or "").strip()
    if not text:
        return None, "Ссылка не указана"
    if text.isdigit():
        return int(text), None
    m = TELEGRAM_LINK_RE.search(text)
    if not m:
        return None, "Не похоже на ссылку вида t.me/<канал>/<id>"
    link_channel, msg_id = m.group(1), m.group(2)
    if telegram_channel and link_channel.lower() != telegram_channel.lower():
        return None, f"Ссылка на другой канал (@{link_channel}), а не @{telegram_channel}"
    return int(msg_id), None


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
  .item .remove-btn {
    width: 26px; height: 26px; margin: 0 0 0 6px; padding: 0; border-radius: 8px;
    background: rgba(255,255,255,0.06); color: var(--on-surface-variant); font-size: 1rem;
    border: none; cursor: pointer; flex-shrink: 0;
  }
  .item .remove-btn:hover { background: var(--primary-container); color: var(--primary); }
  .locked .remove-btn { opacity: 1; }
  label { display: block; font-size: 0.85rem; color: var(--on-surface-variant); margin: 14px 0 6px; }
  input[type=file], select, input[type=text], textarea {
    width: 100%; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px; padding: 10px 12px; color: var(--on-surface); font-size: 0.9rem;
  }
  textarea { font-family: inherit; resize: vertical; }
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
  .add-link-divider {
    text-align: center; margin: 18px 0; color: var(--on-surface-variant);
    font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;
  }
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
          <div class="item locked" data-id="{{ it.id }}">
            <div class="num">{{ loop.index }}</div>
            <div class="txt"><div class="t">{{ it.title }}</div><div class="a">{{ it.artist }}</div></div>
            <button type="button" class="remove-btn" data-location="cache" title="Удалить">&times;</button>
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
            <button type="button" class="remove-btn" data-location="queue" title="Удалить">&times;</button>
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

    <div class="add-link-divider">или</div>

    <form id="add-link-form">
      <label>Ссылки на сообщения в канале, по одной на строку (t.me/{{ telegram_channel or '...' }}/12345)</label>
      <textarea name="channel_links" required rows="4" placeholder="https://t.me/{{ telegram_channel or 'channel' }}/12345&#10;https://t.me/{{ telegram_channel or 'channel' }}/12346&#10;..." id="channel-links-input"></textarea>

      <label>Куда поставить (первую ссылку; дальше по порядку, друг за другом)</label>
      <select name="after" id="after-select-link">
        <option value="__front__">Играть следующим (сразу после того, что уже нельзя переставить)</option>
        {% for it in queue_items %}
          <option value="{{ it.id }}">После «{{ it.title }}» ({{ loop.index }} в очереди)</option>
        {% endfor %}
      </select>

      <button type="submit" id="add-link-submit">Найти в канале и поставить в очередь</button>
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
      const location = locked ? 'cache' : 'queue';
      container.innerHTML = items.map((it, i) => `
        <div class="item${locked ? ' locked' : ''}" data-id="${escapeHtml(it.id)}">
          <div class="num">${i + 1}</div>
          <div class="txt"><div class="t">${escapeHtml(it.title)}</div><div class="a">${escapeHtml(it.artist)}</div></div>
          ${locked ? '' : `
          <div class="reorder">
            <button type="button" class="move-up" ${i === 0 ? 'disabled' : ''}>&#9650;</button>
            <button type="button" class="move-down" ${i === items.length - 1 ? 'disabled' : ''}>&#9660;</button>
          </div>`}
          <button type="button" class="remove-btn" data-location="${location}" title="Удалить">&times;</button>
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

    let removing = false;
    async function handleRemoveClick(e) {
      const btn = e.target.closest('.remove-btn');
      if (!btn || removing) return;
      const item = btn.closest('.item');
      const id = item.dataset.id;
      const location = btn.dataset.location;
      const title = item.querySelector('.t')?.textContent || '';
      const warning = location === 'cache'
        ? `Удалить «${title}»? Этот трек уже сейчас играет или вот-вот начнёт -- удаление файла может оборвать воспроизведение с щелчком/сбоем.`
        : `Удалить «${title}» из очереди?`;
      if (!confirm(warning)) return;
      removing = true;
      try {
        const res = await fetch('remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, location }),
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'unknown error');
        await refreshState();
      } catch (err) {
        console.error('Ошибка удаления:', err);
        alert('Не удалось удалить трек: ' + err.message);
      } finally {
        removing = false;
      }
    }
    queueList.addEventListener('click', handleRemoveClick);
    cacheList.addEventListener('click', handleRemoveClick);

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

    // Several links submit as separate requests in sequence (not all at
    // once -- a Telegram flood-wait from hammering it concurrently would
    // be a worse failure mode than just taking longer), each one only
    // starting once the previous has actually finished downloading. That
    // sequencing is also what makes a real, per-track progress readout
    // possible at all, reusing the same overlay markup as file uploads.
    const addLinkForm = document.getElementById('add-link-form');
    const addLinkSubmit = document.getElementById('add-link-submit');
    const channelLinksInput = document.getElementById('channel-links-input');
    const afterSelectLink = document.getElementById('after-select-link');

    addLinkForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const links = channelLinksInput.value.split('\n').map((s) => s.trim()).filter(Boolean);
      if (!links.length) return;

      showUploadModal();
      uploadTitle.textContent = 'Добавляю треки из канала...';

      // Each successful add's own id becomes the "after" for the next
      // link in the batch, so a multi-link paste lands in the queue in
      // the same top-to-bottom order it was pasted in, each one right
      // after the last, rather than every link competing for the same
      // single insertion point.
      let afterId = afterSelectLink.value;
      const results = [];
      for (let i = 0; i < links.length; i++) {
        uploadSub.textContent = `Ищу трек ${i + 1} из ${links.length} в канале` +
          (links.length > 1 ? ' (большие lossless-файлы могут занять время)' : '') + '...';
        try {
          const res = await fetch('add_by_link', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ link: links[i], after: afterId }),
          });
          const data = await res.json();
          if (data.ok) {
            results.push({ ok: true, title: data.title, artist: data.artist });
            afterId = data.id;
          } else {
            results.push({ ok: false, link: links[i], error: data.error || 'неизвестная ошибка' });
          }
        } catch (err) {
          results.push({ ok: false, link: links[i], error: 'ошибка сети' });
        }
        const pct = Math.round(((i + 1) / links.length) * 100);
        uploadFill.style.width = pct + '%';
        uploadText.textContent = pct + '%';
      }

      const okResults = results.filter((r) => r.ok);
      const failed = results.filter((r) => !r.ok);
      uploadTitle.textContent = failed.length ? 'Готово с ошибками' : 'Готово';
      uploadSub.textContent = `Добавлено: ${okResults.length} из ${links.length}` +
        (okResults.length ? ' — ' + okResults.map((r) => r.title).join(', ') : '') +
        (failed.length ? '. Не удалось: ' + failed.map((f) => f.link + ' (' + f.error + ')').join('; ') : '');
      if (failed.length) {
        uploadSub.classList.add('error');
        uploadClose.classList.add('visible');
      } else {
        setTimeout(() => uploadOverlay.classList.remove('visible'), 1500);
      }
      addLinkForm.reset();
      refreshState();
    });
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
    return render_template_string(
        PAGE_TEMPLATE, cache_items=cache_items, queue_items=queue_items, telegram_channel=telegram_channel
    )


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


TRACK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@app.route("/remove", methods=["POST"])
def remove():
    # Deliberately allowed for cache_items too, not just queue_items --
    # those are only "locked" from *reordering* (their play order is
    # already committed, imminent or in progress), which is a different
    # thing from wanting one gone entirely. Removing the file out from
    # under a track that's already mid-playback on Liquidsoap will glitch
    # that one playback (there's no clean "abort this" primitive for
    # anything past the current track, only .skip for the very first) --
    # asked for explicitly, so it stays a deliberate, understood tradeoff
    # rather than something to silently prevent.
    data = request.get_json(silent=True) or {}
    track_id = str(data.get("id", "")).strip()
    location = data.get("location", "")
    if location not in ("queue", "cache") or not track_id or not TRACK_ID_RE.match(track_id):
        return {"ok": False, "error": "bad request"}, 400

    target_dir = queue_dir if location == "queue" else playing_dir
    removed = False
    for suffix in (".audio", ".json", ".audio.part"):
        path = os.path.join(target_dir, f"{track_id}{suffix}")
        if os.path.exists(path):
            os.remove(path)
            removed = True
    if not removed:
        return {"ok": False, "error": "not found"}, 404
    return {"ok": True}


@app.route("/skip", methods=["POST"])
def skip():
    try:
        # Deliberately output.icecast.skip, not request.dynamic.flush_and_skip
        # -- flush_and_skip's docs say exactly what they do: flush the whole
        # prefetch queue, not just skip the current track. That was silently
        # discarding anything else already prefetched, including
        # admin-uploaded tracks waiting their turn (confirmed: cache file
        # count dropped after using it). Plain .skip only advances past the
        # current track and leaves everything else queued alone (confirmed:
        # cache file count unchanged after skip).
        #
        # Command name changed on the Liquidsoap 2.4.5 upgrade -- the socket's
        # auto-registered command namespace switched from a name-derived id
        # ("MusicmaniA_Radio.skip") to the operator's own generic id
        # (confirmed via the socket's own "help" listing post-upgrade).
        result = liquidsoap_command("output.icecast.skip")
        return {"ok": True, "result": result.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


def compute_insert_base_mtime(after_id):
    # Anchor point: everything currently in queue_dir, keyed by its mtime,
    # excluding whatever we're about to add ourselves. Shared by both the
    # file-upload and add-by-link routes -- same "where do new items land"
    # rule regardless of how the track got here.
    existing = queue_dir_files()
    mtimes = [os.path.getmtime(os.path.join(queue_dir, f)) for f in existing]

    if not existing:
        return time.time()
    if after_id == "__front__":
        return mtimes[0] - 2
    match_idx = None
    for i, fn in enumerate(existing):
        if fn == f"{after_id}.audio":
            match_idx = i
            break
    if match_idx is None:
        # Track played or vanished between page load and submit -- fall
        # back to the front rather than losing the addition.
        return mtimes[0] - 2
    if match_idx == len(existing) - 1:
        return mtimes[match_idx] + 2
    return (mtimes[match_idx] + mtimes[match_idx + 1]) / 2


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
    base_mtime = compute_insert_base_mtime(after_id)

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


async def _fetch_channel_track(message_id, tmp_path):
    client = TelegramClient(telegram_admin_session_path, int(telegram_api_id), telegram_api_hash)
    await client.start()
    try:
        msg = await client.get_messages(telegram_channel, ids=message_id)
        if msg is None:
            return None, "Сообщение не найдено (удалено или неверный id)"
        if not (msg.audio or msg.voice):
            return None, "В этом сообщении нет аудио"
        file_title = getattr(msg.file, "title", None) or ""
        file_performer = getattr(msg.file, "performer", None) or ""
        file_name = getattr(msg.file, "name", None) or ""
        path = await client.download_media(msg, file=tmp_path)
        if not path:
            return None, "Не удалось скачать файл"
        return {"file_title": file_title, "file_performer": file_performer, "file_name": file_name}, None
    finally:
        await client.disconnect()


@app.route("/add_by_link", methods=["POST"])
def add_by_link():
    # JSON in, JSON out -- not a redirect+flash like /upload, since the
    # frontend now submits several links one at a time in sequence (see
    # the script below) to drive a real per-track progress indicator, the
    # same reason /reorder and /remove are JSON endpoints already.
    if not (telegram_api_id and telegram_api_hash and telegram_channel):
        return {"ok": False, "error": "Добавление по ссылке не настроено на сервере (нет TELEGRAM_* переменных)"}, 400

    data = request.get_json(silent=True) or {}
    message_id, err = parse_telegram_link(data.get("link", ""))
    if err:
        return {"ok": False, "error": err}, 400

    os.makedirs(queue_dir, exist_ok=True)
    new_id = f"admin-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    tmp_path = os.path.join(queue_dir, f"{new_id}.audio.part")
    final_path = os.path.join(queue_dir, f"{new_id}.audio")

    try:
        info, err = asyncio.run(_fetch_channel_track(message_id, tmp_path))
    except Exception as e:
        print(f"add_by_link failed: {e}", file=sys.stderr)
        info, err = None, f"Ошибка Telegram: {e}"

    if err:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return {"ok": False, "error": err}, 400

    artist, title = extract_tags(tmp_path)
    if not artist:
        artist = info["file_performer"]
    if not title:
        title = info["file_title"] or info["file_name"]
    title = title or "Неизвестный трек"
    artist = artist or "MusicmaniA"

    link = f"https://t.me/{telegram_channel}/{message_id}"
    meta = {"id": new_id, "artist": artist, "title": title, "link": link}
    with open(os.path.join(queue_dir, f"{new_id}.json"), "w") as mf:
        json.dump(meta, mf)
    os.replace(tmp_path, final_path)

    # "after" resolved fresh for every link in the batch (not just once up
    # front) -- each successive track should land right after the one the
    # batch itself just placed, so a multi-link paste ends up in the same
    # top-to-bottom order it was pasted in, not all piled at the same spot.
    after_id = str(data.get("after", "__front__")).strip()
    base_mtime = compute_insert_base_mtime(after_id)
    os.utime(final_path, (base_mtime, base_mtime))

    return {"ok": True, "id": new_id, "title": title, "artist": artist}


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
