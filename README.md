# UniFi Protect for Indigo

Bridges UniFi Protect camera motion, person, vehicle, and animal detection
into [Indigo](https://www.indigodomo.com) sensor devices, via the official
UniFi OS integration API.

## Requirements

- Verified against UniFi Protect **7.2.105**. Requires the integration API
  (`/proxy/protect/integration/v1`) and console API keys; older releases may
  not work.
- A UniFi OS console (UDM/UNVR/Dream Machine) reachable on your network
- An API key for that console

## Getting an API key

On your UniFi OS console: **Settings → Control Plane → Integrations →
Create API Key**.

## Setup

1. Install the plugin.
2. In the plugin config, enter the console's host/IP and the API key.
   Leave **Verify SSL certificate** unchecked unless you've installed a
   trusted certificate on the console — UniFi OS consoles ship a self-signed
   cert by default.
3. Create a device, and pick a camera from the **Camera** dropdown (populated
   live from the console).
4. Use **Discover Cameras** (plugin menu) or **Refresh Cameras** (plugin
   action) to re-poll the camera list at any time.

## Reading motion state correctly

**Indigo booleans cannot express "unknown".** When the Protect event
WebSocket is down, this plugin forces both `motionDetected` and `connected`
to False — there is no third state for "I can't tell". Any trigger that
acts on motion **must gate on `connected` first**, or a dead socket reads
as an empty, quiet house. This is the single most important thing to know
before wiring this plugin into an automation.

## Latency

Motion detection rides the Protect event WebSocket, not polling. Measured
from Protect's own event timestamp to frame receipt (`docs/ws_probe.py`, 2
events, ±1s — the capture's receive timestamps only have whole-second
resolution):

- **Motion on**: ~1–3s
- **Motion off**: ~6–7s

These figures exclude the plugin's own message parsing and the Indigo
state-write. This is fine for occupancy and security automation, but the
off-latency in particular means this plugin is **not a PIR replacement for
lighting triggers** — don't wire it directly to a "turn lights off when
motion stops" rule expecting sub-second response.
