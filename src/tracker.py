
import re
import subprocess
import requests
import html

from loguru import logger
from urllib.parse import urljoin
from pathlib import Path
from enum import Enum
from ratelimit import limits, sleep_and_retry

PAGING = 500

MEDIA = {
    'cd': 'CD',
    'dvd': 'DVD',
    'vinyl': 'Vinyl',
    'soundboard': 'Soundboard',
    'sacd': 'SACD',
    'dat': 'DAT',
    'web': 'WEB',
    'blu-ray': 'Blu-ray'
    }

_ARTIST_TYPES = {
    'artists': 1,
    'with': 2,
    'remixedBy': 3,
    'composers': 4,
    'conductor': 5,
    'dj': 6,
    'producer': 7
}

_GROUPING_ATTRIBUTES = ['media', 'remasterYear', 'remasterTitle', 'remasterRecordLabel', 'remasterCatalogueNumber']

def _json_unescape(obj):
    if isinstance(obj, str):
        return html.unescape(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            obj[k] = _json_unescape(v)
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            obj[idx] = _json_unescape(v)

    return obj

class Format(Enum):
    FLAC_24 = 0, 'FLAC', '24bit Lossless', '.flac', True
    FLAC_16 = 1, 'FLAC', 'Lossless', '.flac', True
    MP3_320 = 2, 'MP3', '320', '.mp3', False
    MP3_V0 = 3, 'MP3', 'V0 (VBR)', '.mp3', False
    MP3_V1 = 4, 'MP3', 'V1 (VBR)', '.mp3', False
    MP3_V2 = 5, 'MP3', 'V2 (VBR)', '.mp3', False
    MP3_256 = 6, 'MP3', '256', '.mp3', False
    MP3_192 = 7, 'MP3', '192', '.mp3', False
    MP3_96 = 8, 'MP3', '96', '.mp3', False

    def __new__(cls, value, base_format, encoding, ext, lossless):
        obj = object.__new__(cls)

        obj._value_ = value
        obj.base_format = base_format
        obj.encoding = encoding
        obj.ext = ext
        obj.lossless = lossless

        return obj
      
    @classmethod
    def from_encoding(cls, encoding):
        for format in cls:
            if format.encoding == encoding:
                return format
            
        raise ValueError(f"Invalid encoding: {encoding}")

def format_size(num, suffix="B"):
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0

    return f"{num:.1f} Yi{suffix}"

class TrackerException(Exception):
    pass

class Tracker:
    # Files 350MiB to 512MiB: 256KiB piece size (-l 18)
    TORRENT_PIECE_LENGTH = 18

    # Fixed source for RED
    TORRENT_SOURCE = "RED"

    def __init__(self, announce, endpoint, api_key):
        self._endpoint = endpoint
        self._session = requests.Session()
        self._session.headers.update({'Authorization': api_key})

        json = self._ajax_get('index')

        self._user_id = json['id']
        self._announce = announce.format(json['passkey'])

        logger.info(f"Logged-in successfully to {self._endpoint} as {json['username']}")

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self._session.close()

    @sleep_and_retry
    @limits(calls=9, period=10)
    def _rate_limit(self):
        pass

    def _ajax(self, response):
        try:
            json = response.json()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if response.status_code == requests.codes.ok:
                raise TrackerException(f"Unexpected request error: {response.text}", e)
            else:
                raise TrackerException(f"Failed HTTP request: {response.status_code}", e)
        
        if json['status'] != 'success':
            raise TrackerException(f"Failed request: {json['error']}")

        return _json_unescape(json['response'])

    def _ajax_get(self, action, json = True, **kwargs):
        self._rate_limit()

        params = {'action': action}
        params.update(kwargs)

        response = self._session.get(urljoin(self._endpoint, "ajax.php"), params=params)

        if json:
            return self._ajax(response)

        return response

    def _ajax_post(self, action, data=None, files=None, **kwargs):
        self._rate_limit()
        
        params = {'action': action}
        params.update(kwargs)

        return self._ajax(self._session.post(urljoin(self._endpoint, "ajax.php"), params=params, data=data, files=files))

    def _ajax_list(self, action, res_key, **kwargs):
        offset = 0

        while True:
            results = self._ajax_get(action=action, offset=offset, limt=PAGING, **kwargs)[res_key]

            yield from results

            if len(results) < PAGING:
                break
            else:
                offset += PAGING

    def get_user_torrents(self, type):
        for torrent in self._ajax_list(action='user_torrents', id=self._user_id, type=type, res_key=type):
            yield torrent['groupId'], torrent['torrentId']

    def get_torrent_group(self, *, group_id = None, torrent_id = None, url = None):
        if url is not None:
            group_id, torrent_id = self.parse_url(url)

        if torrent_id is None:
            raise ValueError("Missing torrent id")
        
        torrent = None

        if group_id is None:
            torrent = self.get_torrent(torrent_id=torrent_id)
            group_id = torrent['group']['id']
        
        group = self._ajax_get('torrentgroup', id=group_id)

        if torrent is None:
            try:
                torrent = next(t for t in group['torrents'] if t['id'] == torrent_id)
            except StopIteration:
                raise TrackerException(f"Torrent {torrent_id} does not exist in group {group_id}")
        else:
            torrent = torrent['torrent']

        return group['group'], torrent, group['torrents']

    def get_torrent(self, *, torrent_id):
        return self._ajax_get('torrent', id=torrent_id)

    def get_collage(self, id):
        collage = self._ajax_get('collage', id=id)

        return collage
    
    def download(self, id, file: Path, freeleech = False):
        response = self._ajax_get('download', json=False, id=id, usetoken=1 if freeleech else 0)

        if not 'application/x-bittorrent' in response.headers.get('content-type'):
            raise TrackerException(f"Failed to download torrent: {response.text}")
        
        file.write_bytes(response.content)
    
    def make_torrent(self, input_dir: Path, file: Path) -> Path:
        try:
            subprocess.check_output(stderr=subprocess.STDOUT, text=True,
                                    args=["mktorrent", "-p", "-s", Tracker.TORRENT_SOURCE, "-a", self._announce, 
                                          "-o", file, "-l", str(Tracker.TORRENT_PIECE_LENGTH), input_dir])
            
            return file
        except subprocess.CalledProcessError as e:
            raise TrackerException(f"Failed to build torrent for: {dir}", e)

    def upload_torrent(self, group: object, torrent: object, new_torrent: Path, format: Format, description: str = None):
        with open(new_torrent, 'rb') as fd:
            files = {'file_input': ('upload.torrent', fd, 'application/x-bittorrent')}
            data = {
                'groupid': group['id'],
                'release': group['releaseType'],
                'format': format.base_format,
                'bitrate': format.encoding,
                'media': torrent['media'],
                # 'type': 0,
                # 'artists': [],
                # 'importance': []
            }

            # for artist_type, artist_type_id, in ARTIST_TYPES.items():
            #     for artist in group['musicInfo'][artist_type]:
            #         data['artists'].append(artist['name'])
            #         data['importance'].append(artist_type_id)

            if description is not None:
                data['release_desc'] = description

            if torrent['remastered']:
                data['remaster_year'] = torrent['remasterYear']
                data['remaster_title'] = torrent['remasterTitle']
                data['remaster_record_label'] = torrent['remasterRecordLabel']
                data['remaster_catalogue_number'] = torrent['remasterCatalogueNumber']

            return self._ajax_post(action='upload', data=data, files=files)

    def parse_url(self, url):
        group_id = re.search(r'[\?&]id=(\d+)', url)
        torrent_id = re.search(r'[\?&]torrentid=(\d+)', url)

        if group_id is not None:
            group_id = int(group_id.group(1))
        
        if torrent_id is None:
            raise ValueError("Missing torrent id")
        
        return group_id, int(torrent_id.group(1))

    def get_torrent_grouping(self, torrent):
        return list(str(torrent[attr]) for attr in _GROUPING_ATTRIBUTES)

    def get_possible_transcodes(self, group, torrent, other_torrents, formats):
        if re.search(r'pre[- ]?emphasi(s(ed)?|zed)', torrent['remasterTitle'] + torrent['description'], flags=re.IGNORECASE) is not None:
            return set()

        other_torrents = list(filter(lambda t: all(t[attr] == torrent[attr] for attr in _GROUPING_ATTRIBUTES), other_torrents))

        if any(t['trumpable'] or t['reported'] for t in other_torrents):
            logger.warning(f"Torrent group {group['id']} has reported or trumpable formats!")

        available_formats = set(Format.from_encoding(t['encoding']) for t in other_torrents if not t['trumpable'] and not t['reported'])

        return set(formats).difference(available_formats)

    def get_url(self, group_id, torrent_id):
        return f'{self._endpoint}/torrents.php?id={group_id}&torrentid={torrent_id}#torrent{torrent_id}'