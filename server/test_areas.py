import stomp, json, time, os, pathlib

# Load credentials from file if not in environment
creds_path = pathlib.Path.home() / "Library" / "Application Support" / "NearbyTrains" / "credentials"
with open(creds_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

seen_areas = set()

class L(stomp.ConnectionListener):
    def __init__(self, topic):
        self.topic = topic
    def on_message(self, frame):
        try:
            for item in json.loads(frame.body):
                if 'CA_MSG' in item:
                    area = item['CA_MSG'].get('area_id','').upper()
                    if area and area not in seen_areas:
                        seen_areas.add(area)
                        print(f"[{self.topic}] NEW AREA: {area}", flush=True)
        except Exception:
            pass

candidates = ['TD_SC_SIG_AREA', 'TD_HA_SIG_AREA', 'TD_NE_SIG_AREA', 'TD_NW_SIG_AREA']

conn = stomp.Connection([('publicdatafeeds.networkrail.co.uk', 61618)], heartbeats=(10000,10000))
conn.connect(os.environ['NR_USERNAME'], os.environ['NR_PASSWORD'], wait=True)
for i, t in enumerate(candidates, start=1):
    conn.set_listener(t, L(t))
    try:
        conn.subscribe(f'/topic/{t}', id=i, ack='auto')
        print(f"Subscribed to {t}", flush=True)
    except Exception as e:
        print(f"Failed {t}: {e}", flush=True)
print("Listening for 60s...", flush=True)
time.sleep(60)
conn.disconnect()
print("Done.")
