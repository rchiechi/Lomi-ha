# Lomi for Home Assistant

<img src="custom_components/lomi/brand/icon@2x.png" alt="Lomi 3" width="160" align="right">

Unofficial [HACS](https://hacs.xyz) integration for the **Lomi** countertop
composter (Classic, Bloom and Lomi 3 "Sprout"). It talks to the same cloud API
the official app uses (`api.lomi-app.net` behind AWS Cognito) for commands
and account data, and subscribes to the same **AWS IoT device shadow** the app
uses for live state (lid open, running / idle, cycle phase, time remaining).
It works with any Lomi that is registered in the Lomi app. No local access is
possible: the device only speaks to AWS IoT.

Everything here was reverse-engineered from app captures and verified against
production with a Lomi 3. Not affiliated with Pela / Lomi.

## Features

| Platform | Entity | Notes |
|---|---|---|
| select | **Program** (Grow / Eco-Express / Lomi Approved / Cleaning) | Sends `prog` so the device display follows. Value is remembered across restarts. |
| button | **Start**, **Pause**, **Resume**, **Stop** | Uses the Program selector's mode. |
| button | **Cancel scheduled start** | Deletes every pending scheduled start. |
| sensor | **Filter counter** | `lastFilterCounter`; counts up once per cycle. |
| sensor | **Last filter reset** | `lastFilterResetDate`. |
| sensor | **Next scheduled start**, **Pending commands** | From the pending-command queue. |
| sensor | **Cycle state** | Live, from the device shadow (`currState`): Standby, Grow, Eco-Express, Lomi Approved, Mixing, Drying, Cooling, Cleaning, Recovery, Filter … Stage details, pause / finished flags and the raw value are attributes. |
| sensor | **Time remaining**, **Run time**, **Start delay** | Live, from the shadow (`tRem`, `runTime`, `startDelay`). |
| sensor | **Firmware version**, **Filter state**, **Last shadow update** | Live, diagnostic. Firmware is also written to the device registry. |
| binary_sensor | **Lid**, **Running**, **Paused**, **Cycle finished** | Live, from the shadow (`coverOpen`, `currState` + `paused`, `finished`). |
| binary_sensor | **Standby**, **Power-loss recovery**, **Realtime connection** | Live, diagnostic. The connection sensor exposes the last MQTT error as an attribute. |
| sensor | **Status** | From `/devices/status`. Empty while idle; raw payload is exposed as the `raw` attribute. |
| sensor | **Last command**, **Last cloud update**, **Record version**, **Filter counter updated** | Diagnostic. |
| sensor | **Device type**, **IoT thing name**, **Manufacture date** | Diagnostic, disabled by default. |
| binary_sensor | **Scheduled start**, **Command pending** | |
| event | **Activity** | Event types below. |

### Events

The **Activity** event entity (and the `lomi_event` bus event) fire on:

| Event type | Meaning |
|---|---|
| `filter_counter_changed` | **A cycle just finished.** The counter ticks within ~12 s of the device reporting the end of a run. This is the verified "Lomi is done" trigger. |
| `record_updated` | The cloud record's `version` bumped (power cycle, cycle end, settings). |
| `filter_reset` | `lastFilterResetDate` changed. |
| `status_changed` | `/devices/status` payload changed. |
| `command_issued` | A command was sent from Home Assistant. |
| `command_acknowledged` | A pending/scheduled command left the pending queue (device acked it). |
| `cycle_started` / `cycle_finished` | Live: the shadow moved into / out of a running state. |
| `cycle_state_changed` | Live: `currState` changed (`state`, `previous_state`, `time_remaining` in the payload). |
| `cycle_paused` / `cycle_resumed` | Live: `paused` flipped. |
| `lid_opened` / `lid_closed` | Live: `coverOpen` flipped. |
| `shadow_updated` | Bus event only (not on the event entity): every shadow document, with all parsed fields. |

Every event carries `serial`, `nickname` and `user_device_id`, plus
type-specific fields (`old_counter`/`new_counter`, `old_version`/`new_version`,
`command`, `mode`, `command_id`, ...).

### Services

| Service | Fields |
|---|---|
| `lomi.send_command` | `device_id`, `command` (`start`/`pause`/`resume`/`stop`/`prog`), optional `mode`, optional `start_at` |
| `lomi.schedule_start` | `device_id`, `start_at`, optional `mode` |
| `lomi.cancel_scheduled` | `device_id`, optional `command_id` (all scheduled starts if omitted) |
| `lomi.register_device` | `serial_number`, optional `nickname`, optional `config_entry_id` |

`send_command` and `schedule_start` return the command record
(`command_id`, `status`, `issue_at`, ...) when called with a response variable.

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → **Custom repositories**.
2. Add this repository URL with category **Integration**.
3. Install **Lomi**, restart Home Assistant.
4. Settings → Devices & services → **Add integration** → **Lomi**.
5. Sign in with your Lomi app email and password.

### Manual

Copy `custom_components/lomi` into `<config>/custom_components/` and restart.

## Configuration

The config flow signs in through AWS Cognito, stores the long-lived refresh
token (and the password, used only if the refresh token is ever rejected), and
discovers every Lomi registered to the account. If the account has no device
yet, the flow offers to register one by serial number.

Options (Settings → Devices & services → Lomi → Configure):

| Option | Default | |
|---|---|---|
| Poll interval | 60 s | `GET /userDevices` cadence. The device pushes to the cloud, so 30–60 s is plenty. |
| Poll `/devices/status` | on | One extra request per device per poll. |
| Poll pending commands | on | Needed for scheduled starts and the *command acknowledged* event. |
| Realtime device shadow (MQTT) | on | Subscribe to the AWS IoT shadow for live state. Polling keeps running either way. |
| Shadow auth: token / transport / client ID | Id token / headers / `{user_id}` | How the Cognito token is presented to Lomi's IoT custom authorizer. The defaults mirror the iOS app; the other choices exist for troubleshooting (see *Realtime* below). |

## Example automations

Notify when a cycle finishes (live, from the shadow):

```yaml
automation:
  - alias: "Lomi cycle finished"
    triggers:
      - trigger: state
        entity_id: binary_sensor.sprout_running
        from: "on"
        to: "off"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "Lomi is done – empty the bucket."
```

Nag when the lid is left open while idle:

```yaml
    triggers:
      - trigger: state
        entity_id: binary_sensor.sprout_lid
        to: "on"
        for: "00:10:00"
    conditions:
      - condition: state
        entity_id: binary_sensor.sprout_running
        state: "off"
```

Polling-only fallback (works without the realtime channel):

```yaml
    triggers:
      - trigger: state
        entity_id: event.sprout_activity
        attribute: event_type
        to: filter_counter_changed
```

Or with the bus event (works without the event entity):

```yaml
    triggers:
      - trigger: event
        event_type: lomi_event
        event_data:
          type: filter_counter_changed
```

Start an Eco-Express cycle at 23:00 tonight:

```yaml
    actions:
      - action: lomi.schedule_start
        data:
          device_id: !input lomi
          mode: express
          start_at: "{{ today_at('23:00') }}"
```

## How it works

* **Auth**: Cognito user pool `us-east-1_nwqtHcuYy`, app client
  `5hss880dr2s87m2gbrd50ft7th`. `USER_PASSWORD_AUTH` for login,
  `GetTokensFromRefreshToken` (falling back to `REFRESH_TOKEN_AUTH`) hourly.
  The **IdToken** is the Bearer token for the REST API.
* **Telemetry**: `GET /userDevices` is the only channel that is known to
  carry device state. `version` and `lastFilterCounter` update within ~12 s
  of device events. `GET /devices/status` has only been observed as `{}`.
* **Control**: `POST /remoteCommands` with
  `{"command": "start", "value": <mode>, "userDeviceId": ...}`; add a future
  `issueAt` for a scheduled start, `DELETE /remoteCommands?_id=` to cancel.
  Commands are fire-and-forget; the backend relays them to the device.
* **Realtime**: the device keeps an AWS IoT *classic* device shadow
  (`$aws/things/lomi-production-<serial>/shadow`). The integration opens an
  MQTT 3.1.1 session over WebSocket to
  `wss://a3m8zk07454hpt-ats.iot.us-east-1.amazonaws.com/mqtt`, authenticating
  the way the iOS app does: AWS IoT **custom authorizer**
  `lomi_iot_authorizer_production`, token key `authorization`, token value =
  the Cognito session token, no token signature. It subscribes to
  `shadow/update/documents` (+ `update/accepted`, `get/accepted`), publishes an
  empty `shadow/get` on connect, and only accepts documents whose
  `serNo` matches the device. `state.reported` carries `currState`,
  `prevState`, `coverOpen`, `paused`, `finished`, `standby`, `recovery`,
  `tRem`, `runTime`, `startDelay`, `progLoc`, `filterState`, `filterCounter`,
  `fwVer`, `stage1..3`, `lastUpdated`. The client is a ~300-line MQTT codec on
  Home Assistant's `aiohttp` session, so there are still no extra
  requirements. It reconnects with backoff and re-mints the token each time.

### Troubleshooting the realtime channel

If the **Realtime connection** sensor stays off, its `last_error` attribute
says why (for example *Socket closed before CONNACK*, which means the IoT
authorizer refused the token or client ID). Try the other *Shadow auth*
choices in the options, one at a time; the integration reloads and reconnects
on every options change. Enable debug logging for `custom_components.lomi`
to see the exact connect parameters and every shadow document.

## Known gaps

* **Realtime channel is new and lightly tested.** The authorizer accepts the
  WebSocket upgrade unconditionally and only evaluates the token at MQTT
  CONNECT; if it refuses the connection the *Realtime connection* sensor stays
  off with the reason in its `last_error` attribute and everything falls back
  to polling. The unit of **Time remaining** / **Run time** is assumed to be
  minutes (`SHADOW_DURATION_UNIT` in `sensor.py`).
* `/devices/status` has only ever been observed as `{}`. If you catch it
  populated, open an issue with the diagnostics download
  (Settings → Devices & services → Lomi → ⋮ → Download diagnostics; it is
  redacted).
* **Filter reset** ("Set filter change date" in the app) uses an unknown
  route, so there is no reset button yet.
* `resume` is sent exactly like the other commands but has not been observed
  in an app capture.

## Development notes

The integration has no external requirements; it uses Home Assistant's shared
`aiohttp` session for REST and for the MQTT-over-WebSocket shadow client
(`mqtt.py` is a minimal MQTT 3.1.1 codec, `shadow.py` the session manager and
document parser; neither imports Home Assistant). Minimum Home Assistant
version is 2025.3.
