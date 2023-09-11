
import configparser
import argparse
import logging
import contextlib
import sys

from typing import Union, NamedTuple
from datetime import datetime, timedelta
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory

import modes

from transcode import Transcode
from tracker import Format
from cache import Cache

_LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'
_ROOT_FOLDER = Path(sys.argv[0]).parent.parent.resolve(True)

class TranscodeGroup(NamedTuple):
    name: str
    group: object
    torrent: object
    transcode: Transcode
    description: str

def _expand_path(root: Path, path: Union[str, Path]):
    if path is None:
        return None

    if isinstance(path, str):
        path = Path(path)

    path = path.expanduser()

    if not path.is_absolute():
        path = root / path

    return path

def _input_or_cancel(str):
    res = input(str)

    if len(res.strip()) == 0:
        return None
    
    return res

def _unique_file_override(name: Path):
    return _input_or_cancel(f"'{name}' is too long. Input a shorter one (without extension, empty to rename base folder): ")

def _prepare_spectrograms_dir(config):
    if config.spec_dir is None:
        return contextlib.nullcontext
    else:
        config.spec_dir.mkdir(parents=True, exist_ok=True)

        return TemporaryDirectory(dir=config.spec_dir)

def _build_config():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter, prog='redb')
    parser.add_argument('--config', help='the location of the configuration file', default=_expand_path(_ROOT_FOLDER, 'config/autott.cfg'), type=Path)
    parser.add_argument('-v', '--verbose', help="Verbose mode", action='count', default=0)
    parser.add_argument('--fl-tokens', help="Set available FL tokens", type=int, default=0)
    parser.add_argument('--parallel', help='number of parallel transcodes', default=None, type=int)

    subparsers = parser.add_subparsers()

    download = subparsers.add_parser('download', aliases=['do'])
    download.add_argument('file', type=argparse.FileType('r', encoding='UTF-8'))
    download.set_defaults(run=modes.download_list)

    collages = subparsers.add_parser('collages', aliases=['co'])
    collages.add_argument('file', type=argparse.FileType('r', encoding='UTF-8'))
    collages.set_defaults(run=modes.download_collages)

    online = subparsers.add_parser('online', aliases=['on'])
    online.add_argument('release_urls', nargs='*', help='the URL where the release is located')
    online.add_argument('-b', '--batch', help="Run a batch of transcodes", type=int, default=5)
    online.add_argument('-m','--allowed-media', action='append', help='Allowed media types')
    online.add_argument('-T', '--no-torrent', action='store_true', help='transcode only, do not generate torrent')
    online.set_defaults(run=modes.transcode_online)

    local = subparsers.add_parser('local', aliases=['lo'])
    local.add_argument('input', help="Input folder for local transcode", type=Path)
    local.add_argument('output', help="Output folder for local transcode", type=Path)
    local.add_argument('-f', '--format', help="Output format for local transcode", default=Format.FLAC_16.name, choices=[e.name for e in Format])
    local.set_defaults(run=modes.transcode_local)

    test = subparsers.add_parser('test', aliases=['te'])
    test.add_argument('folder', nargs='+', help='folders to test', type=Path)
    test.set_defaults(run=modes.test)

    cache = subparsers.add_parser('cache', aliases=['ca'])
    cache_subparsers = cache.add_subparsers()

    cache_clear = cache_subparsers.add_parser('clear', aliases=['cl'])
    cache_clear.add_argument('type', nargs='+', help="one or more a cache id to clear specific entries, or 'errors' to clear all errors")
    cache_clear.set_defaults(run=modes.cache_clear)  
    
    cache_show = cache_subparsers.add_parser('show', aliases=['sh'])
    cache_show.add_argument('type', nargs='*', help="emtpy to show all cache, one or more a cache id to show specific entries, or 'errors' to show all errors")
    cache_show.set_defaults(run=modes.cache_show)  

    args = parser.parse_args()

    config_file = configparser.ConfigParser(interpolation=None)
    config_file.read(args.config)
    section = config_file['tracker']

    config_root = args.config.parent
             
    config = SimpleNamespace(
        input_dir=_expand_path(config_root, section.get('input_dir')),
        output_dir=_expand_path(config_root, section.get('output_dir')),
        spec_dir=_expand_path(config_root, section.get('spec_dir')),
        spec_template=_expand_path(config_root, section.get('spec_template')),
        spec_report=_expand_path(config_root, section.get('spec_report')),
        torrent_dir=_expand_path(config_root, section.get('torrent_dir')),
        transcoder=_expand_path(config_root, section.get('transcoder')),
        allowed_formats=set(getattr(Format, f) for f in section['formats'].split(',')),
        allowed_media=set(section['media'].split(',')),
        created_cutoff=datetime.now() - timedelta(days=int(section['min_days'])) if 'min_days' in section else None,
        cache=Cache(_expand_path(config_root, section.get('cache'))),
        parallel=int(section['parallel']) if 'parallel' in section else None,
        run=None
    )

    for key, value in section.items():
        if hasattr(config, key):
            continue

        setattr(config, key, value)

    for key, value in vars(args).items():
        current = getattr(config, key, None)

        if value is not None or current is None:
            setattr(config, key, value)

    if hasattr(config, 'format'):
        config.format = getattr(Format, config.format)

    return config

def main() -> int:
    config = _build_config()
   
    if config.run is None:
        print("Missing command", file=sys.stderr)
        return 1

    logging.basicConfig(format=_LOG_FORMAT, level=logging.DEBUG if config.verbose >= 1 else logging.INFO)

    Transcode.max_workers = config.parallel
    Transcode.file_renamer = _unique_file_override
    Transcode.transcoder = config.transcoder

    with _prepare_spectrograms_dir(config) as spec_dir:
        config.spec_dir = Path(spec_dir) if spec_dir is not None else None

        return config.run(config)

if __name__ == "__main__":
    sys.exit(main())