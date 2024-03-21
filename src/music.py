import re
import logging
import subprocess
import mutagen
import mutagen.flac
import mutagen.mp3

from typing import Iterable
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
from datetime import date
from collections import namedtuple
from mutagen.easyid3 import EasyID3

_MIN_YEAR = 1900
_MAX_YEAR = date.today().year
_PATH_INVALID_CHARS = r"[\./\:\*\?\"<>\|\\]"
_NUMERIC_TAGS = set([
    'tracknumber',
    'discnumber',
    'tracktotal',
    'totaltracks',
    'disctotal',
    'totaldiscs',
    ])
_SPECTOGRAMG_ARGS_FULL = ['-x', '3000', '-y', '513', '-z', '120']
_SPECTOGRAMG_ARGS_ZOOM = ['-x', '500', '-y', '1025', '-z', '120', '-S', '1:00', '-d', '0:02']

Spectrogram = namedtuple('Spectrogram', 'valid_logs files')

class TaggingException(Exception):
    pass

def _valid_fractional_tag(value):
    #if re.match(r"""\d+(/(\d+))?$""", value):
    if re.match(r"""[A-Z]?\d+(/(\d+))?$""", value):
        return True
    else:
        return False

def _scrub_tag(name, value):
    """Strip whitespace (and other common problems) from tag values.

    May return the empty string ''.
    """
    scrubbed_value = value.strip().strip('\x00')

    # Strip trailing '/' or '/0' from numeric tags.
    if name in _NUMERIC_TAGS:
        scrubbed_value = re.sub(r"""/(0+)?$""", '', scrubbed_value)

    # Remove leading '/' from numeric tags.
    if name in _NUMERIC_TAGS:
        scrubbed_value = scrubbed_value.lstrip('/')

    # Numeric tags should not be '0' (but tracknumber 0 is OK, e.g.,
    # hidden track).
    if name in _NUMERIC_TAGS - set(['tracknumber']):
        if re.match(r"""0+(/.*)?$""", scrubbed_value):
            return ''

    return scrubbed_value

def check_tags(fd, check_tracknumber_format=True):
    info = mutagen.File(fd, easy=True)

    for tag in ['artist', 'album', 'title', 'tracknumber']:
        if tag not in info.keys():
            raise TaggingException(f'File has no {tag} tag')
        elif len(info[tag]) == 0:
            raise TaggingException(f'File has an emtpy {tag} tag')
        elif any(len(t.strip()) == 0 for t in info[tag]):
            raise TaggingException(f'File has a blank {tag} tag: {info[tag]}')

    if 'MQAENCODER' in info.tags or 'MQA' in info.tags.get('COMMENT'):
        raise TaggingException(f"MQA encoded: {info.tags.get('MQAENCODER')} {info.tags.get('COMMENT')}")

    tracknumber = info['tracknumber'][0]
    if not _valid_fractional_tag(tracknumber):
        if check_tracknumber_format:
            raise TaggingException(f'File has a malformed tracknumber tag "{tracknumber}"')
        else:
            logging.warning(f'File has a malformed tracknumber tag "{tracknumber}"')

def copy_tags(src: Path, dst: Path):
    flac_info = mutagen.flac.FLAC(src)

    if dst.suffix == '.flac':
        transcode_info = mutagen.flac.FLAC(dst)
        valid_key_fn = lambda _: True
    elif dst.suffix == '.mp3':
        transcode_info = mutagen.mp3.EasyMP3(dst)
        valid_key_fn = lambda k: k in EasyID3.valid_keys.keys()
    else:
        raise TaggingException(f'Unsupported file : {dst}')
        
    for tag in filter(valid_key_fn, flac_info):
        # scrub the FLAC tags, just to be on the safe side.
        values = list(map(lambda v: _scrub_tag(tag,v), flac_info[tag]))
        if values and values != [u'']:
            transcode_info[tag] = values

    if dst.suffix == '.mp3':
        # Support for TRCK and TPOS x/y notation, which is not
        # supported by EasyID3.
        #
        # These tags don't make sense as lists, so we just use the head
        # element when fixing them up.
        #
        # totaltracks and totaldiscs may also appear in the FLAC file
        # as 'tracktotal' and 'disctotal'. We support either tag, but
        # in files with both we choose only one.

        if 'tracknumber' in transcode_info.keys():
            totaltracks = None
            if 'totaltracks' in flac_info.keys():
                totaltracks = _scrub_tag('totaltracks', flac_info['totaltracks'][0])
            elif 'tracktotal' in flac_info.keys():
                totaltracks = _scrub_tag('tracktotal', flac_info['tracktotal'][0])

            if totaltracks:
                transcode_info['tracknumber'] = [u'%s/%s' % (transcode_info['tracknumber'][0], totaltracks)]

        if 'discnumber' in transcode_info.keys():
            totaldiscs = None
            if 'totaldiscs' in flac_info.keys():
                totaldiscs = _scrub_tag('totaldiscs', flac_info['totaldiscs'][0])
            elif 'disctotal' in flac_info.keys():
                totaldiscs = _scrub_tag('disctotal', flac_info['disctotal'][0])

            if totaldiscs:
                transcode_info['discnumber'] = [u'%s/%s' % (transcode_info['discnumber'][0], totaldiscs)]

    transcode_info.save()

def _comment_get(id3, _):
    return [comment.text for comment in id3['COMM'].text]

def _comment_set(id3, _, value):
    id3.add(mutagen.id3.COMM(encoding=3, lang='eng', desc='', text=value))

def _originaldate_get(id3, _):
    return [stamp.text for stamp in id3['TDOR'].text]

def _originaldate_set(id3, _, value):
    id3.add(mutagen.id3.TDOR(encoding=3, text=value))

def get_artists(group):
    music_info = group['musicInfo']
    artists = music_info['artists']

    if len(artists) > 2:
        return "Various Artists"
    elif not artists and len(music_info['composers']) > 2:
        return "Various Artists"
    elif len(artists) == 2:
        return artists[0]['name'] + " & " + artists[1]['name']                
    elif not artists and len(music_info['composers']) == 2:
        return music_info['composers'][0]['name'] + " & " + music_info['composers'][1]['name']         
    elif not artists and len(music_info['composers']):
        return music_info['composers'][0]['name']
    else:
        return artists[0]['name']

def _ensure_valid_year(year):
    if year is None or not _MIN_YEAR <= year <= _MAX_YEAR:
        return None
    
    return year

def get_year(group, torrent):
    year = _ensure_valid_year(torrent['remasterYear'])
    if year is not None:
        return year
    
    year = _ensure_valid_year(group['year'])
    if year is not None:
        return year
    
    raise ValueError(f"Torrent has no valid release year: {torrent}")

def build_output_dir(group, torrent, output_format, title_override = None, remaster_override = None):
    basename = f"{get_artists(group)} - {group['name'] if title_override is None else title_override}"

    if len(torrent['remasterTitle']) > 0:
        basename += f" ({torrent['remasterTitle'] if remaster_override is None else remaster_override} - {get_year(group, torrent)})"
    else:
        basename += f" ({get_year(group, torrent)})"

    basename += f" [{torrent['media']} - {output_format.name}]"

    return Path(re.sub(_PATH_INVALID_CHARS, '', basename))

def generate_spectrogram(output_dir: Path, flac: Path, zoom=False):
    target = output_dir / f"{flac.stem.replace('#', '')}_{'zoom' if zoom else 'full'}.png"

    target.parent.mkdir(parents=True, exist_ok=True)

    subprocess.check_call(['sox', flac, '-n', 'remix', '1', 
                                  'spectrogram', '-w', 'Kaiser', '-t', flac.stem, '-o', target] + 
                                  (_SPECTOGRAMG_ARGS_ZOOM if zoom else _SPECTOGRAMG_ARGS_FULL))
    
    return target

def generate_spectrogram_report(template: Path, output: Path, spectrograms: Iterable[Spectrogram]):
    env = Environment(
        loader=FileSystemLoader(template.parent),
        autoescape=select_autoescape()
    )

    output.write_text(env.get_template(template.name).render(root=output.parent, spectrograms=spectrograms))

def test_flac(file: Path):
    subprocess.check_output(args=['flac', '-wt', file], stderr=subprocess.STDOUT, text=True)

for key, frameid in {
    'albumartist': 'TPE2',
    'album artist': 'TPE2',
    'grouping': 'TIT1',
    'content group': 'TIT1',
    }.items():
    EasyID3.RegisterTextKey(key, frameid)

EasyID3.RegisterKey('comment', _comment_get, _comment_set)
EasyID3.RegisterKey('description', _comment_get, _comment_set)
EasyID3.RegisterKey('originaldate', _originaldate_get, _originaldate_set)
EasyID3.RegisterKey('original release date', _originaldate_get, _originaldate_set)
