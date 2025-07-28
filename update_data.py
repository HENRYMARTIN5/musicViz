from typing import List
from ytmusicapi import YTMusic
import os
import json
from tqdm import tqdm
import musicbrainzngs

GENRE_CACHE_FILE = "genre_cache.json"
ALBUM_YEAR_CACHE_FILE = "album_year_cache.json"

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

def get_genres(artist_name: str, song_title: str) -> List[str]:
    """
    Retrieves genres for a song using its title and artist name from MusicBrainz.

    Args:
        artist_name (str): The name of the artist.
        song_title (str): The title of the song.

    Returns:
        list: A list of genres for the song, or an empty list if not found.
    """

    musicbrainzngs.set_useragent(
        "Henry Martin's Jumbled Music Taste Analysis", "0.1", "henrymartin.co@outlook.com"
    )

    try:
        result = musicbrainzngs.search_recordings(
            artist=artist_name, recording=song_title, limit=1
        )

        if result['recording-list']:
            recording = result['recording-list'][0]
            
            if 'tag-list' in recording:
                return [tag['name'] for tag in recording['tag-list']]

            elif 'artist-credit' in recording and recording['artist-credit']:
                artist_id = recording['artist-credit'][0]['artist']['id']
                artist_info = musicbrainzngs.get_artist_by_id(artist_id, includes=['tags'])
                if 'artist' in artist_info and 'tag-list' in artist_info['artist']:
                    return [tag['name'] for tag in artist_info['artist']['tag-list']]

    except musicbrainzngs.WebServiceError as exc:
        print(f"Something went wrong with the request: {exc}")

    return []

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

out = []

try:
    print("Processing tracks...")
    for song in tqdm(playlist["tracks"]):
        primary_artist = song['artists'][0]['name'] if song.get('artists') else 'Unknown Artist'
        title = song.get('title', 'Unknown Title')
        genre_cache_key = f"{primary_artist}::{title}".lower()

        if genre_cache_key in genre_cache:
            genres = genre_cache[genre_cache_key]
        else:
            genres = get_genres(primary_artist, title)
            genre_cache[genre_cache_key] = genres
        
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
                'year': release_year
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