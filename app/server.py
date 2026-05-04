#!/usr/bin/env python3
"""Video Podcast aggregator — Flask app served via HA ingress."""

import os
import json
import time
import hashlib
import threading
import datetime
import logging

import feedparser
import requests
from flask import Flask, jsonify, request, Response

logging.basicConfig(level=logging.INFO, format='[podcasts] %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

SHARE_DIR = '/share/podcasts'
CACHE_FILE = os.path.join(SHARE_DIR, 'cache.json')
OPTIONS = '/data/options.json'
PORT = 8099

_cache_lock = threading.Lock()
_cache = {}          # feed_id -> feed dict
_refresh_status = {'running': False, 'last': None, 'error': None}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_options():
    try:
        with open(OPTIONS) as f:
            return json.load(f)
    except Exception:
        return {}


def get_feeds_config():
    feeds = []
    try:
        raw = os.environ.get('FEEDS', '[]')
        feeds = json.loads(raw)
    except Exception:
        pass
    return feeds


def feed_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def extract_video_url(entry):
    for enc in entry.get('enclosures', []):
        mime = enc.get('type', '')
        if mime.startswith('video/') or mime == 'application/x-mpegURL':
            return enc.get('href', '') or enc.get('url', '')
    for mc in entry.get('media_content', []):
        if mc.get('medium') == 'video' or mc.get('type', '').startswith('video/'):
            return mc.get('url', '')
    for link in entry.get('links', []):
        if link.get('type', '').startswith('video/'):
            return link.get('href', '')
    return ''


def extract_thumbnail(entry, feed_image=''):
    if hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
        return entry.media_thumbnail[0].get('url', '')
    if hasattr(entry, 'itunes_image'):
        return entry.itunes_image.get('href', '')
    img = entry.get('image', {})
    if isinstance(img, dict) and img.get('href'):
        return img['href']
    return feed_image


def parse_duration(entry):
    dur = entry.get('itunes_duration', '')
    if not dur:
        return ''
    try:
        parts = str(dur).split(':')
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'
        elif len(parts) == 2:
            return f'{int(parts[0])}:{int(parts[1]):02d}'
        else:
            secs = int(dur)
            return f'{secs // 60}:{secs % 60:02d}'
    except Exception:
        return str(dur)


def fetch_feed(url, name_override=''):
    log.info(f'Fetching {url}')
    try:
        parsed = feedparser.parse(url, request_headers={'User-Agent': 'Mozilla/5.0'})
        if parsed.bozo and not parsed.entries:
            raise Exception(f'Feed parse error: {parsed.bozo_exception}')

        feed_info = parsed.feed
        feed_image = ''
        if hasattr(feed_info, 'image'):
            feed_image = feed_info.image.get('href', '')
        if not feed_image and hasattr(feed_info, 'itunes_image'):
            feed_image = feed_info.itunes_image.get('href', '')

        episodes = []
        for entry in parsed.entries[:50]:
            video_url = extract_video_url(entry)
            if not video_url:
                continue
            published = entry.get('published_parsed') or entry.get('updated_parsed')
            pub_str = ''
            if published:
                try:
                    pub_str = datetime.datetime(*published[:6]).strftime('%Y-%m-%d')
                except Exception:
                    pass
            episodes.append({
                'id':          feed_id(entry.get('id', video_url)),
                'title':       entry.get('title', 'Untitled'),
                'description': entry.get('summary', ''),
                'published':   pub_str,
                'duration':    parse_duration(entry),
                'url':         video_url,
                'thumbnail':   extract_thumbnail(entry, feed_image),
            })

        return {
            'id':          feed_id(url),
            'url':         url,
            'name':        name_override or feed_info.get('title', url),
            'description': feed_info.get('subtitle', ''),
            'image':       feed_image,
            'updated':     datetime.datetime.utcnow().isoformat(),
            'episodes':    episodes,
            'error':       None,
        }
    except Exception as e:
        log.warning(f'Failed to fetch {url}: {e}')
        return {
            'id':       feed_id(url),
            'url':      url,
            'name':     name_override or url,
            'episodes': [],
            'error':    str(e),
            'updated':  datetime.datetime.utcnow().isoformat(),
        }


def refresh_all_feeds():
    global _cache, _refresh_status
    _refresh_status['running'] = True
    _refresh_status['error'] = None
    log.info('Refreshing feeds...')
    feeds_config = get_feeds_config()
    new_cache = {}
    for fc in feeds_config:
        url = fc.get('url', '').strip()
        if not url:
            continue
        result = fetch_feed(url, fc.get('name', ''))
        new_cache[result['id']] = result
    with _cache_lock:
        _cache = new_cache
    _refresh_status['running'] = False
    _refresh_status['last'] = datetime.datetime.utcnow().isoformat()
    try:
        os.makedirs(SHARE_DIR, exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump(new_cache, f)
    except Exception as e:
        log.warning(f'Cache write failed: {e}')
    log.info(f'Refresh complete — {len(new_cache)} feeds')


def load_cache():
    global _cache
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                with _cache_lock:
                    _cache = json.load(f)
            log.info(f'Loaded {len(_cache)} feeds from cache')
    except Exception:
        pass


def refresh_loop():
    interval = int(os.environ.get('REFRESH_INTERVAL', 3600))
    while True:
        refresh_all_feeds()
        time.sleep(interval)


# ---------------------------------------------------------------------------
# HA helpers
# ---------------------------------------------------------------------------

def get_media_players():
    opts = get_options()
    ha_url = opts.get('ha_url', 'http://homeassistant:8123').rstrip('/')
    ha_token = opts.get('ha_token', '')
    try:
        resp = requests.get(
            f'{ha_url}/api/states',
            headers={'Authorization': f'Bearer {ha_token}'},
            timeout=5,
        )
        players = []
        for s in resp.json():
            if s['entity_id'].startswith('media_player.'):
                players.append({
                    'entity_id': s['entity_id'],
                    'name': s.get('attributes', {}).get('friendly_name', s['entity_id']),
                    'state': s.get('state', ''),
                })
        return sorted(players, key=lambda x: x['name'])
    except Exception as e:
        log.warning(f'Failed to fetch media players: {e}')
        return []


def play_on_device(entity_id, media_url, title):
    opts = get_options()
    ha_url = opts.get('ha_url', 'http://homeassistant:8123').rstrip('/')
    ha_token = opts.get('ha_token', '')
    resp = requests.post(
        f'{ha_url}/api/services/media_player/play_media',
        headers={'Authorization': f'Bearer {ha_token}', 'Content-Type': 'application/json'},
        json={
            'entity_id': entity_id,
            'media_content_id': media_url,
            'media_content_type': 'video/mp4',
            'extra': {'title': title},
        },
        timeout=5,
    )
    return resp.status_code in (200, 201)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return Response(MAIN_HTML, mimetype='text/html')


@app.route('/api/feeds')
def api_feeds():
    with _cache_lock:
        data = list(_cache.values())
    return jsonify(data)


@app.route('/api/players')
def api_players():
    return jsonify(get_media_players())


@app.route('/api/play', methods=['POST'])
def api_play():
    data = request.json or {}
    entity_id = data.get('entity_id', '')
    url = data.get('url', '')
    title = data.get('title', '')
    if not entity_id or not url:
        return jsonify({'ok': False, 'error': 'Missing entity_id or url'}), 400
    try:
        ok = play_on_device(entity_id, url, title)
        return jsonify({'ok': ok})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    if not _refresh_status['running']:
        threading.Thread(target=refresh_all_feeds, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/status')
def api_status():
    return jsonify({
        'feeds':   len(_cache),
        'running': _refresh_status['running'],
        'last':    _refresh_status['last'],
        'error':   _refresh_status['error'],
    })


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

MAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Video Podcasts</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;height:100vh;overflow:hidden}
  #sidebar{width:260px;min-width:260px;background:#1a1a1a;border-right:1px solid #333;display:flex;flex-direction:column;overflow:hidden}
  #sidebar-header{padding:16px;border-bottom:1px solid #333;display:flex;justify-content:space-between;align-items:center}
  #sidebar-header h1{font-size:1em;color:#fff}
  #refresh-btn{background:#333;border:none;color:#aaa;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:.8em}
  #refresh-btn:hover{background:#444;color:#fff}
  #refresh-btn.spinning{color:#4fc3f7}
  #feed-list{overflow-y:auto;flex:1}
  .feed-item{display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;border-bottom:1px solid #222;transition:background .15s}
  .feed-item:hover{background:#222}
  .feed-item.active{background:#1565c0}
  .feed-thumb{width:44px;height:44px;border-radius:6px;object-fit:cover;background:#333;flex-shrink:0}
  .feed-thumb-placeholder{width:44px;height:44px;border-radius:6px;background:#333;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:1.2em}
  .feed-meta{overflow:hidden}
  .feed-name{font-size:.85em;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .feed-count{font-size:.75em;color:#888;margin-top:2px}
  .feed-error{font-size:.72em;color:#ef9a9a;margin-top:2px}
  #main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  #main-header{padding:16px 20px;border-bottom:1px solid #333;background:#161616}
  #main-header h2{font-size:1em;color:#fff}
  #main-header p{font-size:.8em;color:#888;margin-top:4px}
  #episodes{overflow-y:auto;flex:1;padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;align-content:start}
  .ep-card{background:#1a1a1a;border-radius:8px;overflow:hidden;cursor:pointer;transition:transform .15s,box-shadow .15s;border:1px solid #2a2a2a}
  .ep-card:hover{transform:translateY(-2px);box-shadow:0 4px 20px rgba(0,0,0,.5);border-color:#444}
  .ep-thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#222;display:block}
  .ep-thumb-placeholder{width:100%;aspect-ratio:16/9;background:#222;display:flex;align-items:center;justify-content:center;font-size:2em;color:#555}
  .ep-body{padding:12px}
  .ep-title{font-size:.85em;font-weight:600;line-height:1.3;margin-bottom:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .ep-meta{font-size:.75em;color:#888;display:flex;gap:10px}
  .ep-actions{padding:10px 12px;border-top:1px solid #222;display:flex;gap:8px;align-items:center}
  select.device-select{flex:1;background:#222;border:1px solid #444;color:#eee;padding:5px 8px;border-radius:4px;font-size:.8em}
  .play-btn{background:#1565c0;border:none;color:#fff;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:.8em;font-weight:600;white-space:nowrap}
  .play-btn:hover{background:#1976d2}
  .play-btn:disabled{background:#333;color:#666;cursor:default}
  #empty{color:#555;text-align:center;margin-top:60px;grid-column:1/-1;font-size:.9em}
  #toast{position:fixed;bottom:24px;right:24px;background:#1565c0;color:#fff;padding:10px 18px;border-radius:6px;font-size:.85em;display:none;z-index:999}
  #toast.err{background:#c62828}
  .loading{color:#555;text-align:center;margin-top:60px;grid-column:1/-1}
</style>
</head>
<body>
<div id="sidebar">
  <div id="sidebar-header">
    <h1>📺 Video Podcasts</h1>
    <button id="refresh-btn" onclick="refreshFeeds()">↻ Refresh</button>
  </div>
  <div id="feed-list"></div>
</div>
<div id="main">
  <div id="main-header">
    <h2 id="feed-title">Select a feed</h2>
    <p id="feed-desc"></p>
  </div>
  <div id="episodes"><div class="loading">Loading…</div></div>
</div>
<div id="toast"></div>

<script>
let feeds = [];
let players = [];
let activeFeedId = null;

async function load() {
  const [feedsRes, playersRes] = await Promise.all([
    fetch('api/feeds').then(r => r.json()),
    fetch('api/players').then(r => r.json()),
  ]);
  feeds = feedsRes;
  players = playersRes;
  renderSidebar();
  if (feeds.length > 0) selectFeed(feeds[0].id);
  else document.getElementById('episodes').innerHTML = '<div id="empty">No feeds configured.<br>Add feeds in the addon configuration.</div>';
}

function renderSidebar() {
  const list = document.getElementById('feed-list');
  list.innerHTML = feeds.map(f => `
    <div class="feed-item${f.id === activeFeedId ? ' active' : ''}" onclick="selectFeed('${f.id}')">
      ${f.image
        ? `<img class="feed-thumb" src="${esc(f.image)}" onerror="this.style.display='none'" loading="lazy">`
        : `<div class="feed-thumb-placeholder">📺</div>`}
      <div class="feed-meta">
        <div class="feed-name">${esc(f.name)}</div>
        <div class="feed-count">${f.episodes.length} episodes</div>
        ${f.error ? `<div class="feed-error">⚠ ${esc(f.error)}</div>` : ''}
      </div>
    </div>`).join('');
}

function selectFeed(id) {
  activeFeedId = id;
  const feed = feeds.find(f => f.id === id);
  if (!feed) return;
  document.getElementById('feed-title').textContent = feed.name;
  document.getElementById('feed-desc').textContent = feed.description || '';
  renderEpisodes(feed);
  document.querySelectorAll('.feed-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.feed-item').forEach(el => {
    if (el.onclick.toString().includes(id)) el.classList.add('active');
  });
  renderSidebar();
}

function renderEpisodes(feed) {
  const container = document.getElementById('episodes');
  if (!feed.episodes.length) {
    container.innerHTML = '<div id="empty">No video episodes found in this feed.</div>';
    return;
  }
  const deviceOptions = players.length
    ? players.map(p => `<option value="${esc(p.entity_id)}">${esc(p.name)}</option>`).join('')
    : '<option value="">No devices found</option>';
  container.innerHTML = feed.episodes.map(ep => `
    <div class="ep-card">
      ${ep.thumbnail
        ? `<img class="ep-thumb" src="${esc(ep.thumbnail)}" loading="lazy" onerror="this.parentNode.querySelector('.ep-thumb-placeholder').style.display='flex';this.remove()">`
        : ''}
      <div class="ep-thumb-placeholder" style="display:${ep.thumbnail ? 'none' : 'flex'}">▶</div>
      <div class="ep-body">
        <div class="ep-title">${esc(ep.title)}</div>
        <div class="ep-meta">
          ${ep.published ? `<span>${esc(ep.published)}</span>` : ''}
          ${ep.duration ? `<span>${esc(ep.duration)}</span>` : ''}
        </div>
      </div>
      <div class="ep-actions">
        <select class="device-select">${deviceOptions}</select>
        <button class="play-btn" onclick="playEpisode(this,'${esc(ep.url)}','${esc(ep.title.replace(/'/g,"\\'"))}')"
          ${!players.length ? 'disabled' : ''}>▶ Play</button>
      </div>
    </div>`).join('');
}

async function playEpisode(btn, url, title) {
  const card = btn.closest('.ep-card');
  const entityId = card.querySelector('.device-select').value;
  if (!entityId) return;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const res = await fetch('api/play', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({entity_id: entityId, url, title}),
    });
    const data = await res.json();
    if (data.ok) toast('Playing on device');
    else toast('Failed: ' + (data.error || 'unknown'), true);
  } catch(e) {
    toast('Error: ' + e.message, true);
  }
  btn.disabled = false;
  btn.textContent = '▶ Play';
}

async function refreshFeeds() {
  const btn = document.getElementById('refresh-btn');
  btn.classList.add('spinning');
  btn.textContent = '↻ Refreshing…';
  await fetch('api/refresh', {method: 'POST'});
  await new Promise(r => setTimeout(r, 2000));
  await load();
  btn.classList.remove('spinning');
  btn.textContent = '↻ Refresh';
  toast('Feeds refreshed');
}

function toast(msg, err=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = err ? 'err' : '';
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

load();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    os.makedirs(SHARE_DIR, exist_ok=True)
    load_cache()
    threading.Thread(target=refresh_loop, daemon=True).start()
    log.info(f'Starting on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False)
