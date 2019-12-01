#!/usr/bin/python3

"""
cacophony-processing - this is a server side component that runs alongside
the Cacophony Project API, performing post-upload processing tasks.
Copyright (C) 2019, The Cacophony Project

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

import contextlib
import logging
import multiprocessing
import sys
import time
import traceback

from pebble import ProcessPool

import processing
from processing import API, S3, logs, audio_convert, audio_analysis, thermal

SLEEP_SECS = 2

logger = logs.master_logger()


def main():
    conf = processing.Config.load()

    Processor.conf = conf
    Processor.api = API(conf.api_url)
    Processor.log_q = logs.init_master()

    processors = Processors()
    processors.add("audio", "toMp3", audio_convert.process, conf.audio_convert_workers)
    processors.add(
        "audio", "analyse", audio_analysis.process, conf.audio_analysis_workers
    )
    processors.add("thermalRaw", "getMetadata", thermal.process, conf.thermal_workers)

    logger.info("checking for recordings")
    while True:
        try:
            for processor in processors:
                processor.poll()
        except:
            logger.error(traceback.format_exc())

        time.sleep(SLEEP_SECS)


class Processors(list):
    def add(self, recording_type, processing_state, process_func, num_workers):
        if num_workers < 1:
            return
        p = Processor(recording_type, processing_state, process_func, num_workers)
        self.append(p)


class Processor:

    conf = None
    api = None
    log_q = None

    def __init__(self, recording_type, processing_state, process_func, num_workers):
        self.recording_type = recording_type
        self.processing_state = processing_state
        self.process_func = process_func
        self.num_workers = num_workers
        self.pool = ProcessPool(
            num_workers, initializer=logs.init_worker, initargs=(self.log_q,)
        )
        self.in_progress = {}

    def poll(self):
        self.reap_completed()
        if len(self.in_progress) >= self.num_workers:
            return

        recording = self.api.next_job(self.recording_type, self.processing_state)
        if recording:
            logger.debug(
                "scheduling %s (%s: %s)",
                recording["id"],
                recording["type"],
                self.processing_state,
            )
            future = self.pool.schedule(self.process_func, (recording, self.conf))
            self.in_progress[recording["id"]] = future

    def reap_completed(self):
        for recording_id, future in list(self.in_progress.items()):
            if future.done():
                del self.in_progress[recording_id]
                err = future.exception()
                if err:
                    logger.error(
                        f"{self.recording_type}.{self.processing_state} processing of {recording_id} failed: {err}:\n{err.traceback}"
                    )


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
