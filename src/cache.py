import pickle

from loguru import logger
from pathlib import Path
from collections import namedtuple

CacheEntry = namedtuple('CacheEntry', 'group_id error retry created')

class Cache:
    def __init__(self, path: Path) -> None:
        self._path: Path = path
        
        if self._path is None:
            return

        try:
            with open(self._path, 'rb') as fd: 
                self.items = pickle.load(fd)
        except:
            self.items = dict()

    def _save(self):
        if self._path is None:
            return

        self._path.write_bytes(pickle.dumps(self.items))

    def complete(self, group_id: int, torrent_id: int):
        self.items[torrent_id] = CacheEntry(group_id, None, False, None)
        self._save()

    def bad(self, group_id: int, torrent_id: int, error: str):
        logger.debug(error)

        self.items[torrent_id] = CacheEntry(group_id, error, False, None)
        self._save()

    def error(self, group_id: int, torrent_id: int, error: str, retry_callback = lambda: True):
        logger.exception(error)

        self.items[torrent_id] = CacheEntry(group_id, error, retry_callback(), None)
        self._save()

    def retry(self, group_id: int, torrent_id: int, created, error: str):
        logger.debug(error)

        self.items[torrent_id] = CacheEntry(group_id, error, True, created)
        self._save()

    def _should_try(self, cache, cutoff):
        return cache.error is not None and cache.retry and \
            (cache.created is None or cutoff is None or cache.created < cutoff)

    def clear(self, id = None, errors = False):
        if errors:
            self.items = { torrent_id: cache for torrent_id, cache in self.items.items() if not cache.error}

        if id is not None:
            self.items.pop(id)

        self._save()

    def should_try(self, torrent_id, cutoff):
        cache = self.items.get(torrent_id)

        if cache is None:
            return True

        return self._should_try(cache, cutoff)

    def get_cached(self, cutoff):
        for torrent_id, cache in self.items.items():
            if self._should_try(cache, cutoff):
                yield cache.group_id, torrent_id