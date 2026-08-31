# Test fixtures

`cameras.json`, `ws_capture.json`, and `ws_capture_audio.json` are real
captures from a live UniFi Protect console (see `docs/CONTRACT.md`).

`sensors_spec.json`, `lights_spec.json`, `chimes_spec.json`, and
`nvrs_spec.json` (issue #8) are **spec-derived, not captures** — built by
hand from the official OpenAPI 3.1 spec (v6.2.83:
`components.schemas.{sensor,light,chime,nvr}`), because the reference rig
has no Protect sensors, floodlights, or chimes: `GET /sensors`, `GET
/lights`, and `GET /chimes` all return `[]` there. None of the shapes in
these four files have been observed on the wire. Treat any field not
covered by `docs/CONTRACT.md`'s "UNVERIFIED" markers with appropriate
suspicion, and please file a debug capture if real hardware disagrees with
them.

`nvrs_spec.json` is the one exception with a partly real basis: `GET
/nvrs` **does** return live data on the reference rig (a single object,
not the array the plural path name might suggest), so this fixture blends
the spec's `nvr` schema (`doorbellSettings`, etc.) with the live-observed
`armMode`/`type`/`guid`/`mac` fields that are not in the spec at all — see
docs/CONTRACT.md for exactly which parts of the NVR shape are verified.
