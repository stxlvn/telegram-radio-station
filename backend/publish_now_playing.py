import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request
import urllib.parse

from queue_list_writer import write_queue_list

webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
channel_username = os.environ["TELEGRAM_CHANNEL"]
history_path = os.environ.get("PLAYING_HISTORY_PATH", "/opt/radio/cache/.history.json")
history_keep = 2  # current + 1 previous, to safely cover crossfade overlap

# Where prefetch_enrich() (see below) stashes lookups it ran ahead of time
# for whichever track queue_list_writer.py's _trigger_prefetch decided was
# coming up soon -- main()'s track-start path reads this back instantly
# instead of redoing the same Discogs/Wikipedia/YouTube round trips live.
prefetch_dir = os.environ.get("PREFETCH_DIR", "/opt/radio/prefetch_enrich")
prefetch_max_age = 1800  # seconds; sweeps prefetches for tracks that got skipped/removed before playing


def update_history_and_cleanup(current_path):
    history = []
    if os.path.exists(history_path):
        try:
            with open(history_path) as f:
                history = json.load(f)
        except Exception:
            history = []
    if current_path in history:
        history.remove(current_path)
    history.append(current_path)
    while len(history) > history_keep:
        old_path = history.pop(0)
        if old_path == current_path:
            continue
        for candidate in (old_path, old_path.rsplit(".", 1)[0] + ".json"):
            try:
                os.remove(candidate)
            except OSError:
                pass
    tmp = history_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f)
    os.replace(tmp, history_path)


# Both optional -- see try_discogs_lookup / _find_youtube_video_id_via_api
# below, which already degrade gracefully (skip the lookup, or fall
# through to the yt-dlp search fallback) when these aren't set.
discogs_token = os.environ.get("DISCOGS_TOKEN")
youtube_api_key = os.environ.get("YOUTUBE_API_KEY")

# Same filtering as the site used to do client-side, moved here so the
# lookup happens once per track instead of once per listener's browser --
# with search.list costing 100 quota units per call and tracks changing
# every few minutes, doing this per-listener blew through the free daily
# quota (10,000 units) within hours; per-track is the only way this fits
# even loosely within budget.
YOUTUBE_AUDIO_ONLY_HINT_RE = re.compile(
    r"\b(audio|аудио|lyric|lyrics|lyric video|текст песни|static|cover art|provided to youtube)\b",
    re.IGNORECASE,
)
YOUTUBE_TOPIC_CHANNEL_RE = re.compile(r"- Topic$", re.IGNORECASE)


def _find_youtube_video_id_via_api(artist, title):
    query = urllib.parse.quote(f"{artist} {title} official video")
    url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&maxResults=5&q={query}&key={youtube_api_key}"
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # Notably: search.list's free quota (10,000 units/day, 100 units per
        # call) runs out partway through most days at this track-change
        # rate -- every subsequent call fails with HTTP 429 until the
        # quota resets, which is exactly what the yt-dlp fallback below is
        # for.
        print(f"youtube api search failed: {e}", file=sys.stderr)
        return None
    items = [item for item in (data.get("items") or []) if item.get("id", {}).get("videoId")]
    for item in items:
        snippet = item.get("snippet") or {}
        video_title = snippet.get("title") or ""
        channel = snippet.get("channelTitle") or ""
        if not YOUTUBE_AUDIO_ONLY_HINT_RE.search(video_title) and not YOUTUBE_TOPIC_CHANNEL_RE.search(channel):
            return item["id"]["videoId"]
    return items[0]["id"]["videoId"] if items else None


# Defaults to whatever "yt-dlp" resolves to on PATH if not set.
YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")


def _find_youtube_video_id_via_ytdlp(artist, title):
    query = f"ytsearch5:{artist} {title} official video"
    try:
        result = subprocess.run(
            [YTDLP_BIN, query, "--flat-playlist", "--dump-json", "--no-warnings"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        print(f"yt-dlp search failed: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"yt-dlp search error: {result.stderr[:300]}", file=sys.stderr)
        return None
    items = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if item.get("id"):
            items.append(item)
    for item in items:
        video_title = item.get("title") or ""
        channel = item.get("channel") or item.get("uploader") or ""
        if not YOUTUBE_AUDIO_ONLY_HINT_RE.search(video_title) and not YOUTUBE_TOPIC_CHANNEL_RE.search(channel):
            return item["id"]
    return items[0]["id"] if items else None


def find_youtube_video_id(artist, title):
    if not artist or not title:
        return None
    # Official API first (sanctioned, quota-limited); yt-dlp only as a
    # fallback once that quota is exhausted for the day, not the primary
    # path -- yt-dlp works by reverse-engineering YouTube's internal,
    # unofficial endpoints rather than their public API, which is less
    # stable and sits in more of a grey area against their terms, but it's
    # still only ever used for search/metadata here, never to fetch or
    # store the actual video/audio content itself.
    return _find_youtube_video_id_via_api(artist, title) or _find_youtube_video_id_via_ytdlp(artist, title)


def _strip_accents(text):
    # Russian Wikipedia routinely marks stress with a combining acute
    # accent (U+0301) on the lead word of an article -- "Ленингра́д", not
    # "Ленинград" -- which silently breaks every plain substring check
    # below: confirmed directly, the real "Ленинград" band article opens
    # with "«Ленингра́д»" and a bare `"ленинград" in "ленингра́д"` check
    # returns False despite being an exact match to a human eye, because
    # the accent is a separate, invisible codepoint sitting between the
    # "а" and "д". NFD splits every accented character into base+combining
    # form, so filtering out the "Mn" (nonspacing mark) category strips
    # exactly the accent marks and nothing else.
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _looks_related(artist, candidate_text):
    # Discogs/Wikipedia search both always return *something* for a short
    # query, even for small independent artists with no real entry there
    # (confirmed directly: "Staple R" -- an independent metalcore
    # artist -- returned an unrelated top result on both, which got
    # shown as this track's description with nothing to say it was
    # wrong). Cheap guard: refuse a match unless the artist name (or a
    # meaningful word from it) actually shows up in what was found.
    if not artist:
        return True
    artist_norm = _strip_accents(artist.strip().lower())
    candidate_norm = _strip_accents((candidate_text or "").lower())
    if artist_norm and artist_norm in candidate_norm:
        return True
    # A multi-artist credit (comma/"feat."/"&"-joined) only needs ONE of
    # its names to actually show up, not every word across the whole
    # combined string -- confirmed directly: "Celldweller, Styles Of
    # Beyond - Shapeshifter" (the actual matching Discogs release) only
    # credits "Celldweller" in its own title, "Styles Of Beyond" being a
    # featured artist noted elsewhere on the release, not in the title
    # text this function ever sees -- requiring every word of the full
    # combined credit string rejected a fully correct match.
    for segment in re.split(r",|&|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b", artist_norm):
        segment = segment.strip()
        if not segment:
            continue
        words = [w for w in re.findall(r"\w+", segment) if len(w) >= 3]
        if not words:
            continue
        # Confirmed directly: "Wind Rose" (an Italian folk-metal band)
        # matched on "any" -- a totally unrelated Wikipedia article about a
        # person named Rose Porteous passed because it contains "Rose",
        # one of the band's two name-words, with nothing to do with
        # "Wind". A multi-word *single* name needs every one of its own
        # words present; what's relaxed above is only requiring every
        # co-credited artist's name too, not every word within one name.
        if all(w in candidate_norm for w in words):
            return True
    return False


def _tracklist_has_title(resource_url, title):
    req = urllib.request.Request(
        resource_url,
        headers={
            "User-Agent": "MusicmaniaRadio/1.0",
            "Authorization": f"Discogs token={discogs_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"discogs tracklist fetch failed: {e}", file=sys.stderr)
        return False
    for track in release.get("tracklist") or []:
        if _looks_related(title, track.get("title") or ""):
            return True
    return False


def try_discogs_lookup(artist, title, fallback_query=""):
    query_text = f"{artist} {title}".strip() or fallback_query.strip()
    if not query_text:
        return None
    query = urllib.parse.quote(query_text)
    url = f"https://api.discogs.com/database/search?q={query}&type=release&per_page=10"
    # Unauthenticated search requests silently return "" for cover_image/thumb
    # on every result (confirmed against several well-known releases) -- the
    # search itself and its text fields work fine either way, but a token is
    # required to actually get image URLs back.
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MusicmaniaRadio/1.0",
            "Authorization": f"Discogs token={discogs_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results") or []
        if not results:
            return None

        # Only checking results[0] missed genuinely correct matches ranked
        # below other, more "popular" releases by the same artist --
        # confirmed directly: "Celldweller, Styles Of Beyond - Shapeshifter"
        # found nothing because Discogs' own top hit for that query was
        # Celldweller's self-titled album (several reissues of it, all
        # ranked above the actual "Shapeshifter" single), which sits 6th.
        # Walk the results instead of trusting relevance ranking alone.
        for result in results:
            found_artist, found_title = "", ""
            release_title = result.get("title") or ""
            if not _looks_related(artist, release_title):
                continue
            if " - " in release_title:
                found_artist, found_title = release_title.split(" - ", 1)
            else:
                found_title = release_title
            # Same class of bug confirmed on the Wikipedia path (see
            # fetch_wikipedia_description): checking the artist alone lets a
            # fuzzy search match some other release by the right artist, whose
            # cover/notes then get shown for a completely different album.
            # Only checked when a title was actually given -- the fallback
            # (fallback_query, no title) call has no specific title to verify.
            if title and not _looks_related(title, found_title):
                # found_title here is the *release* (album) title, not the
                # matched track -- Discogs' release search matches text
                # anywhere in the release, tracklist included, so a query for
                # one specific song routinely surfaces the right release under
                # an album title that shares nothing with the song name.
                # Confirmed directly: a search for "Потап и Настя Не Хватило
                # Воздуха" correctly found the release "Потап и Настя - Все
                # Пучком", track 20 of which is literally "Не Хватило
                # Воздуха" -- rejecting on the album-title mismatch alone
                # threw away a fully correct match. Before giving up, check
                # whether the track is actually in this release's tracklist.
                resource_url = result.get("resource_url") or ""
                if resource_url and _tracklist_has_title(resource_url, title):
                    found_title = title
                else:
                    continue

            cover_bytes = None
            img_url = result.get("cover_image") or result.get("thumb")
            if img_url:
                try:
                    img_req = urllib.request.Request(img_url, headers={"User-Agent": "MusicmaniaRadio/1.0"})
                    with urllib.request.urlopen(img_req, timeout=6) as img_resp:
                        cover_bytes = img_resp.read()
                except Exception as e:
                    print(f"discogs cover fetch failed: {e}", file=sys.stderr)

            return {
                "artist": found_artist.strip(),
                "title": found_title.strip(),
                "cover": cover_bytes,
                "resource_url": result.get("resource_url") or "",
            }
        return None
    except Exception as e:
        print(f"discogs lookup failed: {e}", file=sys.stderr)
        return None


# Discogs' "notes" field is *release* packaging/pressing/legal trivia as
# often as it's an actual write-up -- confirmed directly on Король и Шут's
# various self-titled release entries: "Moscow region version, on cover:
# 'sale prohibited in Saint Petersburg'", "exclusive distributor in
# Moscow...", recording-studio credit lines with nothing else. None of
# that is a "description" in any useful sense, but nothing about it makes
# try_discogs_lookup's artist-match check reject it -- it's genuinely
# about the right artist, just not descriptive content. Reject on these
# administrative/packaging signal phrases instead.
_DISCOGS_BOILERPLATE_SIGNALS = (
    "booklet", "insert card", "gatefold", "digipak", "jewel case",
    "pressed at", "distributor", "distributed by", "barcode", "matrix",
    "numbered copies", "limited edition of",
    "tracklist is specified according to",
    "sale prohibited", "sale is prohibited", "sold only in",
    # Box-set/reissue packaging text -- confirmed on a real 5-CD bundle's
    # back-cover/sleeve/disc legal copy: nothing but trademark and
    # manufacturing notices repeated per-disc, no actual description.
    "all trademarks", "registered trademark", "marca registrada",
    "previously released material", "made in the eu", "made in austria",
    "distributed by sony", "exclusive trademark of",
    # Vinyl/CD publisher-rights credit blocks -- confirmed on a real Sergio
    # Mendes A&M pressing: a per-track list of performing-rights-org codes
    # and "Produced for X Productions" lines, nothing descriptive at all.
    "printed in", "produced for", " bmi", " ascap", " sesac",
    "дистрибьютор", "тираж", "штрихкод", "продажа на территории",
    "запрещена",
)

# Per-track position codes (A1, A2a, B4, C4, D5, ...) are how vinyl/CD
# publisher-credit blocks key each line to a specific track -- three or
# more of these is a strong, general signal of exactly that kind of listing
# regardless of which specific boilerplate phrases happen to be present.
_TRACK_CODE_RE = re.compile(r"\b[A-D]\d{1,2}[a-z]?\b")


def _looks_like_real_description(text):
    if not text:
        return False
    low = text.lower()
    if any(signal in low for signal in _DISCOGS_BOILERPLATE_SIGNALS):
        return False
    # Legal/manufacturing copy is dense with copyright (©) and phonogram
    # (℗) marks -- real descriptive prose essentially never uses either
    # more than once, if at all.
    if (text.count("©") + text.count("℗")) >= 2:
        return False
    if len(set(_TRACK_CODE_RE.findall(text))) >= 3:
        return False
    # Short admin-only notes ("Recorded 1997. (C) Label.") slip past the
    # phrase list above but still aren't a description -- require enough
    # actual content for it to plausibly be one.
    return len(text.strip()) >= 80


def fetch_discogs_description(resource_url):
    # The search endpoint above only returns a release summary -- the actual
    # liner-note-style write-up (label history, personnel, context) only
    # comes back from the full release resource, one extra call per track.
    # Discogs' quota is per-minute not per-day, so this is sustainable at
    # this track-change rate.
    if not resource_url:
        return ""
    req = urllib.request.Request(
        resource_url,
        headers={
            "User-Agent": "MusicmaniaRadio/1.0",
            "Authorization": f"Discogs token={discogs_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            release = json.loads(resp.read().decode("utf-8"))
        notes = (release.get("notes") or "").strip()[:2000]
        if not _looks_like_real_description(notes):
            return ""
        return notes
    except Exception as e:
        print(f"discogs description fetch failed: {e}", file=sys.stderr)
        return ""


_QUOTED_NAME_RE = re.compile(r"«[^»]+»")
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def _quoted_spans(text):
    return " ".join(_QUOTED_NAME_RE.findall(text or ""))


def _looks_related_in_extract(name, extract):
    # A plain substring hit anywhere in the extract is too easy to satisfy
    # by accident for a Cyrillic name -- confirmed directly: the band
    # "Ленинград" fuzzy-matched the Saint Petersburg Wikipedia article,
    # passing only via "...Ленинградской областью..." ("Leningrad Oblast",
    # an unrelated neighboring administrative region) -- "Ленинград" is
    # simply a case-inflection prefix of that region's own name, nothing to
    # do with the band. The article's own title ("Санкт-Петербург") never
    # matched at all, so this was the *only* signal that passed for an
    # article that isn't about the artist in any sense. Russian
    # orthographic convention puts a musical group's (or song's) own name
    # in «guillemets» specifically to set it apart from an ordinary word or
    # place name -- confirmed against the real band's own article, which
    # opens "«Ленингра́д» ... российская музыкальная группа" exactly this
    # way. Require that instead of a bare substring hit, but only for
    # Cyrillic names: Latin-script artist names aren't conventionally
    # quoted like this in Russian prose, so the plain check still applies
    # to them, where this kind of common-word/toponym collision doesn't
    # arise in the first place.
    if _is_cyrillic(name):
        return _looks_related(name, _quoted_spans(extract))
    return _looks_related(name, extract)


def _is_cyrillic(text):
    return bool(_CYRILLIC_RE.search(text or ""))


def _trim_to_boundary(text, limit):
    # A hard slice at a fixed character count cuts mid-word/mid-sentence
    # about as often as not (confirmed directly on a real description that
    # landed exactly on the 2000-char mark, ending "...до 2030 года»
    # предпо"). Prefer the last full sentence within the limit; if none
    # exists (a long sentence with no period in the first `limit` chars),
    # fall back to the last full word instead of chopping one in half.
    if len(text) <= limit:
        return text
    window = text[:limit]
    for stop in (". ", "! ", "? "):
        idx = window.rfind(stop)
        if idx != -1:
            return window[: idx + 1].rstrip()
    idx = window.rfind(" ")
    return (window[:idx] if idx != -1 else window).rstrip() + "…"


def _wikipedia_lookup(lang, query_text, artist="", title=""):
    # Second-tier description source, for when Discogs has no release notes
    # (a lot of releases don't). Wikipedia's own search+extract API, no key
    # needed -- search first since we rarely know the exact article title,
    # then pull the plain-text intro of whatever it finds.
    headers = {"User-Agent": "MusicmaniaRadio/1.0"}
    try:
        # srlimit=5, not 1 -- a plain-word artist name can rank an
        # unrelated, more-linked article above its own (confirmed directly:
        # "Ленинград" the band's own article is titled "Ленинград
        # (группа)", but a bare "Ленинград" search ranks the Saint
        # Petersburg city article -- itself formerly named "Ленинград" --
        # ahead of it). Only taking the top hit meant that once it failed
        # the relatedness checks below, the search gave up entirely instead
        # of trying the next, actually-correct candidate.
        search_url = (
            f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json"
            "&list=search&srlimit=5&srsearch=" + urllib.parse.quote(query_text)
        )
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            search_data = json.loads(resp.read().decode("utf-8"))
        results = (search_data.get("query") or {}).get("search") or []
        if not results:
            return ""
        page_ids = [str(r["pageid"]) for r in results]
        extract_url = (
            f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json"
            f"&prop=extracts&exintro=1&explaintext=1&pageids={'|'.join(page_ids)}"
        )
        req2 = urllib.request.Request(extract_url, headers=headers)
        with urllib.request.urlopen(req2, timeout=6) as resp2:
            extract_data = json.loads(resp2.read().decode("utf-8"))
        pages_by_id = (extract_data.get("query") or {}).get("pages") or {}

        for result in results:
            page = pages_by_id.get(str(result["pageid"])) or {}
            extract = (page.get("extract") or "").strip()
            if not extract:
                continue
            # Wikipedia's disambiguation pages ("Mako may refer to:" /
            # russian "Название может означать:") are a search hit like
            # any other article -- they just list unrelated things sharing
            # a word, not a description of anything. Confirmed directly
            # this needed to search the whole extract, not just its first
            # 40 characters: "Альянс" (a real Soviet/Russian band) matched
            # an article that opens with the ordinary dictionary
            # definition of "альянс" the common noun ("союз, объединение...
            # договорных обязательств") for ~150 characters *before*
            # getting to "Также может означать:" -- still not actually
            # about the band, just further into the text than the position
            # check allowed. The trailing colon is what actually makes
            # this phrase distinctive (a normal sentence essentially never
            # uses it), so anchoring on that instead of position is both
            # safer and catches this case.
            if re.search(r"\b(may refer to|может означать)\s*:", extract, re.IGNORECASE):
                continue
            # Same idea, for a different class of false match: rural
            # locality stub articles (thousands of them, all near-identical
            # copy, in both the English and Russian Wikipedias) share a
            # name with plenty of bands/artists purely by coincidence --
            # confirmed directly on the English side: the band "The Вепри"
            # has no article of its own, so the artist-only fallback query
            # (no track title left to cross-check against, see below)
            # matched the village "Вепри, Vologda Oblast" instead: "The"
            # trivially appears in any English prose and "Вепри" is the
            # village's own name, so the artist-relatedness check right
            # below this passed despite being a completely unrelated
            # place. These template phrases ("is a rural locality ...
            # Oblast, Russia. The population was N as of YYYY" / "—
            # (деревня|село|посёлок) в ... районе") are distinctive enough
            # to reject outright -- they never legitimately describe a
            # musician.
            if re.search(r"\bis a rural locality\b", extract, re.IGNORECASE):
                continue
            if re.search(r"—\s*(деревня|село|посёлок|хутор)\b.{0,60}\bрайон", extract, re.IGNORECASE):
                continue
            # Accept if the artist shows up in either the matched article's
            # own title or its text -- a real article about the
            # track/artist will have one or the other, an unrelated
            # fuzzy-search hit will have neither.
            article_title = result.get("title", "")
            artist_matches_own_title = _looks_related(artist, article_title)
            if not (artist_matches_own_title or _looks_related_in_extract(artist, extract)):
                continue
            # Same guard, but for the specific track title -- confirmed
            # directly this was missing and caused a real mismatch:
            # searching "Robbie Williams By All Means Necessary" (a deep
            # album cut with no article of its own) fuzzy-matched the "Let
            # Me Entertain You" single's page instead. That page passed
            # the artist check above (same artist!) but described a
            # completely different song. Only checked when a title was
            # actually given -- the artist-only fallback call below has no
            # specific song to verify against.
            title_matches_own_title = _looks_related(title, article_title) if title else False
            if title and not (title_matches_own_title or _looks_related_in_extract(title, extract)):
                continue
            # Neither check matched the article's own title -- both artist
            # and title relied entirely on scattered mentions somewhere in
            # the extract, which is weak once that extract is a long list
            # of names. Confirmed directly: "АлисА Шанс" surfaced
            # "Рыженко, Сергей Ильич" (an unrelated session musician),
            # matching "Алиса" only because it's one of many bands in a
            # "recorded with..." enumeration, and "Шанс" only via an
            # unrelated band called "Последний шанс" mentioned nearby --
            # the article is about neither the artist nor the track, just
            # a person who happened to brush past both names in a long
            # career summary. A bio like that reads as a dense run of
            # quoted proper nouns; a real song/artist article essentially
            # never does.
            if title and not artist_matches_own_title and not title_matches_own_title:
                if len(_QUOTED_NAME_RE.findall(extract)) >= 5:
                    continue
            return _trim_to_boundary(extract, 2000)
        return ""
    except Exception as e:
        print(f"wikipedia lookup failed ({lang}): {e}", file=sys.stderr)
        return ""


def fetch_wikipedia_description(query_text, artist="", title=""):
    # Russian-speaking audience -- prefer a Russian-language description
    # when Wikipedia actually has one, only falling back to English when
    # it doesn't.
    query_text = (query_text or "").strip()
    if not query_text:
        return ""
    return _wikipedia_lookup("ru", query_text, artist, title) or _wikipedia_lookup("en", query_text, artist, title)


def extract_fast_metadata(path):
    """Everything derivable from local files alone -- no network calls.
    Kept separate from the enrichment lookups below so the "what's
    playing" tag can go out the instant a track starts instead of only
    once Discogs/Wikipedia/YouTube have all answered.
    """
    artist = ""
    title = ""
    duration = None
    data = None
    f = None
    try:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4
        from mutagen.easyid3 import EasyID3

        try:
            f = mutagen.File(path)
        except Exception:
            f = None
        if f is None:
            # Some FLAC rips have a bogus/garbled ID3v2 tag glued onto the
            # front (from old ripping tools), which trips up mutagen's
            # generic format auto-detection even though the real, correct
            # tags -- and cover art -- are sitting right there in the
            # native FLAC block.
            try:
                f = FLAC(path)
            except Exception:
                f = None
        if f is not None and getattr(f, "info", None) is not None:
            duration = getattr(f.info, "length", None)
        if isinstance(f, FLAC) and f.pictures:
            data = f.pictures[0].data
        if f is not None and f.tags:
            try:
                artist = (f.tags.get("artist") or [""])[0]
                title = (f.tags.get("title") or [""])[0]
            except Exception:
                pass
        if f is not None and not artist and not title:
            try:
                easy = EasyID3(path)
                artist = artist or (easy.get("artist") or [""])[0]
                title = title or (easy.get("title") or [""])[0]
            except Exception:
                pass
        if f is not None:
            try:
                tags = ID3(path)
                apics = tags.getall("APIC")
                if apics and not data:
                    data = apics[0].data
            except Exception:
                pass
            if data is None and isinstance(f, MP4):
                covr = f.tags.get("covr") if f.tags else None
                if covr:
                    data = bytes(covr[0])
                if f.tags:
                    artist = artist or (f.tags.get("\xa9ART") or [""])[0]
                    title = title or (f.tags.get("\xa9nam") or [""])[0]
    except Exception as e:
        print(f"metadata extraction failed: {e}", file=sys.stderr)

    # queue_filler.py already captured whatever Telegram itself knew about
    # this file (its own title/performer fields) at download time, as a
    # fallback for exactly this situation -- our own tag extraction failing.
    # It's sitting right next to the audio file; use it before reaching for
    # an external API.
    sidecar_query = ""
    sidecar = {}
    sidecar_path = os.path.splitext(path)[0] + ".json"
    try:
        with open(sidecar_path) as sf:
            sidecar = json.load(sf)
        if not artist or not title:
            artist = artist or (sidecar.get("artist") or "")
            title = title or (sidecar.get("title") or "")
            sidecar_query = f"{sidecar.get('artist', '')} {sidecar.get('title', '')}".strip()
    except Exception:
        pass

    # The description post's own "🎧 Качество: FLAC 16 bit / 44.1 kHz,
    # WEB" line (parsed by queue_filler.py at download time, into the
    # sidecar) is authoritative -- it names the release's actual source
    # (WEB/CD/LP), which the file itself can never tell us. Only fall
    # back to the file's own real bit depth/sample rate (no source tag
    # possible that way) when the post didn't have that line at all.
    quality = sidecar.get("quality") or None
    if not quality and isinstance(f, FLAC) and getattr(f, "info", None) is not None:
        bits = getattr(f.info, "bits_per_sample", None)
        rate = getattr(f.info, "sample_rate", None)
        if bits and rate:
            khz = round(rate / 1000, 1)
            khz_str = f"{khz:g}"
            quality = f"{bits} bit / {khz_str} kHz"

    source_bitrate_kbps = None
    if duration and duration > 0:
        try:
            size_bytes = os.path.getsize(path)
            source_bitrate_kbps = round((size_bytes * 8) / duration / 1000)
        except OSError:
            pass

    return {
        "artist": artist,
        "title": title,
        "duration": duration,
        "cover_data": data,
        "quality": quality,
        "source_bitrate_kbps": source_bitrate_kbps,
        "sidecar_query": sidecar_query,
    }


def write_cover(data):
    tmp_cover = os.path.join(webroot, "cover.jpg.tmp")
    with open(tmp_cover, "wb") as out:
        out.write(data)
    os.replace(tmp_cover, os.path.join(webroot, "cover.jpg"))


def enrich(track_id, artist, title, sidecar_query, cover_already_written, started_at):
    """The slow, network-bound half: Discogs/Wikipedia/YouTube lookups.
    Runs in a fully detached background process (see main()) so it never
    holds up the fast nowplaying.json write above, and patches the same
    file in-place once it has answers.
    """
    description = ""
    cover_written = cover_already_written
    cover_data = None
    discogs = try_discogs_lookup(artist, title, fallback_query=sidecar_query)
    if discogs:
        artist = artist or discogs["artist"]
        title = title or discogs["title"]
        if not cover_written and discogs.get("cover"):
            cover_data = discogs["cover"]
        description = fetch_discogs_description(discogs.get("resource_url"))

    if not description and artist and title:
        description = fetch_wikipedia_description(f"{artist} {title}", artist, title)
    if not description and artist:
        description = fetch_wikipedia_description(artist, artist)

    video_id = find_youtube_video_id(artist, title) if (artist and title) else None

    # The track that was live when this enrichment started may already be
    # over by the time these lookups come back (Discogs/Wikipedia/YouTube
    # together can take several seconds) -- only patch nowplaying.json if
    # it's still describing the same track, otherwise this would overwrite
    # a newer track's info with stale data for whatever's playing now.
    now = int(time.time())
    try:
        with open(os.path.join(webroot, "nowplaying.json")) as fh:
            live = json.load(fh)
    except Exception:
        live = None
    if live is None or live.get("started_at") != started_at:
        print(f"track moved on during enrichment for {track_id}, discarding", file=sys.stderr)
        return

    # Actually writing cover.jpg has to wait for the same staleness check --
    # confirmed directly: this write used to happen unconditionally above,
    # so a slow enrichment for a track that already ended could still
    # clobber cover.jpg with the *previous* track's art moments after
    # nowplaying.json (correctly guarded) had already moved on to the next
    # song, leaving the site showing the new track's name over the old
    # track's cover.
    if cover_data:
        write_cover(cover_data)
        cover_written = True

    live["artist"] = live.get("artist") or artist or ""
    live["title"] = live.get("title") or title or ""
    if cover_written and not live.get("cover"):
        live["cover"] = f"/radio/cover.jpg?t={now}"
    live["video_id"] = video_id
    live["description"] = description
    live["updated"] = now
    tmp_path = os.path.join(webroot, "nowplaying.json.tmp")
    with open(tmp_path, "w") as fh:
        json.dump(live, fh)
    os.replace(tmp_path, os.path.join(webroot, "nowplaying.json"))


def _prefetch_paths(track_id):
    base = os.path.join(prefetch_dir, str(track_id))
    return base + ".json", base + ".jpg"


def prefetch_enrich(track_id, artist, title):
    """Same lookups as enrich(), just run for a track that isn't live yet
    -- triggered by queue_list_writer.py once a track reaches the "coming
    up soon" position in the queue (see _trigger_prefetch there), well
    before Liquidsoap actually starts playing it. main()'s track-start
    path reads the result back instantly instead of redoing the same
    Discogs/Wikipedia/YouTube round trips live, which is what used to
    leave the site showing no description/video for the first few seconds
    of every track.
    """
    json_path, cover_path = _prefetch_paths(track_id)
    if os.path.exists(json_path):
        return
    os.makedirs(prefetch_dir, exist_ok=True)
    description = ""
    cover_data = None
    discogs = try_discogs_lookup(artist, title)
    if discogs:
        if discogs.get("cover"):
            cover_data = discogs["cover"]
        description = fetch_discogs_description(discogs.get("resource_url"))
    if not description and artist and title:
        description = fetch_wikipedia_description(f"{artist} {title}", artist, title)
    if not description and artist:
        description = fetch_wikipedia_description(artist, artist)
    video_id = find_youtube_video_id(artist, title) if (artist and title) else None

    has_cover = False
    if cover_data:
        tmp_cover = cover_path + ".tmp"
        with open(tmp_cover, "wb") as f:
            f.write(cover_data)
        os.replace(tmp_cover, cover_path)
        has_cover = True

    result = {"description": description, "video_id": video_id, "has_cover": has_cover}
    tmp_json = json_path + ".tmp"
    with open(tmp_json, "w") as f:
        json.dump(result, f)
    os.replace(tmp_json, json_path)


def _sweep_stale_prefetch(keep_id=None):
    if not os.path.isdir(prefetch_dir):
        return
    now = time.time()
    for name in os.listdir(prefetch_dir):
        track_id = name.split(".", 1)[0]
        if keep_id is not None and track_id == str(keep_id):
            continue
        path = os.path.join(prefetch_dir, name)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age > prefetch_max_age:
            try:
                os.remove(path)
            except OSError:
                pass


def main():
    if len(sys.argv) < 2:
        return

    if sys.argv[1] == "--enrich":
        _, _, track_id, artist, title, sidecar_query, cover_flag, started_at = sys.argv[:8]
        enrich(track_id, artist, title, sidecar_query, cover_flag == "1", int(started_at))
        return

    if sys.argv[1] == "--prefetch":
        _, _, track_id, artist, title = sys.argv[:5]
        prefetch_enrich(track_id, artist, title)
        return

    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"file gone, skipping publish: {path}", file=sys.stderr)
        return
    update_history_and_cleanup(path)
    track_id = os.path.splitext(os.path.basename(path))[0]

    meta = extract_fast_metadata(path)
    artist = meta["artist"]
    title = meta["title"]
    duration = meta["duration"]

    cover_written = False
    if meta["cover_data"]:
        write_cover(meta["cover_data"])
        cover_written = True

    # If queue_list_writer.py already had this track prefetched (it was
    # sitting at the "coming up soon" queue position for long enough --
    # see _trigger_prefetch there), use those answers immediately instead
    # of kicking off a fresh async enrich() below: no Discogs/Wikipedia/
    # YouTube round trip needed, no few-seconds gap where the site shows
    # no description/video for a track that's already playing.
    prefetch_json_path, prefetch_cover_path = _prefetch_paths(track_id)
    prefetched = None
    if os.path.exists(prefetch_json_path):
        try:
            with open(prefetch_json_path) as pf:
                prefetched = json.load(pf)
        except Exception as e:
            print(f"prefetch read failed for {track_id}: {e}", file=sys.stderr)

    description = ""
    video_id = None
    if prefetched is not None:
        description = prefetched.get("description") or ""
        video_id = prefetched.get("video_id")
        if not cover_written and prefetched.get("has_cover") and os.path.exists(prefetch_cover_path):
            try:
                with open(prefetch_cover_path, "rb") as cf:
                    write_cover(cf.read())
                cover_written = True
            except Exception as e:
                print(f"prefetch cover read failed for {track_id}: {e}", file=sys.stderr)
        for stale_path in (prefetch_json_path, prefetch_cover_path):
            try:
                os.remove(stale_path)
            except OSError:
                pass

    now = int(time.time())
    info = {
        "artist": artist or "",
        "title": title or "",
        "cover": f"/radio/cover.jpg?t={now}" if cover_written else None,
        "link": f"https://t.me/{channel_username}/{track_id}" if track_id.isdigit() else None,
        "duration": round(duration) if duration else None,
        "video_id": video_id,
        "description": description,
        "quality": meta["quality"],
        "source_bitrate_kbps": meta["source_bitrate_kbps"],
        "started_at": now,
        "updated": now,
    }
    tmp_path = os.path.join(webroot, "nowplaying.json.tmp")
    with open(tmp_path, "w") as fh:
        json.dump(info, fh)
    os.replace(tmp_path, os.path.join(webroot, "nowplaying.json"))

    # Refresh queue.json in the same instant nowplaying.json changes --
    # previously queue.json was only ever refreshed on queue_filler.py's
    # own loop cadence, independent of actual track transitions, so it
    # could lag behind what's really playing by however long that loop's
    # current iteration (sometimes a slow download+lookup cycle) took.
    # This is also what triggers the *next* track's prefetch (see
    # _trigger_prefetch in queue_list_writer.py).
    try:
        write_queue_list()
    except Exception as e:
        print(f"queue list refresh failed: {e}", file=sys.stderr)

    _sweep_stale_prefetch(keep_id=track_id)

    if prefetched is not None:
        # Already have everything -- nothing left to look up live.
        return

    # Everything above is local-only and finishes in well under a second;
    # the Discogs/Wikipedia/YouTube lookups below are the part that used to
    # make nowplaying.json (and therefore the site's "now playing" tag) lag
    # behind the actual audio by several seconds, because they all ran
    # synchronously before nowplaying.json was ever written. Launching them
    # as a fully detached subprocess (its own session, stdio on /dev/null so
    # it doesn't hold open the pipe Liquidsoap's process.read() is waiting
    # on) lets this process exit immediately -- Liquidsoap's on_new_track
    # handler returns right away, and the enrichment patches cover art /
    # description / YouTube link into nowplaying.json a few seconds later,
    # once it actually has them. Only still needed as a fallback now -- the
    # common case is the prefetch above already having the answers.
    subprocess.Popen(
        [
            sys.executable, os.path.abspath(__file__), "--enrich",
            track_id, artist or "", title or "", meta["sidecar_query"],
            "1" if cover_written else "0", str(now),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


main()
