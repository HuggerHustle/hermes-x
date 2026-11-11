"""Stand-in for the xurl binary.

Appends {"argv", "pid", "start", "end", "client_id_env"} to $FAKE_XURL_DIR/calls.jsonl and answers from
$FAKE_XURL_DIR/get.json or post.json: {"stdout": str, "stderr": str, "exit": int, "sleep": float}.
"""

import json
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["FAKE_XURL_DIR"])
argv = sys.argv[1:]
start = time.time()
(root / "pids").mkdir(exist_ok=True)
(root / "pids" / str(os.getpid())).touch()
spec = json.loads((root / ("post.json" if "POST" in argv else "get.json")).read_text())
time.sleep(spec.get("sleep", 0))
sys.stdout.write(spec.get("stdout", ""))
sys.stderr.write(spec.get("stderr", ""))
with (root / "calls.jsonl").open("a") as log:
    log.write(
        json.dumps(
            {
                "argv": argv,
                "pid": os.getpid(),
                "start": start,
                "end": time.time(),
                "client_id_env": "CLIENT_ID" in os.environ,
            }
        )
        + "\n"
    )
sys.exit(spec.get("exit", 0))
