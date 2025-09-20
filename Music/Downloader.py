import re
import asyncio
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor

from yt_dlp import YoutubeDL, DownloadError  # kept for type/compat even though we won't call it
from Config.Configs import VConfigs
from Music.Song import Song
from Utils.Utils import Utils, run_async
from Config.Exceptions import DownloadingError

# --- Spotify ---
from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials


class Downloader:
    config = VConfigs()

    # Kept for compatibility; we won't use YouTube in "Spotify-only" mode
    __YDL_OPTIONS = {
        'format': 'bestaudio/best',
        'default_search': 'auto',
        'playliststart': 0,
        'extract_flat': False,
        'playlistend': config.MAX_PLAYLIST_LENGTH,
        'quiet': True,
        'ignore_no_formats_error': True
    }
    __YDL_OPTIONS_EXTRACT = {
        'format': 'bestaudio/best',
        'default_search': 'auto',
        'playliststart': 0,
        'extract_flat': True,
        'playlistend': config.MAX_PLAYLIST_LENGTH,
        'quiet': True,
        'ignore_no_formats_error': True
    }
    __YDL_OPTIONS_FORCE_EXTRACT = {
        'format': 'bestaudio/best',
        'default_search': 'auto',
        'playliststart': 0,
        'extract_flat': False,
        'playlistend': config.MAX_PLAYLIST_LENGTH,
        'quiet': True,
        'ignore_no_formats_error': True
    }

    # Only used if you still allow YouTube; left here so callers don't break
    __BASE_URL = 'https://www.youtube.com/watch?v={}'

    # --- REGEX for Spotify URLs ---
    _RE_SPOTIFY_TRACK = re.compile(r"(?:open|play)\.spotify\.com/track/([A-Za-z0-9]+)")
    _RE_SPOTIFY_PLAYLIST = re.compile(r"(?:open|play)\.spotify\.com/playlist/([A-Za-z0-9]+)")
    _RE_SPOTIFY_ALBUM = re.compile(r"(?:open|play)\.spotify\.com/album/([A-Za-z0-9]+)")

    def __init__(self) -> None:
        self.__config = VConfigs()
        import os
sid = os.getenv("SPOTIFY_ID") or getattr(self.__config, "SPOTIFY_ID", None)
ssec = os.getenv("SPOTIFY_SECRET") or getattr(self.__config, "SPOTIFY_SECRET", None)
def _mask(v): 
    return v if not v else f"{v[:3]}…{v[-4:]}"
print(f"DEVELOPER NOTE -> Spotify ID seen: {_mask(sid)}, Secret seen: {_mask(ssec)}")
if not sid or not ssec:
    print("DEVELOPER NOTE -> Spotify credentials not found in config; Spotify-only playback will fail.")

        self.__music_keys_only = ['resolution', 'fps', 'quality']
        self.__not_extracted_keys_only = ['ie_key']
        self.__not_extracted_not_keys = ['entries']
        self.__playlist_keys = ['entries']

        # ---- Spotify client (client credentials flow) ----
        self._sp: Optional[Spotify] = None
        try:
            sp_conf = VConfigs.getSpotify()
        except Exception:
            sp_conf = None

        if sp_conf and getattr(sp_conf, "client_id", None) and getattr(sp_conf, "client_secret", None):
            try:
                auth_mgr = SpotifyClientCredentials(
                    client_id=sp_conf.client_id,
                    client_secret=sp_conf.client_secret
                )
                self._sp = Spotify(auth_manager=auth_mgr)
            except Exception as e:
                print(f"DEVELOPER NOTE -> Failed to initialize Spotify client: {e}")
                self._sp = None
        else:
            print("DEVELOPER NOTE -> Spotify credentials not found in config; Spotify-only playback will fail.")

        # Hard enforce Spotify-only: never call YouTube/yt_dlp
        self._spotify_only = True

    # -------------------------
    # Public API (called by bot)
    # -------------------------

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
            # yt_dlp error converted to own error (kept for compatibility)
            raise DownloadingError(e.msg)
        except Exception as e:
            print(f"DEVELOPER NOTE -> finish_one_song failed: {e}")
            raise

    @run_async
    def extract_info(self, url: str) -> List[str]:
        """
        Return a list of identifiers to enqueue. In Spotify-only mode,
        we expand Spotify playlist/album/track URLs to Spotify track URLs.
        """
        if url == '':
            return []

        if not Utils.is_url(url):
            return []

        if self._is_spotify_url(url):
            if not self._sp:
                print("DEVELOPER NOTE -> No Spotify client; cannot expand Spotify URL.")
                return []
            try:
                track_id = self._extract_spotify_track_id(url)
                if track_id:
                    return [self._build_spotify_track_url(track_id)]

                playlist_id = self._extract_spotify_playlist_id(url)
                if playlist_id:
                    tracks = self._get_playlist_track_ids(playlist_id)
                    return [self._build_spotify_track_url(tid) for tid in tracks]

                album_id = self._extract_spotify_album_id(url)
                if album_id:
                    tracks = self._get_album_track_ids(album_id)
                    return [self._build_spotify_track_url(tid) for tid in tracks]

                print(f"DEVELOPER NOTE -> Unknown Spotify URL type: {url}")
                return []
            except Exception as e:
                print(f"DEVELOPER NOTE -> Error extracting Spotify URL: {e}")
                return []

        # If ever re-enabling non-Spotify, you could fall back here.
        # In enforced Spotify-only mode, *do not* return YouTube items.
        print("DEVELOPER NOTE -> Non-Spotify URL blocked by Spotify-only mode.")
        return []

    async def download_song(self, song: Song) -> None:
        if song.source is not None:  # already preloaded
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

    # -------------------------
    # Core download paths
    # -------------------------

    def __download_url(self, url: str) -> dict:
        """
        URL path. If it's a Spotify track URL, fetch preview_url and metadata.
        If it's already a preview (mp3) URL, wrap it as a song_info dict.
        All non-Spotify URLs are rejected in Spotify-only mode.
        """
        # Direct Spotify track URL
        if self._is_spotify_track_url(url):
            return self._spotify_track_info_from_track_url(url)

        # A raw preview_url (mp3) – allow it
        if "audio.scdn.co" in url or "p.scdn.co" in url:
            return {
                "url": url,
                "title": "Spotify Preview",
                "webpage_url": None,
                "uploader": None,
                "thumbnail": None,
                "duration": 30
            }

        # Block everything else in Spotify-only mode
        if self._spotify_only:
            print(f"DEVELOPER NOTE -> Non-Spotify URL blocked: {url}")
            # Returning empty dict to keep caller logic happy
            return {}

        # (If you ever re-enable YouTube, the legacy code would go here)
        return {}

    def __download_title(self, title: str) -> dict:
        """
        Text search path. Use Spotify search and return the first result
        that has a preview_url.
        """
        if not self._sp:
            print("DEVELOPER NOTE -> No Spotify client; cannot search.")
            return {}

        try:
            res = self._sp.search(q=title, type="track", limit=5)
            items = (res or {}).get("tracks", {}).get("items", []) or []
            for tr in items:
                info = self._build_spotify_preview_info(tr)
                if info:  # only tracks with preview_url
                    return info

            print(f"DEVELOPER NOTE -> No Spotify preview found for '{title}'")
            return {}
        except Exception as e:
            print(f"DEVELOPER NOTE -> Spotify search failed for '{title}': {e}")
            return {}

    # -------------------------
    # Helpers (Spotify)
    # -------------------------

    def _is_spotify_url(self, url: str) -> bool:
        return bool(
            self._RE_SPOTIFY_TRACK.search(url)
            or self._RE_SPOTIFY_PLAYLIST.search(url)
            or self._RE_SPOTIFY_ALBUM.search(url)
        )

    def _is_spotify_track_url(self, url: str) -> bool:
        return bool(self._RE_SPOTIFY_TRACK.search(url))

    def _extract_spotify_track_id(self, url: str) -> Optional[str]:
        m = self._RE_SPOTIFY_TRACK.search(url)
        return m.group(1) if m else None

    def _extract_spotify_playlist_id(self, url: str) -> Optional[str]:
        m = self._RE_SPOTIFY_PLAYLIST.search(url)
        return m.group(1) if m else None

    def _extract_spotify_album_id(self, url: str) -> Optional[str]:
        m = self._RE_SPOTIFY_ALBUM.search(url)
        return m.group(1) if m else None

    def _build_spotify_track_url(self, track_id: str) -> str:
        return f"https://open.spotify.com/track/{track_id}"

    def _spotify_track_info_from_track_url(self, url: str) -> dict:
        if not self._sp:
            print("DEVELOPER NOTE -> No Spotify client; cannot load track.")
            return {}

        track_id = self._extract_spotify_track_id(url)
        if not track_id:
            print(f"DEVELOPER NOTE -> Could not parse Spotify track id from {url}")
            return {}

        try:
            tr = self._sp.track(track_id)
            info = self._build_spotify_preview_info(tr)
            if info:
                return info
            print(f"DEVELOPER NOTE -> Spotify track has no preview_url: {url}")
            return {}
        except Exception as e:
            print(f"DEVELOPER NOTE -> Spotify track fetch failed for {url}: {e}")
            return {}

    def _get_playlist_track_ids(self, playlist_id: str) -> List[str]:
        ids: List[str] = []
        try:
            limit = 100
            offset = 0
            while True:
                page = self._sp.playlist_tracks(playlist_id, limit=limit, offset=offset)
                items = (page or {}).get("items", []) or []
                if not items:
                    break
                for it in items:
                    tr = (it or {}).get("track") or {}
                    tid = tr.get("id")
                    if tid:
                        ids.append(tid)
                if len(items) < limit:
                    break
                offset += limit
        except Exception as e:
            print(f"DEVELOPER NOTE -> playlist_tracks failed: {e}")
        return ids[: self.__config.MAX_PLAYLIST_LENGTH]

    def _get_album_track_ids(self, album_id: str) -> List[str]:
        ids: List[str] = []
        try:
            limit = 50
            offset = 0
            while True:
                page = self._sp.album_tracks(album_id, limit=limit, offset=offset)
                items = (page or {}).get("items", []) or []
                if not items:
                    break
                for tr in items:
                    tid = tr.get("id")
                    if tid:
                        ids.append(tid)
                if len(items) < limit:
                    break
                offset += limit
        except Exception as e:
            print(f"DEVELOPER NOTE -> album_tracks failed: {e}")
        return ids[: self.__config.MAX_PLAYLIST_LENGTH]

    def _build_spotify_preview_info(self, track_obj: dict) -> Optional[dict]:
        preview = (track_obj or {}).get("preview_url")
        if not preview:
            return None
        artists = ", ".join(a["name"] for a in track_obj.get("artists", []) if a and a.get("name"))
        album_images = (track_obj.get("album") or {}).get("images", []) or []
        thumb = album_images[0]["url"] if album_images else None
        return {
            "url": preview,  # direct MP3 URL (30s)
            "title": track_obj.get("name"),
            "webpage_url": (track_obj.get("external_urls") or {}).get("spotify"),
            "uploader": artists,
            "thumbnail": thumb,
            "duration": 30
        }

    # -------------------------
    # Legacy helpers (kept so other code paths don't break if called)
    # -------------------------

    def __get_forced_extracted_info(self, url: str) -> dict:
        # Not used in Spotify-only mode
        return {}

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
