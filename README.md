# Telegram Radio Station

*[Русская версия](README.ru.md)*

A 24/7 internet radio station built on top of a Telegram channel's music
archive, with a synced re-broadcast into a Telegram video chat and a
Material Design 3 web player.

## Features

**Streaming**

- Continuous MP3 (128kbps) and lossless FLAC quality tiers from a single
  live source, selectable per listener
- HLS mirror alongside the plain live stream, giving the web player real
  seek support (rewind 10s, jump back to the live edge) that a raw chunked
  stream can't offer
- Optional ICY-metadata proxy for the FLAC/Ogg mount, for hardware
  streamers that only ever read classic ICY tags and never re-parse a live
  Ogg stream's own chained per-track headers -- opt-in per client, doesn't
  touch anyone who doesn't ask for it
- Simultaneous re-broadcast of the same audio into a Telegram group video
  call via RTMP, paired with a live-rendered video frame showing the
  current track, upcoming queue, and a subscribe call-to-action --
  regenerated on every tick rather than a static placeholder image

**Auto-curated queue**

- Tracks pulled at random from a Telegram channel's full audio archive, no
  manual playlist maintenance
- Duration filtering (skips short jingles/stingers and hour-long medley
  uploads alike)
- Vinyl-rip/track-listing artifact cleanup (side markers like `A2`/`B1`,
  leading track numbers) so titles don't show up mangled
- Layered ambient/soundtrack-cue detection so instrumental score/filler
  doesn't dominate the rotation, without catching real songs that merely
  share a release with one: channel hashtag conventions, title keywords,
  a Discogs style/genre check gated on the release's tracklist (not just
  its title) and overridden by an accompanying ordinary song genre
  (rock/pop/hip-hop/etc.) or an explicit featured-vocalist credit, plus an
  intro/outro/interview/live-version filter by title pattern
- Per-album daily play quota, so one release can't dominate an hour of
  airtime
- 48-hour repeat cooldown so the same track doesn't resurface too soon
- Every exclusion is logged with its specific reason, not just a bare ID
- Admin-uploaded tracks are exempt from the automatic queue-staleness sweep

**Metadata enrichment**

- Layered fallback chain for artist/title: embedded file tags, then
  Telegram's own per-message title/performer fields, then the channel's
  own description posts (parsed for genre hashtags and "Artist — Album"
  lines), then a Discogs lookup as a last resort
- Discogs track matching by duration (or, for the description lookup,
  actual tracklist presence) against a release's full tracklist rather
  than just trusting a release search's own title -- avoids mislabeling a
  track with its album's name, or rejecting a correct match just because
  the queried song isn't the release's title track
- Description text tried against Discogs' own release notes first, then
  Wikipedia (Russian first, falling back to English), with relevance
  guards against disambiguation pages, geography-stub false matches, and
  loosely-related search hits
- Cover art resolved the same way, with an automatic fallback to the
  channel's own avatar when nothing else is available
- Answers for an upcoming track are looked up *ahead* of time, once it
  reaches a "coming up soon" position in the queue -- by the time it's
  actually live, the description/cover/video link are already sitting
  there instead of needing a live round trip on every track change

**Music video links**

- A "Watch on YouTube" link only ever appears once a real matching video
  has actually been found for the current track -- a link out, not an
  embedded player
- Looked up once server-side per track change, not once per listener's
  browser -- the YouTube Data API's free quota is tight enough that doing
  it client-side would exhaust it within hours at any real listener count
- Filters out audio-only placeholders: auto-generated "Artist - Topic"
  channels, videos tagged as lyric/audio-only uploads, etc.
- Falls back to yt-dlp's own search once the official API's daily quota
  runs out -- search only, the video itself is never downloaded or
  proxied

**Admin panel**

- Password-protected web UI for uploading tracks straight into the queue,
  individually or in bulk
- Insert a track to play immediately after a specific queued item, not just
  at the end of the line
- Reorder already-queued tracks
- Skip the currently playing track without disturbing the rest of the
  prefetch queue
- Live dynamic status (current track, up-next queue) without needing to
  refresh the page
- Upload progress UI for large files over mobile connections

**Web player**

- Responsive layout: three-card desktop view (channel info, live player,
  queue) that collapses into a full-screen mobile player on tap, with a
  dedicated side-by-side layout on short/landscape phone screens
- Synced progress bar anchored to server time, immune to buffering
  hiccups, with automatic reconnect if the connection drops -- including
  compensating the on-screen track change for the HLS player's own
  live-edge buffering distance, so the displayed track doesn't jump ahead
  of what's actually audible yet
- OS-level media session integration -- lock screen / notification shade
  playback controls, cover art, and a live progress scrubber on any
  platform that supports the Web Media Session API
- Quality switcher (MP3 / FLAC) with a bandwidth-usage warning
- Live subscriber counter pulled from the Telegram channel
- Full-screen cover view (proportions preserved, not cropped) with a
  translate-to-Russian option for non-Russian description text
- Installable as a PWA (manifest + service worker) -- an installed app gets
  meaningfully more background-execution leeway from both Android and iOS
  than a plain browser tab, which the OS otherwise throttles into dropping
  live playback after a while with the screen locked; a low-key banner with
  an explanation and an install button (or instructions on iOS, which has
  no programmatic install trigger at all) shows once after playback starts

**Infrastructure**

- Multi-region relay chain (origin + edge relays) so the stream stays
  reachable across networks with selective routing restrictions, with
  automatic failover to a backup origin
- Self-healing watchdog service that monitors every component (stream
  reachability, playback freshness, individual systemd units, the RTMP
  broadcast's own audio-input health) and restarts whatever's stuck

## Layout

```
backend/     Queue filler, now-playing publisher (+ prefetch), queue-list
             writer, ICY-metadata proxy, listener-count publisher, admin
             panel, watchdog, one-off Telegram indexing/scanning scripts
liquidsoap/  Radio source graph, crossfading, Icecast/HLS outputs
tgstream/    Telegram RTMP re-broadcast pipeline (video frame rendering,
             ffmpeg encode, subscriber count updater)
frontend/    Web player (single-page, no build step)
nginx/       Reverse proxy configs (origin + edge relay)
systemd/     Unit files for every backend service
deploy.sh    Fresh-VPS bootstrap: system packages, a current Liquidsoap,
             the Python venv, systemd units -- stops short of starting
             anything or filling in secrets, prints what's left to do
```

## Setup

The fast path on a fresh Ubuntu/Debian VPS: `sudo ./deploy.sh`, then follow
the checklist it prints at the end (Telegram credentials, Icecast source
password, domain/nginx/SSL, the one-time track index build, starting
services in order).

Doing it by hand:

1. Copy `.env.example` to `/etc/musicbestman/env` and fill in real values
   (Telegram API credentials, Icecast password, RTMP URL, optionally
   Discogs/YouTube API keys -- see the comments in that file for what's
   required vs optional).
2. Install the systemd units from `systemd/` and the Liquidsoap script from
   `liquidsoap/`. Needs Liquidsoap >= 2.4 -- Ubuntu's own apt package tends
   to lag well behind that; grab a current release from
   [Savonet's releases page](https://github.com/savonet/liquidsoap/releases)
   if `apt`'s version is older.
3. Adjust the placeholder IPs/domain in `nginx/` for your own servers.
4. Authenticate a Telegram session for the account these scripts run as
   (Telethon will prompt on first run) -- keep the resulting `.session`
   file out of version control entirely, same as any other credential.
5. Build the initial track index (`backend/tg_index_build.py`) before
   starting `queue-filler.service` for the first time.
