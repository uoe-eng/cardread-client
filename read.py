import json
import logging
import multiprocessing
import multiprocessing.queues
import os
import select
import signal
import time
import requests
from evdev import InputDevice, categorize, ecodes

# FIXME:
# Actually do http POST stuff
# Disk caching and re-run of failed POST at start.
# Logging (especially start/shutdown/signals, post fails etc)
# Safer key mappings for events

class CardReader:

    def __init__(self):
        # FIXME: Permanent path
        self.cache_dir = '/tmp/cardread'
        # Define the input device path
        self.device_path = '/dev/input/by-id/usb-Dell_Dell_Wired_Multimedia_Keyboard-event-kbd'
        # Define the number of worker processes to run
        self.num_workers = 4
        self.reader = "Readername"
        # Timeout (secs) for all blocking calls
        # This is the longest the app will take to stop when signalled
        self.timeout = 5
        self.headers = {"Content-Type": "application/jsonapi"}
        # FIXME: Real url, not random status code one
        self.url = "http://httpbin.org/status/200%2C409%2C500"
        self.queue = multiprocessing.Queue()
        # Event object to signal workers to exit
        self.stop_event = multiprocessing.Event()
        self.main()

    def event_listener(self):
        """Listen for key events, capture 'words' and push to the queue."""
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

    def worker(self):
        # Loop until stop_event received
        while not self.stop_event.is_set():
            # Blocks waiting for queue item
            try:
                word = self.queue.get(timeout=self.timeout)
            except multiprocessing.queues.Empty:
                # Restart if queue was empty
                continue
            print(f"Received Word: {word}")
            now = time.time()
            jsonapi = {
                "type": "log",
                # FIXME: use real uuids
                "id": "uuid",
                "data": {
                    "reader": self.reader,
                    "time": now,
                    "card_id": word,
                }
            }
            self.update_cache(jsonapi)
            while not self.http_post(jsonapi):
                print("POST failed, retrying...")
                time.sleep(self.timeout)
            self.clear_cache(jsonapi)

    def http_post(self, jsonapi):
        response = requests.post(self.url, headers=self.headers, json=jsonapi)
        # Success or Conflict indicates data has reached server successfully
        if response.status_code in (200, 409):
            return True

    def update_cache(self, jsonapi):
        print(f'Update Cache:', jsonapi)
        with open(os.path.join(self.cache_dir, f'{jsonapi["data"]["card_id"]}_{jsonapi["data"]["time"]}'), 'w') as cache_f:
            # FIXME: Save jsonapi JSON for easy POSTing on restart
            cache_f.write(json.dumps(jsonapi))


    def clear_cache(self, jsonapi):
        print(f'Clear Cache:', jsonapi)


    def main(self):
        # Handle 'stop' signals
        signal.signal(signal.SIGTERM, lambda sig, fr: self.stop_event.set())
        signal.signal(signal.SIGHUP, lambda sig, fr: self.stop_event.set())
        signal.signal(signal.SIGINT, lambda sig, fr: self.stop_event.set())

        # Launch the keyboard listener
        process = multiprocessing.Process(target=self.event_listener)

        # Start the daemon process
        process.start()

        # Set up API worker pool and wait for them to finish
        workers = []
        for _ in range(self.num_workers):
            p = multiprocessing.Process(target=self.worker)
            p.start()
            p.join()

if __name__ == "__main__":
    CardReader()
