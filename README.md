# service.webostv

A Kodi service addon that controls the LG webOS TV it runs on, matching what Home Assistant's
`webostv` integration does, over the TV's local SSAP WebSocket API. Built for Kodi 21+ running
natively on webOS (dev-mode install). It also works from Kodi on another box (CoreELEC,
LibreELEC, desktop) controlling a webOS TV on the LAN.

See [`spec.md`](spec.md) for the full design.

## Status

| Milestone | State |
| --- | --- |
| 1. Shim + vendor | Done: aiowebostv 0.10.0 + websockets 17.2 vendored, aiohttp shim, fake SSAP server, conformance tests |
| 2. Headless service | Done: loop thread, pairing, key storage, reconnect/backoff, loopback→LAN fallback, Window properties |
| 3. Actions + IPC | Done: action registry, `RunScript` argv parsing, NotifyAll request/reply, example keymap |
| 4. UI + settings | Done: program UI, settings v2, Re-pair, debug logging, strings.po |
| 5. On-device validation | Off-TV mode done (see below); on-TV mode not yet tested |
| 6. Phase 2 (picture presets, HDR rules) | Not started |

### On-device results

Kodi 22.0 RC1 on a separate box controlling an LG OLED77G5WUA (webOS 10.3.0, firmware 33.30.97),
driven over JSON-RPC (`JSONRPC.NotifyAll`) on 2026-10-10:

- Pairing, key storage, and reconnect after addon upgrades without re-prompting.
- `status`, `command`, `catalog`, `volume`, `mute`, `button`, `sound_output`, `toast`, `app`,
  `input`, `screen`, `power_off`, and `power_on` (Wake-on-LAN, MAC learned automatically) all
  work; bad requests are rejected before reaching the TV. Replies arrive in ~50–200 ms.
- Skin properties follow TV-side changes within about 1 s, with no polling.
- With Quick Start+ on, `power_off` puts the TV in `Active Standby` and the control connection
  stays up, so use `WebOS.On` / `WebOS.Power` (not `WebOS.Connected`) to tell if the TV is on.
- This firmware refuses plain `ws://` on port 3000; the `wss://` 3001 fallback is used.
- `getCurrentSWInformation` returns 401; firmware comes from `config/getConfigs` instead.
- Find TV lists the TV and filters out other SSDP responders (a Hue bridge).
- Not tested on hardware: following a paired TV to a new IP (the test TV has a static IP;
  covered by tests against the fake TV), and the drop/reconnect path when the TV fully
  powers off.

## Install

Build a zip and install it from Kodi (*Add-ons → Install from zip file*):

```sh
python3 scripts/build_zip.py   # -> dist/service.webostv-<version>.zip
```

Finding the TV:

- **Automatic:** with the default host (`127.0.0.1`) and nothing paired yet, if nothing answers
  locally the service searches the network (SSDP). If exactly one webOS TV answers, it connects,
  the TV shows the pairing prompt, and the TV's address is saved as the host.
- **Find TV:** *Settings → Connection → Find TV on the network* (or *Find TV* in the program
  UI) lists the TVs on the network; picking one sets the host.
- **Address changes:** if the saved address stops answering, the service searches (at most every
  2 minutes) for the TV with the same device UUID and moves to its new address, reusing the
  pairing key. It never connects to a different TV this way.

On first start the TV shows a pairing prompt; accept it. If the MAC address setting is empty, the service fills it in from
the TV's own report (wired preferred) so `power_on` via Wake-on-LAN works without typing it. The key is stored in
`special://profile/addon_data/service.webostv/client_key.json`, never in settings.

## Actions

Every action works the same from `RunScript`, keymaps, skin buttons, and JSON-RPC.

| Action | Args | Example |
| --- | --- | --- |
| `power_off` | none | `RunScript(service.webostv,power_off)` |
| `power_on` | none (needs MAC; off-TV mode) | `RunScript(service.webostv,power_on)` |
| `screen` | `on` / `off` | `RunScript(service.webostv,screen,off)` |
| `volume` | `up`, `down`, or 0–100 | `RunScript(service.webostv,volume,25)` |
| `mute` | `on`, `off`, `toggle` (default) | `RunScript(service.webostv,mute,toggle)` |
| `input` | input id or label, case-insensitive | `RunScript(service.webostv,input,HDMI_2)` |
| `app` | app id or title, optional JSON params | `RunScript(service.webostv,app,netflix)` |
| `sound_output` | e.g. `tv_speaker`, `external_arc` | `RunScript(service.webostv,sound_output,external_arc)` |
| `button` | name from aiowebostv `buttons.py` | `RunScript(service.webostv,button,HOME)` |
| `toast` | message | `RunScript(service.webostv,toast,Hello)` |
| `media` | `play`, `pause`, `stop`, `rewind`, `fast_forward` | `RunScript(service.webostv,media,pause)` |
| `channel` | `up`, `down`, or channel id | `RunScript(service.webostv,channel,up)` |
| `command` | SSAP uri, optional JSON payload | `RunScript(service.webostv,command,ssap://system/getSystemInfo)` |
| `status` | none | Shows connection, model, firmware |
| `repair` | none | Forgets the key and re-prompts on the TV |
| `ui` | none | Opens the program UI (also the default with no args) |

`RunScript` splits arguments on commas; the addon rejoins free text (toast messages, JSON
payloads), so `RunScript(service.webostv,toast,Hello, world)` works.

### JSON-RPC

External clients (Home Assistant, scripts, a phone) can drive the addon through Kodi's
notification bus:

```json
{"jsonrpc": "2.0", "id": 1, "method": "JSONRPC.NotifyAll",
 "params": {"sender": "service.webostv", "message": "request",
            "data": {"id": "abc123", "action": "volume", "args": ["25"]}}}
```

The service answers with a `reply` notification (`Other.reply` on the Kodi WebSocket) carrying
the same `id`: `{"id": "abc123", "ok": true, "result": ...}` or
`{"id": "abc123", "ok": false, "error": "bad_request|disconnected|command_error", "message": "..."}`.

## Skin properties

Published on `Window(Home)`; only changed values are written.

| Property | Example |
| --- | --- |
| `WebOS.Connected` | `true` |
| `WebOS.Paired` | `true` |
| `WebOS.Host` | `127.0.0.1` |
| `WebOS.Power` | `Active`, `Screen Off`, `Suspend` |
| `WebOS.On` / `WebOS.ScreenOn` | `true` / `false` |
| `WebOS.Volume` | `25` |
| `WebOS.Muted` | `false` |
| `WebOS.App` / `WebOS.AppTitle` | `netflix` / `Netflix` |
| `WebOS.Input` / `WebOS.InputLabel` | `HDMI_2` / `Apple TV` |
| `WebOS.SoundOutput` | `external_arc` |
| `WebOS.PicturePreset` | (phase 2) |

Example: `$INFO[Window(Home).Property(WebOS.Volume)]`.

## Development

Tests run off-Kodi and off-TV with fake `xbmc*` modules and a fake SSAP server.

```sh
uv sync --python 3.11
uv run pytest -q
```

Layout notes:

- `resources/lib/vendor/` holds unmodified aiowebostv and websockets (see `VENDORED.md`).
- `resources/lib/aiohttp_shim/` provides the slice of aiohttp that aiowebostv uses;
  `bootstrap.install()` registers it as `sys.modules["aiohttp"]`.
- `resources/lib/tv.py` (`TvController`) is the only code that touches `WebOsClient`.

## Open questions

To be answered during on-device validation (milestone 5):

- Which Python version does the official Kodi 21 webOS build ship? aiowebostv needs 3.11+;
  the service logs and disables itself cleanly on older interpreters.
- Does the SSAP server accept connections on `127.0.0.1`, or only on the LAN interface?
  The service falls back to the LAN IP automatically and remembers what worked.
- Does webOS suspend or kill Kodi's process when another input or app is in the foreground?
  The service reconnects immediately once after a drop, before entering backoff.
- Does the createAlert-to-luna technique still work on current firmware? (phase 2)
- Does `VideoPlayer.HdrType` report correctly on the webOS build? (phase 2)

## License

Apache-2.0. Vendored packages keep their own licenses (aiowebostv: Apache-2.0,
websockets: BSD-3-Clause).
