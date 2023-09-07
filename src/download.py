import logging
import re
import string
import random

from pathlib import Path
from functools import reduce
from typing import Iterable, TextIO, List

from tracker import Tracker, Format

_GROUPING_ATTRIBUTES = ['remasterYear', 'remasterTitle', 'remasterRecordLabel', 'remasterCatalogueNumber']
_MEDIA_ORDER = ['CD', 'Vinyl', 'SACD', 'WEB']
_FL_CUTOFF = 450 * 1024 * 1024
_TORRENT_NAME_LENGTH = 32

def _group_filter_torrents(torrents):
    grouped = {}

    for torrent in torrents:
        if torrent['media'] == '':
            continue

        group = tuple(torrent[attr] for attr in _GROUPING_ATTRIBUTES)

        grouped.setdefault(group, []).append(torrent)

    return grouped

def _format_size(num, suffix="B"):
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0

    return f"{num:.1f} Yi{suffix}"

def _get_torrent_id(torrent):
    return torrent.get('torrentid', torrent.get('id'))

def _format_torrent(tracker, group, torrent):
    return f"{group['name']} - ({torrent['remasterYear']} / {torrent['remasterTitle']} / {torrent['remasterRecordLabel']} / {torrent['remasterCatalogueNumber']}) - {torrent['encoding']} {torrent['media']} {_format_size(torrent['size'])} {tracker.get_url(group['id'], _get_torrent_id(torrent))}"

def _filter_torrents(tracker, torrents):
    while True:
        if len(torrents) == 0:
            return

        for idx, (group, torrent) in enumerate(torrents):
            print(f"\t- #{idx + 1} > {_format_torrent(tracker, group, torrent)}")

        response = input("\nDelete any? ")

        if response == '':
            break
        else:
            del torrents[int(response) - 1]

def _new_torrent_file(folder):
    return folder / (''.join(random.choice(string.ascii_lowercase) for i in range(_TORRENT_NAME_LENGTH)) + '.torrent')

def _explore_collages(collages: List[str], tracker: Tracker):
    for collage_id in collages:
        if len(collage_id) == 0 or collage_id[0] == '#':
            continue

        if not collage_id.isdecimal():
            match = re.search(r'[\?&]id=(\d+)', collage_id)

            if match:
                collage_id = match.group(1)
            else:
                raise ValueError(f"Invalid collage: {collage_id}")

        collage = tracker.get_collage(collage_id)

        for torrent_group in collage['torrentgroups']:
            for _, torrents in _group_filter_torrents(torrent_group['torrents']).items():
                torrents.sort(key=lambda t: (Format.from_encoding(t['encoding']).value, _MEDIA_ORDER.index(t['media'])))
                yield torrent_group, torrents[0]

def _download(torrents: Iterable[int], tracker: Tracker, fl_tokens: int, watch_dir: Path):
    bad, good = [], []

    for group, torrent, *_ in torrents:
        if torrent['has_snatched']:
            continue

        if torrent.get('trumpable', False) or torrent['reported'] or (torrent['hasLog'] and torrent['logScore'] < 100):
            bad.append((group, torrent))
        else:
            good.append((group, torrent))

    empty = True

    if len(good) > 0:
        print("Good torrents\n")
        _filter_torrents(tracker, good)
        empty = False

    if len(bad) > 0:
        print("Bad torrents\n")
        _filter_torrents(tracker, bad)
        empty = False

    if empty:
        return

    downloads = []
    fl_downloads = []

    for _, torrent in good + bad:
        if torrent.get('canUseToken', True) and not torrent['freeTorrent'] and \
                torrent['size'] > _FL_CUTOFF and len(fl_downloads) < fl_tokens:
            fl_downloads.append(torrent)
        else:
            downloads.append(torrent)

    logging.info(f"Total FL tokens: {len(fl_downloads)}")
    logging.info(f"Total downloads: {_format_size(reduce(lambda s, t: s + t['size'], downloads, 9))}")

    if input("Continue? (y/n) ") != 'y':
        return

    for torrent in downloads:
        tracker.download(_get_torrent_id(torrent), _new_torrent_file(watch_dir), False)

    for torrent in fl_downloads:
        tracker.download(_get_torrent_id(torrent), _new_torrent_file(watch_dir), True)

def download_collages(file: TextIO, tracker: Tracker, fl_tokens: int, watch_dir: Path):
    _download(_explore_collages(file.readlines(), tracker), tracker, fl_tokens, watch_dir)

def download_list(file: TextIO, tracker: Tracker, fl_tokens: int, watch_dir: Path):
    _download((tracker.get_torrent_group(url=url) for url in file.readlines()), tracker, fl_tokens, watch_dir)