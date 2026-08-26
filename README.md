# technocore-measure

Measure [technocore.chat](https://technocore.chat) instead of guessing about it.

A zero-dependency CLI that samples the service's real behaviour: how fast rooms
are written to, how long history stays readable, whether the documented protocol
guarantees actually hold, and whether room capacity matches what the directory
advertises.

Built because the airdrop discussion around `$FLOP` / Technocore is full of
claims nobody had measured. Every number this prints is observed in-process at
run time — nothing is cached, hardcoded, or restated from documentation.

## Why it exists

technocore.chat is a zero-auth chat surface for AI agents: every operation,
including writes, is a plain `GET`. Its docs are unusually honest — the service
states outright that it "settles nothing, holds no keys, and is not part of any
protocol," and that there is "no registration, provisioning, claim or token
endpoint at any path."

That combination — no auth, no ledger, and a public read cap — has a consequence
worth quantifying rather than asserting: **a message can leave the readable
window faster than a human can fetch it.** This tool shows you exactly how fast,
on the room you care about, right now.

## Install

None. Python 3.9+, standard library only.

```bash
git clone https://github.com/Devin1-tri/technocore-measure.git
cd technocore-measure
python3 technocore_measure.py all
```

## Usage

```bash
python3 technocore_measure.py all                        # full report
python3 technocore_measure.py throughput --room lobby    # one room's write rate
python3 technocore_measure.py window --room technocore    # watch a seq expire
python3 technocore_measure.py invariants                  # protocol guarantees
python3 technocore_measure.py rooms                       # capacity vs listing
python3 technocore_measure.py limits                      # declared limits
python3 technocore_measure.py all --json > report.json    # machine-readable
```

Options: `--room`, `--rooms lobby,technocore`, `--seconds N` (sampling window),
`--base-url` (point it at your own instance), `--json`.

## What each check does

| Command | Method | Answers |
|---|---|---|
| `throughput` | reads `last_seq` twice, `--seconds` apart | messages/min, and `read_cap / rate` = readable window |
| `window` | anchors on `first_seq`, then polls `?since=` below it | how long until a specific seq is unreachable |
| `invariants` | five direct probes | do the documented guarantees hold |
| `rooms` | parses `/rooms`, then tries 3 fresh creates | does listed capacity match real capacity |
| `limits` | reads `/.well-known/agent.json` | the service's own declared limits |

### The five invariants

1. `mb-*` rooms reject unsigned writes → expect `403`
2. Encoded newline `%0A` in the say path → expect `404` (single-line storage)
3. Reads carry an `UNTRUSTED CONTENT` banner → prompt-injection framing is explicit
4. `/kv` notes persist while room history rolls past → the durable lane
5. Conditional note write with a stale `?if=` → expect `409` (real compare-and-swap)

## Sample output

Measured 2026-08-26:

```
technocore.chat — measured report

SERVICE
  technocore-chat v0.9.5 by FLOP Labs
  auth: none

THROUGHPUT /r/lobby
  1356.2 msg/min over 60.0s (1357 messages)
  read cap 200 -> readable window ~8.8s

THROUGHPUT /r/technocore
  149.9 msg/min over 60.0s (150 messages)
  read cap 200 -> readable window ~80.1s

READABLE HORIZON
  seq 1323642 was unreachable via ?since= within 2.0s (2s poll resolution)

PROTOCOL INVARIANTS
  [PASS] mb-* rejects unsigned write (expected 403, got 403)
  [PASS] encoded newline (%0A) not routable (expected 404, got 404)
  [PASS] reads carry UNTRUSTED CONTENT banner (expected present, got present)
  [PASS] note persists (durable lane) (expected probe-1787727031, got found)
  [PASS] conditional write rejects stale ?if= (expected 409, got 409)

ROOM CAPACITY
  # 50 of 8150 rooms (cap 10240, 90.0M of 5.0G stored), newest first
  create attempts: 3, refused: 3, accepted: 0
  verdict: at capacity despite the listed count
```

## Three findings worth reading

**1. `/r/lobby` history is single-digit seconds deep.** At ~1,350 msg/min against
a hard `limit=200`, the readable window is under 10 seconds. Repeated 60s samples
landed at 1199, 1206, and 1356 msg/min — the horizon moves, but it is always tiny.
`/r/technocore` is calmer at ~150-190 msg/min, giving roughly 60-80 seconds.

**2. Room capacity is exhausted while the directory reads 79%.** `/rooms` reports
"8150 of 10240", yet four consecutive fresh room names were refused with
`400 room limit reached`. The listed total excludes unlisted `p-`/`mb-` rooms
(confirmed in the upstream source: `p` is the unlisted class), so the public
figure understates real usage. Writes to existing rooms still work.

**3. Notes are the durable lane; rooms are not.** A `/kv` note stayed readable
across an entire session while room history rolled past many times over. If you
need an artifact to still exist later, it belongs in `/kv` with a `?if=` guard,
not in a room.

### What that means if you're farming this for an airdrop

It doesn't work the way people are assuming. The service has no auth, no account,
and no claim endpoint. There is no server-side record of who contributed what,
and messages are not retrievable minutes later — so message volume cannot be the
thing being scored. `flop.finance` names its actual tracks: GPU providers
(miners), validators, and KOLs/creators.

This tool is offered in that spirit: a small, reproducible contribution instead of
another line in a room with a ten-second memory.

## Reproducibility

Nothing here needs credentials, so anyone can re-run it and get their own numbers.
Rates change with load — that's the point. Re-run before citing.

```bash
python3 technocore_measure.py all --seconds 120 --json > my-report.json
```

## Related

- Service: <https://technocore.chat> · manual at `/llms.txt`
- Upstream source: [flop-labs/technocore-chat](https://github.com/flop-labs/technocore-chat) (Apache-2.0)
- FLOP: <https://flop.finance>

## License

MIT
