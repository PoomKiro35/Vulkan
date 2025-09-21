import os
import re
import asyncio
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor

from Config.Configs import VConfigs
from Config.Exceptions import DownloadingError
from Music.Song import Song
from Utils.Utils import Utils, run_async

# Keep yt_dlp import to minimize ripple changes elsewhere,
# but we will no longer call it for YouTube.
from yt_dlp import YoutubeDL, DownloadError

try:
    from spotipy import Spotify
    from spotipy.oauth2 import SpotifyClientCredentials
except Exception:
    Spotify = None
    SpotifyClientCredentials = None


class Downloader:
    config = VConfigs()

    # yt_dlp options kept to preserve signatures, but we avoid using
    # them for YouTube entirely.
    __YDL_OPTIONS = {
        'format': 'bestaudio/best',
        'default_search': 'auto',
        'playliststart': 0,
        'extract_flat': False,
        'playlistend': config.MAX_PLAYLIST_LENGTH,
        'quiet': True,
        'ignore_no_formats_error': True,
    }
    __YDL_OPTIONS_EXTRACT = {
        'format': 'bestaudio/best',
        'default_search': 'auto',
        'playliststart': 0,
        'extract_flat': True,
        'playlistend': config.MAX_PLAYLIST_LENGTH,
        'quiet': True,
        'ignore_no_formats_error': True,
    }
    __YDL_OPTIONS_FORCE_EXTRACT = {
        'format': 'bestaudio/best',
        'default_search': 'auto',
        'playliststart': 0,
        'extract_flat': False,
        'playlistend': config.MAX_PLAYLIST_LENGTH,
        'quiet': True,
        'ignore_no_formats_error': True,
    }

    SPOTIFY_TRACK_RE = re.compile(r"(?:spotify:track:|open\.spotify\.com/track/)([A-Za-z0-9]+)")

    def __init__(self) -> None:
        self.__config = VConfigs()
        self.__music_keys_only = ['resolution', 'fps', 'quality']
        self.__not_extracted_keys_only = ['ie_key']
        self.__not_extracted_not_keys = ['entries']
        self.__playlist_keys = ['entries']

        # --- Spotify client (for preview playback) ---
        self.__sp: Optional[Spotify] = None
        cid = os.getenv("SPOTIFY_ID") or getattr(self.__config, "SPOTIFY_ID", None)
        csec = os.getenv("SPOTIFY_SECRET") or getattr(self.__config, "SPOTIFY_SECRET", None)
        if Spotify and SpotifyClientCredentials and cid and csec and cid != "SPOTIFY_ID" and csec != "SPOTIFY_SECRET":
            try:
                auth = SpotifyClientCredentials(client_id=cid, client_secret=csec)
                self.__sp = Spotify(auth_manager=auth, requests_timeout=15, retries=2)
            except Exception as e:
                print(f"DEVELOPER NOTE -> Failed to init Spotify client: {e}")
        else:
            # We won’t crash here; songs will simply fail if no Spotify client.
            print("DEVELOPER NOTE -> Spotify client not initialized (check SPOTIFY_ID/SECRET and spotipy install).")

    def finish_one_song(self, song: Song) -> Song:
        try:
            if song.identifier is None:
                return None

            if Utils.is_url(song.identifier):
                song_info = self.__download_url(song.identifier)
            else:
                song_info = self.__download_title(song.identifier)

            song.finish_down(song_info)
            return song
        except DownloadError as e:
            raise DownloadingError(e.msg)

    # ---------- PUBLIC: extract_info ----------
    # For URL inputs only; used for playlists/links.
    @run_async
    def extract_info(self, url: str) -> List[str]:
        if url == '':
            return []

        if not Utils.is_url(url):
            return []

        # Spotify inputs -> return normalized list of Spotify track URLs
        if "open.spotify.com" in url or "spotify:track:" in url or "spotify.com/playlist" in url or "spotify.com/album" in url:
            if self.__sp is None:
                print("DEVELOPER NOTE -> Spotify client missing; cannot extract Spotify URLs.")
                return []

            try:
                if "/track/" in url or "spotify:track:" in url:
                    # Single track
                    tid = self.__extract_spotify_track_id(url)
                    return [f"https://open.spotify.com/track/{tid}"] if tid else []

                # Playlist
                if "/playlist/" in url:
                    pl_id = self.__extract_between(url, "/playlist/", "?", allow_end=True)
                    if not pl_id:
                        return []
                    items = self.__sp.playlist_items(pl_id, additional_types=['track'], limit=100)
                    urls = []
                    while items:
                        for it in items.get("items", []):
                            tr = it.get("track") or {}
                            tid = tr.get("id")
                            if tid:
                                urls.append(f"https://open.spotify.com/track/{tid}")
                        next_url = items.get("next")
                        if next_url:
                            items = self.__sp.next(items)
                        else:
                            break
                    return urls

                # Album
                if "/album/" in url:
                    alb_id = self.__extract_between(url, "/album/", "?", allow_end=True)
                    if not alb_id:
                        return []
                    alb = self.__sp.album_tracks(alb_id, limit=50)
                    urls = []
                    while alb:
                        for tr in alb.get("items", []):
                            tid = tr.get("id")
                            if tid:
                                urls.append(f"https://open.spotify.com/track/{tid}")
                        next_url = alb.get("next")
                        if next_url:
                            alb = self.__sp.next(alb)
                        else:
                            break
                    return urls

                # Fallback: unknown spotify URL type
                return []
            except Exception as e:
                print(f"DEVELOPER NOTE -> Error Extracting Spotify URL(s): {e}")
                return []

        # Any YouTube-like URL: **skip** (don’t try yt_dlp)
        if "youtu.be" in url or "youtube.com" in url or "music.youtube" in url:
            print(f"DEVELOPER NOTE -> Skipping YouTube extract for URL: {url}")
            return []

        # Other providers not supported here -> return empty
        return []

    # ---------- INTERNAL HELPERS ----------
    def __extract_spotify_track_id(self, s: str) -> Optional[str]:
        m = self.SPOTIFY_TRACK_RE.search(s)
        return m.group(1) if m else None

    def __extract_between(self, s: str, start: str, stop: str, allow_end: bool = False) -> Optional[str]:
        try:
            i = s.index(start) + len(start)
            j = s.find(stop, i)
            if j == -1 and allow_end:
                return s[i:]
            return s[i:j] if j != -1 else None
        except ValueError:
            return None

    def __spotify_track_to_info(self, track_obj: dict) -> dict:
        """
        Build the minimal info dict used by Song:
        - title: clean title (Artist - Name)
        - duration: seconds (float)
        - url: direct audio URL (Spotify preview_url, 30s). If None -> no playback.
        """
        if not track_obj:
            return {}
        name = track_obj.get("name") or "Unknown"
        artists = ", ".join(a.get("name", "") for a in track_obj.get("artists", [])) or "Unknown"
        preview = track_obj.get("preview_url")  # may be None!
        duration = float(track_obj.get("duration_ms", 0) / 1000.0)
        info = {
            "title": f"{artists} - {name}",
            "duration": duration if duration > 0 else 5.0,
        }
        if preview:
            info["url"] = preview
        # If no preview, we intentionally do NOT set 'url' -> Song will self-destroy gracefully.
        return info

    def __download_spotify_url(self, url: str) -> dict:
        if self.__sp is None:
            print("DEVELOPER NOTE -> Spotify client missing; cannot download Spotify URL.")
            return {}
        tid = self.__extract_spotify_track_id(url)
        if not tid:
            return {}
        try:
            tr = self.__sp.track(tid)
            return self.__spotify_track_to_info(tr)
        except Exception as e:
            print(f"DEVELOPER NOTE -> Error fetching Spotify track {tid}: {e}")
            return {}

    def __search_spotify_first(self, query: str) -> dict:
        if self.__sp is None:
            print("DEVELOPER NOTE -> Spotify client missing; cannot search Spotify.")
            return {}
        try:
            res = self.__sp.search(q=query, type="track", limit=1)
            items = ((res or {}).get("tracks") or {}).get("items") or []
            if not items:
                return {}
            return self.__spotify_track_to_info(items[0])
        except Exception as e:
            print(f"DEVELOPER NOTE -> Error searching Spotify for '{query}': {e}")
            return {}

    # ---------- ORIGINAL PRIVATE METHODS (modified to block YouTube) ----------

    def __get_forced_extracted_info(self, url: str) -> list:
        # Disabled for YouTube-only behavior: keep signature but do nothing useful
        print(f"DEVELOPER NOTE -> Forced extract skipped for: {url}")
        return []

    def __download_url(self, url) -> dict:
        # Spotify URL -> use Spotify preview audio
        if isinstance(url, str) and ("open.spotify.com/track/" in url or "spotify:track:" in url):
            return self.__download_spotify_url(url)

        # Any YouTube-like URL -> explicitly block
        if isinstance(url, str) and ("youtu.be" in url or "youtube.com" in url or "music.youtube" in url):
            print(f"DEVELOPER NOTE -> YouTube disabled for URL: {url}")
            return {}

        # Unknown/other providers: do nothing (no YT fallback)
        print(f"DEVELOPER NOTE -> Unsupported URL provider (no fallback): {url}")
        return {}

    async def download_song(self, song: Song) -> None:
        if song.source is not None:  # already has audio URL
            return None

        def __download_func(song: Song) -> None:
            try:
                if Utils.is_url(song.identifier):
                    song_info = self.__download_url(song.identifier)
                else:
                    song_info = self.__download_title(song.identifier)
                song.finish_down(song_info)
            except Exception as e:
                print(f'DEVELOPER NOTE -> Error Downloading {song.identifier} -> {e}')

        loop = asyncio.get_event_loop()
        executor = ThreadPoolExecutor(max_workers=self.__config.MAX_PRELOAD_SONGS)
        fs = {loop.run_in_executor(executor, __download_func, song)}
        await asyncio.wait(fs=fs, return_when=asyncio.ALL_COMPLETED)

    def __download_title(self, title: str) -> dict:
        # DO NOT use yt_dlp search; go straight to Spotify
        if not title or not isinstance(title, str):
            return {}
        info = self.__search_spotify_first(title)
        if info and "url" in info:
            return info
        # No preview available -> fail (no YouTube fallback)
        print(f"DEVELOPER NOTE -> No Spotify preview for '{title}'.")
        return {}

    # The checks below were part of the original extractor logic;
    # we keep them to avoid breaking other code paths, but they
    # are no-ops for the Spotify-only flow.

    def __is_music(self, extracted_info: dict) -> bool:
        for key in self.__music_keys_only:
            if key not in extracted_info.keys():
                return False
        return True

    def __is_multiple_musics(self, extracted_info: dict) -> bool:
        for key in self.__playlist_keys:
            if key not in extracted_info.keys():
                return False
        return True

    def __failed_to_extract(self, extracted_info: dict) -> bool:
        if type(extracted_info) is not dict:
            return False
        for key in self.__not_extracted_keys_only:
            if key not in extracted_info.keys():
                return False
        for key in self.__not_extracted_not_keys:
            if key in extracted_info.keys():
                return False
        return True
