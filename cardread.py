#!/usr/bin/python3
"""Card Reader Daemon."""

import argparse
from datetime import datetime
import logging
import multiprocessing
import multiprocessing.queues
import os
import select
import signal
import sqlite3
import sys
import time
from configparser import ConfigParser
from pathlib import Path
import requests
from evdev import InputDevice, categorize, ecodes

# FIXME:
# HTTP POST correct URL and secrets
# Safer key mappings for events?

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'config_uri',
        default="/etc/cardread/config.ini",
        help='Configuration file, e.g., development.ini',
    )
    args = parser.parse_args(argv[1:])
    return args


class CardReader:
    """Card Reader Daemon class."""

    def __init__(self, config_uri=None):
        self.config_uri = config_uri
        self.config = None
        # DB connection/cursor
        self.con = None
        self.cur = None
        # Timeout (secs) for all blocking calls
        # This is the longest the app will take to stop when signalled
        self.timeout = 30

        # Create a persistent API session
        self.requests = requests.Session()
        self.requests.headers.update({"Content-Type": "application/jsonapi"})

        # Multiprocessing
        self.queue = multiprocessing.Queue()
        # Event object to signal workers to exit
        self.stop_event = multiprocessing.Event()
        # Event handler to trigger pushes to the API
        self.push_event = multiprocessing.Event()
        self.push_lock = multiprocessing.Lock()
        # Seconds since last push
        self.last_push = 0

        self.main()

    def event_listener(self):
        """Listen for keypress events, capture 'words' (\n separator) and push to the queue."""
        log.debug("Starting event_listener...")
        device = InputDevice(self.config.get("cardread", "device"))
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
        """Process items from the queue and save to the cache DB."""
        log.debug("Starting queue_worker...")
        # Loop until stop_event received
        while not self.stop_event.is_set():
            # Blocks waiting for queue item
            try:
                card_id = self.queue.get(timeout=self.timeout)
            except multiprocessing.queues.Empty:
                # Restart if queue was empty
                continue
            timestamp = datetime.fromtimestamp(time.time()).isoformat()
            log.debug("Received card_id: %s - %s", timestamp, card_id)
            jsonapi = self.make_jsonapi(card_id, timestamp)
            self.update_cache_row(jsonapi)
            # Trigger push event
            self.push_event.set()

    def push_event_handler(self):
        """Call push_cache debounced to every 'timeout' seconds."""
        while not self.stop_event.is_set():
            self.push_event.wait()
            elapsed = time.time() - self.last_push
            if elapsed <= self.timeout:
                wait = self.timeout - elapsed
                log.debug("Debouncing wait: %s", wait)
                # Wait for the remaining time
                time.sleep(wait)

            self.push_event.clear()
            self.push_cache()

    def http_post(self, dbid, jsonapi):
        """POST card data to server, repeating forever until success."""
        api_url = self.config.get('cardread', 'api_url')
        log.debug("Posting data: %s", jsonapi)
        try:
            response = self.requests.post(
                api_url,
                json=jsonapi,
                timeout=self.timeout
            )
            # Success or Conflict indicates data has reached server successfully
            if response.status_code not in (200, 201, 409):
                raise requests.exceptions.RequestException
        except requests.exceptions.RequestException as err:
            # Set the row pending again, and trigger a push event to retry
            log.debug("Post failed: %s %s", jsonapi, err)
            self.unlock_cache_row(dbid)
            self.push_event.set()
        else:
            log.debug("Post success: %s %s", jsonapi, response.status_code)
            self.clear_cache_row(jsonapi)

    def update_cache_row(self, jsonapi):
        """Add a new entry to the DB cache."""
        log.debug('Update Cache: %s', jsonapi)
        self.cur.execute(
            'insert into log (card_id, timestamp, state) VALUES (?, ?, ?)',
            (jsonapi["data"]["attributes"]["card_id"], jsonapi["data"]["attributes"]["timestamp"], "pending")
        )
        self.con.commit()

    def clear_cache_row(self, jsonapi):
        """Delete an entry from the DB cache."""
        log.debug('Clear Cache: %s', jsonapi)
        self.cur.execute(
            'DELETE FROM log WHERE card_id = ? and timestamp = ?',
            (jsonapi["data"]["attributes"]["card_id"], jsonapi["data"]["attributes"]["timestamp"])
        )
        self.con.commit()

    def unlock_cache_row(self, dbid):
        """Unlock a row in the DB cache."""
        log.debug("Unlocking row: %s", dbid)
        self.cur.execute("UPDATE log SET state = 'pending' WHERE id = ?", (dbid,))
        self.con.commit()

    def create_cache(self):
        """Set up sqlite3 cache database."""
        # Create cache dir & db
        cache_dir = os.path.join(Path.home(), ".local", "share", "cardread")
        try:
            os.makedirs(cache_dir)
        except FileExistsError:
            # Not an error if already present
            pass

        self.con = sqlite3.connect(os.path.join(cache_dir, "cardread.db"))
        # Add ability to make dicts from rows
        self.con.row_factory = sqlite3.Row
        self.cur = self.con.cursor()
        self.cur.execute("""
                         CREATE TABLE IF NOT EXISTS log (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         card_id,
                         timestamp,
                         state)
                         """)

    def push_cache(self):
        """Launch parallel workers to POST previously cached values to the server."""

        # Only allow one push process at a time
        with self.push_lock:
            # Lock all pending rows
            # API updates may take a long time to complete, so lock 'in-flight' rows so they don't get re-POSTed when push_cache re-runs
            log.debug("Locking rows...")
            res = self.cur.execute("UPDATE log SET state = 'locked' WHERE state = 'pending' RETURNING id, card_id, timestamp")
            rows = res.fetchall()
            self.con.commit()

            # Convert row data to JSON and POST
            for row in rows:
                rdict = dict(row)
                jsonapi = self.make_jsonapi(rdict["card_id"], rdict["timestamp"])
                multiprocessing.Process(target=self.http_post, args=(rdict["id"], jsonapi)).start()

            # record last-run time for debouncing
            self.last_push = time.time()

    def make_jsonapi(self, card_id, timestamp):
        """Create jsonapi for POST"""
        return {
            "data": {
                "type": "log_entries",
                "attributes": {
                    "card_id": card_id,
                    "timestamp": timestamp,
                }
            }
        }

    def parse_config(self):
        """Parse config file, using default values."""
        defaults = {
            "timeout": 5,
            "workers": 1,
        }

        self.config = ConfigParser(defaults)
        print(f'reading config from {self.config_uri}')
        self.config.read(self.config_uri)

        # Set Timeout
        self.timeout = self.config.getint('cardread', 'timeout')

        # Add API key to headers if defined in config
        if (api_key := self.config.get('cardread', 'api_key')):
            self.requests.headers.update({"Authorization": api_key})

    def main(self):
        """Main daemon process."""

        self.parse_config()
        # Handle 'stop' signals
        signal.signal(signal.SIGTERM, lambda sig, fr: self.stop_event.set())
        signal.signal(signal.SIGHUP, lambda sig, fr: self.stop_event.set())
        signal.signal(signal.SIGINT, lambda sig, fr: self.stop_event.set())

        # Set up the cache DB
        self.create_cache()

        # Start the push event handler process
        push_ev_handler_process = multiprocessing.Process(target=self.push_event_handler)
        push_ev_handler_process.start()

        # Trigger a push of any outstanding results to server
        self.push_event.set()

        # Launch the input device listener
        process = multiprocessing.Process(target=self.event_listener)
        process.start()

        # Set up worker pool processing input listener events
        workers = []
        for _ in range(self.config.getint("cardread", "workers")):
            p = multiprocessing.Process(target=self.queue_worker)
            p.start()
            workers.append(p)

        # Wait for workers to finish
        for worker in workers:
            worker.join()


if __name__ == "__main__":
    CardReader(config_uri=parse_args(sys.argv).config_uri)
