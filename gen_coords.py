"""
Companion to build_vectors.py, for the music viz website integration.

Builds genre vectors (same TF-IDF + junk-tag-filtering pipeline as
build_vectors.py) and reduces them to 2D/3D via both PCA and UMAP, then
dumps everything to a single JSON file the site can fetch client-side.

This is meant to be re-run whenever out.json / out_v2.json changes (i.e.
after re-running backfill_genres.py) - it's a build step, not something
computed in the browser. UMAP in particular has no good lightweight JS
port, and there's no reason to recompute PCA/UMAP on every page load when
the underlying genre data only changes when you re-tag songs.

Output: genre_coords.json, a list of objects:
{
  "position": 42,              // matches the `position` field in out.json,
                                // used to join back to the full song list
  "title": "...", "artists": [...], "year": 2019,
  "genres": ["indie rock", "lo-fi"],
  "family": "rock",            // coarse bucket, for coloring the plot
  "pca2": [x, y], "pca3": [x, y, z],
  "umap2": [x, y], "umap3": [x, y, z]
}

Songs with no genre tags are omitted (nothing to place them with).

Usage:
    python export_coords.py out_v2.json --top-k 80 --output genre_coords.json
"""

import argparse
import json
import re
import numpy as np
from collections import Counter
from sklearn.decomposition import PCA

NON_GENRE_TAGS = {
    "american", "british", "english", "uk", "usa", "australia", "australian",
    "canadian", "german", "french", "irish", "scottish", "welsh", "european",
    "japanese", "korean", "swedish", "norwegian", "danish", "dutch",
    "united kingdom", "united states", "great britain",
    "female vocalists", "male vocalists", "male vocalist", "female vocalist",
    "lgbt", "lgbtq", "queer", "transgender", "furry",
    "60s", "70s", "80s", "90s", "00s", "10s", "20s",
    "1960s", "1970s", "1980s", "1990s", "2000s", "2010s", "2020s",
    "favorites", "favourite", "favorite", "seen live", "awesome",
    "under 2000 listeners",
}
JUNK_PATTERNS = [
    re.compile(r"\d{4}"),
    re.compile(r"boycott"),
    re.compile(r"victim"),
    re.compile(r"-artiest$"),
    re.compile(r"^spotify"),
    re.compile(r"listeners$"),
]


def is_junk(tag: str) -> bool:
    if tag in NON_GENRE_TAGS:
        return True
    return any(p.search(tag) for p in JUNK_PATTERNS)


SOURCE_WEIGHT = {"recording": 1.0, "release-group": 0.85, "artist": 0.7, None: 1.0}

FAMILY_KEYWORDS = [
    ("metal/hardcore", ["metal", "hardcore", "screamo", "grindcore", "swancore"]),
    ("hip hop/rap", ["hip hop", "rap", "trap"]),
    ("electronic", ["electronic", "house", "techno", "idm", "breakcore", "synth", "edm", "dance", "hyperpop"]),
    ("rock", ["rock"]),
    ("pop", ["pop"]),
    ("jazz/soul/funk", ["jazz", "soul", "funk", "r&b"]),
    ("folk/country/acoustic", ["folk", "country", "acoustic", "singer-songwriter"]),
    ("soundtrack/vgm", ["soundtrack", "video game", "vgm", "score"]),
    ("ambient/experimental", ["ambient", "experimental", "noise", "drone"]),
]


def bucket_family(genres):
    gl = [g.lower() for g in genres]
    for family, keywords in FAMILY_KEYWORDS:
        if any(any(k in g for g in gl) for k in keywords):
            return family
    return "other"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="path to out.json / out_v2.json")
    parser.add_argument("--top-k", type=int, default=80)
    parser.add_argument("--min-count", type=int, default=2)
    parser.add_argument("--output", default="genre_coords.json")
    parser.add_argument("--skip-umap", action="store_true", help="skip UMAP (slower, needs umap-learn)")
    args = parser.parse_args()

    data = json.load(open(args.input))
    tagged = [d for d in data if d.get("genres")]
    print(f"{len(tagged)}/{len(data)} songs have genre tags")

    def clean_tags(genres):
        return [g.lower().strip() for g in genres if not is_junk(g.lower().strip())]

    tag_counts = Counter()
    for d in tagged:
        for g in clean_tags(d["genres"]):
            tag_counts[g] += 1
    eligible = [t for t, c in tag_counts.items() if c >= args.min_count]
    top_tags = [t for t, _ in Counter({t: tag_counts[t] for t in eligible}).most_common(args.top_k)]
    tag_idx = {t: i for i, t in enumerate(top_tags)}
    print(f"Using {len(top_tags)} genre tags as dimensions")

    n, d = len(tagged), len(top_tags)
    X = np.zeros((n, d), dtype=np.float32)
    for r, song in enumerate(tagged):
        weight = SOURCE_WEIGHT.get(song.get("genre_source"), 1.0)
        for g in clean_tags(song["genres"]):
            if g in tag_idx:
                X[r, tag_idx[g]] = weight

    doc_freq = X.sum(axis=0)
    idf = np.log((n + 1) / (doc_freq + 1)) + 1.0
    X_tfidf = X * idf
    norms = np.linalg.norm(X_tfidf, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X_norm = X_tfidf / norms

    print("Running PCA (2D and 3D)...")
    pca2 = PCA(n_components=2, random_state=42).fit_transform(X_norm)
    pca3 = PCA(n_components=3, random_state=42).fit_transform(X_norm)

    umap2 = umap3 = None
    if not args.skip_umap:
        try:
            import umap
            print("Running UMAP (2D and 3D)...")
            umap2 = umap.UMAP(n_components=2, random_state=42).fit_transform(X_norm)
            umap3 = umap.UMAP(n_components=3, random_state=42).fit_transform(X_norm)
        except ImportError:
            print("umap-learn not installed, skipping UMAP coords "
                  "(pip install umap-learn --break-system-packages)")

    out = []
    for i, song in enumerate(tagged):
        entry = {
            "position": song.get("position"),
            "title": song["title"],
            "artists": song["artists"],
            "year": song.get("year"),
            "genres": song["genres"],
            "family": bucket_family(song["genres"]),
            "pca2": [round(float(v), 4) for v in pca2[i]],
            "pca3": [round(float(v), 4) for v in pca3[i]],
        }
        if umap2 is not None:
            entry["umap2"] = [round(float(v), 4) for v in umap2[i]]
            entry["umap3"] = [round(float(v), 4) for v in umap3[i]]
        out.append(entry)

    with open(args.output, "w") as f:
        json.dump(out, f)
    print(f"Wrote {len(out)} songs to {args.output}")


if __name__ == "__main__":
    main()