# MusicmaniA Radio

A 24/7 internet radio station built on top of a Telegram channel's music
archive, with a synced re-broadcast into a Telegram video chat and a
Material Design 3 web player.

## First release

**Streaming**

- Continuous MP3 (128kbps) and lossless FLAC quality tiers from a single
  live source, selectable per listener
- HLS mirror alongside the plain live stream, giving the web player real
  seek support (rewind 10s, jump back to the live edge) that a raw chunked
  stream can't offer
- Simultaneous re-broadcast of the same audio into a Telegram group video
  call via RTMP, paired with a live-rendered video frame showing the
  current track, upcoming queue, and a subscribe call-to-action --
  regenerated on every tick rather than a static placeholder image

**Auto-curated queue**

- Tracks pulled at random from a Telegram channel's full audio archive, no
  manual playlist maintenance
- Duration filtering (skips short jingles/stingers and hour-long medley
  uploads alike)
- Ambient/soundtrack-cue detection (hashtags, title keywords, duration) to
  keep instrumental filler from dominating the rotation
- Intro/jingle filtering by title pattern
- 48-hour repeat cooldown so the same track doesn't resurface too soon
- Admin-uploaded tracks are exempt from the automatic queue-staleness sweep

**Metadata enrichment**

- Layered fallback chain for artist/title: embedded file tags, then
  Telegram's own per-message title/performer fields, then the channel's
  own description posts (parsed for genre hashtags and "Artist — Album"
  lines), then a Discogs lookup as a last resort
- Discogs track matching by duration against a release's full tracklist,
  rather than just trusting a release search's own title -- avoids
  mislabeling a track with its album's name
- Cover art resolved the same way, with an automatic fallback to the
  channel's own avatar when nothing else is available

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
  queue) that collapses into a full-screen mobile player on tap
- Synced progress bar anchored to server time, immune to buffering
  hiccups, with automatic reconnect if the connection drops
- OS-level media session integration -- lock screen / notification shade
  playback controls, cover art, and a live progress scrubber on any
  platform that supports the Web Media Session API
- Quality switcher (MP3 / FLAC) with a bandwidth-usage warning
- Live subscriber counter pulled from the Telegram channel

**Infrastructure**

- Multi-region relay chain (origin + edge relays) so the stream stays
  reachable across networks with selective routing restrictions, with
  automatic failover to a backup origin
- Self-healing watchdog service that monitors every component (stream
  reachability, playback freshness, individual systemd units) and restarts
  whatever's stuck

## Layout

```
backend/     Queue filler, now-playing publisher, admin panel, watchdog,
             one-off Telegram indexing/scanning scripts
liquidsoap/  Radio source graph, crossfading, Icecast/HLS outputs
tgstream/    Telegram RTMP re-broadcast pipeline (video frame rendering,
             ffmpeg encode, subscriber count updater)
frontend/    Web player (single-page, no build step)
nginx/       Reverse proxy configs (origin + edge relay)
systemd/     Unit files for every backend service
```

## Setup

1. Copy `.env.example` to `/etc/musicbestman/env` and fill in real values
   (Telegram API credentials, Icecast password, RTMP URL, optionally a
   Discogs token -- see the comments in that file for what's required vs
   optional).
2. Install the systemd units from `systemd/` and the Liquidsoap script from
   `liquidsoap/`.
3. Adjust the placeholder IPs/domain in `nginx/` for your own servers.
4. Authenticate a Telegram session for the account these scripts run as
   (Telethon will prompt on first run) -- keep the resulting `.session`
   file out of version control entirely, same as any other credential.
