from pathlib import Path
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.flac import FLAC

MUSIC_ROOT = Path("media/Music")
COVER_NAME = "cover.jpg"
MISSING_LOG = Path("missing-album-art.txt")

AUDIO_EXTS = {".mp3", ".flac", ".m4a"}

missing = []

def get_audio_files(folder: Path):
    return sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS])

def extract_cover(audio_path: Path):
    ext = audio_path.suffix.lower()

    try:
        if ext == ".mp3":
            tags = ID3(audio_path)
            for tag in tags.values():
                if tag.FrameID == "APIC":
                    return bytes(tag.data)

        elif ext == ".m4a":
            tags = MP4(audio_path)
            covers = tags.tags.get("covr", [])
            if covers:
                return bytes(covers[0])

        elif ext == ".flac":
            tags = FLAC(audio_path)
            if tags.pictures:
                return bytes(tags.pictures[0].data)

    except Exception:
        return None

    return None

for artist_dir in [p for p in MUSIC_ROOT.iterdir() if p.is_dir()]:
    for album_dir in [p for p in artist_dir.iterdir() if p.is_dir()]:
        cover_path = album_dir / COVER_NAME

        if cover_path.exists():
            print(f"Skipping existing: {artist_dir.name} / {album_dir.name}")
            continue

        audio_files = get_audio_files(album_dir)
        if not audio_files:
            print(f"Skipping (no audio files): {artist_dir.name} / {album_dir.name}")
            continue

        saved = False
        for audio_file in audio_files:
            art = extract_cover(audio_file)
            if art:
                cover_path.write_bytes(art)
                print(f"Saved: {cover_path}")
                saved = True
                break

        if not saved:
            missing.append(f"{artist_dir.name} / {album_dir.name}")
            print(f"No embedded art found: {artist_dir.name} / {album_dir.name}")

if missing:
    MISSING_LOG.write_text("\n".join(missing), encoding="utf-8")
    print(f"\nMissing art log written to: {MISSING_LOG}")
else:
    if MISSING_LOG.exists():
        MISSING_LOG.unlink()
    print("\nAll scanned albums had embedded art.")