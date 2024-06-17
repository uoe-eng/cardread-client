import logging
import multiprocessing
import multiprocessing.queues
import select
import signal
from evdev import InputDevice, categorize, ecodes


def handle_signal(stop_event):
    """Exit cleanly when certain signals received."""
    # Signal all workers to exit
    stop_event.set()

def event_listener(device_path, queue, stop_event):
    """Listen for key events, capture 'words' and push to the queue."""
    device = InputDevice(device_path)
    word = []
    while not stop_event.is_set():
        # Listen for read events on input device fd
        r, _, _ = select.select([device.fd], [], [], 5)
        if r:
            for event in device.read():
                # Keypress 'down'
                if event.type == ecodes.EV_KEY and event.value == 1:
                    key_event = categorize(event)
                    # Translate keycode to char
                    # FIXME: Would be better to have a proper map - e.g KEY_A='a', KEY_ENTER='\n' etc
                    char = key_event.keycode.split('_')[1].lower()
                    # Add chars to word until enter received, then push to Queue
                    if char == "enter":
                        queue.put(''.join(word))
                        word = []
                    else:
                        word.append(char)

def api_worker(queue, stop_event):
    # Loop until stop_event received
    while not stop_event.is_set():
        # Blocks waiting for queue item
        try:
            # FIXME: timeout can be longer (length will set max time app takes to shut down after signal)
            word = queue.get(timeout=5)
        # Restart loop if the queue was empty
        except multiprocessing.queues.Empty:
            continue
        # FIXME: actually do HTTP stuff here
        print("WORD: ", word)
        

def main():
    # Define the input device
    device_path = '/dev/input/by-id/usb-Dell_Dell_Wired_Multimedia_Keyboard-event-kbd'

    # Number of Request http workers
    num_workers = 4
    
    # Create a stop event to cancel all workers
    stop_event = multiprocessing.Event()

    # Register the signal handler for 'stop' signals
    signal.signal(signal.SIGTERM, lambda sig, frame: handle_signal(stop_event))
    signal.signal(signal.SIGHUP, lambda sig, frame: handle_signal(stop_event))
    signal.signal(signal.SIGINT, lambda sig, frame: handle_signal(stop_event))


    # Create a multiprocessing queue
    queue = multiprocessing.Queue()

    # Set up API worker pool    
    workers = []
    for _ in range(num_workers):
        p = multiprocessing.Process(target=api_worker, args=(queue, stop_event))
        p.start()
        workers.append(p)

    # Launch the keyboard listener
    process = multiprocessing.Process(target=event_listener, args=(device_path, queue, stop_event), daemon=True)
    
    # Start the daemon process
    process.start()
    
    # Wait for all worker processes to finish
    for p in workers:
        p.join()

if __name__ == "__main__":
    main()
