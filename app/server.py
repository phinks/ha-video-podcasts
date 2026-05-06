#!/usr/bin/env python3
"""Video Podcast aggregator — Flask app served via HA ingress."""

import os
import re
import json
import time
import hashlib
import threading
import datetime
import logging

import feedparser
import requests
from urllib.parse import urlencode, urlparse
from flask import Flask, jsonify, request, Response

logging.basicConfig(level=logging.INFO, format='[podcasts] %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

SHARE_DIR              = '/share/podcasts'
CACHE_FILE             = os.path.join(SHARE_DIR, 'cache.json')
PATREON_COOKIES_FILE   = os.path.join(SHARE_DIR, 'patreon_cookies.txt')
OPTIONS           = '/data/options.json'
PORT              = 8099


def _read_media_dir():
    try:
        with open(OPTIONS) as f:
            v = json.load(f).get('media_path', '').strip()
        return v or '/media/podcasts'
    except Exception:
        return '/media/podcasts'

MEDIA_DIR = _read_media_dir()

_cache_lock = threading.Lock()
_cache      = {}
_refresh_status = {'running': False, 'last': None, 'error': None}

_dl_lock   = threading.Lock()
_downloads = {}   # ep_id -> {status, path, local_url, error}


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
    opts = get_options()
    base = opts.get('feeds', [])
    try:
        base = json.loads(os.environ.get('FEEDS', json.dumps(base)))
    except Exception:
        pass
    extra = load_extra_feeds()
    seen = {f.get('url') for f in base}
    return base + [f for f in extra if f.get('url') not in seen]


EXTRA_FEEDS_FILE = os.path.join(SHARE_DIR, 'extra_feeds.json')

def load_extra_feeds():
    try:
        with open(EXTRA_FEEDS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_extra_feeds(feeds):
    os.makedirs(SHARE_DIR, exist_ok=True)
    with open(EXTRA_FEEDS_FILE, 'w') as f:
        json.dump(feeds, f)
    _sync_feeds_to_options(feeds)


def _sync_feeds_to_options(extra_feeds):
    """Write extra feeds into HA addon options so they appear in the config page."""
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not supervisor_token:
        return
    try:
        opts = get_options()
        base = opts.get('feeds', [])
        seen_urls = {f.get('url') for f in base}
        merged = base + [f for f in extra_feeds if f.get('url') not in seen_urls]
        # Ensure name is always first so HA uses it as the list item label
        def _normalize(f):
            name = f.get('name') or f.get('url', '')
            d = {'name': name, 'url': f.get('url', '')}
            if f.get('method'):
                d['method'] = f['method']
            if f.get('username'):
                d['username'] = f['username']
            if f.get('password'):
                d['password'] = f['password']
            return d
        opts['feeds'] = [_normalize(f) for f in merged]
        requests.post(
            'http://supervisor/addons/self/options',
            headers={'Authorization': f'Bearer {supervisor_token}', 'Content-Type': 'application/json'},
            json={'options': opts},
            timeout=5,
        )
    except Exception as e:
        log.warning(f'Failed to sync feeds to HA options: {e}')


def lookup_channel(query):
    """Resolve a channel/URL to feed info. Handles YouTube and any other yt-dlp-supported source."""
    import yt_dlp
    query = query.strip()

    is_youtube = (
        'youtube.com' in query or
        'youtu.be' in query or
        query.startswith('@') or
        not (query.startswith('http://') or query.startswith('https://'))
    )

    if query.startswith('http://') or query.startswith('https://'):
        url = query
    elif query.startswith('@'):
        url = f'https://www.youtube.com/{query}'
    else:
        url = f'https://www.youtube.com/@{query}'

    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'playlistend': 1}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    channel_name = info.get('channel') or info.get('uploader') or info.get('title') or query

    if is_youtube:
        channel_id = info.get('channel_id') or info.get('uploader_id', '')
        if not channel_id or not channel_id.startswith('UC'):
            raise ValueError(f'Could not resolve YouTube channel ID for: {query}')
        feed_url = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
        uploads_url = f'https://www.youtube.com/playlist?list=UU{channel_id[2:]}'
        return {'name': channel_name, 'feed_url': feed_url, 'uploads_url': uploads_url,
                'channel_id': channel_id, 'is_youtube': True}
    else:
        return {'name': channel_name, 'url': url, 'is_youtube': False}


def feed_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

VIDEO_EXTS = ('.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi', '.m3u8')

def extract_video_url(entry):
    yt_id = entry.get('yt_videoid', '')
    if yt_id:
        return f'https://www.youtube.com/watch?v={yt_id}'
    for enc in entry.get('enclosures', []):
        mime = enc.get('type', '')
        url = enc.get('href', '') or enc.get('url', '')
        if mime.startswith('video/') or mime in ('application/x-mpegURL',):
            return url
    for enc in entry.get('enclosures', []):
        url = enc.get('href', '') or enc.get('url', '')
        if any(url.lower().endswith(ext) for ext in VIDEO_EXTS):
            return url
    for mc in entry.get('media_content', []):
        url = mc.get('url', '')
        mime = mc.get('type', '')
        medium = mc.get('medium', '')
        if medium == 'video' or mime.startswith('video/') or any(url.lower().endswith(ext) for ext in VIDEO_EXTS):
            return url
    for enc in entry.get('enclosures', []):
        url = enc.get('href', '') or enc.get('url', '')
        if url:
            return url
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
        raw = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        raw.raise_for_status()
        parsed = feedparser.parse(raw.content)
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

        episodes.sort(key=lambda e: e['published'] or '0000-00-00', reverse=True)

        return {
            'id':          feed_id(url),
            'url':         url,
            'name':        name_override or feed_info.get('title', url),
            'description': feed_info.get('subtitle', ''),
            'image':       feed_image,
            'updated':     datetime.datetime.now(datetime.UTC).isoformat(),
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
            'updated':  datetime.datetime.now(datetime.UTC).isoformat(),
        }


def _cookie_path(fid):
    os.makedirs(os.path.join(SHARE_DIR, 'cookies'), exist_ok=True)
    return os.path.join(SHARE_DIR, 'cookies', f'{fid}.txt')


def fetch_feed_ytdlp(url, name_override='', username='', password='', fid=None):
    import yt_dlp
    log.info(f'Fetching (yt-dlp) {url}')
    fid = fid or feed_id(url)
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'ignoreerrors': True,
        'cookiefile': _cookie_path(fid),
    }
    if username:
        ydl_opts['username'] = username
    if password:
        ydl_opts['password'] = password
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise Exception('No info returned from yt-dlp')

        entries_list = list(info.get('entries') or [])

        channel_name = name_override or info.get('channel') or info.get('uploader') or info.get('title', url)
        channel_thumb = ''
        for t in reversed(info.get('thumbnails', [])):
            if t.get('url'):
                channel_thumb = t['url']
                break

        episodes = []
        for entry in entries_list:
            if not entry:
                continue
            vid_id = entry.get('id', '')
            # Use entry URL directly; fall back to YouTube URL construction for YT playlists
            video_url = (entry.get('url') or entry.get('webpage_url') or
                         (f'https://www.youtube.com/watch?v={vid_id}' if vid_id else None))
            if not video_url:
                continue
            thumbnail = (entry.get('thumbnail') or
                         (f'https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg' if vid_id else ''))

            duration = ''
            dur_secs = entry.get('duration')
            if dur_secs:
                try:
                    h, r = divmod(int(dur_secs), 3600)
                    m, s = divmod(r, 60)
                    duration = f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'
                except Exception:
                    pass

            upload_date = entry.get('upload_date', '')
            pub_str = ''
            if upload_date and len(upload_date) == 8:
                pub_str = f'{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}'

            title = entry.get('title') or ''
            if not title:
                slug = video_url.rstrip('/').split('/')[-1].split('?')[0]
                slug = re.sub(r'-\d+$', '', slug)
                title = slug.replace('-', ' ').title() or 'Untitled'

            episodes.append({
                'id':          feed_id(vid_id or video_url),
                'title':       title,
                'description': entry.get('description', ''),
                'published':   pub_str,
                'duration':    duration,
                'url':         video_url,
                'thumbnail':   thumbnail,
            })

        episodes.sort(key=lambda e: e['published'] or '0000-00-00', reverse=True)
        log.info(f'yt-dlp fetched {len(episodes)} episodes from {url}')
        return {
            'id':       feed_id(url),
            'url':      url,
            'name':     channel_name,
            'description': '',
            'image':    channel_thumb,
            'updated':  datetime.datetime.now(datetime.UTC).isoformat(),
            'episodes': episodes,
            'error':    None,
            'method':   'ytdlp',
        }
    except Exception as e:
        log.warning(f'yt-dlp fetch failed ({url}): {e}')
        return {
            'id':       feed_id(url),
            'url':      url,
            'name':     name_override or url,
            'episodes': [],
            'error':    str(e),
            'updated':  datetime.datetime.now(datetime.UTC).isoformat(),
            'method':   'ytdlp',
        }


def refresh_all_feeds():
    global _cache, _refresh_status
    _refresh_status['running'] = True
    _refresh_status['error'] = None
    log.info('Refreshing feeds...')
    with _cache_lock:
        old_cache = dict(_cache)
    new_cache = {}
    for fc in get_feeds_config():
        url = fc.get('url', '').strip()
        if not url:
            continue
        method = fc.get('method', 'rss')
        if method == 'ytdlp':
            result = fetch_feed_ytdlp(url, fc.get('name', ''),
                                      username=fc.get('username', ''),
                                      password=fc.get('password', ''))
        else:
            result = fetch_feed(url, fc.get('name', ''))
        fid = result['id']
        if result.get('error') and fid in old_cache and old_cache[fid].get('episodes'):
            merged = dict(old_cache[fid])
            merged['error'] = result['error']
            new_cache[fid] = merged
        else:
            new_cache[fid] = result
    with _cache_lock:
        _cache = new_cache
    _refresh_status['running'] = False
    _refresh_status['last'] = datetime.datetime.now(datetime.UTC).isoformat()
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
# Download helpers
# ---------------------------------------------------------------------------

def sanitize_path(s, max_len=80):
    """Strip filesystem-unsafe chars and trim length."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:max_len]


def local_media_url(path):
    """Convert /media/foo/bar.mp4 to media-source://media_source/local/foo/bar.mp4"""
    rel = path[len('/media/'):]
    return f'media-source://media_source/local/{rel}'


def scan_media_dir():
    """Re-populate _downloads from files already on disk (survives restarts)."""
    if not os.path.exists(MEDIA_DIR):
        return
    count = 0
    for channel_dir in os.listdir(MEDIA_DIR):
        fdir = os.path.join(MEDIA_DIR, channel_dir)
        if not os.path.isdir(fdir):
            continue
        for fname in os.listdir(fdir):
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in ('.mp4', '.mkv', '.webm', '.m4v', '.mov'):
                continue
            # New format: "<ep_id>_<title>.mp4" — ep_id is always 12 hex chars
            ep_id = stem.split('_', 1)[0] if '_' in stem else stem
            path = os.path.join(fdir, fname)
            with _dl_lock:
                _downloads[ep_id] = {
                    'status': 'done',
                    'path': path,
                    'local_url': local_media_url(path),
                    'error': None,
                }
            count += 1
    log.info(f'Scanned media dir: {count} downloaded episodes')


def do_download(ep_id, url, title, fid):
    import yt_dlp
    with _cache_lock:
        feed_name = _cache.get(fid, {}).get('name', fid)
    channel_dir = sanitize_path(feed_name)
    safe_title  = sanitize_path(title)
    stem = f'{ep_id}_{safe_title}'

    out_dir = os.path.join(MEDIA_DIR, channel_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_tmpl = os.path.join(out_dir, f'{stem}.%(ext)s')

    # Look up credentials from feed config
    feed_creds = {}
    for fc in get_feeds_config():
        if feed_id(fc.get('url', '')) == fid:
            if fc.get('username'):
                feed_creds['username'] = fc['username']
            if fc.get('password'):
                feed_creds['password'] = fc['password']
            break

    with _dl_lock:
        _downloads[ep_id] = {'status': 'downloading', 'path': None, 'local_url': None, 'error': None}

    ydl_opts = {
        # H.264+AAC in MP4 — broadest Chromecast/TV compatibility
        'format': 'bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': out_tmpl,
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'cookiefile': _cookie_path(fid),
        **feed_creds,
    }

    if 'patreon.com' in url and os.path.exists(PATREON_COOKIES_FILE):
        ydl_opts['cookiefile'] = PATREON_COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        mp4_path = os.path.join(out_dir, f'{stem}.mp4')
        if not os.path.exists(mp4_path):
            for fname in os.listdir(out_dir):
                if fname.startswith(ep_id + '_') or fname.startswith(ep_id + '.'):
                    mp4_path = os.path.join(out_dir, fname)
                    break
        with _dl_lock:
            _downloads[ep_id] = {
                'status': 'done',
                'path': mp4_path,
                'local_url': local_media_url(mp4_path),
                'error': None,
            }
        log.info(f'Downloaded: {title}')
    except Exception as e:
        log.warning(f'Download failed ({title}): {e}')
        with _dl_lock:
            _downloads[ep_id] = {'status': 'error', 'path': None, 'local_url': None, 'error': str(e)}


# ---------------------------------------------------------------------------
# Patreon cookie management
# ---------------------------------------------------------------------------


@app.route('/api/patreon_cookies', methods=['POST'])
def api_patreon_cookies_upload():
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'No file'}), 400
    os.makedirs(SHARE_DIR, exist_ok=True)
    f.save(PATREON_COOKIES_FILE)
    log.info('Patreon cookies file saved')
    return jsonify({'ok': True})

@app.route('/api/patreon_cookies', methods=['DELETE'])
def api_patreon_cookies_delete():
    try:
        os.remove(PATREON_COOKIES_FILE)
    except FileNotFoundError:
        pass
    return jsonify({'ok': True})

@app.route('/api/patreon_status')
def api_patreon_status():
    return jsonify({'cookies_present': os.path.exists(PATREON_COOKIES_FILE)})


# ---------------------------------------------------------------------------
# HA helpers
# ---------------------------------------------------------------------------

def _ha_creds():
    """Return (url, token) for HA API access.

    Prefer the explicitly configured token — the supervisor proxy token is
    unreliable for local addons and often returns 401 against the core API.
    """
    opts = get_options()
    configured_token = opts.get('ha_token', '')
    if configured_token:
        return opts.get('ha_url', 'http://homeassistant:8123').rstrip('/'), configured_token
    supervisor_token = os.environ.get('SUPERVISOR_TOKEN', '')
    if supervisor_token:
        return 'http://supervisor/core', supervisor_token
    return opts.get('ha_url', 'http://homeassistant:8123').rstrip('/'), ''


def get_media_players():
    ha_url, ha_token = _ha_creds()
    try:
        resp = requests.get(
            f'{ha_url}/api/states',
            headers={'Authorization': f'Bearer {ha_token}'},
            timeout=5,
        )
        resp.raise_for_status()
        all_players = {}
        for s in resp.json():
            if not s['entity_id'].startswith('media_player.'):
                continue
            if s.get('state') == 'unavailable':
                continue
            attrs = s.get('attributes', {})
            name = attrs.get('friendly_name', s['entity_id'])
            if not name:
                continue
            is_ma = 'Music Assistant Queue' in attrs.get('source_list', [])
            existing = all_players.get(name)
            if existing is None or (is_ma and not existing['is_ma']):
                all_players[name] = {
                    'entity_id': s['entity_id'],
                    'name': name,
                    'state': s.get('state', ''),
                    'is_ma': is_ma,
                }
        players = [{'entity_id': v['entity_id'], 'name': v['name'], 'state': v['state']}
                   for v in all_players.values()]
        return sorted(players, key=lambda x: x['name'])
    except Exception as e:
        log.warning(f'Failed to fetch media players: {e}')
        return []


def play_on_device(entity_id, media_url, title):
    ha_url, ha_token = _ha_creds()
    is_youtube = 'youtube.com' in media_url or 'youtu.be' in media_url
    content_type = 'url' if is_youtube else 'video/mp4'
    resp = requests.post(
        f'{ha_url}/api/services/media_player/play_media',
        headers={'Authorization': f'Bearer {ha_token}', 'Content-Type': 'application/json'},
        json={
            'entity_id': entity_id,
            'media_content_id': media_url,
            'media_content_type': content_type,
            'extra': {'title': title},
        },
        timeout=5,
    )
    if resp.status_code not in (200, 201):
        log.warning(f'play_media failed {resp.status_code}: {resp.text[:200]}')
        raise Exception(f'HA returned {resp.status_code}: {resp.text[:120]}')
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.after_request
def add_headers(r):
    r.headers['Cache-Control'] = 'no-store'
    r.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src * data: blob:; "
        "connect-src *;"
    )
    return r


@app.route('/')
def index():
    return Response(MAIN_HTML, mimetype='text/html')


@app.route('/app.js')
def app_js():
    return Response(APP_JS, mimetype='application/javascript')


@app.route('/api/feeds')
def api_feeds():
    with _cache_lock:
        data = list(_cache.values())
    with _dl_lock:
        dl_snap = dict(_downloads)
    for feed in data:
        for ep in feed.get('episodes', []):
            dl = dl_snap.get(ep['id'])
            ep['local_url'] = dl['local_url'] if (dl and dl.get('local_url')) else None
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


@app.route('/api/stream', methods=['POST'])
def api_stream():
    import yt_dlp
    data = request.json or {}
    url       = data.get('url', '')
    title     = data.get('title', '')
    entity_id = data.get('entity_id', '')
    fid       = data.get('feed_id', 'default')
    if not url or not entity_id:
        return jsonify({'ok': False, 'error': 'Missing url or entity_id'}), 400
    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best[height<=1080]/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
        }
        if 'patreon.com' in url and os.path.exists(PATREON_COOKIES_FILE):
            ydl_opts['cookiefile'] = PATREON_COOKIES_FILE
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        stream_url = info.get('url') or ''
        if not stream_url:
            for fmt in reversed(info.get('formats', [])):
                if fmt.get('url') and fmt.get('vcodec') != 'none':
                    stream_url = fmt['url']
                    break
        if not stream_url:
            return jsonify({'ok': False, 'error': 'Could not extract stream URL'}), 500
        play_on_device(entity_id, stream_url, title)
        log.info(f'Streaming to {entity_id}: {title}')
        return jsonify({'ok': True})
    except Exception as e:
        log.warning(f'Stream failed ({title}): {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.json or {}
    ep_id  = data.get('ep_id', '')
    url    = data.get('url', '')
    title  = data.get('title', '')
    fid    = data.get('feed_id', 'default')
    if not ep_id or not url:
        return jsonify({'ok': False, 'error': 'Missing ep_id or url'}), 400
    with _dl_lock:
        if _downloads.get(ep_id, {}).get('status') == 'downloading':
            return jsonify({'ok': True, 'status': 'already_downloading'})
    threading.Thread(target=do_download, args=(ep_id, url, title, fid), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/downloads')
def api_downloads():
    with _dl_lock:
        return jsonify(dict(_downloads))


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    if not _refresh_status['running']:
        threading.Thread(target=refresh_all_feeds, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/feeds_config')
def api_feeds_config():
    feeds = get_feeds_config()
    return jsonify([{k: ('***' if k == 'password' else v) for k, v in f.items()} for f in feeds])


@app.route('/api/status')
def api_status():
    return jsonify({
        'feeds':   len(_cache),
        'running': _refresh_status['running'],
        'last':    _refresh_status['last'],
        'error':   _refresh_status['error'],
    })


@app.route('/api/lookup_channel', methods=['POST'])
def api_lookup_channel():
    data = request.json or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'ok': False, 'error': 'Missing query'}), 400
    try:
        result = lookup_channel(query)
        return jsonify({'ok': True, **result})
    except Exception as e:
        log.warning(f'Channel lookup failed ({query}): {e}')
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/add_feed', methods=['POST'])
def api_add_feed():
    data = request.json or {}
    url = data.get('url', '').strip()
    name = data.get('name', '').strip()
    if not url:
        return jsonify({'ok': False, 'error': 'Missing url'}), 400
    extra = load_extra_feeds()
    if any(f.get('url') == url for f in extra):
        return jsonify({'ok': True, 'note': 'already_added'})
    method = data.get('method', 'rss')
    entry = {'name': name or url, 'url': url, 'method': method}
    if data.get('username'):
        entry['username'] = data['username']
    if data.get('password'):
        entry['password'] = data['password']
    extra.append(entry)
    save_extra_feeds(extra)
    threading.Thread(target=refresh_all_feeds, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/remove_feed', methods=['POST'])
def api_remove_feed():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'ok': False, 'error': 'Missing url'}), 400
    extra = [f for f in load_extra_feeds() if f.get('url') != url]
    save_extra_feeds(extra)
    threading.Thread(target=refresh_all_feeds, daemon=True).start()
    return jsonify({'ok': True})


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
  #sidebar-header{padding:12px 16px;border-bottom:1px solid #333;display:flex;justify-content:space-between;align-items:center;gap:6px}
  #sidebar-header h1{font-size:1em;color:#fff;flex:1}
  #refresh-btn,#add-btn{background:#333;border:none;color:#aaa;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:.8em}
  #refresh-btn:hover,#add-btn:hover{background:#444;color:#fff}
  #refresh-btn.spinning{color:#4fc3f7}
  #add-btn.active{background:#1565c0;color:#fff}
  #add-panel{background:#111;border-bottom:1px solid #333;padding:12px 14px;display:none}
  #add-panel.open{display:block}
  #channel-input{width:100%;background:#222;border:1px solid #444;color:#eee;padding:7px 10px;border-radius:4px;font-size:.82em;margin-bottom:8px;box-sizing:border-box}
  #channel-input::placeholder{color:#666}
  #lookup-btn{background:#1565c0;border:none;color:#fff;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:.8em;font-weight:600}
  #lookup-btn:disabled{background:#333;color:#666}
  #lookup-result{margin-top:10px;font-size:.8em;display:none}
  #lookup-result .ch-name{font-weight:600;color:#fff;margin-bottom:4px}
  #lookup-result .ch-url{color:#888;word-break:break-all;margin-bottom:8px;font-size:.9em}
  #lookup-result .add-feed-btn{background:#2e7d32;border:none;color:#fff;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:.8em;font-weight:600}
  #lookup-result .err{color:#ef9a9a}
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
  .ep-card{background:#1a1a1a;border-radius:8px;border:1px solid #2a2a2a;transition:transform .15s,box-shadow .15s;display:flex;flex-direction:column}
  .ep-card:hover{transform:translateY(-2px);box-shadow:0 4px 20px rgba(0,0,0,.5);border-color:#444}
  .ep-thumb-wrap{position:relative;cursor:pointer;overflow:hidden;border-radius:8px 8px 0 0}
  .ep-thumb-wrap:hover .ep-thumb-overlay{opacity:1}
  .ep-thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#222;display:block}
  .ep-thumb-placeholder{width:100%;aspect-ratio:16/9;background:#222;display:flex;align-items:center;justify-content:center;font-size:2em;color:#555}
  .ep-thumb-overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:2.5em;background:rgba(0,0,0,.45);opacity:0;transition:opacity .15s;pointer-events:none}
  .ep-body{padding:12px}
  .ep-title{font-size:.85em;font-weight:600;line-height:1.3;margin-bottom:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .ep-meta{font-size:.75em;color:#888;display:flex;gap:10px}
  .ep-actions{padding:10px 12px;border-top:1px solid #222;display:flex;flex-direction:column;gap:6px;flex-shrink:0}
  select.device-select{width:100%;background:#222;border:1px solid #444;color:#eee;padding:6px 8px;border-radius:4px;font-size:.8em}
  .play-btn{background:#2e7d32;border:none;color:#fff;padding:8px 14px;border-radius:4px;cursor:pointer;font-size:.85em;font-weight:600;width:100%}
  .play-btn:hover{background:#388e3c}
  .play-btn:disabled{background:#333;color:#666;cursor:default}
  .dl-btn{background:#1565c0;border:none;color:#fff;padding:8px 14px;border-radius:4px;cursor:pointer;font-size:.85em;font-weight:600;width:100%}
  .dl-btn:hover{background:#1976d2}
  .dl-btn:disabled{opacity:.5;cursor:default}
  .dl-status{font-size:.82em;text-align:center;padding:4px 0}
  .dl-progress{color:#4fc3f7}
  .dl-done{color:#81c784}
  .dl-err{color:#ef9a9a}
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
    <button id="add-btn">+ Add</button>
    <button id="refresh-btn">↻</button>
  </div>
  <div id="add-panel">
    <input id="channel-input" type="text" placeholder="YouTube @handle, channel name, or any URL (Patreon, Vimeo…)" oninput="onChannelInput(this.value)">
    <button id="lookup-btn">Look up</button>
    <div id="lookup-result"></div>
  </div>
  <div id="feed-list"></div>
  <div id="patreon-connect" style="padding:10px 14px;border-top:1px solid #333;font-size:.8em;margin-top:auto">
    <div id="patreon-status"></div>
  </div>
</div>
<div id="main">
  <div id="main-header">
    <h2 id="feed-title">Select a feed</h2>
    <p id="feed-desc"></p>
  </div>
  <div id="episodes"><div class="loading" id="status-msg">Loading…</div></div>
</div>
<div id="toast"></div>

<script src="app.js"></script>
</body>
</html>"""

APP_JS = """
let feeds = [];
let players = [];
let activeFeedId = null;
let downloads = {};
let dlPollTimer = null;

function setStatus(msg) {
  const el = document.getElementById('status-msg');
  if (el) el.textContent = msg;
}

async function fetchJ(url, opts={}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 12000);
  try {
    const r = await fetch(url, {...opts, signal: ctrl.signal});
    clearTimeout(timer);
    if (!r.ok) throw new Error(url + ' returned ' + r.status);
    return r.json();
  } catch(e) {
    clearTimeout(timer);
    throw e;
  }
}

async function load() {
  setStatus('Fetching feeds…');
  try {
    const [feedsRes, playersRes] = await Promise.all([
      fetchJ('api/feeds'),
      fetchJ('api/players'),
    ]);
    feeds = feedsRes;
    players = playersRes;
  } catch(e) {
    setStatus('Failed to load: ' + e.message);
    return;
  }
  setStatus('Loading downloads…');
  try {
    downloads = await fetchJ('api/downloads');
    if (Object.values(downloads).some(d => d.status === 'downloading')) startDlPoll();
  } catch(e) { /* non-fatal */ }
  renderSidebar();
  if (feeds.length > 0) selectFeed(feeds[0].id);
  else document.getElementById('episodes').innerHTML = '<div id="empty">No feeds configured.<br>Add feeds in the addon configuration.</div>';
}

function renderSidebar() {
  const list = document.getElementById('feed-list');
  list.innerHTML = feeds.map(f => `
    <div class="feed-item${f.id === activeFeedId ? ' active' : ''}" data-feed-id="${esc(f.id)}">
      ${f.image
        ? `<img class="feed-thumb" src="${esc(f.image)}" loading="lazy">`
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
  renderSidebar();
}

function dlStateForEp(ep) {
  const dl = downloads[ep.id];
  if (dl && dl.status === 'done') return 'done';
  if (dl && dl.status === 'downloading') return 'downloading';
  if (dl && dl.status === 'error') return 'error';
  if (ep.local_url) return 'done';
  return '';
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

  container.innerHTML = feed.episodes.map(ep => {
    const state = dlStateForEp(ep);
    let actionsHtml;
    const btnStyle = 'display:block;width:100%;padding:9px 12px;border:none;border-radius:4px;cursor:pointer;font-size:14px;font-weight:600;text-align:center';
    if (state === 'downloading') {
      actionsHtml = `<span style="display:block;color:#4fc3f7;text-align:center;padding:6px 0;font-size:13px">↻ Downloading…</span>`;
    } else if (state === 'done') {
      actionsHtml = `
        <select class="device-select">${deviceOptions}</select>
        <button data-action="play" style="${btnStyle};background:#2e7d32;color:#fff" ${!players.length ? 'disabled' : ''}>▶ Play</button>`;
    } else if (state === 'error') {
      actionsHtml = `<button data-action="download" style="${btnStyle};background:#c62828;color:#fff">↺ Retry</button>`;
    } else {
      actionsHtml = `
        <select class="device-select">${deviceOptions}</select>
        <button data-action="stream" style="${btnStyle};background:#6a1b9a;color:#fff;margin-bottom:6px" ${!players.length ? 'disabled' : ''}>▶ Stream</button>
        <button data-action="download" style="${btnStyle};background:#1565c0;color:#fff">⬇ Download</button>`;
    }
    return `
    <div class="ep-card" data-ep-id="${esc(ep.id)}" data-remote-url="${esc(ep.url)}" data-title="${esc(ep.title)}" data-feed-id="${esc(feed.id)}">
      <div class="ep-thumb-wrap">
        ${ep.thumbnail ? `<img class="ep-thumb" src="${esc(ep.thumbnail)}" loading="lazy">` : ''}
        <div class="ep-thumb-placeholder" style="display:${ep.thumbnail ? 'none' : 'flex'}">▶</div>
      </div>
      <div class="ep-body">
        <div class="ep-title">${esc(ep.title)}</div>
        <div class="ep-meta">
          ${ep.published ? `<span>${esc(ep.published)}</span>` : ''}
          ${ep.duration ? `<span>${esc(ep.duration)}</span>` : ''}
        </div>
      </div>
      <div class="ep-actions">${actionsHtml}</div>
    </div>`;
  }).join('');
}

async function playEpisode(card) {
  const playBtn = card.querySelector('.play-btn');
  const epId = card.dataset.epId;
  const title = card.dataset.title;
  const entityId = card.querySelector('.device-select')?.value;
  if (!entityId) { toast('Select a device first', true); return; }
  const dl = downloads[epId] || {};
  const url = dl.local_url;
  if (!url) { toast('Download the episode first', true); return; }
  if (playBtn) { playBtn.disabled = true; playBtn.textContent = '…'; }
  try {
    const data = await fetchJ('api/play', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({entity_id: entityId, url, title}),
    });
    if (data.ok) toast('▶ Playing on ' + entityId.split('.').pop().replace(/_/g,' '));
    else toast('Failed: ' + (data.error || 'unknown'), true);
  } catch(e) {
    toast('Error: ' + e.message, true);
  }
  if (playBtn) { playBtn.disabled = false; playBtn.textContent = '▶ Play'; }
}

async function streamEpisode(card) {
  const streamBtn = card.querySelector('[data-action="stream"]');
  const url = card.dataset.remoteUrl;
  const title = card.dataset.title;
  const feedId = card.dataset.feedId;
  const entityId = card.querySelector('.device-select')?.value;
  if (!entityId) { toast('Select a device first', true); return; }
  if (streamBtn) { streamBtn.disabled = true; streamBtn.textContent = '↻ Resolving…'; }
  try {
    const data = await fetchJ('api/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, title, entity_id: entityId, feed_id: feedId}),
    });
    if (data.ok) toast('▶ Streaming on ' + entityId.split('.').pop().replace(/_/g,' '));
    else toast('Stream failed: ' + (data.error || 'unknown'), true);
  } catch(e) {
    toast('Error: ' + e.message, true);
  }
  if (streamBtn) { streamBtn.disabled = false; streamBtn.textContent = '▶ Stream'; }
}

async function downloadEp(card) {
  const epId = card.dataset.epId;
  const url = card.dataset.remoteUrl;
  const title = card.dataset.title;
  const feedId = card.dataset.feedId;
  const actionsDiv = card.querySelector('.ep-actions');
  actionsDiv.innerHTML = `<span class="dl-status dl-progress">↻ Starting…</span>`;
  try {
    const data = await fetchJ('api/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ep_id: epId, url, title, feed_id: feedId}),
    });
    if (!data.ok) throw new Error(data.error || 'unknown');
    downloads[epId] = {status: 'downloading', path: null, local_url: null, error: null};
    actionsDiv.innerHTML = `<span class="dl-status dl-progress">↻ Downloading…</span>`;
    startDlPoll();
  } catch(e) {
    toast('Download failed: ' + e.message, true);
    actionsDiv.innerHTML = `<button class="dl-btn" data-action="download">↺ Retry download</button>`;
  }
}

function updateCardDl(epId) {
  const card = document.querySelector(`.ep-card[data-ep-id="${epId}"]`);
  if (!card) return;
  const dl = downloads[epId] || {};
  const actionsDiv = card.querySelector('.ep-actions');
  if (!actionsDiv) return;
  const deviceOptions = players.map(p => `<option value="${esc(p.entity_id)}">${esc(p.name)}</option>`).join('');
  if (dl.status === 'downloading') {
    actionsDiv.innerHTML = `<span class="dl-status dl-progress">↻ Downloading…</span>`;
  } else if (dl.status === 'done') {
    actionsDiv.innerHTML = `
      <select class="device-select">${deviceOptions}</select>
      <button class="play-btn" data-action="play" ${!players.length ? 'disabled' : ''}>▶ Play</button>`;
    toast('Download complete — select a device and press Play');
  } else if (dl.status === 'error') {
    actionsDiv.innerHTML = `<button class="dl-btn" data-action="download">↺ Retry download</button>`;
    toast('Download failed', true);
  }
}

async function pollDownloads() {
  try {
    const data = await fetch('api/downloads').then(r => r.json());
    let anyActive = false;
    for (const [epId, dl] of Object.entries(data)) {
      const prev = downloads[epId] || {};
      if (prev.status !== dl.status) { downloads[epId] = dl; updateCardDl(epId); }
      else downloads[epId] = dl;
      if (dl.status === 'downloading') anyActive = true;
    }
    if (!anyActive) { clearInterval(dlPollTimer); dlPollTimer = null; }
  } catch(e) { /* ignore */ }
}

function startDlPoll() {
  if (!dlPollTimer) dlPollTimer = setInterval(pollDownloads, 3000);
}

async function refreshFeeds() {
  const btn = document.getElementById('refresh-btn');
  btn.classList.add('spinning');
  btn.textContent = '↻…';
  await fetch('api/refresh', {method: 'POST'});
  await new Promise(r => setTimeout(r, 2000));
  await load();
  btn.classList.remove('spinning');
  btn.textContent = '↻';
  toast('Feeds refreshed');
}

function toast(msg, err=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = err ? 'err' : '';
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3500);
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toggleAddPanel() {
  const panel = document.getElementById('add-panel');
  const btn = document.getElementById('add-btn');
  const open = panel.classList.toggle('open');
  btn.classList.toggle('active', open);
  if (open) document.getElementById('channel-input').focus();
}

function onChannelInput(val) {
  const q = val.trim();
  const isYT = q.includes('youtube.com') || q.includes('youtu.be') ||
               q.startsWith('@') || !q.startsWith('http');
  const btn = document.getElementById('lookup-btn');
  // Show "Look up" only for YouTube; hide it for other URLs (form appears instantly)
  btn.style.display = (!q || isYT) ? '' : 'none';
  if (!isYT && q.startsWith('http')) {
    const result = document.getElementById('lookup-result');
    const guessedName = q.replace(/^https?:\/\//, '').replace(/\/$/, '');
    result.style.display = 'block';
    result.innerHTML = `
      <div class="ch-url" style="margin-bottom:6px">${esc(q)}</div>
      <div style="display:flex;flex-direction:column;gap:5px">
        <input id="feed-dispname" type="text" placeholder="Display name" value="${esc(guessedName)}" style="background:#222;border:1px solid #444;color:#eee;padding:5px 8px;border-radius:4px;font-size:.82em">
        <input id="feed-username" type="text" placeholder="Username (optional)" style="background:#222;border:1px solid #444;color:#eee;padding:5px 8px;border-radius:4px;font-size:.82em">
        <input id="feed-password" type="password" placeholder="Password (optional)" style="background:#222;border:1px solid #444;color:#eee;padding:5px 8px;border-radius:4px;font-size:.82em">
        <button class="add-feed-btn" style="width:100%;background:#2e7d32" data-feed-url="${esc(q)}" data-feed-name="" data-method="ytdlp">+ Add (yt-dlp)</button>
      </div>`;
  } else if (!q) {
    const result = document.getElementById('lookup-result');
    result.style.display = 'none';
    result.innerHTML = '';
  }
}

async function lookupChannel() {
  const input = document.getElementById('channel-input');
  const btn = document.getElementById('lookup-btn');
  const result = document.getElementById('lookup-result');
  const query = input.value.trim();
  if (!query) return;

  btn.disabled = true;
  btn.textContent = 'Looking up…';
  result.style.display = 'none';
  result.innerHTML = '';
  try {
    const data = await fetchJ('api/lookup_channel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query}),
    });
    result.style.display = 'block';
    result.innerHTML = `
      <div class="ch-name">${esc(data.name)}</div>
      <div class="ch-url">${esc(data.feed_url)}</div>
      <div style="display:flex;gap:6px;margin-top:8px">
        <button class="add-feed-btn" style="flex:1" data-feed-url="${esc(data.feed_url)}" data-feed-name="${esc(data.name)}" data-method="rss">+ RSS (15 latest)</button>
        <button class="add-feed-btn" style="flex:1;background:#2e7d32" data-feed-url="${esc(data.uploads_url)}" data-feed-name="${esc(data.name)}" data-method="ytdlp">+ Full catalog</button>
      </div>`;
  } catch(e) {
    result.style.display = 'block';
    result.innerHTML = `<div class="err">⚠ ${esc(e.message)}</div>`;
  }
  btn.disabled = false;
  btn.textContent = 'Look up';
}

async function addFeed(url, name, method='rss', username='', password='') {
  try {
    await fetchJ('api/add_feed', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, name, method, username, password}),
    });
    toast('Feed added — refreshing…');
    document.getElementById('add-panel').classList.remove('open');
    document.getElementById('add-btn').classList.remove('active');
    document.getElementById('channel-input').value = '';
    document.getElementById('lookup-result').style.display = 'none';
    document.getElementById('lookup-btn').style.display = '';
    setTimeout(load, 2000);
  } catch(e) {
    toast('Failed to add feed: ' + e.message, true);
  }
}

// Single delegated listener — no inline onclick anywhere
document.addEventListener('click', function(e) {
  // Static header buttons
  if (e.target.id === 'refresh-btn') { refreshFeeds(); return; }
  if (e.target.id === 'add-btn') { toggleAddPanel(); return; }
  if (e.target.id === 'lookup-btn') { lookupChannel(); return; }

  // Feed list item
  const feedItem = e.target.closest('.feed-item[data-feed-id]');
  if (feedItem) { selectFeed(feedItem.dataset.feedId); return; }

  // Episode play (thumb or play button)
  const playEl = e.target.closest('[data-action="play"]');
  if (playEl) { const card = playEl.closest('.ep-card'); if (card) playEpisode(card); return; }

  // Episode stream button
  const streamEl = e.target.closest('[data-action="stream"]');
  if (streamEl) { const card = streamEl.closest('.ep-card'); if (card) streamEpisode(card); return; }

  // Episode download button
  const dlEl = e.target.closest('[data-action="download"]');
  if (dlEl) { const card = dlEl.closest('.ep-card'); if (card) downloadEp(card); return; }

  // Add-feed button in lookup result
  const addBtn = e.target.closest('.add-feed-btn[data-feed-url]');
  if (addBtn) {
    const username = (document.getElementById('feed-username') || {}).value || '';
    const password = (document.getElementById('feed-password') || {}).value || '';
    const dispName = (document.getElementById('feed-dispname') || {}).value || addBtn.dataset.feedName;
    addFeed(addBtn.dataset.feedUrl, dispName, addBtn.dataset.method || 'rss', username, password);
    return;
  }
});

document.addEventListener('keydown', function(e) {
  if (e.target.id === 'channel-input' && e.key === 'Enter') lookupChannel();
});

window.onerror = function(msg, src, line) {
  document.getElementById('episodes').innerHTML = '<div style="color:#ef9a9a;padding:20px">JS Error: ' + msg + ' (' + line + ')</div>';
};

async function checkPatreonStatus() {
  const el = document.getElementById('patreon-status');
  if (!el) return;
  try {
    const s = await fetchJ('api/patreon_status');
    if (!s.cookies_present) {
      el.innerHTML = `
        <div style="color:#aaa;margin-bottom:4px">Patreon: upload cookies for downloads</div>
        <input type="file" id="patreon-cookie-file" accept=".txt" style="font-size:.75em;color:#eee;width:100%">
        <button id="patreon-cookie-btn" style="margin-top:4px;width:100%;background:#f4845f;color:#fff;border:none;padding:4px;cursor:pointer;box-sizing:border-box">Upload cookies.txt</button>`;
      document.getElementById('patreon-cookie-btn').addEventListener('click', async () => {
        const f = document.getElementById('patreon-cookie-file').files[0];
        if (!f) { toast('Select a cookies.txt file first', true); return; }
        const fd = new FormData(); fd.append('file', f);
        try {
          await fetch('api/patreon_cookies', {method:'POST', body: fd});
          toast('✓ Patreon cookies uploaded');
          checkPatreonStatus();
        } catch(e) { toast('Upload failed: ' + e.message, true); }
      });
    } else {
      el.innerHTML = '<span style="color:#4caf50">✓ Patreon cookies loaded</span> <button id="patreon-cookie-clear" style="background:none;border:none;color:#888;cursor:pointer;font-size:.75em">remove</button>';
      document.getElementById('patreon-cookie-clear').addEventListener('click', async () => {
        await fetch('api/patreon_cookies', {method:'DELETE'});
        checkPatreonStatus();
      });
    }
  } catch(e) { el.innerHTML = ''; }
}

load();
checkPatreonStatus();
"""


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    os.makedirs(SHARE_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    scan_media_dir()
    load_cache()
    _sync_feeds_to_options(load_extra_feeds())
    threading.Thread(target=refresh_loop, daemon=True).start()
    log.info(f'Starting on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False)
