import asyncio
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_path = os.environ.get("TELEGRAM_SESSION_PATH", "/opt/radio/session")
index_path = os.environ.get("TRACK_INDEX_PATH", "/opt/radio/track_index.json")
excluded_path = os.environ.get("EXCLUDED_IDS_PATH", "/opt/radio/excluded_ids.json")
play_history_path = os.environ.get("PLAY_HISTORY_PATH", "/opt/radio/play_history.json")
queue_dir = os.environ.get("RADIO_QUEUE_DIR", "/opt/radio/queue")
playing_dir = os.environ.get("RADIO_CACHE_DIR", "/opt/radio/cache")
webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
# Downloaded well ahead of what's actually shown as "up next" on the site
# (see DISPLAY_QUEUE_SIZE in queue_list_writer.py) -- a bigger prefetch
# buffer means playback keeps running smoothly through a slow patch (heavy
# ambient/Discogs filtering, Telegram being slow) without the visible queue
# ever actually running dry.
target_queue_size = 10
# 24 bit / 192 kHz stereo FLAC (vinyl rips, "LP" quality tag) runs roughly
# 350-450MB for a full-length track at max_duration below -- the old 20MB
# cap was sized for 16/44.1 only and silently, *permanently* excluded every
# hi-res FLAC that came through (added to excluded_ids.json like a genuine
# defect). 700MB comfortably covers 24/192 up to max_duration with headroom,
# while still bounding a truly broken/mislabeled upload.
max_size = 700 * 1024 * 1024
min_duration = 60  # seconds; shorter clips (stingers, jingles) are skipped
max_duration = 600  # seconds; the channel has occasional hour-long medley/compilation
                     # uploads that would otherwise hog the stream for ages if randomly picked
repeat_cooldown = 48 * 3600  # seconds; a track that's been queued recently is skipped,
                              # not permanently excluded -- it becomes eligible again after this
channel_username = os.environ["TELEGRAM_CHANNEL"]

from telethon import TelegramClient

# write_queue_list (plus its current_track_id/recently_played_ids helpers)
# now lives in queue_list_writer.py, shared with publish_now_playing.py --
# that script calls it right after every track transition so queue.json
# stays in sync with nowplaying.json at the instant playback actually
# changes, not just whenever this loop's own (sometimes slow, mid-download)
# iteration gets back around to it.
from queue_list_writer import write_queue_list


def ready_files():
    files = [f for f in os.listdir(queue_dir) if f.endswith(".audio")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(queue_dir, f)))
    return files


# Some rips leave a trailing duration/quality annotation on the filename
# itself, e.g. "Hiccup Focus [2m12]" -- harmless for display, but it
# poisons the Discogs search query enough to return zero results (confirmed:
# same query minus the bracket found the correct soundtrack release on the
# first try). Strip it before the title is used for anything.
TRAILING_BRACKET_RE = re.compile(r"\s*\[[^\[\]]*\]\s*$")


def strip_trailing_bracket(text):
    return TRAILING_BRACKET_RE.sub("", text or "").strip()


# A raw filename used as title fallback (embedded title tag missing)
# carries its extension along -- a real title tag would never end in
# ".flac" as actual title text, so this is always safe to strip.
AUDIO_EXTENSION_RE = re.compile(r"\.(flac|mp3|m4a|ogg|wav|wma|aac)$", re.IGNORECASE)


def strip_audio_extension(text):
    return AUDIO_EXTENSION_RE.sub("", text or "").strip()


# Two separate rip-tool artifacts that land on either end of the title:
# - Vinyl side + track number, e.g. "A2 Эти Реки" or "Song Title - B1".
#   Single letter A-D only (not A-Z) -- confirmed a 2-letter version
#   false-positives on real acts like D12 (still possible even restricted
#   to one letter, since "D12" itself reads as "side D, track 12" -- no
#   way to tell those apart by pattern alone, but A-D at least rules out
#   collisions like "U2" that a full A-Z range would catch).
# - Plain track-listing number, e.g. "1. Title" / "01 Title". Only
#   stripped when unambiguous: explicit list punctuation ("1.", "1)") or
#   zero-padded ("01") -- a leading zero never occurs in a genuine title,
#   but a bare "1 Title"/"7 Title" is indistinguishable from real songs
#   that start with a number ("7 Rings", "24K Magic", "99 Luftballons",
#   "2 Become 1", "9 to 5"), so that bare form is deliberately left alone.
# Both require a separator between the marker and the rest, so a title
# that's genuinely just "A2" or "01" and nothing else is left untouched
# rather than stripped down to an empty string.
VINYL_SIDE_PREFIX_RE = re.compile(r"^[A-Da-d]\d{1,2}[\s\-.:]+")
VINYL_SIDE_SUFFIX_RE = re.compile(r"[\s\-.:]+[A-Da-d]\d{1,2}$")
TRACK_NUMBER_PREFIX_RE = re.compile(r"^(?:\d{1,3}[.)]|0\d{1,3})\s+")
TRACK_NUMBER_SUFFIX_RE = re.compile(r"\s+(?:\d{1,3}[.)]|0\d{1,3})$")


def clean_title_markers(title):
    if not title:
        return title
    # Prefix markers can stack (e.g. a raw filename fallback like
    # "06. B1 Эти Реки.flac" has both a track number AND a vinyl side
    # letter before the real title), so loop until neither matches anymore.
    prefix_stripped_any = False
    for _ in range(3):
        candidate = VINYL_SIDE_PREFIX_RE.sub("", title)
        if candidate != title and candidate.strip():
            title = candidate
            prefix_stripped_any = True
            continue
        candidate = TRACK_NUMBER_PREFIX_RE.sub("", title)
        if candidate != title and candidate.strip():
            title = candidate
            prefix_stripped_any = True
            continue
        break
    # Only look at the suffix if no prefix marker was ever found -- confirmed
    # directly that checking both unconditionally double-stripped a title
    # like "1. Трек 01": the "1." prefix is the real artifact, but "Трек 01"
    # is the actual title and its own trailing "01" isn't a second marker.
    if not prefix_stripped_any:
        for _ in range(3):
            before = title
            candidate = VINYL_SIDE_SUFFIX_RE.sub("", title)
            if candidate.strip():
                title = candidate
            candidate = TRACK_NUMBER_SUFFIX_RE.sub("", title)
            if candidate.strip():
                title = candidate
            if title == before:
                break
    return title.strip()


def extract_tags(path):
    artist, title, genre, album = "", "", "", ""
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
            # Some FLAC rips have a bogus/garbled ID3v2 tag glued onto the
            # front (from old ripping tools), which trips up mutagen's
            # generic format auto-detection even though the real, correct
            # tags are sitting right there in the native FLAC block.
            try:
                f = FLAC(path)
            except Exception:
                f = None
        if isinstance(f, MP4) and f.tags:
            # MP4/M4A stores tags under iTunes-style atom keys, not the
            # plain "artist"/"title" keys ID3/Vorbis comments use.
            artist = (f.tags.get("\xa9ART") or [""])[0]
            title = (f.tags.get("\xa9nam") or [""])[0]
            genre = (f.tags.get("\xa9gen") or [""])[0]
            album = (f.tags.get("\xa9alb") or [""])[0]
        elif f is not None and f.tags:
            try:
                artist = (f.tags.get("artist") or [""])[0]
                title = (f.tags.get("title") or [""])[0]
                genre = (f.tags.get("genre") or [""])[0]
                album = (f.tags.get("album") or [""])[0]
            except Exception:
                pass
        if not artist and not title:
            try:
                easy = EasyID3(path)
                artist = (easy.get("artist") or [""])[0]
                title = (easy.get("title") or [""])[0]
                genre = genre or (easy.get("genre") or [""])[0]
                album = album or (easy.get("album") or [""])[0]
            except Exception:
                pass
    except Exception as e:
        print(f"tag extraction failed: {e}", file=sys.stderr)
    return artist, title, genre, album


def is_intro(*texts):
    combined = " ".join(t for t in texts if t).lower()
    return "intro" in combined or "outro" in combined


# Spoken-word interview recordings, not music -- confirmed one got on air
# ("Dio Interview"). Same plain substring check as is_intro above: real
# song titles essentially never contain the word "interview", so no
# suffix-only complexity needed the way is_live_version's "live" check
# required (that one had real collisions like "Live and Let Die").
def is_interview(*texts):
    combined = " ".join(t for t in texts if t).lower()
    return "interview" in combined


# Plain substring matching on "live" would misfire on real studio titles
# that just happen to start with the word ("Live and Let Die", "Live
# Forever", "Live Wire") -- those aren't live *recordings*, "live" is just
# the song's actual name. The tagging convention for an actual live
# recording puts it in a trailing annotation instead: "Song (Live)",
# "Song (Live at Wembley)", "Song - Live", so only match there. Album
# titles don't have that ambiguity ("Live" or "MTV Unplugged" as an album
# name is essentially always a live release), so a plain whole-word match
# is safe for album/hashtags.
LIVE_SUFFIX_RE = re.compile(r"[(\[]\s*live\b[^)\]]*[)\]]\s*$", re.IGNORECASE)
LIVE_DASH_SUFFIX_RE = re.compile(r"[-–—]\s*live\b.*$", re.IGNORECASE)
LIVE_WORD_RE = re.compile(r"\blive\b|\bunplugged\b", re.IGNORECASE)
LIVE_HASHTAGS = {"live", "unplugged", "liveperformance", "livealbum", "liverecording"}


def is_live_version(title=None, album=None, *texts):
    if title and (LIVE_SUFFIX_RE.search(title) or LIVE_DASH_SUFFIX_RE.search(title)):
        return True
    if album and LIVE_WORD_RE.search(album):
        return True
    combined = " ".join(t for t in texts if t)
    if combined:
        hashtags = set(HASHTAG_RE.findall(combined.lower()))
        if hashtags & LIVE_HASHTAGS:
            return True
    return False


# Background/score cues meant to sit under a film scene (tension-building,
# atmospheric, no real song structure) rather than stand on their own --
# excluded outright now, not just thinned out, per explicit request.
# "soundtrack" is deliberately NOT one of these signals: plenty of real
# songs (with vocals, normal structure) get released as soundtrack singles
# too, and those should just play like any other track. Only the more
# specific score/instrumental/cinematic-style signals actually mean "this
# is background scoring, not a song".
# "game music" catches publisher-style batch credits that name the
# development team instead of the actual composers -- confirmed directly:
# "TEKKEN Project, Bandai Namco Game Music - Heat Haze Shadow 1" (a Tekken
# 7 battle-theme cue) aired uncaught because Discogs credits that same
# release to the real composers by name ("BNSI, Yuu Miyake, AJURIKA...",
# zero string overlap with the file's own artist credit), so the
# Discogs-tag check never even got past its own artist-match gate to see
# the release's Stage & Screen/Video Game Music tags. A publisher crediting
# itself as "[Company] Game Music" directly in the artist field is a
# reliable signal on its own -- no ordinary band or solo artist's own name
# reads that way.
#
# "sound team" is the same class of problem, different naming convention:
# confirmed directly, "ATLUS Sound Team" aired uncaught on several Persona
# 5 (Royal) score cues ("The Almighty", "The Genesis", "I'll Face Myself
# -another version-") for the exact same reason -- Discogs credits the
# real individual composer (Shoji Meguro et al.), not "ATLUS Sound Team",
# so the artist-match gate never got far enough to see the release's own
# game-music genre tags either. Several other studios use this same
# in-house-credit convention (Nintendo/Capcom/Konami "Sound Team" are all
# real, common examples), so this is a reusable signal, not an
# Atlus-specific patch.
AMBIENT_KEYWORDS = ("ambient", "suite", "theme", "score", "cue", "underscore", "reprise", "game music", "sound team")
# The channel's own hashtag convention (e.g. "#disco #eurodisco #rnb #pop")
# on the description post right before a batch of tracks -- a much more
# precise signal than fuzzy keyword matching when it's there.
# "stage_and_screen" mirrors the channel's own use of Discogs' "Stage &
# Screen" genre category to tag game/film OST posts -- confirmed directly:
# Borislav Slavov's "Surface Zero Action" (Crysis 3 OST) aired uncaught
# because its description post was tagged "#Stage_and_Screen #Orchestral
# #Modern_Classical #Electronic", none of which were in this set, and the
# release doesn't exist on Discogs at all for the API-based check to catch
# either.
AMBIENT_HASHTAGS = {"ambient", "score", "instrumental", "cinematic", "underscore", "stage_and_screen"}
# A post can carry one of the hashtags above alongside one of these --
# confirmed directly: Aoi Teshima's "Oka no Ue no Blues" post was tagged
# "#j_pop #vocal #jazz_pop #cinematic #japanese" all at once, a single-
# artist album post (not a multi-artist compilation, so the Various
# Artists check elsewhere doesn't catch this case), just one whose tagger
# apparently applied "#cinematic" loosely to the whole thing despite it
# being an ordinary vocal j-pop/jazz-pop record. "#vocal" directly
# contradicts "this is wordless background scoring" -- when both are
# present, the vocal signal wins. "hip_hop" is the same case from the
# genre side rather than a direct "#vocal" tag: confirmed directly, The
# Notorious B.I.G.'s "I Love The Dough" (a real rapped song, Life After
# Death) got excluded on "#cinematic" despite being tagged "#hip_hop
# #east_coast_rap #gangsta_rap #mafioso_rap" in the same breath --
# rapping is vocal content by definition, so a hip-hop genre tag is just
# as strong a contradiction of "wordless background scoring" as "#vocal"
# itself. "pop" is the same: confirmed directly, Max Barskih's "По
# Фрейду" (an ordinary sung synth-pop single) got excluded on
# "#cinematic" despite being tagged "#pop #synth_pop #indie_pop
# #melancholic" right alongside it -- pop, like hip-hop, is a genre
# defined around vocals, so its presence contradicts "wordless
# background scoring" just as directly.
NON_AMBIENT_HASHTAGS = {"vocal", "vocals", "hip_hop", "pop"}

HASHTAG_RE = re.compile(r"#(\w+)")
# The channel's own caption convention always includes a "🏷 Лейбл: X" line
# naming the release's record label -- arbitrary metadata about who put the
# record out, not a description of the music itself, but "X" is free text
# and label names collide with real keywords purely by coincidence.
# Confirmed directly: both Depeche Mode's "Suffer Well" ("🏷 Лейбл: Mute /
# Reprise") and Green Day's "Basket Case" ("🏷 Лейбл: Reprise Records") --
# two ordinary vocal songs with nothing to do with a musical reprise --
# got excluded because "reprise" is one of AMBIENT_KEYWORDS and Reprise
# Records is a real, fairly common label. Stripped out before any
# keyword/hashtag check runs against caption text.
LABEL_LINE_RE = re.compile(r"^.*🏷.*$", re.MULTILINE)


def strip_label_line(caption):
    return LABEL_LINE_RE.sub("", caption or "")
# Channel convention on multi-composer/multi-film score compilations:
# tagging one specific entry "(Song)" to call it out from the surrounding
# score cues -- confirmed directly on "Magic Works (Song)" (the real
# in-universe song from Harry Potter and the Goblet of Fire, credited to
# score composer Patrick Doyle) sitting inside an 8-film Harry Potter score
# compilation tagged #score/#soundtrack. The channel's own explicit
# annotation on the individual track outranks the shared post's hashtags.
# The word "song" also turns up unannotated, right in a legitimate track's
# own official title -- confirmed directly: The Monkees' "The Porpoise Song
# (Theme From The Head)" (a real, sung 1968 single from the film "Head")
# got excluded because "theme" in its own parenthetical is one of
# AMBIENT_KEYWORDS -- "Theme From X" is also how several real vocal singles
# tied to a film brand themselves, not just instrumental scoring, so it's
# not a reliable ambient signal on its own. Any occurrence of "song" as a
# whole word is treated the same as the "(Song)" annotation above (and
# "\bsong\b" can't false-positive on words like "Songwriter" since the
# trailing word boundary requires a non-word character right after).
SONG_ANNOTATION_RE = re.compile(r"\(song\)|\bsong\b", re.IGNORECASE)

# An explicit featured-vocalist credit right in the artist field is direct
# evidence real singing is on the track -- confirmed directly: "Two
# Feathers ft. Cristina Scabbia - Dream of the Beast" got excluded because
# its genre tag was literally "Game Score", despite Cristina Scabbia (a
# named vocalist) being credited right there in the artist string. Same
# class of override as NON_AMBIENT_HASHTAGS below, just sourced from the
# artist credit instead of a post hashtag.
FEATURED_VOCALIST_RE = re.compile(r"\b(feat\.?|ft\.?|featuring)\b", re.IGNORECASE)

# Matched with word boundaries, not naive substring -- confirmed directly:
# generic rip-tool filenames like "cue_track_12.m4a" contain "cue" as a
# substring with nothing to do with an actual musical cue, and got a real
# ICE MC dance track ("Afrikan Buzz") wrongly excluded. Whole-word matching
# still catches genuine hits like "Main Title Cue" or "Game Score".
_AMBIENT_KEYWORD_RES = [re.compile(rf"\b{re.escape(kw)}\b") for kw in AMBIENT_KEYWORDS]


# duration used to be able to trigger this on its own (< 120s, no other
# signal needed) -- confirmed that wrongly caught real, short SONGS with
# nothing to do with being a score/cue snippet: The Clash's "Koka Kola
# (Remastered)" (107s) got excluded on duration alone, punk/garage rock
# just commonly runs under 2 minutes as a genre convention. Duration is
# accepted as a parameter still (kept for API-compat with existing call
# sites) but no longer used -- a keyword or hashtag hit is required now,
# never duration by itself.
def looks_ambient(duration=None, *texts):
    combined = " ".join(t for t in texts if t).lower()
    if SONG_ANNOTATION_RE.search(combined):
        return False
    if FEATURED_VOCALIST_RE.search(combined):
        return False
    hashtags = set(HASHTAG_RE.findall(combined))
    if hashtags & AMBIENT_HASHTAGS and not (hashtags & NON_AMBIENT_HASHTAGS):
        return True
    if any(kw_re.search(combined) for kw_re in _AMBIENT_KEYWORD_RES):
        return True
    return False


# Channel convention: the description post's track line looks like
# "🎵 Boney M. — Love For Sale" -- but that second part is the ALBUM being
# described, not the individual track title (confirmed: a Powerwolf post
# named its album there while the actual track underneath was "Dancing
# With The Dead", a different song entirely). Only the artist half is
# reliably reusable from this line; the album half is a search hint for
# Discogs, never a title.
CAPTION_TRACK_RE = re.compile(r"🎵\s*(.+)")
CAPTION_DASH_SPLIT_RE = re.compile(r"\s[-–—]\s")

# Same description post also has a "🎧 Качество: FLAC 16 bit / 44.1 kHz,
# WEB" line (codec/bit-depth/sample-rate/source), e.g. from a real post:
# "🎧 Качество: FLAC 24 bit / 96 kHz, LP". Captured separately from the
# codec name (FLAC/ALAC) since that's redundant with the site's own
# MP3/FLAC toggle -- only bit depth, sample rate and source are shown.
CAPTION_QUALITY_RE = re.compile(
    r"Качество:\s*\S+\s*(\d+)\s*bit\s*/\s*([\d.]+)\s*kHz\s*,\s*([^\n]+)",
    re.IGNORECASE,
)


def parse_caption_quality(caption):
    if not caption:
        return None
    m = CAPTION_QUALITY_RE.search(caption)
    if not m:
        return None
    bit_depth, sample_rate, source = m.groups()
    return f"{bit_depth} bit / {sample_rate} kHz, {source.strip()}"


def parse_caption_artist_album(caption):
    if not caption:
        return None, None
    for line in caption.splitlines():
        m = CAPTION_TRACK_RE.search(line)
        if not m:
            continue
        parts = CAPTION_DASH_SPLIT_RE.split(m.group(1).strip(), maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    return None, None


# A "Various Artists" compilation post's own hashtags describe the batch as
# a whole, not any one track in it -- confirmed directly: Marilyn Manson's
# "Rock is Dead" (a real rock song) and Aoi Teshima's "Oka no Ue no Blues"
# (vocal jazz-pop) were both excluded as ambient purely because they shared
# a post with real score cues (Don Davis's Matrix score; a #cinematic-
# tagged batch) under a multi-artist credit. A single-composer soundtrack
# post ("🎵 John Powell — How To Train Your Dragon") doesn't have this
# problem -- every track under it really is that composer's score -- so
# this only suppresses the caption text for the specific "Various Artists"
# case, not soundtrack posts generally.
# "VA" / "V.A." / "V/A" are common shorthand for "Various Artists" in music
# tagging conventions -- confirmed directly: a post crediting the artist as
# literally "VA" (an 8-film, 4-composer Harry Potter score compilation)
# didn't match the full-phrase check, so "Magic Works (Song)" -- the real
# in-universe song from Goblet of Fire, not a score cue -- still got
# excluded on the post's #score/#soundtrack hashtags. Matched as the whole
# credited name, not a substring, so it doesn't misfire on some other
# artist whose name happens to contain "va".
_VARIOUS_ARTISTS_EXACT = {"va", "v.a.", "v/a", "various", "various artists"}

# The channel's own caption convention also carries a separate "✍️ Author:"
# / "✍️ Автор:" line, distinct from the "🎵 Artist — Album" line above --
# confirmed directly: a Hotline Miami OST batch captioned "🎵 Hotline Miami
# – Soundtracks ... ✍️ Author: Various Artists" doesn't trip the check
# above at all, since the "🎵" line's own artist half reads as the game's
# own name ("Hotline Miami"), not "Various Artists" -- wrongly leaving Sun
# Araw's real "Deep Cover" (one contributing artist among many on that
# soundtrack) exposed to the whole batch's shared, wildly varied genre
# hashtags (#ambient among them) meant for the compilation as a whole, not
# his one track. Checked independently, not only as a fallback to the "🎵"
# line, since either one alone is real evidence of a Various-Artists post.
AUTHOR_LINE_RE = re.compile(r"✍️\s*(?:Author|Автор)\s*:?\s*(.+)", re.IGNORECASE)


def is_various_artists_caption(caption):
    candidates = []
    cap_artist, _ = parse_caption_artist_album(caption)
    if cap_artist:
        candidates.append(cap_artist.strip().lower())
    m = AUTHOR_LINE_RE.search(caption or "")
    if m:
        candidates.append(m.group(1).strip().lower())
    for normalized in candidates:
        if "various artist" in normalized:
            return True
        if normalized in _VARIOUS_ARTISTS_EXACT:
            return True
    return False


# Same token as publish_now_playing.py's Discogs fallback, but text-only --
# a queued-but-not-yet-playing track doesn't need cover art fetched
# (publish_now_playing.py does that itself once the track actually starts).
# Optional: ambient/soundtrack detection via Discogs style/genre tags is
# skipped (not a hard failure) when this isn't set.
discogs_token = os.environ.get("DISCOGS_TOKEN")


def _discogs_request(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MusicmaniaRadio/1.0",
            "Authorization": f"Discogs token={discogs_token}",
        },
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_track_duration(duration_str):
    parts = (duration_str or "").split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


# A release search's own "title" field is the ALBUM name, same trap as the
# caption line above -- using it as a track title would be just as wrong.
# The only way to get an actual track title out of Discogs is to open the
# matched release and read its tracklist, then pick the one entry whose own
# duration lines up with this file's -- if nothing lines up closely enough,
# returning nothing is better than confidently attaching the wrong track's
# name (or the album's) to this file.
DISCOGS_DURATION_TOLERANCE = 5  # seconds


def try_discogs_track_lookup(artist, album_hint, duration, fallback_query=""):
    query_text = f"{artist} {album_hint}".strip() or fallback_query.strip()
    if not query_text or duration is None:
        return None, None
    query = urllib.parse.quote(query_text)
    search_url = f"https://api.discogs.com/database/search?q={query}&type=release&per_page=3"
    try:
        data = _discogs_request(search_url)
    except Exception as e:
        print(f"discogs search failed: {e}", file=sys.stderr)
        return None, None

    for result in (data.get("results") or [])[:3]:
        release_id = result.get("id")
        release_title = result.get("title") or ""
        found_artist = release_title.split(" - ", 1)[0].strip() if " - " in release_title else artist
        if not release_id:
            continue
        try:
            release = _discogs_request(f"https://api.discogs.com/releases/{release_id}")
        except Exception as e:
            print(f"discogs release fetch failed: {e}", file=sys.stderr)
            continue
        for track in release.get("tracklist") or []:
            track_seconds = _parse_track_duration(track.get("duration"))
            track_title = (track.get("title") or "").strip()
            if track_seconds is None or not track_title:
                continue
            if abs(track_seconds - duration) <= DISCOGS_DURATION_TOLERANCE:
                return found_artist, track_title
    return None, None


# Discogs tags every release with genre/style fields, right there in the
# search response -- no extra per-release fetch needed. Used as a tie-
# breaker specifically for tracks that carry a soft "soundtrack"-ish local
# signal but nothing strong enough on their own to exclude outright (see
# looks_like_soundtrack_hint below): a real song happens to share a release
# with score cues often enough that "soundtrack" alone isn't reliable, but
# Discogs' own style tag on the matched release is a genuinely independent
# signal.
DISCOGS_AMBIENT_STYLES = {"ambient", "score", "soundtrack", "non-music", "field recording", "musique concrete"}

# "Soundtrack" and "Non-Music" are the two tags here that describe the
# *release*, not necessarily the music itself, and both show up on
# ordinary vocal songs: "soundtrack" gets applied to any song that
# happened to appear in a film (confirmed: "Конец Фильма - Юность В
# Сапогах", a real vocal rock track, matched genre=['Rock', 'Stage &
# Screen'] style=['Soundtrack', 'Pop Rock']); "non-music" shows up on
# anniversary/box-set compilations that bundle a spoken-word/video bonus
# disc alongside perfectly normal studio tracks (confirmed: Alisa's
# "Родина" -- an ordinary vocal rock song, correctly on the tracklist --
# matched via the box set "Мы Вместе 20 Лет", genre=['Rock', 'Non-Music'],
# and got excluded purely on the "Non-Music" side of that tag). A genuine
# score/OST release (confirmed: Toby Fox's Deltarune Chapter 1 OST,
# style=['Soundtrack','Chiptune','Video Game Music']) never carries one of
# these ordinary song genres alongside it, so requiring the absence of a
# vocal-leaning genre is what actually distinguishes the real thing from
# a vocal track dragged in by a compilation/soundtrack tie-in. "Ambient",
# "Score", "Field Recording" and "Musique Concrete" describe the music
# itself rather than its packaging context, so they stay trusted outright.
_CONTEXTUAL_AMBIENT_STYLES = {"soundtrack", "non-music"}
_VOCAL_LEANING_GENRES = {
    "rock", "pop", "pop rock", "hip hop", "r&b", "reggae", "country",
    "folk", "metal", "punk",
}


# Discogs' full-text search matches loosely -- confirmed directly: "Black
# Lakes Children of Caverns" (a black metal track) top-matched a completely
# unrelated field-recording/nature audiobook release (shared the word
# "Children"), tagged Non-Music/Field Recording, while the *actual* Black
# Lakes release two rows down was plain Rock/Black Metal. Trusting whichever
# of the first few results happened to carry an ambient-ish tag -- without
# checking the result was actually about the queried artist -- flagged a
# real black metal track as ambient. Release titles are "Artist - Album",
# sometimes with a Discogs disambiguation suffix ("Black Lakes (2)") when
# multiple artists share a name -- strip that before comparing.
def _artist_matches_result(artist, result_title):
    if not artist or not result_title:
        return False
    artist_norm = artist.strip().lower()
    result_artist = result_title.split(" - ", 1)[0]
    result_artist = re.sub(r"\s*\(\d+\)\s*$", "", result_artist).strip().lower()
    if artist_norm in result_artist or result_artist in artist_norm:
        return True
    # Some rips have the film/show title glued onto the front of the
    # performer tag instead of the real composer's name alone -- confirmed
    # directly: a whole Cars (Pixar) OST batch tagged performer as literally
    # "Cars - Randy Newman", which doesn't equal or contain/get-contained-by
    # Discogs' own "Randy Newman" (or "Randy Newman, Various") credit as one
    # whole string, so a genuinely correct, correctly Score-tagged match got
    # rejected here and the track played on air uncaught. Fall back to
    # checking each dash-separated segment on its own.
    for segment in artist_norm.split(" - "):
        segment = segment.strip()
        if segment and (segment in result_artist or result_artist in segment):
            return True
    return False


# Same word-inclusion check used on the Wikipedia/Discogs enrichment side
# (publish_now_playing.py) -- a multi-word name only counts as related if
# every meaningful word shows up, not just any one of them (a single
# common word matching some unrelated text isn't enough evidence).
def _looks_related(name, candidate_text):
    if not name:
        return True
    name_norm = name.strip().lower()
    candidate_norm = (candidate_text or "").lower()
    if name_norm and name_norm in candidate_norm:
        return True
    words = [w for w in re.findall(r"\w+", name_norm) if len(w) >= 3]
    if not words:
        return False
    return all(w in candidate_norm for w in words)


# A release-level genre/style tag describes the whole album, not
# necessarily the one track being checked -- confirmed directly: a search
# for "Moby 7" (a real, ordinary Moby track) top-matched Moby's self-
# titled album (genre=['Electronic'] style=['Acid House','Techno',
# 'Ambient']) purely because "7" is short enough to loosely match all
# sorts of things (track numbers, catalog codes), even though no track on
# that album is actually titled "7" -- its real 12-track list is "Drop A
# Beat", "Everything", "Yeah", etc. Trusting the album's tags without
# checking the track is actually on it excluded a normal song outright.
# Only worth the extra fetch once a tag match is already in hand, not on
# every candidate.
def _tracklist_has_title(resource_url, title):
    try:
        release = _discogs_request(resource_url)
    except Exception as e:
        print(f"discogs tracklist fetch failed: {e}", file=sys.stderr)
        return False
    for track in release.get("tracklist") or []:
        if _looks_related(title, track.get("title") or ""):
            return True
    return False


def check_ambient_via_discogs(artist, title):
    if not discogs_token or not artist or not title:
        return None
    query = urllib.parse.quote(f"{artist} {title}")
    url = f"https://api.discogs.com/database/search?q={query}&type=release&per_page=3"
    try:
        data = _discogs_request(url)
    except Exception as e:
        print(f"discogs ambient check failed: {e}", file=sys.stderr)
        return None
    for result in (data.get("results") or [])[:3]:
        if not _artist_matches_result(artist, result.get("title") or ""):
            continue
        tags = set()
        for field in ("style", "genre"):
            for v in result.get(field) or []:
                tags.add(v.lower())
        strong_tags = tags & (DISCOGS_AMBIENT_STYLES - _CONTEXTUAL_AMBIENT_STYLES)
        contextual_hit = (tags & _CONTEXTUAL_AMBIENT_STYLES) and not (tags & _VOCAL_LEANING_GENRES)
        if not (strong_tags or contextual_hit):
            continue
        resource_url = result.get("resource_url") or ""
        if resource_url and _tracklist_has_title(resource_url, title):
            return True
    return False


# Score composers whose work reads as real, standalone-listenable music
# rather than mere scene filler -- exempted from both ambient checks above
# regardless of what the local text signals or Discogs genre/style say.
# Matched case-insensitively as a substring of the artist field, since
# credits show up as anything from "Joe Hisaishi" to "Joe Hisaishi, Studio
# Ghibli" to just "Hisaishi".
#
# "магомаев" is the same kind of unfixable-by-a-general-rule case, just
# from the opposite direction: confirmed directly, Муслим Магомаев's
# "Парад заграничных певцов" (a real sung comedic number from the 1973
# Soviet animated musical "По следам Бременских музыкантов") got excluded
# via its embedded FLAC genre tag "Soundtrack, Score, Stage & Screen" --
# release-level Discogs style tags on a film-music compilation, copied
# wholesale into every track's genre field by whoever ripped it, with
# nothing distinguishing this one sung track from the compilation's actual
# instrumental cues. The caption's own hashtags (#soundtrack #musical
# #fairytale #childrens #nostalgic, no #score/#ambient) already got this
# right; only the embedded tag disagreed. Discogs' contextual/vocal-genre
# override (_CONTEXTUAL_AMBIENT_STYLES/_VOCAL_LEANING_GENRES) doesn't save
# this either, since "Score" is trusted outright there and no vocal-
# leaning genre rides along beside it to contradict it. Same fix shape as
# Hisaishi: a small, explicit exemption for a specific artist known to get
# mistagged this way, rather than a general rule that would need
# track-level tracklist verification against the *local* embedded tag
# (which, unlike Discogs' own search results, has no such check today).
AMBIENT_EXEMPT_ARTISTS = ("hisaishi", "studio ghibli", "miyazaki", "ghibli", "магомаев")


def is_ambient_exempt(artist):
    artist_lower = (artist or "").lower()
    return any(name in artist_lower for name in AMBIENT_EXEMPT_ARTISTS)


# How many prior messages to check for a description post. Some game/movie
# OST dumps run 100+ tracks deep under a single description -- confirmed
# (METAL GEAR SOLID Delta OST, #cinematic/#soundtrack hashtags on the post)
# that with a short lookback only the first track or two after the post
# ever actually see those hashtags; everything deeper in the same batch
# never finds it and falls through to Discogs alone, which doesn't reliably
# cover brand-new game soundtracks.
CAPTION_LOOKBACK = 150


async def get_preceding_caption(client, entity, candidate_id):
    # Channel convention: a text post describing an album/soundtrack (with
    # genre hashtags at the top), then the individual tracks follow as
    # separate audio messages right after it. The tracks' own tags/filenames
    # usually don't repeat those hashtags, so it's worth checking on top of
    # the track's own metadata. Fetched as one batched call rather than one
    # request per candidate message -- at this lookback depth, one-by-one
    # would be 150 round trips for every track deep in a large batch.
    start = max(1, candidate_id - CAPTION_LOOKBACK)
    ids = list(range(candidate_id - 1, start - 1, -1))
    if not ids:
        return ""
    try:
        messages = await client.get_messages(entity, ids=ids)
    except Exception:
        return ""
    for msg in messages:
        if msg is None:
            continue
        if msg.audio or msg.voice:
            # still inside the same track batch -- keep looking further back
            continue
        text = (msg.raw_text or "").strip()
        if text:
            return text
        # a real, text-less message in between (sticker, etc) -- batch
        # boundary, nothing further back belongs to this candidate
        break
    return ""


def load_excluded():
    try:
        with open(excluded_path) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_excluded(excluded):
    tmp = excluded_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(excluded), f)
    os.replace(tmp, excluded_path)


def load_play_history():
    try:
        with open(play_history_path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_play_history(history):
    # Prune anything past the cooldown window so this doesn't grow forever.
    now = time.time()
    pruned = {tid: ts for tid, ts in history.items() if now - ts < repeat_cooldown}
    tmp = play_history_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pruned, f)
    os.replace(tmp, play_history_path)
    return pruned


def recently_queued(history, track_id):
    ts = history.get(str(track_id))
    return ts is not None and time.time() - ts < repeat_cooldown


# Same rolling-window idea as repeat_cooldown above, but keyed on the
# album rather than the individual track -- a channel batch-upload often
# drops half a dozen different songs off the same album at once, and pure
# per-track cooldown does nothing to stop several of those from getting
# picked the same day. Keyed on artist+album together since album titles
# alone collide across different artists ("Greatest Hits").
album_history_path = os.environ.get("ALBUM_PLAY_HISTORY_PATH", "/opt/radio/album_play_history.json")
album_window = 24 * 3600  # seconds
album_daily_limit = 2  # max plays from the same album inside that window


def load_album_history():
    try:
        with open(album_history_path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_album_history(history):
    now = time.time()
    pruned = {
        key: [ts for ts in stamps if now - ts < album_window]
        for key, stamps in history.items()
    }
    pruned = {key: stamps for key, stamps in pruned.items() if stamps}
    tmp = album_history_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pruned, f)
    os.replace(tmp, album_history_path)
    return pruned


def album_key(artist, album):
    return f"{(artist or '').strip().lower()}::{(album or '').strip().lower()}"


def album_quota_exceeded(history, key):
    now = time.time()
    stamps = history.get(key, [])
    return sum(1 for ts in stamps if now - ts < album_window) >= album_daily_limit


# album_quota_exceeded only ever limits repeats of one specific album --
# real-world reports of specific artists (Curta'n Wall, Marilyn Manson)
# showing up constantly turned out to be exactly that: each *individual*
# album/single they're credited on was correctly staying under its own
# 2-per-24h cap (confirmed directly, both against real captions and the
# actual album-history file), but an artist with several different
# releases in the archive has nothing capping how often *any one of them*
# gets picked, in aggregate. This runs whenever *artist* is known at all
# (almost always, unlike album, which several real batch uploads turned
# out to have no caption/embedded tag for at all -- this also covers that
# narrower case as a side effect, though it wasn't the main cause here).
#
# Two-tier, not a flat rolling-window count like the album quota: up to
# artist_daily_limit plays inside artist_day_window (24h) are allowed, but
# once that's been reached, the next play needs a full artist_cooldown
# (48h) gap from the *most recent* play specifically -- not just for the
# oldest of the two to roll back out of a 24h window, which could still
# let a third play land only a little more than a day after the first.
# Since artist_cooldown (48h) is longer than artist_day_window (24h), it's
# always the binding constraint once the daily cap is hit: by the time
# 48h have passed since the most recent play, the daily-window count has
# necessarily dropped below the limit too.
artist_history_path = os.environ.get("ARTIST_PLAY_HISTORY_PATH", "/opt/radio/artist_play_history.json")
artist_day_window = 24 * 3600  # seconds
artist_daily_limit = 2  # max plays of the same artist inside that window before the cooldown below kicks in
artist_cooldown = 48 * 3600  # seconds; required gap since the most recent play once the daily limit's been hit


def load_artist_history():
    try:
        with open(artist_history_path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_artist_history(history):
    now = time.time()
    # Pruned against the longer of the two windows (the cooldown, not the
    # daily one) -- an entry still has to survive long enough to be seen
    # by the cooldown check even after it's aged out of the daily count.
    pruned = {
        key: [ts for ts in stamps if now - ts < artist_cooldown]
        for key, stamps in history.items()
    }
    pruned = {key: stamps for key, stamps in pruned.items() if stamps}
    tmp = artist_history_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pruned, f)
    os.replace(tmp, artist_history_path)
    return pruned


def artist_key(artist):
    # Not split on multiple co-credited artists ("Curta'n Wall, DJ JOHN")
    # -- treated as its own distinct identity, same as album_key already
    # does for artist+album together. Splitting would mean a collab counts
    # against *both* names' individual quotas, which isn't obviously more
    # correct and adds real complexity for a case that hasn't come up as
    # a problem on its own.
    return (artist or "").strip().lower()


def artist_quota_exceeded(history, key):
    now = time.time()
    stamps = history.get(key, [])
    recent_in_day = sum(1 for ts in stamps if now - ts < artist_day_window)
    if recent_in_day < artist_daily_limit:
        return False
    most_recent = max(stamps)
    return now - most_recent < artist_cooldown


# MTProto itself multiplexes many connections per client (this is how
# Telegram-backed proxies push high throughput), and the box's own uplink is
# nowhere near saturated by a single download (~550KB/s on one connection vs
# 32MB/s it can push to an unrelated host) -- matched to target_queue_size
# so a full queue wipe can redownload all of it at once instead of trickling
# back in one at a time.
max_concurrent_downloads = 10
state_lock = asyncio.Lock()
in_progress_ids = set()


async def reserve_candidate(ids, excluded, play_history):
    # Quick and lock-held: picks a candidate nobody else has claimed yet and
    # marks it claimed, then hands off. The actual download/tagging/ambient
    # checks happen in process_candidate() *outside* the lock, since those
    # are the slow part and would otherwise serialize every worker back into
    # one-at-a-time.
    async with state_lock:
        existing_ids = {f.split(".")[0] for f in ready_files()}
        if len(existing_ids) + len(in_progress_ids) >= target_queue_size:
            return None
        for _ in range(15):
            candidate_id = random.choice(ids)
            sid = str(candidate_id)
            if sid in existing_ids or sid in excluded or sid in in_progress_ids:
                continue
            if recently_queued(play_history, sid):
                continue
            in_progress_ids.add(sid)
            return candidate_id
    return None


async def mark_excluded(sid, excluded, reason):
    async with state_lock:
        excluded.add(sid)
        save_excluded(excluded)
    print(f"excluded {sid}: {reason}", file=sys.stderr)


async def process_candidate(client, entity, candidate_id, excluded, play_history, album_history, artist_history):
    sid = str(candidate_id)
    candidate = await client.get_messages(entity, ids=candidate_id)
    if candidate is None or not (candidate.audio or candidate.voice):
        await mark_excluded(sid, excluded, "not audio/voice or message missing")
        return False

    duration = getattr(candidate.file, "duration", None)
    file_title = getattr(candidate.file, "title", None) or ""
    file_performer = getattr(candidate.file, "performer", None) or ""
    file_name = getattr(candidate.file, "name", None) or ""
    mime_type = getattr(candidate.file, "mime_type", None) or ""
    # Lossy source uploads -- this channel mixes lossless rips with plain
    # MP3s of the same tracks; skip the MP3 copies rather than waste a
    # download on quality the stream doesn't want. Checked on mime type
    # first (what Telegram itself reports for the file) since a renamed or
    # missing extension would slip past a filename-only check.
    if mime_type == "audio/mpeg" or file_name.lower().endswith(".mp3"):
        await mark_excluded(sid, excluded, f"mp3 source (mime={mime_type!r}, name={file_name!r})")
        return False
    if duration is not None and duration < min_duration:
        await mark_excluded(sid, excluded, f"duration {duration}s < min {min_duration}s")
        return False
    if duration is not None and duration > max_duration:
        await mark_excluded(sid, excluded, f"duration {duration}s > max {max_duration}s")
        return False
    if is_intro(file_title, file_name, candidate.raw_text or ""):
        await mark_excluded(sid, excluded, f"is_intro (early, filename/caption): title={file_title!r} name={file_name!r}")
        return False
    if is_interview(file_title, file_name, candidate.raw_text or ""):
        await mark_excluded(sid, excluded, f"is_interview (early, filename/caption): title={file_title!r} name={file_name!r}")
        return False
    if is_live_version(file_title or file_name):
        await mark_excluded(sid, excluded, f"is_live_version (early, filename): {file_title or file_name!r}")
        return False

    preceding_caption = ""
    if not looks_ambient(duration, file_title, file_name):
        preceding_caption = await get_preceding_caption(client, entity, candidate_id)
    ambient_caption = "" if is_various_artists_caption(preceding_caption) else strip_label_line(preceding_caption)
    if not is_ambient_exempt(file_performer) and looks_ambient(duration, file_title, file_name, ambient_caption):
        await mark_excluded(sid, excluded, f"looks_ambient (early, filename+caption): title={file_title!r} caption={ambient_caption[:120]!r}")
        return False

    size = candidate.file.size if candidate.file else None
    if size is not None and size > max_size:
        await mark_excluded(sid, excluded, f"file size {size} > max {max_size}")
        return False

    tmp_dest = os.path.join(queue_dir, f"{candidate_id}.audio.part")
    final_dest = os.path.join(queue_dir, f"{candidate_id}.audio")
    path = await client.download_media(candidate, file=tmp_dest)
    if not path:
        return False
    artist, title, genre, album = extract_tags(path)
    if not artist:
        artist = file_performer
    # Was gated on "not artist" too, so a known artist (from tags or
    # Telegram's own performer field) silently blocked ever recovering
    # a still-missing title from file_title/file_name -- e.g. Powerwolf
    # tracks with performer set but no title ended up stuck as "" here,
    # then shown as "Неизвестный трек" in the queue despite the artist
    # being known the whole time.
    if not title and (file_title or file_name):
        title = file_title or file_name
    title = strip_trailing_bracket(title)
    title = strip_audio_extension(title)
    title = clean_title_markers(title)
    # Free, already-fetched source before reaching for an API: the
    # description post's own "🎵 Artist — Album" line. Artist only --
    # the second half is the album being described, not this track's
    # title (see try_discogs_track_lookup below for why that distinction
    # matters).
    caption_album_hint = ""
    if (not artist or not title or not album) and preceding_caption:
        cap_artist, cap_album = parse_caption_artist_album(preceding_caption)
        if not artist and cap_artist:
            artist = cap_artist
        if not album and cap_album:
            album = cap_album
        caption_album_hint = cap_album or ""

    # Enforce before spending a Discogs lookup on a track we're going
    # to throw away anyway -- artist is often already known by here
    # (tags or Telegram's performer field), only album needed the
    # caption fallback above.
    if album:
        key = album_key(artist, album)
        async with state_lock:
            exceeded = album_quota_exceeded(album_history, key)
        if exceeded:
            os.remove(path)
            return False
    # Unlike the album check above, this runs whenever artist is known at
    # all -- confirmed directly this gap was real: a batch upload with no
    # caption and no embedded album tag skips the album check entirely
    # (it only ever runs `if album:`), so an artist could show up as often
    # as the random draw happened to pick them with nothing to slow it
    # down. See artist_quota_exceeded for the exact allowance.
    if artist:
        artist_k = artist_key(artist)
        async with state_lock:
            artist_exceeded = artist_quota_exceeded(artist_history, artist_k)
        if artist_exceeded:
            os.remove(path)
            return False
    # Last resort: ask Discogs for the release (artist + album hint from
    # the caption, or whatever text is available), then match this
    # file's own duration against that release's tracklist to find the
    # actual track title -- never just the release/album title.
    if not title:
        disc_artist, disc_title = try_discogs_track_lookup(
            artist, caption_album_hint, duration, fallback_query=preceding_caption[:150]
        )
        if not artist and disc_artist:
            artist = disc_artist
        if disc_title:
            title = disc_title
    if is_intro(artist, title):
        os.remove(path)
        await mark_excluded(sid, excluded, f"is_intro (full): artist={artist!r} title={title!r}")
        return False
    if is_interview(artist, title, preceding_caption):
        os.remove(path)
        await mark_excluded(sid, excluded, f"is_interview (full): artist={artist!r} title={title!r}")
        return False
    if is_live_version(title, album, preceding_caption):
        os.remove(path)
        await mark_excluded(sid, excluded, f"is_live_version (full): title={title!r} album={album!r}")
        return False
    ambient_caption_full = "" if is_various_artists_caption(preceding_caption) else strip_label_line(preceding_caption)
    if not is_ambient_exempt(artist) and looks_ambient(duration, genre, artist, title, ambient_caption_full):
        os.remove(path)
        await mark_excluded(sid, excluded, f"looks_ambient (full): artist={artist!r} title={title!r} genre={genre!r}")
        return False
    # Local text signals miss a lot of real cases -- game-score
    # composers (Jeremy Soule, Toby Fox, etc.) routinely have plain,
    # unhinted track titles ("Dawn", "Glowing Snow") with no "ambient"/
    # "score"/"soundtrack" word or hashtag anywhere nearby, and their
    # tracks aren't reliably short either. Discogs' own genre/style
    # tags catch these without needing a text hint first -- run it for
    # every track that got this far, not just ones that already look
    # soundtrack-ish. Discogs has no daily quota like YouTube's (just a
    # per-minute rate limit), so checking every track is sustainable at
    # this track-change rate.
    if not is_ambient_exempt(artist) and check_ambient_via_discogs(artist, title):
        os.remove(path)
        await mark_excluded(sid, excluded, f"check_ambient_via_discogs: artist={artist!r} title={title!r}")
        return False
    meta = {
        "id": candidate_id,
        "artist": artist,
        "title": title,
        "album": album,
        "link": f"https://t.me/{channel_username}/{candidate_id}",
        "quality": parse_caption_quality(preceding_caption),
    }
    # Write the metadata sidecar *before* the audio file becomes visible
    # under its final name -- otherwise pop_from_queue.py can grab a
    # freshly-renamed .audio file in the split-second window before its
    # .json sidecar exists, and the track plays with no tags forever.
    with open(os.path.join(queue_dir, f"{candidate_id}.json"), "w") as mf:
        json.dump(meta, mf)
    os.replace(path, final_dest)
    async with state_lock:
        play_history[sid] = time.time()
        save_play_history(play_history)
        if album:
            key = album_key(artist, album)
            album_history.setdefault(key, []).append(time.time())
            save_album_history(album_history)
        if artist:
            artist_k = artist_key(artist)
            artist_history.setdefault(artist_k, []).append(time.time())
            save_artist_history(artist_history)
    print(f"queued {final_dest}", file=sys.stderr)
    return True


async def worker(client, entity, ids, excluded, play_history, album_history, artist_history):
    while True:
        candidate_id = await reserve_candidate(ids, excluded, play_history)
        if candidate_id is None:
            await asyncio.sleep(3)
            continue
        failed = False
        try:
            await process_candidate(client, entity, candidate_id, excluded, play_history, album_history, artist_history)
        except Exception as e:
            print(f"fill error: {e}", file=sys.stderr)
            failed = True
        finally:
            async with state_lock:
                in_progress_ids.discard(str(candidate_id))
        if failed:
            # A dropped Telegram connection (seen in production: "Cannot send
            # requests while disconnected") makes every request fail
            # instantly -- with no delay here, 10 concurrent workers just
            # spam the same error dozens of times a second until the
            # connection recovers on its own instead of backing off and
            # giving it room to.
            await asyncio.sleep(5)
        async with state_lock:
            write_queue_list()


async def main():
    os.makedirs(queue_dir, exist_ok=True)
    with open(index_path) as f:
        data = json.load(f)
    ids = data["ids"]
    channel = data["channel"]
    excluded = load_excluded()
    play_history = load_play_history()
    album_history = load_album_history()
    artist_history = load_artist_history()

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    entity = await client.get_entity(channel)

    workers = [
        asyncio.create_task(worker(client, entity, ids, excluded, play_history, album_history, artist_history))
        for _ in range(max_concurrent_downloads)
    ]
    await asyncio.gather(*workers)


if __name__ == "__main__":
    # Confirmed directly this was a real, live footgun: importing this
    # file as a module (e.g. from a one-off diagnostic script wanting to
    # reuse parse_caption_artist_album or similar) ran the *entire* fill
    # loop as a side effect of the import statement alone, with no way to
    # opt out short of not importing it at all -- a second, unintended
    # instance ended up racing the real systemd service against the same
    # shared state files and downloading into the same queue directory.
    asyncio.run(main())
