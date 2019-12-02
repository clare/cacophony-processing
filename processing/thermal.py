"""
cacophony-processing - this is a server side component that runs alongside
the Cacophony Project API, performing post-upload processing tasks.
Copyright (C) 2018, The Cacophony Project

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from cptv import CPTVReader

import processing
from .tagger import calculate_tags


DOWNLOAD_FILENAME = "recording.cptv"
SLEEP_SECS = 10
FRAME_RATE = 9

MIN_TRACK_CONFIDENCE = 0.85
FALSE_POSITIVE = "false-positive"
UNIDENTIFIED = "unidentified"
MULTIPLE = "multiple animals"


def process(recording, conf):
    logger = processing.logs.worker_logger("thermal", recording["id"])

    api = processing.API(conf.api_url)
    s3 = processing.S3(conf)

    with tempfile.TemporaryDirectory() as temp_dir:
        filename = Path(temp_dir) / DOWNLOAD_FILENAME
        recording["filename"] = filename
        logger.debug("downloading recording")
        s3.download(recording["rawFileKey"], str(filename))

        update_metadata(conf, recording, api)
        logger.debug("metadata updated")

        if conf.do_classify:
            classify(conf, recording, api, s3, logger)


def classify(conf, recording, api, s3, logger):
    working_dir = recording["filename"].parent
    command = conf.classify_cmd.format(
        folder=str(working_dir), source=recording["filename"].name
    )

    logger.debug("processing %s", recording["filename"])

    proc = subprocess.run(
        command,
        cwd=conf.classify_dir,
        shell=True,
        encoding="ascii",
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        classify_info = json.loads(proc.stdout)
    except json.decoder.JSONDecodeError as err:
        raise ValueError(
            "failed to JSON decode classifier output:\n{}\n{}".format(
                proc.stdout, proc.stderr
            )
        ) from err

    track_info = classify_info["tracks"]
    formatted_tracks = format_track_data(track_info)

    # Auto tag the video
    tagged_tracks, tags = calculate_tags(formatted_tracks, conf)
    for tag in tags:
        logger.debug("tag: %s (%.2f)", tag, tags[tag]["confidence"])
        if tag == MULTIPLE:
            api.tag_recording(recording, tag, tags[tag])

    algorithm_id = api.get_algorithm_id(classify_info["algorithm"])

    upload_tracks(api, recording, algorithm_id, tagged_tracks)

    # Upload mp4
    video_filename = str(replace_ext(recording["filename"], ".mp4"))
    logger.debug("uploading %s", video_filename)
    new_key = s3.upload_recording(video_filename)

    metadata = {"additionalMetadata": {"algorithm": algorithm_id}}
    api.report_done(recording, new_key, "video/mp4", metadata)
    logger.info("Finished (new key: %s)", new_key)


def format_track_data(tracks):
    if not tracks:
        return {}

    for track in tracks:
        if "frame_start" in track:
            del track["frame_start"]
    return tracks


def replace_ext(filename, ext):
    return filename.parent / (filename.stem + ext)


def update_metadata(conf, recording, api):
    with open(str(recording["filename"]), "rb") as f:
        reader = CPTVReader(f)
        metadata = {}
        metadata["recordingDateTime"] = reader.timestamp.isoformat()
        if reader.latitude != 0 and reader.longitude != 0:
            metadata["location"] = (reader.latitude, reader.longitude)

        if reader.preview_secs:
            metadata["additionalMetadata"] = {"previewSecs": reader.preview_secs}

        count = 0
        for _ in reader:
            count += 1
        metadata["duration"] = round(count / FRAME_RATE)
    complete = not conf.do_classify
    api.update_metadata(recording, metadata, complete)


def upload_tracks(api, recording, algorithm_id, tracks):
    for track in tracks:
        track["id"] = api.add_track(recording, track, algorithm_id)
        if "tag" in track:
            api.add_track_tag(recording, track)