#!/usr/bin/env python3
"""
td_test.py — Print the first 10 messages from the Network Rail TD feed.

Requires:
    pip install stomp.py

Environment variables:
    NR_USERNAME  — Network Rail Open Data portal username
    NR_PASSWORD  — Network Rail Open Data portal password
"""

import os
import json
import threading
import stomp

HOST = "publicdatafeeds.networkrail.co.uk"
PORT = 61618
TOPIC = "/topic/TD_ALL_SIG_AREA"
MAX_MESSAGES = 10


class TDListener(stomp.ConnectionListener):
    def __init__(self, conn, done_event):
        self.conn = conn
        self.done_event = done_event
        self.count = 0

    def on_connected(self, frame):
        print(f"Connected. Subscribing to {TOPIC} …\n")
        self.conn.subscribe(destination=TOPIC, id=1, ack="auto")

    def on_message(self, frame):
        self.count += 1
        print(f"--- Message {self.count} ---")
        try:
            body = json.loads(frame.body)
            print(json.dumps(body, indent=2))
        except (json.JSONDecodeError, TypeError):
            print(frame.body)
        print()

        if self.count >= MAX_MESSAGES:
            print(f"Received {MAX_MESSAGES} messages. Disconnecting.")
            self.done_event.set()

    def on_error(self, frame):
        print(f"ERROR: {frame.body}")
        self.done_event.set()

    def on_disconnected(self):
        print("Disconnected.")


def main():
    username = os.environ.get("NR_USERNAME")
    password = os.environ.get("NR_PASSWORD")
    if not username or not password:
        raise SystemExit("Set NR_USERNAME and NR_PASSWORD environment variables before running.")

    done = threading.Event()

    conn = stomp.Connection(
        host_and_ports=[(HOST, PORT)],
        heartbeats=(10000, 10000),
    )
    conn.set_listener("", TDListener(conn, done))
    conn.connect(username, password, wait=True)

    print(f"Waiting for up to {MAX_MESSAGES} messages …")
    done.wait()
    conn.disconnect()


if __name__ == "__main__":
    main()
