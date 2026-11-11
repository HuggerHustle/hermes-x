"""Stand-in for the xurl binary.

Appends {"argv", "pid", "start", "end", "client_id_env"} to $FAKE_XURL_DIR/calls.jsonl and answers from
$FAKE_XURL_DIR/get.json or post.json: {"stdout": str, "stderr": str, "exit": int, "sleep": float}.
A spec with "events" instead of "stdout" serves them as pages, honoring max_results and
pagination_token like GET /2/dm_events.
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

root = Path(os.environ["FAKE_XURL_DIR"])
argv = sys.argv[1:]
start = time.time()
(root / "pids").mkdir(exist_ok=True)
(root / "pids" / str(os.getpid())).touch()
spec = json.loads((root / ("post.json" if "POST" in argv else "get.json")).read_text())
time.sleep(spec.get("sleep", 0))
if "events" in spec:
    query = parse_qs(urlsplit(next(a for a in argv if a.startswith("/2/"))).query)
    size = int(query.get("max_results", ["100"])[0])
    offset = int(query.get("pagination_token", ["tok0"])[0][3:])
    rows = spec["events"][offset : offset + size]
    meta: dict[str, object] = {"result_count": len(rows)}
    if offset + size < len(spec["events"]):
        meta["next_token"] = f"tok{offset + size}"
    sys.stdout.write(json.dumps({"data": rows, "meta": meta}))
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
