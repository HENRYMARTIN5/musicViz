from typing import List, Tuple
from ytmusicapi import YTMusic
import os
import json
from tqdm import tqdm
import musicbrainzngs

GENRE_CACHE_FILE = "genre_cache.json"
ALBUM_YEAR_CACHE_FILE = "album_year_cache.json"
GENRE_BATCH_SIZE = 25

def load_cache(filepath: str) -> dict:
    """Loads a JSON cache file if it exists."""
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return json.load(f)
    return {}

def save_cache(cache: dict, filepath: str):
    """Saves a cache dictionary to a JSON file."""
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
    Retrieves genres for a batch of (artist_name, song_title) pairs using a
    single MusicBrainz search query per batch, instead of one query per song.

    Returns:
        dict: maps (artist_name, song_title) -> list of genres (possibly empty).
    """
    results = {pair: [] for pair in songs}

    query = " OR ".join(
        f'(artist:"{_lucene_escape(artist)}" AND recording:"{_lucene_escape(title)}")'
        for artist, title in songs
    )

    try:
        result = musicbrainzngs.search_recordings(query=query, limit=len(songs) * 3)
    except musicbrainzngs.WebServiceError as exc:
        print(f"Something went wrong with the batch request: {exc}")
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
                if 'tag-list' in recording:
                    results[(artist, title)] = [tag['name'] for tag in recording['tag-list']]
                unmatched.discard((artist, title))
                break

    return results

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

musicbrainzngs.set_useragent(
    "Henry Martin's Jumbled Music Taste Analysis", "0.1", "henrymartin.co@outlook.com"
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

    print(f"Fetching genres for {len(uncached_pairs)} uncached tracks in batches of {GENRE_BATCH_SIZE}...")
    for i in tqdm(range(0, len(uncached_pairs), GENRE_BATCH_SIZE)):
        batch = uncached_pairs[i:i + GENRE_BATCH_SIZE]
        batch_results = get_genres_batch(batch)
        for (artist, title), genres in batch_results.items():
            genre_cache[f"{artist}::{title}".lower()] = genres

    print("Processing tracks...")
    for position, song in enumerate(tqdm(playlist["tracks"])):
        primary_artist, title = song_artist_title(song)
        genre_cache_key = f"{primary_artist}::{title}".lower()
        genres = genre_cache.get(genre_cache_key, [])

        release_year = None
        if song.get('album'):
            album_id = song.get('album', {}).get('id')
        else:
            album_id = None

        if album_id:
            album_name = song.get('album').get('name')
            if album_id in album_year_cache:
                release_year = album_year_cache[album_id]
            else:
                try:
                    album_details = ytmusic.get_album(album_id)
                    release_year = album_details.get('year')
                    album_year_cache[album_id] = release_year
                except Exception:
                    album_year_cache[album_id] = None
        else:
            album_name = "Unknown Album"
        

        out.append(
            {
                'title': song['title'],
                'artists': [artist['name'] for artist in song.get('artists', [])],
                'is_explicit': song['isExplicit'],
                'duration': song['duration_seconds'],
                'genres': genres,
                'album': album_name,
                'year': release_year,
                'position': position
            }
        )
except KeyboardInterrupt:
    print("\nInterrupted, saving anyway")
finally:
    print("Writing out...")
    with open("out.json", "w") as f:
        json.dump(out, f, indent=2)
    
    print("Saving caches...")
    save_cache(genre_cache, GENRE_CACHE_FILE)
    save_cache(album_year_cache, ALBUM_YEAR_CACHE_FILE)
    print("Done.")