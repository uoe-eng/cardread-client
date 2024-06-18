"""Card Reader Daemon."""

import logging
import multiprocessing
import multiprocessing.queues
import os
import select
import signal
import sqlite3
import time
from pathlib import Path
import requests
from evdev import InputDevice, categorize, ecodes

# FIXME:
# UUIDs?
# Config file
# HTTP POST correct URL
# Safer key mappings for events

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


class CardReader:
    """Card Reader Daemon class."""

    def __init__(self):
        self.cache_dir = os.path.join(Path.home(), ".local", "share", "cardread")
        # DB connection/cursor
        self.con = None
        self.cur = None
        # Define the input device path
        self.device_path = '/dev/input/by-id/usb-Dell_Dell_Wired_Multimedia_Keyboard-event-kbd'
        # Define the number of worker processes to run
        self.num_workers = 4
        self.reader = "Readername"
        # Timeout (secs) for all blocking calls
        # This is the longest the app will take to stop when signalled
        self.timeout = 5
        self.headers = {"Content-Type": "application/jsonapi"}
        self.url = "http://httpbin.org/status/200%2C409%2C500"
        # self.url = "http://httpbin.org/delay/10"
        self.queue = multiprocessing.Queue()
        # Event object to signal workers to exit
        self.stop_event = multiprocessing.Event()
        self.main()

    def event_listener(self):
        """Listen for keypress events, capture 'words' (\n separator) and push to the queue."""
        log.debug("Starting event_listener...")
        device = InputDevice(self.device_path)
        word = []
        while not self.stop_event.is_set():
            # Listen for read events on input device fd
            r, _, _ = select.select([device.fd], [], [], self.timeout)
            if not r:
                continue
            for event in device.read():
                # Keypress 'down'
                if event.type == ecodes.EV_KEY and event.value == 1:
                    key_event = categorize(event)
                    # Translate keycode to char
                    char = key_event.keycode.split('_')[1].lower()
                    # Add chars to word until enter received, then push to Queue
                    if char == "enter":
                        self.queue.put(''.join(word))
                        word = []
                    else:
                        word.append(char)

    def queue_worker(self):
        """Process items from the queue, launching background workers to POST to server."""
        log.debug("Starting queue_worker...")
        # Loop until stop_event received
        while not self.stop_event.is_set():
            # Blocks waiting for queue item
            try:
                card_id = self.queue.get(timeout=self.timeout)
            except multiprocessing.queues.Empty:
                # Restart if queue was empty
                continue
            timestamp = time.time()
            log.debug("Received card_id: %s - %s", timestamp, card_id)
            jsonapi = self.make_jsonapi(card_id, timestamp)
            self.update_cache(jsonapi)
            # Launch http_post process in background
            multiprocessing.Process(target=self.http_post, args=(jsonapi,)).start()

    def http_post(self, jsonapi):
        """POST card data to server, repeating forever until success."""
        while not self.stop_event.is_set():
            log.debug("Posting data: %s", jsonapi)
            response = requests.post(
                self.url,
                headers=self.headers,
                json=jsonapi,
                timeout=self.timeout
            )
            # Success or Conflict indicates data has reached server successfully
            if response.status_code in (200, 409):
                log.debug("Post success: %s %s", jsonapi, response.status_code)
                self.clear_cache(jsonapi)
                break
            time.sleep(self.timeout)

    def update_cache(self, jsonapi):
        """Add a new entry to the DB cache."""
        log.debug('Update Cache: %s', jsonapi)
        self.cur.execute(
            'insert into log VALUES (?, ?)',
            (jsonapi["data"]["card_id"], jsonapi["data"]["time"])
        )
        self.con.commit()

    def clear_cache(self, jsonapi):
        """Delete an entry from the DB cache."""
        log.debug('Clear Cache: %s', jsonapi)
        self.cur.execute(
            'DELETE FROM log WHERE card_id = ? and time = ?',
            (jsonapi["data"]["card_id"], jsonapi["data"]["time"])
        )
        self.con.commit()

    def create_db(self):
        """Set up sqlite3 cache database."""
        self.con = sqlite3.connect(os.path.join(self.cache_dir, "cardread.db"))
        # Add ability to make dicts from rows
        self.con.row_factory = sqlite3.Row
        self.cur = self.con.cursor()
        self.cur.execute("CREATE TABLE IF NOT EXISTS log(card_id, time)")

    def push_cache(self):
        """Launch workers to POST previously cached values to the server."""
        res = self.cur.execute("select * from log")
        for card_id, timestamp in dict(res.fetchall()).items():
            jsonapi = self.make_jsonapi(card_id, timestamp)
            multiprocessing.Process(target=self.http_post, args=(jsonapi,)).start()

    def make_jsonapi(self, card_id, timestamp):
        """Create jsonapi for POST"""
        return {
            "type": "log",
            "id": "uuid",
            "data": {
                "reader": self.reader,
                "card_id": card_id,
                "time": timestamp,
            }
        }

    def main(self):
        """Main daemon process."""
        # Handle 'stop' signals
        signal.signal(signal.SIGTERM, lambda sig, fr: self.stop_event.set())
        signal.signal(signal.SIGHUP, lambda sig, fr: self.stop_event.set())
        signal.signal(signal.SIGINT, lambda sig, fr: self.stop_event.set())

        # Create cache_dir & db
        try:
            os.mkdir(self.cache_dir)
        except FileExistsError:
            # Not an error if already present
            pass
        self.create_db()

        # Push cached results
        self.push_cache()

        # Launch the keyboard listener
        process = multiprocessing.Process(target=self.event_listener)
        process.start()

        # Set up API worker pool and wait for them to finish
        workers = []
        for _ in range(self.num_workers):
            p = multiprocessing.Process(target=self.queue_worker)
            p.start()
            workers.append(p)

        # Wait for workers to finish
        for worker in workers:
            worker.join()


if __name__ == "__main__":
    CardReader()
