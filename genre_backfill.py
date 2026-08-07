"""
Extended version of the original pull script with genre backfill.

Key change: recording-level MusicBrainz tags are sparse (a huge fraction of
recordings have zero tags even for well-known songs). This adds two fallback
tiers when a recording search comes back empty:

  1. Artist-level tags (artists get tagged far more consistently)
  2. Release-group (album) tags

Each genre entry in the output now carries a `genre_source` field
("recording" / "artist" / "release-group" / "none") so you can weight
confidence downstream if you want (e.g. discount artist-inherited tags
in your vector encoding).

Usage: same as your original script - reads playlist_cache.json if present,
otherwise fetches via ytmusicapi. Caches are separate files so you don't
have to re-hit the API for stuff you've already resolved.
"""

from typing import List, Tuple, Optional
from ytmusicapi import YTMusic
import os
import json
from tqdm import tqdm
import musicbrainzngs

GENRE_CACHE_FILE = "genre_cache_v2.json"
ALBUM_YEAR_CACHE_FILE = "album_year_cache.json"
ARTIST_TAG_CACHE_FILE = "artist_tag_cache.json"
GENRE_BATCH_SIZE = 25


def load_cache(filepath: str) -> dict:
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict, filepath: str):
    with open(filepath, "w") as f:
        json.dump(cache, f, indent=2)


def _lucene_escape(text: str) -> str:
    for char in '+-&&||!(){}[]^"~*?:\\/':
        text = text.replace(char, f'\\{char}')
    return text


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def get_genres_batch(songs: List[Tuple[str, str]]) -> dict:
    """
    Tier 1: recording-level tags, batched via a single Lucene OR query.
    Returns dict[(artist, title)] -> (genres list, source str or None)
    """
    results = {pair: ([], None) for pair in songs}

    query = " OR ".join(
        f'(artist:"{_lucene_escape(artist)}" AND recording:"{_lucene_escape(title)}")'
        for artist, title in songs
    )

    try:
        result = musicbrainzngs.search_recordings(query=query, limit=len(songs) * 3)
    except musicbrainzngs.WebServiceError as exc:
        print(f"Recording batch request failed: {exc}")
        return results

    unmatched = set(songs)
    for recording in result.get('recording-list', []):
        if not unmatched:
            break

        rec_title = _normalize(recording.get('title', ''))
        rec_artists = [
            _normalize(credit['artist']['name'])
            for credit in recording.get('artist-credit', [])
            if isinstance(credit, dict) and 'artist' in credit
        ]

        for artist, title in list(unmatched):
            if _normalize(title) == rec_title and _normalize(artist) in rec_artists:
                if 'tag-list' in recording and recording['tag-list']:
                    tags = [tag['name'] for tag in recording['tag-list']]
                    results[(artist, title)] = (tags, "recording")
                unmatched.discard((artist, title))
                break

    return results


def get_artist_tags(artist_name: str, cache: dict) -> List[str]:
    """
    Tier 2 fallback: look up tags on the artist entity itself.
    Cached separately since many songs share an artist (huge dedup win).
    """
    key = artist_name.lower()
    if key in cache:
        return cache[key]

    tags: List[str] = []
    try:
        result = musicbrainzngs.search_artists(artist=_lucene_escape(artist_name), limit=5)
        for artist in result.get('artist-list', []):
            if _normalize(artist.get('name', '')) == _normalize(artist_name):
                if 'tag-list' in artist and artist['tag-list']:
                    tags = [t['name'] for t in artist['tag-list']]
                break
        # if no exact match, fall back to top result if reasonably close
        if not tags and result.get('artist-list'):
            top = result['artist-list'][0]
            if 'tag-list' in top and top['tag-list']:
                tags = [t['name'] for t in top['tag-list']]
    except musicbrainzngs.WebServiceError as exc:
        print(f"Artist lookup failed for {artist_name}: {exc}")

    cache[key] = tags
    return tags


def get_release_group_tags(artist_name: str, album_name: str, cache: dict) -> List[str]:
    """Tier 3 fallback: tags on the release-group (album)."""
    key = f"{artist_name}::{album_name}".lower()
    if key in cache:
        return cache[key]

    tags: List[str] = []
    if album_name and album_name != "Unknown Album":
        try:
            result = musicbrainzngs.search_release_groups(
                artist=_lucene_escape(artist_name),
                releasegroup=_lucene_escape(album_name),
                limit=3,
            )
            for rg in result.get('release-group-list', []):
                if 'tag-list' in rg and rg['tag-list']:
                    tags = [t['name'] for t in rg['tag-list']]
                    break
        except musicbrainzngs.WebServiceError as exc:
            print(f"Release-group lookup failed for {album_name}: {exc}")

    cache[key] = tags
    return tags


print("Logging in...")
ytmusic = YTMusic()

if not os.path.exists("playlist_cache.json"):
    playlist_id = os.getenv("PLAYLIST_ID", "PLIgbDqfLovfQ8o5jWuQFL0p36vLxu1487")
    print("Fetching playlist...")
    playlist = ytmusic.get_playlist(playlist_id, limit=None)
    with open("playlist_cache.json", "w") as f:
        json.dump(playlist, f)
else:
    print("Loading playlist from playlist_cache.json...")
    with open("playlist_cache.json", "r") as f:
        playlist = json.load(f)

print("Loading API caches...")
genre_cache = load_cache(GENRE_CACHE_FILE)
album_year_cache = load_cache(ALBUM_YEAR_CACHE_FILE)
artist_tag_cache = load_cache(ARTIST_TAG_CACHE_FILE)
release_group_cache = load_cache("release_group_tag_cache.json")

musicbrainzngs.set_useragent(
    "Henry Martin's Jumbled Music Taste Analysis", "0.2", "henrymartin.co@outlook.com"
)

out = []


def song_artist_title(song: dict) -> Tuple[str, str]:
    primary_artist = song['artists'][0]['name'] if song.get('artists') else 'Unknown Artist'
    title = song.get('title', 'Unknown Title')
    return primary_artist, title


try:
    uncached_pairs = []
    seen_pairs = set()
    for song in playlist["tracks"]:
        primary_artist, title = song_artist_title(song)
        genre_cache_key = f"{primary_artist}::{title}".lower()
        pair = (primary_artist, title)
        if genre_cache_key not in genre_cache and pair not in seen_pairs:
            uncached_pairs.append(pair)
            seen_pairs.add(pair)

    print(f"Tier 1 (recording tags): fetching {len(uncached_pairs)} uncached tracks in batches of {GENRE_BATCH_SIZE}...")
    for i in tqdm(range(0, len(uncached_pairs), GENRE_BATCH_SIZE)):
        batch = uncached_pairs[i:i + GENRE_BATCH_SIZE]
        batch_results = get_genres_batch(batch)
        for (artist, title), (genres, source) in batch_results.items():
            genre_cache[f"{artist}::{title}".lower()] = {"genres": genres, "source": source}

    print("Processing tracks (with artist/release-group fallback)...")
    for position, song in enumerate(tqdm(playlist["tracks"])):
        primary_artist, title = song_artist_title(song)
        genre_cache_key = f"{primary_artist}::{title}".lower()
        entry = genre_cache.get(genre_cache_key, {"genres": [], "source": None})
        genres = entry["genres"]
        source = entry["source"]

        album_id = song.get('album', {}).get('id') if song.get('album') else None
        album_name = song.get('album', {}).get('name') if song.get('album') else "Unknown Album"

        # Tier 2: artist tags
        if not genres:
            genres = get_artist_tags(primary_artist, artist_tag_cache)
            if genres:
                source = "artist"

        # Tier 3: release-group tags
        if not genres:
            genres = get_release_group_tags(primary_artist, album_name, release_group_cache)
            if genres:
                source = "release-group"

        if not genres:
            source = "none"

        # persist resolved result back into main cache so re-runs don't repeat the fallback lookups
        genre_cache[genre_cache_key] = {"genres": genres, "source": source}

        release_year = None
        if album_id:
            if album_id in album_year_cache:
                release_year = album_year_cache[album_id]
            else:
                try:
                    album_details = ytmusic.get_album(album_id)
                    release_year = album_details.get('year')
                    album_year_cache[album_id] = release_year
                except Exception:
                    album_year_cache[album_id] = None

        out.append(
            {
                'title': song['title'],
                'artists': [artist['name'] for artist in song.get('artists', [])],
                'is_explicit': song['isExplicit'],
                'duration': song['duration_seconds'],
                'genres': genres,
                'genre_source': source,
                'album': album_name,
                'year': release_year,
                'position': position,
            }
        )
except KeyboardInterrupt:
    print("\nInterrupted, saving anyway")
finally:
    print("Writing out...")
    with open("out_v2.json", "w") as f:
        json.dump(out, f, indent=2)

    print("Saving caches...")
    save_cache(genre_cache, GENRE_CACHE_FILE)
    save_cache(album_year_cache, ALBUM_YEAR_CACHE_FILE)
    save_cache(artist_tag_cache, ARTIST_TAG_CACHE_FILE)
    save_cache(release_group_cache, "release_group_tag_cache.json")

    tagged = sum(1 for d in out if d['genres'])
    print(f"Done. {tagged}/{len(out)} tracks now have genre tags ({tagged/len(out)*100:.1f}%).")
    from collections import Counter
    src_counts = Counter(d['genre_source'] for d in out)
    print("Source breakdown:", dict(src_counts))