import subprocess
import json

from loguru import logger
from subprocess import CalledProcessError

_EXTENDED_VALIDATION_IGNORE = [
    'Since release seems to contain 24bit FLACs, the folder name could mention it',
    'Since release does not .log/.cue, it is probably a WEB or Vinyl release. The folder name could mention it',
    'Format (FLAC) not found in folder name',
    'Title of album (as found in the tags of the first track) is not in the folder name',
    'Year of album (as found in the tags of the first track) is not in the folder name',
    'Not all album artists (as found in the tags of the first track) found in the folder name'
]

_EXTENDED_VALIDATION_WARNING = [
    'Title of album (as found in the tags of the first track) is not in the folder name'
]

class ValidateException(Exception):
    pass

def extended_test(config, path):
    if config.extended_validator is None:
        return
    
    try:
        raw = subprocess.check_output(args=[config.extended_validator] + config.extended_validator_args + [path], stderr=subprocess.STDOUT, text=True)
    except CalledProcessError as e:
        raise ValidateException(f"Extended validator failed with code {e.returncode}:\n{e.output}", e)

    # Hack as JSON is mixed on stdout with an initial log message
    if raw[0] != '{':
        raw = raw[raw.index("{\n"):]

    try:
        analysis = json.loads(raw)
    except Exception as e:
        raise ValidateException(f"Extended validation output is invalid:\n\n{raw}", e)

    errors = ""

    for check in analysis['checks']:
        if check['result'] == 0 or any(s in check['result_comment'] for s in _EXTENDED_VALIDATION_IGNORE):
            continue

        if check['level'] == 1 or any(s in check['result_comment'] for s in _EXTENDED_VALIDATION_WARNING):
            logger.warning(check['result_comment'])
        elif check['level'] == 2:
            errors += f"\t- {check['result_comment']}\n"
    
    if len(errors) > 0:
        logger.error(f"Validation failed:\n{errors}")

        if input("Ignore (y/n)? ") != 'y':
            raise ValidateException(f"Extended validation failed!")