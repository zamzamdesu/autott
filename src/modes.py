import logging
import shutil

from collections import namedtuple
from typing import List
from dateutil.parser import isoparse
from itertools import groupby, chain
from pathlib import Path
from tempfile import TemporaryDirectory

import music
import download

from transcode import Transcode, NamingException, Resample
from tracker import Format, Tracker, TrackerException, MEDIA

TranscodeGroup = namedtuple('TranscodeGroup', 'name group torrent transcode description')

def _input_or_cancel(str):
    res = input(str)

    if len(res.strip()) == 0:
        return None
    
    return res

def _build_tracker(config):
    return Tracker(endpoint=config.endpoint, announce=config.announce, api_key=config.api_key)

def _prepare_transcode(group, torrent, spec_dir: Path, input_dir: Path, output_dir: Path, needed_formats):
    title_override = None
    remaster_override = None
    confirm = False

    while True:
        outputs = dict((format, output_dir / music.build_output_dir(group, torrent, format, title_override, remaster_override)) for format in needed_formats)

        if confirm:
            folders = '\n'.join('\t- ' + p.name for f, p in outputs.items())
            
            if _input_or_cancel(f"Outputs folder will be:\n{folders}\nContinue? (y/n) ") != 'y':
                raise ValueError(f"Output name not confirmed!")

        try:
            return Transcode(input_dir, spec_dir, outputs)
        except NamingException as e:
            confirm = True
            title_override = _input_or_cancel(f"{e}\nInput a shorter base name or empty to set release name: ")

            if title_override is None:
                remaster_override = _input_or_cancel(f"{e}\nInput a shorter release name or empty to cancel: ")

                if remaster_override is None:
                    raise ValueError(f"No name override provided!") 

def _error_check_retry(config, group, torrent, error):
    config.cache.error(group['id'], torrent['id'], error, lambda: input("Retry torrent later? (y/n) ") == 'y')
    
def _key_resample(track):
    return track.resample.value

def _build_transcode_group(processing, tracker, group_id, torrent_id, config):
    try:
        group, torrent, other_torrents = tracker.get_torrent_group(group_id=group_id, torrent_id=torrent_id)
    except TrackerException as e:
        config.cache.error(group_id, torrent_id, f"Invalid torrent: {e}", lambda: False)

    torrent_unique_id = "##".join([str(group_id)] + tracker.get_torrent_grouping(torrent))

    if torrent_unique_id in processing:
        return

    if group_id is None:
        group_id = group['id']

    torrent_name = f"{group['name']} ({tracker.get_url(group_id, torrent_id)})"
    created_time = isoparse(torrent['time'])
    format = Format.from_encoding(torrent['encoding'])


    if torrent['remasterYear'] is not None and torrent['remasterYear'] == 0:
        config.cache.bad(group_id, torrent_id, f"Torrent is unkown release: {torrent_name}")
        return

    if torrent['lossyMasterApproved'] or torrent['lossyWebApproved']:
        config.cache.bad(group_id, torrent_id, f"Torrent is lossy master: {torrent_name}")
        return
    
    if not format.lossless:
        config.cache.bad(group_id, torrent_id, f"Torrent is not lossless: {torrent_name}")
        return

    if torrent['reported'] or torrent['trumpable']:
        config.cache.bad(group_id, torrent_id, f"Torrent is reported or trumpable: {torrent_name}")
        return

    if not torrent['media'].lower() in config.allowed_media:
        config.cache.bad(group_id, torrent_id, f"Torrent has disallowed media ({torrent['media']}): {torrent_name}")
        return
    
    if torrent['hasLog'] and torrent['logScore'] < 100:
        config.cache.bad(group_id, torrent_id, f"Torrent has bad log: {torrent_name}")
        return

    if config.created_cutoff is not None and created_time >= config.created_cutoff:
        config.cache.retry(group_id, torrent_id, created_time, f"Torrent is too recent ({created_time}): {torrent_name}")
        return

    needed_formats = tracker.get_possible_transcodes(group, torrent, other_torrents, config.allowed_formats)
    
    if len(needed_formats) == 0:
        config.cache.complete(group_id, torrent_id)
        return

    source_dir = config.input_dir / torrent['filePath']

    if not source_dir.is_dir():
        _error_check_retry(config, group, torrent, f"Source folder does not exist ({source_dir}): {torrent_name}")
        return

    logging.info(f"Preparing: {torrent_name}")

    try:
        transcode = _prepare_transcode(group, torrent, config.spec_dir, source_dir, config.output_dir, needed_formats)
    except:
        _error_check_retry(config, group, torrent, f"Failed to prepare transcode: {torrent_name}")
        raise

    description=f"Transcoded from {tracker.get_url(group_id, torrent_id)}"

    if transcode.global_resample == Resample.KEEP:
        if format == Format.FLAC_24:
            _error_check_retry(config, group, torrent, f"Torrent is supposed to be 24-bit but no resample needed: {torrent_name}")
            return
    else:
        if format != Format.FLAC_24:
            logging.warning(f"Source files are actually 24-bit: {torrent_name}")

            needed_formats.add(Format.FLAC_16)

            try:
                transcode = _prepare_transcode(group, torrent, config.spec_dir, source_dir, config.output_dir, needed_formats)
            except:
                _error_check_retry(config, group, torrent, f"Failed to prepare transcode after adding FLAC transcode: {torrent_name}")
                raise

        if transcode.global_resample is None:
            logging.warning(f"Source files have inconsistent sample: {torrent_name}")

            description += "\nSource files have varying sampling rate"
            some_not_resampled = False

            for resample, tracks in groupby(sorted(transcode.tracks, key=_key_resample), _key_resample):
                if resample == Resample.KEEP.value:
                    some_not_resampled = True
                    continue

                description += f"\n\n[b]Resampled to 16-bit ({resample} Hz)[/b]"

                for track in tracks:
                    description += f"\n  {track.input.name}"

            if some_not_resampled:
                description += "\n\nOther tracks were not resampled"
        elif transcode.global_resample != Resample.KEEP:
            description += f"\nAll tracks were resampled to 16-bit ({transcode.global_resample.value} Hz)"

    if transcode.valid_logs > 0:
        logging.info(f"Logs are valid: {torrent_name}")

        description += f"\nLog checksum was valid"

    processing.add(torrent_unique_id)

    return TranscodeGroup(torrent_name, group, torrent, transcode, description)

def _build_transcode_remove_prompt(transcode_groups):
    prompt = ""

    for idx, transcode_group in enumerate(transcode_groups):
        prompt += f"[{idx + 1}] {transcode_group.name}\n"

    return prompt + "\nSelect transcode to drop (empty to continue): "

def transcode_local(config):
    logging.info("Preparing and validating transcode...")

    transcode = Transcode(config.input, config.spec_dir, {config.format: config.output})

    if next(config.spec_dir.iterdir(), None) is not None:
        logging.info("Generating spectrograms report...")

        music.generate_spectrogram_report(config.spec_template, config.spec_report, 
            (track.spectrogram for track in transcode.tracks))

        if input(f"Spectrogram report generated at {config.spec_report}. Continue (y/n)? ") != 'y':
            return 1
        
    logging.info("Transcoding...")

    try:
        transcode.execute()

        logging.info(f"Transcode successful into: {config.output}")
    except:
        logging.exception("Trancode failed!")

        return 1

    return 0

def transcode_online(config):
    if config.release_urls:
        logging.warning("Transcode of specific releases: allowed media and cutoff for recent torrents ignored!")

        config.created_cutoff = None
        config.allowed_media = MEDIA.keys()
        config.batch = None

    if config.allowed_media is None or len(config.allowed_media) == 0:
        logging.error("No media allowed, cannot continue")

        return 1

    with _build_tracker(config) as tracker:
        if config.release_urls:
            candidates = (tracker.parse_url(url) for url in config.release_urls)
        else:
            logging.info("Searching for transcode candidates in seeding torrents...")

            candidates = chain(config.cache.get_cached(config.created_cutoff), tracker.get_user_torrents('seeding'))

        transcode_groups: List[TranscodeGroup] = []

        logging.info("Preparing and validating transcodes...")

        processing = set()
        
        for group_id, torrent_id in candidates:
            try:
                if not config.cache.should_try(torrent_id, config.created_cutoff):
                    logging.debug(f"Ignored by cache: {torrent_id}")

                    continue

                transcode_group = _build_transcode_group(processing, tracker, group_id, torrent_id, config) 

                if transcode_group is None:
                    continue

                transcode_groups.append(transcode_group)

                if config.batch is not None:        
                    logging.info(f"Transcode {len(processing)}/{config.batch}: {transcode_group.name}")

                    if len(processing) == config.batch:
                        break
                    
                else:
                    logging.info(f"Transcode #{len(processing)}: {transcode_group.name}")
            except (KeyboardInterrupt, TrackerException):
                raise
            except:
                logging.exception(f"Unhandled error with: {tracker.get_url(group_id, torrent_id)}")
                    
        if next(config.spec_dir.iterdir(), None) is not None:
            logging.info("Generating spectrograms report...")

            music.generate_spectrogram_report(config.spec_template, config.spec_report, 
                (track.spectrogram for transcode_group in transcode_groups for track in transcode_group.transcode.tracks))

            while True:
                res = input(f"Spectrogram report generated at {config.spec_report}. Continue (y/n)? ")
                
                if res == 'y':
                    break
                elif res == 'n':
                    while len(transcode_groups) > 0:
                        res = input(_build_transcode_remove_prompt(transcode_groups))

                        if len(res) == 0:
                            break
                        else:
                            transcode_group = transcode_groups[int(res) - 1]
                            config.cache.bad(transcode_group.group['id'], transcode_group.torrent['id'], "Rejected spectrograms")

                            del transcode_groups[int(res) - 1]

                    break

    logging.info("Transcoding...")

    # (ignore_cleanup_errors=True) for Python 3.10+
    with TemporaryDirectory() as root:
        for transcode_group in transcode_groups:
            try:
                transcode_group.transcode.execute()

                logging.info(f"Transcode successful: {transcode_group.name}")
            except:
                _error_check_retry(config, transcode_group.group, transcode_group.torrent, f"Transcode failed: {transcode_group.name}")

                continue

            try:
                for format, output_dir in transcode_group.transcode.outputs.items():
                    tmp_torrent = tracker.make_torrent(output_dir, (Path(root) / output_dir.name).with_suffix('.torrent'))

                    tracker.upload_torrent(transcode_group.group, transcode_group.torrent, tmp_torrent, format, transcode_group.description)

                    shutil.move(tmp_torrent, config.torrent_dir / tmp_torrent.relative_to(root))

                logging.info(f"Upload successful: {transcode_group.name}")

                config.cache.complete(transcode_group.group['id'], transcode_group.torrent['id'])
            except:
                _error_check_retry(config, transcode_group.group, transcode_group.torrent, f"Torrent generation or upload failed: {transcode_group.name}")

                transcode_group.transcode.cancel()
                continue

    logging.info("Completed!")

    return 0

def _cache_show_entry(config, id: int):
    logging.info(f"Entry {id}: {config.cache.items.get(id)}")

def cache_show(config):
    if len(config.type) == 0 or 'errors' in config.type:
        for id, entry in config.cache.items.items():
            if len(config.type) == 0 or entry.error:
                _cache_show_entry(config, id)

    for type in config.type:
        if type.isdecimal():
            _cache_show_entry(config, int(type))
        elif type != 'errors':
            raise ValueError(f"Invalid type: {type}")

    return 0


def cache_clear(config):
    if 'errors' in config.type:
        logging.info("Clearing errors from cache...")
        config.cache.clear(errors=True)

    for type in config.type:
        if type.isdecimal():
            logging.info(f"Clearing error {type} from cache...")
            config.cache.clear(id=int(type))
        elif type != 'errors':
            raise ValueError(f"Invalid type: {type}")

    return 0

def _test_tracks(config):
    for dir in config.folder:
        logging.info(f"Testing: {dir}")

        transcode = Transcode(dir, config.spec_dir, {})

        if transcode.global_resample is None:
            logging.info(f"Validation successful! Mixed resample required!")
        else:
            logging.info(f"Validation successful! Global resample: {transcode.global_resample.name}")

        yield from transcode.tracks

def test(config):
    try:
        tracks = list(_test_tracks(config))

        if next(config.spec_dir.iterdir(), None) is not None:
            logging.info("Generating spectrograms report...")

            music.generate_spectrogram_report(config.spec_template, config.spec_report, 
                (track.spectrogram for track in tracks))

            input(f"Check spectrograms and press ENTER to clean-up.\n")

        return 0
    except:
        logging.exception("Track validation failed!")
        return 1
    
def download_list(config):
    with _build_tracker(config) as tracker:
        download.download_list(config.file, tracker, config.fl_tokens, config.torrent_dir)

        return 0

def download_collages(config):
    with _build_tracker(config) as tracker:
        download.download_collages(config.cofilellages, tracker, config.fl_tokens, config.torrent_dir)
    
        return 0
