import asyncio
from typing import List
from Config.Exceptions import DownloadingError
from Music.Song import Song
from Utils.Utils import run_async


class Downloader:
    """
    Stub Downloader to disable all YouTube/yt_dlp activity.
    Keeps the same interface so other code that imports Downloader
    will still work, but nothing will ever contact YouTube.
    """

    def __init__(self) -> None:
        pass

    def finish_one_song(self, song: Song) -> Song:
        # Immediately fail so the caller knows YouTube is disabled
        raise DownloadingError("YouTube downloading is disabled in this build.")

    @run_async
    def extract_info(self, url: str) -> List[dict]:
        # Return empty list instead of calling yt_dlp
        return []

    async def download_song(self, song: Song) -> None:
        # Do nothing—no background downloads
        return None
