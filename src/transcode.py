import re
import os
import shutil
import subprocess
import logging

from typing import List, Mapping, NamedTuple, Union
from enum import Enum
from pathlib import Path
from mutagen.flac import FLAC
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait

import music
import log_checker

from music import Spectrogram
from tracker import Format, format_size

class Resample(Enum):
    KEEP = 0
    KHZ_44_1 = 44100
    KHZ_48 = 48000

HTML_ENCODE = re.compile(r'&[a-z0-9]+;')
TRANSCODE_INCLUDE_EXTENSIONS = ['.cue', '.gif', '.jpeg', '.jpg', '.log', '.md5', '.nfo', '.pdf', '.png', '.sfv', '.txt']
TRANSCODE_TIMEOUT=60 *10

PATH_MAX_LENGTH = 180
ADD_FILES_MAX_SIZE = 1024 * 1024

class TranscodedTrack(NamedTuple):
    output: Path
    output_format: Format

class Track(NamedTuple):
    input: Path
    spectrogram: List[Path]
    resample: Resample
    transcodes: List[TranscodedTrack]    

class NamingException(Exception):
    pass

class TranscodeException(Exception):
    pass

# TODO: Remove with Python 3.9+
def _with_stem(path, stem):
    return path.with_name(stem + path.suffix)

def _check_valid_path(dir: Path, file: Path, strict=True):
    path = str(file.relative_to(dir.parent))
    length = len(path)

    if length > PATH_MAX_LENGTH:
        err = f"Path '{path}' has {length} characters and exceeds maximum ({PATH_MAX_LENGTH})"

        if strict:
            raise NamingException(err)
        else:
            logging.warning(err)

    if HTML_ENCODE.search(path) is not None:
        err = f"Path '{path}' has invalid characters"

        if strict:
            raise NamingException(err)
        else:
            logging.warning(err)

    return file

def _get_extension(path: Path, ignore_case):
    if ignore_case:
        return path.suffix.lower()
    else:
        return path.suffix

def _find_by_extension(root: Path, *extensions, ignore_case: bool = False):
    for path in root.rglob('*'):
        if len(extensions) == 0 or _get_extension(path, ignore_case) in extensions:
            yield path

def _get_resample(metadata: object) -> Resample:
    if metadata.bits_per_sample > 16 or metadata.sample_rate > 48000:
        if metadata.sample_rate % 44100 == 0:
            return Resample.KHZ_44_1
        elif metadata.sample_rate % 48000 == 0:
            return Resample.KHZ_48
        else:
            raise ValueError(f'File has unsupported sample rate: {metadata.sample_rate}')
    else:
        return Resample.KEEP

def _transcode_one(transcoder: str, track: Track, transcoded: TranscodedTrack):
    if transcoder is None:
        raise ValueError("transcoder must be set")

    args=[transcoder, str(track.input), str(transcoded.output), transcoded.output_format.name]

    try:
        if track.resample != Resample.KEEP:
            args.append(str(track.resample.value))

        if transcoded.output_format == Format.FLAC_16 and track.resample == Resample.KEEP:
            logging.warning(f"Source file is already 16 bits and will be copied as-is: {track.input}")

            shutil.copyfile(track.input, transcoded.output)

            result = None
        else:
            logging.debug(f'Transcoding to "{transcoded.output}" as {transcoded.output_format.name} ({track.resample.name} resample)...')
        
            result = subprocess.check_output(args=args, stderr=subprocess.STDOUT, text=True, timeout=TRANSCODE_TIMEOUT)

        music.copy_tags(track.input, transcoded.output)

        try:
            with open(transcoded.output, 'rb') as fd:
                music.check_tags(fd)
        except music.TaggingException as e:
            raise TranscodeException(f'Tag check failed on: {transcoded.output}', e)
        
        return result
    except subprocess.CalledProcessError as e:
        raise TranscodeException(f"Transcode of {track.input} failed!\nArguments: {' '.join(args)}\nOutput: {e.output}")
    except Exception as e:
        raise TranscodeException(f'Transcode of {track.input} failed!', e)

def _validate_track(track: Path, input_dir: Path, spec_dir: Union[Path, None]):
    spectrograms = [
            music.generate_spectrogram(spec_dir / input_dir.name, track, False),
            music.generate_spectrogram(spec_dir / input_dir.name, track, True)
            ] if spec_dir is not None else None

    with open(track, 'rb') as fd:
        metadata = FLAC(fd).info

        if metadata.channels > 2:
            raise TranscodeException(f'Track {track} has {metadata.channels} channels, which is not supported')
        
        try:
            music.check_tags(fd, check_tracknumber_format=False)
        except music.TaggingException as e:
            raise TranscodeException(f'Tag check failed on track: {track}', e)
        
    try:
        music.test_flac(track)
    except subprocess.CalledProcessError as e:
        raise TranscodeException(f"Bad source FLAC: {track}", e)
    
    return metadata, spectrograms

def _check_logs(input_dir: Path):
    valid_logs = 0

    for log in _find_by_extension(input_dir, '.log', ignore_case=True):
        if log_checker.check_log(log):
            valid_logs += 1

    return valid_logs

def _iter_tracks(input_dir: Path):
    return _find_by_extension(input_dir, '.flac', ignore_case=True)

class Transcode:
    max_workers = None
    file_renamer = lambda: None
    transcoder: Path = None

    def __init__(self, input_dir: Path, spec_dir: Union[Path, None], outputs: Mapping[Format, Path]) -> None:
        if any(map(Path.exists, outputs.values())):
            raise TranscodeException(f"Cannot transcode into existing folders: {', '.join(map(str, outputs.values()))}")
        
        if not input_dir.is_dir():
            raise ValueError(f"Source directory does not exist: {input_dir}")
   
        self.spec_dir = spec_dir
        self.input_dir = input_dir
        self.outputs = outputs

        self.valid_logs = _check_logs(input_dir)
        self.tracks = list(self._get_tracks())
        self.global_resample = self._get_global_resample()

        self.additional_files = list(self._get_additional_files())

        if self.global_resample == Resample.KEEP and Format.FLAC_16 in outputs.keys():
            raise TranscodeException("Source files do not require resampling, transcoding into FLAC is invalid") 

    def _to_output(self, output_dir: Path, file: Path):
        return output_dir / file.relative_to(self.input_dir)

    def _get_additional_files(self):
        files = list(_find_by_extension(self.input_dir, *TRANSCODE_INCLUDE_EXTENSIONS))

        total_size = sum(f.stat().st_size for f in files)

        if total_size > ADD_FILES_MAX_SIZE:
            logging.warning(f"Additional files are large: {format_size(total_size)}")

        for file in files:
            stem = file.stem

            while True:
                try:
                    outputs = []

                    for output_dir in self.outputs.values():
                        output = _with_stem(self._to_output(output_dir, file), stem)

                        _check_valid_path(output_dir, output, True)
                        outputs.append(output)
                        
                    break
                except NamingException:
                    stem = Transcode.file_renamer(file.name)

                    if stem is None:
                        logging.warning("No specific file rename provider, base folder will be renamed instead!")

                        raise

            for output in outputs:
                yield file, output

    def _build_transcoded_tracks(self, input: Path):
        for (output_format, output_dir) in self.outputs.items():
            output = self._to_output(output_dir, input).with_suffix(output_format.ext)
            _check_valid_path(output_dir, output, strict=True) 

            yield TranscodedTrack(output, output_format)

    def _get_track(self, input: Path):
        metadata, spectrograms = _validate_track(input, self.input_dir, self.spec_dir)           
        resample = _get_resample(metadata)

        _check_valid_path(self.input_dir, input)

        return Track(
            input=input, 
            resample=resample,
            spectrogram=Spectrogram(self.valid_logs, spectrograms),
            transcodes=list(self._build_transcoded_tracks(input))
            )

    def _get_tracks(self):
        with ThreadPoolExecutor(max_workers=Transcode.max_workers, thread_name_prefix='validator') as pool:
            return pool.map(self._get_track, _iter_tracks(self.input_dir))

    def _get_global_resample(self) -> Union[Resample, None]:
        first = self.tracks[0]

        for track in self.tracks:
            if track.resample != first.resample:
                return None
            
        return first.resample

    def _transcode_parallel(self):
        # Ensure all folders exist
        for folder in set(t.output.parent for track in self.tracks for t in track.transcodes):
            folder.mkdir(parents=True, exist_ok=True)

        with ThreadPoolExecutor(max_workers=Transcode.max_workers, thread_name_prefix='transcoder') as pool:
            tasks = [pool.submit(_transcode_one, self.transcoder, track, t) for track in self.tracks for t in track.transcodes]

            done, not_done = wait(tasks, return_when=FIRST_EXCEPTION)

            for future in done:
                # Validate all completed
                future.result()

            if len(not_done) > 0:
                raise TranscodeException(f"{len(not_done)} transcodes not executed!")

    def cancel(self):
        for output_dir in self.outputs.values():
            shutil.rmtree(output_dir, ignore_errors=True)

    def execute(self):
        logging.debug(f"Transcoding {len(self.tracks)} files to {', '.join(map(str, self.outputs.values()))}")

        try:
            self._transcode_parallel()

            logging.debug(f"Transcode completed! Hard linking additional files...")

            for input, output in self.additional_files:
                output.parent.mkdir(exist_ok=True, parents=True)

                os.link(input, output)
                # output.hardlink_to(file) Use for python 3.10
        except Exception as e:
            self.cancel()

            raise e