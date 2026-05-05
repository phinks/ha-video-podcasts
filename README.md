# Video Podcasts — Home Assistant Add-on

A Home Assistant add-on that aggregates video podcast feeds and plays episodes directly on your HA media player devices.

## Features

- Add YouTube channels by name, `@handle`, or URL
- Add any yt-dlp-compatible source (Patreon, Vimeo, etc.)
- Two fetch modes per feed: **RSS** (15 latest episodes) or **Full catalog** (entire back-catalog via yt-dlp)
- Downloads episodes to your HA media library
- Play directly to any HA media player (Chromecast, Sonos, etc.)
- Embedded panel in the HA sidebar

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the menu (⋮) and choose **Repositories**
3. Add: `https://github.com/phinks/ha-video-podcasts`
4. Find **Video Podcasts** in the store and install it

## Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `ha_url` | Home Assistant URL | `http://homeassistant:8123` |
| `ha_token` | Long-lived access token | _(required)_ |
| `refresh_interval` | Feed refresh interval in seconds | `3600` |
| `media_path` | Where downloaded videos are stored | `/media/podcasts` |
| `feeds` | List of feeds (managed via the UI) | — |

### Feed options

Each feed has:
- `name` — display name
- `url` — feed or playlist URL
- `method` — `rss` (15 latest) or `ytdlp` (full catalog)

Feeds are best managed through the add-on's own UI rather than editing the configuration directly.

## Usage

1. Open the **Video Podcasts** panel in the HA sidebar
2. Click **+** to add a channel — enter a YouTube `@handle`, channel name, or paste any URL
3. Choose **RSS (15 latest)** or **Full catalog**
4. Episodes appear in the feed list — click **Download** then **Play** to send to a media player

## Storage

Downloaded videos are stored at `/media/podcasts/<channel name>/` (configurable via `media_path`). They are accessible in HA under **Media → Local Media → podcasts**.
