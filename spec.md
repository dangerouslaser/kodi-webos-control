# service.webostv: Kodi addon spec

Oct 9, 2026 · @dangerouslaser

## Overview and goals

`service.webostv` is a Kodi service addon that controls the LG webOS TV it runs on, matching what Home Assistant's `webostv` integration does, over the TV's local SSAP WebSocket API. The primary target is Kodi 21+ running natively on webOS (dev-mode install). A secondary target is Kodi on another box (CoreELEC, etc.) controlling a webOS TV on the LAN, which falls out of the same design for free.

Goals:

- **Parity with HA `webostv`:** power off, volume and mute, input and app switching, sound output selection, remote button presses, toast notifications, live state (power, volume, foreground app, input).
- **Kodi-native control surface:** every action reachable from keymaps, `RunScript`, JSON-RPC (via `JSONRPC.NotifyAll` / addon signals), skin buttons, and a small program UI.
- **Playback awareness (phase 2):** react to Kodi playback events, e.g. switch picture preset or HDR tone mapping based on `VideoPlayer.HdrType`.

Non-goals for v1: power-on via Wake-on-LAN when Kodi runs on the TV itself (if the TV is off, Kodi isn't running), calibration or 3D LUT upload, LG ThinQ cloud APIs, multi-TV management beyond one configured target.

## Key decision: vendor aiowebostv, replace aiohttp with a shim

Use [aiowebostv](https://github.com/home-assistant-libs/aiowebostv) 0.10.0 as the protocol core, vendored unmodified except for its import of aiohttp, which is swapped for a small compatibility shim backed by the pure-Python `websockets` library. aiowebostv itself is pure Python (about 1,400 lines across 7 modules) and Apache-2.0, but its only dependency, `aiohttp>=3.11`, pulls in C extensions and around eight transitive packages that Kodi on webOS cannot pip-install.

The aiohttp surface aiowebostv actually touches is narrow, so a shim is cheap:

- `ClientSession()` and `ClientSession.ws_connect(uri, heartbeat=, ssl=False, max_msg_size=)` returning a WS object, plus `ClientSession.close()`
- On the WS object: `send_json()`, `send_str()`, `receive_json(timeout=)`, `close()`, and async iteration yielding messages with `.type` and `.data`
- `WSMsgType` (at minimum `TEXT`, `CLOSED`, `ERROR`)
- Exceptions `aiohttp.ClientConnectionError` and `aiohttp.WSServerHandshakeError` (used for the ws:3000 to wss:3001 fallback)

| Option | Effort | Risk | Verdict |
| --- | --- | --- | --- |
| Vendor aiowebostv + aiohttp shim over `websockets` | Low (shim \~150 lines) | Low; upstream updates drop in, shim tested against the surface above | **Chosen** |
| Vendor aiowebostv + full aiohttp with `AIOHTTP_NO_EXTENSIONS` and pure-Python multidict/yarl/frozenlist/propcache | Medium | Large vendored tree, slow import on TV SoC, version churn | Fallback only |
| Port aiowebostv's endpoints and handshake to sync `websocket-client` (`script.module.websocket`) | Medium | Diverges from upstream; re-implements subscription and reconnect logic | Rejected |

Rules for Claude Code:

1. Vendor under `resources/lib/vendor/aiowebostv/` and `resources/lib/vendor/websockets/`, keep their LICENSE files, and record versions in `resources/lib/vendor/VENDORED.md`.
2. The only edit to aiowebostv is redirecting `import aiohttp` / `from aiohttp import …` to `resources/lib/aiohttp_shim`. Do this with an import alias (a `sys.modules['aiohttp']` registration before import), not by editing aiowebostv source, so upgrades are a straight file swap.
3. Force the pure-Python `websockets` path (no `speedups` C module) and confirm it imports under Kodi's interpreter.
4. Picture and luna features (phase 2) are not in aiowebostv. Its `endpoints.py` defines the luna URIs but the client has no luna call path. Implement the createAlert-to-luna technique ourselves in `resources/lib/luna.py`, modeled on [bscpylgtv](https://github.com/chros73/bscpylgtv).

## Target environment and constraints

The addon must run inside Kodi 21 (Omega) or later with only the standard library, Kodi's `xbmc*` modules, and vendored pure-Python code.

| Constraint | Detail | Consequence |
| --- | --- | --- |
| Kodi version | 21+ (first release with an official webOS build) | `addon.xml` requires `xbmc.python` 3.0.1 |
| Python | aiowebostv needs 3.11+ (uses `asyncio.timeout`) | Verify Kodi-on-webOS interpreter version at startup; log and disable cleanly if < 3.11 |
| No pip | Dev-mode app sandbox, read-only system | Everything vendored; no C extensions |
| Jailed process | Dev-mode apps cannot call `luna-send` directly | All control goes through SSAP over the network, even on-device |
| Target host | Kodi on the TV: `127.0.0.1` (default) | Fall back to the TV's LAN IP if loopback is refused; setting allows any host for off-TV use |
| TLS | Self-signed cert on 3001 | `ssl=False` (no verification), as aiowebostv already does |
| Weak SoC, low RAM | webOS may kill background apps under memory pressure | Lazy imports, no polling, single asyncio loop, small footprint |
| Sub-interpreters | Kodi runs each script/service invocation in its own interpreter | `RunScript` entry points cannot share the service's socket; they talk to the service via Kodi notifications (see Kodi integration surface) |

Also supported without code changes: Kodi on CoreELEC/LibreELEC/desktop controlling a webOS TV on the LAN. In that mode, power-on via Wake-on-LAN becomes meaningful and is included.

## Addon structure and file layout

One addon, id `service.webostv`, with two extension points: an `xbmc.service` (start `login`) that owns the TV connection, and an `xbmc.python.script` / `xbmc.python.pluginsource` entry for actions and the small UI.

```
service.webostv/
├── addon.xml
├── LICENSE.txt
├── service.py                  # entry: starts WebOsService, blocks on xbmc.Monitor
├── default.py                  # entry: RunScript actions + program UI
├── resources/
│   ├── settings.xml            # Kodi settings v2 format
│   ├── language/resource.language.en_gb/strings.po
│   ├── keymaps/webostv_example.xml   # sample keymap, user copies to userdata
│   └── lib/
│       ├── service.py          # WebOsService: asyncio thread, Monitor, Player hooks
│       ├── tv.py               # TvController: wraps WebOsClient, state, commands
│       ├── state.py            # TvState dataclass + Window(10000) property publisher
│       ├── actions.py          # action registry: name -> coroutine + arg schema
│       ├── ipc.py              # NotifyAll send/receive helpers, request/reply ids
│       ├── pairing.py          # pairing flow, client-key storage
│       ├── luna.py             # phase 2: createAlert -> luna technique
│       ├── automations.py      # phase 2: playback-driven rules (HDR -> preset)
│       ├── ui.py               # dialogs: inputs, apps, sound outputs, status
│       ├── wol.py              # magic packet (off-TV mode only)
│       ├── aiohttp_shim/       # __init__.py exposing the aiohttp surface listed above
│       └── vendor/
│           ├── VENDORED.md
│           ├── aiowebostv/     # 0.10.0, unmodified
│           └── websockets/     # pure-Python, unmodified
└── tests/                      # runs off-Kodi with a fake xbmc module
```

Runtime data lives in `special://profile/addon_data/service.webostv/`: `client_key.json` (keyed by TV host), `apps_cache.json`, `inputs_cache.json`. The client key is never stored in `settings.xml`, so settings exports and screenshots don't leak it.

## Protocol layer

`TvController` (in `tv.py`) is the only code that touches `WebOsClient`; everything else calls `TvController` methods or reads `TvState`. It mirrors how HA's `webostv` integration uses aiowebostv.

**Event loop.** `WebOsService` runs one asyncio loop in a dedicated daemon thread. `WebOsClient` must be constructed inside that loop, because its `__init__` calls `asyncio.get_running_loop()`. Kodi callbacks (Monitor, Player) run on the service's main thread and hand work to the loop with `asyncio.run_coroutine_threadsafe`. Nothing blocks the Kodi thread on network I/O.

**Connection lifecycle:**

1. Build `WebOsClient(host, client_key=<stored key or None>)` with the shim session.
2. `await client.connect()`. aiowebostv tries `ws://host:3000` first and falls back to `wss://host:3001` on a connection or handshake error; the shim must raise the matching exception types for this to work.
3. On first connect (no key), the TV shows the PROMPT pairing dialog. On success, read `client.client_key` and persist it to `client_key.json` immediately.
4. Register `register_state_update_callback(cb)`. aiowebostv subscribes to power state, volume, mute, foreground app, inputs, apps, sound output, and channel data, and calls `cb` on every push. `cb` copies `client.tv_state` and `client.tv_info` into `TvState` and publishes it.
5. On disconnect or error, reconnect with backoff (see Error handling). aiowebostv has no reconnect loop of its own; HA drives reconnects from its update cycle, so we supply one.

**Commands** go through aiowebostv's typed methods where they exist: `power_off`, `set_volume`, `volume_up`/`volume_down`, `set_mute`, `set_input`, `launch_app`, `launch_app_with_params`, `change_sound_output`, `send_message` (toast), `set_channel`, `channel_up`/`channel_down`, media `play`/`pause`/`stop`/`rewind`/`fast_forward`, `set_screen_state(on)`. Anything else goes through `client.request(uri, payload)`, exposed as the generic `command` action (HA's `webostv.command` equivalent).

**Buttons** use `client.button(name)`, which opens the pointer input socket (`com.webos.service.networkinput/getPointerInputSocket`) on first use and sends `type:button` frames. The shim must support this second WebSocket on the same session. Valid names come from aiowebostv's `buttons.py`; reject anything else before sending.

**Registration manifest.** Use aiowebostv's stock `REGISTRATION_MESSAGE` unchanged in v1. If phase 2 luna calls need permissions it lacks, extend the manifest in our code by deep-copying it, not by editing the vendored file, and note that a changed manifest triggers a fresh pairing prompt.

## Feature scope

Phase 1 is HA `webostv` parity; phase 2 adds what HA users bolt on with bscpylgtv and automations.

| Feature | HA equivalent | Backed by | Phase |
| --- | --- | --- | --- |
| Pairing and key storage | Config flow | `connect()` + PROMPT | 1 |
| Live state: power, volume, mute, app, input, sound output | `media_player` attributes | State update callback | 1 |
| Power off, screen off/on | `turn_off` | `power_off`, `set_screen_state` | 1 |
| Power on via WOL | Device trigger + `wake_on_lan` | `wol.py`, off-TV mode only | 1 |
| Volume set/step, mute | `volume_*` | Typed methods | 1 |
| Input / app switching | `select_source` | `set_input`, `launch_app` | 1 |
| Sound output | `webostv.select_sound_output` | `change_sound_output` | 1 |
| Remote buttons | `webostv.button` | Pointer socket | 1 |
| Raw SSAP request | `webostv.command` | `client.request` | 1 |
| Toast notification | `notify` platform | `send_message` | 1 |
| Media transport, channels | `media_*`, `play_media` | Typed methods | 1 |
| Picture preset, backlight, tone mapping | Not in core (bscpylgtv) | `luna.py` createAlert technique | 2 |
| Playback rules: HDR type to preset | HA automation | `automations.py` + `xbmc.Player` | 2 |
| Turn screen off on music playback / screensaver | HA automation | `automations.py` | 2 |

**Phase 2 playback rules.** On `Player.onAVStarted`, read `VideoPlayer.HdrType` (values include `hdr10`, `dolbyvision`, `hlg`, empty for SDR) and apply the mapped picture preset; on stop, restore the preset captured before playback. Rules are configured in settings as a mapping per HDR type, with a debounce so seeking or quick stop/start doesn't thrash the TV.

## Kodi integration surface

The service owns the only TV connection; every other entry point sends it a request over Kodi's notification bus and, where useful, waits for a reply.

**Actions (single registry in `actions.py`).** Each action has a name, an argument schema, and a coroutine on `TvController`. The same names are used by every entry point.

| Action | Args | Example |
| --- | --- | --- |
| `power_off` | none | `RunScript(service.webostv,power_off)` |
| `power_on` | none (off-TV mode) | `RunScript(service.webostv,power_on)` |
| `screen` | `on` or `off` | `RunScript(service.webostv,screen,off)` |
| `volume` | `up`, `down`, or 0–100 | `RunScript(service.webostv,volume,25)` |
| `mute` | `on`, `off`, `toggle` | `RunScript(service.webostv,mute,toggle)` |
| `input` | input id or label (case-insensitive) | `RunScript(service.webostv,input,HDMI_2)` |
| `app` | app id or title | `RunScript(service.webostv,app,netflix)` |
| `sound_output` | e.g. `tv_speaker`, `external_arc` | `RunScript(service.webostv,sound_output,external_arc)` |
| `button` | name from `buttons.py` | `RunScript(service.webostv,button,HOME)` |
| `toast` | message | `RunScript(service.webostv,toast,Hello)` |
| `command` | SSAP uri, optional JSON payload | `RunScript(service.webostv,command,ssap://system/getSystemInfo)` |
| `picture_preset` (phase 2) | preset name | `RunScript(service.webostv,picture_preset,cinema)` |
| `ui` | none | Opens the program UI |

**IPC.** `default.py` parses `sys.argv`, builds `{"id": <uuid>, "action": …, "args": […]}`, and sends it with JSON-RPC `JSONRPC.NotifyAll` (sender `service.webostv`, message `request`). The service receives it in `xbmc.Monitor.onNotification`, runs the action, and replies with message `reply` carrying the same id. `default.py` waits up to 5 s for the reply only when it needs data (UI lists, `command` results); fire-and-forget otherwise. External JSON-RPC clients (HA, scripts, a phone) can drive the addon the same way by calling `JSONRPC.NotifyAll`.

Note: `onNotification(sender, method, data)` receives NotifyAll messages with `method` set to `Other.<message>` (so `Other.request` / `Other.reply`) and `data` as a JSON string. Filter on `sender == "service.webostv"`; the service ignores its own `reply` messages.

**State for skins.** `state.py` publishes `TvState` as `Window(10000)` properties, e.g. `WebOS.Connected`, `WebOS.Power`, `WebOS.Volume`, `WebOS.Muted`, `WebOS.App`, `WebOS.AppTitle`, `WebOS.Input`, `WebOS.SoundOutput`, `WebOS.PicturePreset` (phase 2). Skins read them with `$INFO[Window(Home).Property(WebOS.Volume)]`. Only changed properties are written.

**Program UI** (`default.py` with no args, or `ui`). A select dialog: Inputs, Apps, Sound output, Buttons (a remote grid), Send toast, Status (host, connection, firmware from `get_software_info`, paired yes/no), Re-pair.

**Settings (`settings.xml`, v2 format).**

- Connection: host (default `127.0.0.1`), auto-fallback to LAN IP on loopback refusal (default on), MAC address (off-TV WOL only), connect timeout (default 10 s).
- Behavior: show toast on connect/failure (default off), reconnect backoff cap (default 60 s), debug logging.
- Pairing: action button to re-pair (deletes stored key, reconnects).
- Phase 2: per-HDR-type preset mapping (SDR, HDR10, HLG, Dolby Vision), restore on stop, debounce (default 1.5 s).

## Error handling, reconnect, logging

The service never crashes Kodi and never spams the TV: every failure maps to a state, a log line, and at most one user-visible notification.

| Condition | Detected by | Behavior |
| --- | --- | --- |
| TV unreachable / refused | `connect()` raises connection error or timeout | `WebOS.Connected=false`; retry with exponential backoff 2, 4, 8 … up to the cap (default 60 s); reset backoff on success |
| Loopback refused on-TV | Connection error on `127.0.0.1` | If auto-fallback is on, discover LAN IP (UDP connect trick to a non-routable address, read local socket name) and retry once there; persist the working host |
| Pairing declined or timed out | `WebOsTvPairError` | Notification "Pairing declined"; stop retrying until user picks Re-pair |
| Stored key rejected | Pair error with a key present | Delete key, show one notification, require Re-pair (don't auto-prompt in a loop) |
| Socket drop mid-session | Message loop ends / `is_connected()` false | Enter reconnect loop; pending action requests fail fast with reply `{"error": "disconnected"}` |
| Command error from TV | `WebOsTvCommandError` | Reply carries the TV's error text; log at warning |
| Unknown action or bad args | Registry validation | Reply `{"error": "bad_request"}`; nothing sent to the TV |
| Python < 3.11 | Version check at service start | Log error, one notification, service exits cleanly |
| Kodi shutdown | `Monitor.abortRequested()` | Cancel tasks, `client.disconnect()`, stop loop thread within 2 s |

Logging uses `xbmc.log` with a `[service.webostv]` prefix. INFO for connect, disconnect, pairing; DEBUG (setting-gated) for every SSAP request and response, with the client key redacted. Route aiowebostv's `logging` logger into `xbmc.log` via a handler so its debug output follows the same setting.

## Testing plan and acceptance criteria

Most of the code is testable off-Kodi and off-TV; only the final checks need real hardware.

**Off-device (pytest, CI):**

1. A fake `xbmc`, `xbmcaddon`, `xbmcgui`, `xbmcvfs` package in `tests/fakes/` so `resources/lib` imports cleanly.
2. A fake SSAP server (asyncio + `websockets`) that implements hello, register with PROMPT, `request`/`subscribe` echo for the endpoints we use, a pointer input socket, and scripted drops. Serve both ws:3000 and wss:3001 (self-signed cert) to exercise the fallback.
3. Shim conformance tests that run aiowebostv's real `connect()` → subscribe → command → `button()` → `disconnect()` against the fake server.
4. Unit tests for the action registry (arg parsing from `RunScript` argv), IPC round trip, backoff timing, key persistence, and state-to-property diffing.
5. An import-time test that the vendored tree contains no `.so`/`.pyd` files.

**On-device (manual checklist, LG webOS TV with Kodi 21 in dev mode):**

- [ ] Service starts at login; first run shows the TV pairing prompt; accepting stores the key; a Kodi restart reconnects with no prompt.
- [ ] Connects over `127.0.0.1`; if refused, falls back to the LAN IP and logs which host worked.
- [ ] `volume`, `mute`, `input`, `app`, `sound_output`, `button`, `toast`, `power_off` each work from a keymap and from `RunScript`.
- [ ] Changing volume with the LG remote updates `WebOS.Volume` within 1 s.
- [ ] Switching to another input or app (Kodi backgrounded) and back leaves the service connected or reconnects within the backoff cap.
- [ ] `command` with `ssap://system/getSystemInfo` returns data to the UI.
- [ ] Re-pair from settings clears the key and prompts once.
- [ ] Idle service CPU stays near zero (no polling loops) over 10 minutes.
- [ ] Phase 2: HDR10 file switches preset on start and restores on stop; seeking does not re-trigger.

**Off-TV mode check:** the same build on CoreELEC targeting a LAN TV passes the list above plus `power_on` via WOL.

## Milestones and open questions

Build in this order; each milestone ends green on its tests before the next starts.

1. **Shim + vendor.** `aiohttp_shim` over vendored `websockets`, aiowebostv 0.10.0 vendored, fake SSAP server, conformance tests passing off-Kodi.
2. **Headless service.** `service.py` with loop thread, pairing, key storage, reconnect/backoff, state callback writing Window properties. No actions yet.
3. **Actions + IPC.** Registry, `default.py` argv parsing, NotifyAll request/reply, all phase 1 actions, example keymap.
4. **UI + settings.** Program UI, settings v2, Re-pair, debug logging toggle, strings.po.
5. **On-device validation.** Run the on-device checklist; fix loopback or permission surprises.
6. **Phase 2.** `luna.py` (createAlert technique), `picture_preset` action, HDR playback rules with capture/restore and debounce.

Open questions to resolve during milestone 1–2 and record in the README:

- Which Python version does the official Kodi 21 webOS build ship? aiowebostv needs 3.11+; if lower, pin an older aiowebostv or backport `asyncio.timeout`.
- Does the SSAP server accept connections on `127.0.0.1`, or only on the LAN interface? Behavior may vary by webOS version.
- Does webOS suspend or kill Kodi's process when another input or app is in the foreground? If so, connection state on return needs to be rebuilt fast rather than through the full backoff.
- Does the createAlert-to-luna technique still work on the firmware in use (it may be restricted on newer firmware)? If not, phase 2 picture control falls back to SSH on rooted sets, as script.piccap does.
- Does `VideoPlayer.HdrType` report correctly on the webOS build, where decoding may go through the TV's own media pipeline?
