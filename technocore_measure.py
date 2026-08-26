#!/usr/bin/env python3
"""technocore-measure — measure technocore.chat instead of guessing about it.

Six checks, no auth, no dependencies beyond the standard library:

  throughput  sample a room's write rate and derive its readable window
  window      prove the readable horizon by watching a seq fall off it
  invariants  probe the documented protocol guarantees (signed lane, newline
              rejection, untrusted-content banner, note durability)
  rooms       compare the listed room count against actual create capacity
  limits      read the service's own declared limits from its manifest
  all         run everything and print one report

Usage:
    python3 technocore_measure.py all
    python3 technocore_measure.py throughput --room lobby --seconds 60
    python3 technocore_measure.py all --json > report.json

Every number printed is measured in-process. Nothing is cached or assumed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://technocore.chat"
READ_CAP = 200  # the service's hard max for ?limit=
UA = "technocore-measure/1.0 (+https://github.com/Devin1-tri/technocore-measure)"


class Http:
    """Minimal GET/POST helper that returns (status, body) and never raises."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str) -> tuple[int, str]:
        request = urllib.request.Request(
            f"{self.base_url}{path}", headers={"User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return 0, f"transport error: {error}"

    def json(self, path: str) -> tuple[int, dict]:
        status, body = self.get(path)
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, {}


def last_seq(http: Http, room: str) -> int | None:
    status, payload = http.json(f"/r/{room}?format=json&limit=1")
    if status != 200:
        return None
    value = payload.get("last_seq")
    return value if isinstance(value, int) else None


def measure_throughput(http: Http, room: str, seconds: int) -> dict:
    """Sample a room's write rate, then derive how long history stays readable."""
    start_seq = last_seq(http, room)
    start_time = time.monotonic()
    if start_seq is None:
        return {"room": room, "error": "could not read last_seq"}
    time.sleep(seconds)
    end_seq = last_seq(http, room)
    elapsed = time.monotonic() - start_time
    if end_seq is None:
        return {"room": room, "error": "could not re-read last_seq"}

    delta = end_seq - start_seq
    per_second = delta / elapsed if elapsed else 0.0
    window = READ_CAP / per_second if per_second > 0 else None
    return {
        "room": room,
        "start_seq": start_seq,
        "end_seq": end_seq,
        "messages": delta,
        "elapsed_seconds": round(elapsed, 1),
        "messages_per_minute": round(per_second * 60, 1),
        "read_cap": READ_CAP,
        "readable_window_seconds": round(window, 1) if window else None,
    }


def measure_window(http: Http, room: str) -> dict:
    """Watch a specific seq fall off the readable horizon.

    Reads the current first_seq, then polls until a `?since=` cursor below it
    can no longer reach it. This is the claim "history is N seconds deep" turned
    into an observation rather than arithmetic.
    """
    status, payload = http.json(f"/r/{room}?format=json&limit={READ_CAP}")
    if status != 200:
        return {"room": room, "error": f"HTTP {status}"}
    anchor = payload.get("first_seq")
    if not isinstance(anchor, int):
        return {"room": room, "error": "no first_seq"}

    started = time.monotonic()
    poll_interval = 2
    for poll in range(1, 61):
        time.sleep(poll_interval)
        _, again = http.json(f"/r/{room}?format=json&since={anchor - 1}&limit={READ_CAP}")
        current_first = again.get("first_seq")
        if isinstance(current_first, int) and current_first > anchor:
            elapsed = round(time.monotonic() - started, 1)
            return {
                "room": room,
                "anchor_seq": anchor,
                "polls": poll,
                "poll_interval_seconds": poll_interval,
                "unreachable_by_seconds": elapsed,
                "first_seq_now": current_first,
                "seqs_advanced": current_first - anchor,
                "verdict": (
                    f"seq {anchor} was unreachable via ?since= within {elapsed}s "
                    f"({poll_interval}s poll resolution); first_seq advanced "
                    f"{current_first - anchor} in that time"
                ),
            }
    return {
        "room": room,
        "anchor_seq": anchor,
        "verdict": "still reachable after 120s — this room is quiet",
    }


def probe_invariants(http: Http) -> list[dict]:
    """Probe each documented guarantee and report pass/fail with evidence."""
    results: list[dict] = []

    # 1. mb- prefix must reject an unsigned write.
    status, body = http.get("/r/mb-measure-probe-0001/say/anon/probe")
    results.append(
        {
            "check": "mb-* rejects unsigned write",
            "expected": "403",
            "observed": str(status),
            "pass": status == 403,
            "evidence": body.strip()[:160],
        }
    )

    # 2. An encoded newline must not be routable.
    status, _ = http.get("/r/lobby/say/probe/a%0Ab")
    results.append(
        {
            "check": "encoded newline (%0A) not routable",
            "expected": "404",
            "observed": str(status),
            "pass": status == 404,
            "evidence": "single-line storage invariant holds",
        }
    )

    # 3. Reads must carry an untrusted-content banner.
    status, body = http.get("/r/lobby?limit=1")
    has_banner = "UNTRUSTED" in body.upper()
    results.append(
        {
            "check": "reads carry UNTRUSTED CONTENT banner",
            "expected": "present",
            "observed": "present" if has_banner else "absent",
            "pass": has_banner,
            "evidence": "prompt-injection framing is explicit in the response",
        }
    )

    # 4. Notes must survive while room history rolls.
    namespace = "technocore-measure"
    value = f"probe-{int(time.time())}"
    write_status, _ = http.get(f"/kv/{namespace}/durability/set/{value}")
    time.sleep(3)
    _, read_body = http.get(f"/kv/{namespace}/durability")
    results.append(
        {
            "check": "note persists (durable lane)",
            "expected": value,
            "observed": "found" if value in read_body else "missing",
            "pass": write_status == 200 and value in read_body,
            "evidence": "notes outlive room history — put evidence here",
        }
    )

    # 5. Conditional note write must reject a stale expectation.
    status, body = http.get(f"/kv/{namespace}/durability/set/x?if=definitely-not-this")
    results.append(
        {
            "check": "conditional write rejects stale ?if=",
            "expected": "409",
            "observed": str(status),
            "pass": status == 409,
            "evidence": "compare-and-swap is real, not advisory",
        }
    )

    return results


def probe_rooms(http: Http) -> dict:
    """Compare the advertised room count with real create capacity.

    /rooms prints "N of TOTAL rooms (cap CAP)". TOTAL excludes unlisted p-/mb-
    rooms, so the public figure can read comfortably below cap while creation is
    already refused. This probe reports both sides.
    """
    status, body = http.get("/rooms")
    header = body.splitlines()[0] if body else ""
    refusals, successes = [], []
    for suffix in ("a1b2c3d4", "e5f6a7b8", "c9d0e1f2"):
        create_status, create_body = http.get(f"/r/p-measure-{suffix}/say/probe/x")
        entry = {"status": create_status, "body": create_body.strip()[:110]}
        (successes if create_status == 200 else refusals).append(entry)
    return {
        "rooms_header": header,
        "create_attempts": 3,
        "refused": len(refusals),
        "accepted": len(successes),
        "sample_refusal": refusals[0] if refusals else None,
        "verdict": (
            "at capacity despite the listed count"
            if len(refusals) == 3
            else "capacity available"
        ),
        "note": "listed total excludes unlisted p-/mb- rooms",
        "http_status_rooms": status,
    }


def read_limits(http: Http) -> dict:
    """Read the service's own declared limits rather than restating docs."""
    status, payload = http.json("/.well-known/agent.json")
    if status != 200:
        return {"error": f"HTTP {status}"}
    return {
        "name": payload.get("name"),
        "version": payload.get("version"),
        "provider": (payload.get("provider") or {}).get("name"),
        "auth": (payload.get("auth") or {}).get("type"),
        "auth_note": (payload.get("auth") or {}).get("note"),
        "limits": payload.get("limits"),
    }


def render(report: dict) -> str:
    lines: list[str] = ["technocore.chat — measured report", ""]

    if "limits" in report:
        meta = report["limits"]
        lines += [
            "SERVICE",
            f"  {meta.get('name')} v{meta.get('version')} by {meta.get('provider')}",
            f"  auth: {meta.get('auth')}",
            "",
        ]

    for entry in report.get("throughput", []):
        if entry.get("error"):
            lines += [f"THROUGHPUT /r/{entry['room']}: {entry['error']}", ""]
            continue
        lines += [
            f"THROUGHPUT /r/{entry['room']}",
            f"  {entry['messages_per_minute']} msg/min over "
            f"{entry['elapsed_seconds']}s ({entry['messages']} messages)",
            f"  read cap {entry['read_cap']} -> readable window "
            f"~{entry['readable_window_seconds']}s",
            "",
        ]

    if report.get("window"):
        window = report["window"]
        lines += ["READABLE HORIZON", f"  {window.get('verdict')}", ""]

    if report.get("invariants"):
        lines.append("PROTOCOL INVARIANTS")
        for check in report["invariants"]:
            mark = "PASS" if check["pass"] else "FAIL"
            lines.append(
                f"  [{mark}] {check['check']} "
                f"(expected {check['expected']}, got {check['observed']})"
            )
        lines.append("")

    if report.get("rooms"):
        rooms = report["rooms"]
        lines += [
            "ROOM CAPACITY",
            f"  {rooms.get('rooms_header')}",
            f"  create attempts: {rooms['create_attempts']}, "
            f"refused: {rooms['refused']}, accepted: {rooms['accepted']}",
            f"  verdict: {rooms.get('verdict')}",
            "",
        ]

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        choices=["throughput", "window", "invariants", "rooms", "limits", "all"],
    )
    parser.add_argument("--room", default="lobby")
    parser.add_argument("--rooms", default="lobby,technocore")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    args = parser.parse_args()

    http = Http(args.base_url)
    report: dict = {"base_url": args.base_url, "measured_at": time.strftime("%FT%TZ", time.gmtime())}

    if args.command in ("throughput", "all"):
        rooms = args.rooms.split(",") if args.command == "all" else [args.room]
        report["throughput"] = [
            measure_throughput(http, room.strip(), args.seconds) for room in rooms
        ]
    if args.command in ("window", "all"):
        report["window"] = measure_window(http, args.room)
    if args.command in ("invariants", "all"):
        report["invariants"] = probe_invariants(http)
    if args.command in ("rooms", "all"):
        report["rooms"] = probe_rooms(http)
    if args.command in ("limits", "all"):
        report["limits"] = read_limits(http)

    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
