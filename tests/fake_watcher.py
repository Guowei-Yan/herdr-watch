"""Small child process for monitor lifecycle tests."""
import sys
import time

print("[herdr-watch sample] ARMED idle", flush=True)
if sys.argv[1] == "wait":
    while True:
        time.sleep(1)
print("[herdr-watch sample] FINISHED worker-1", flush=True)
print("    Actual final report", flush=True)
print("[herdr-watch sample] ENDED once", flush=True)
