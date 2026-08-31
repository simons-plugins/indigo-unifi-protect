"""Routes frames from the UniFi Protect `/subscribe/devices` WebSocket.

Pure logic only: no Indigo imports, no network, no file I/O -- same contract
as `event_tracker.py`. `DeviceUpdateRouter.route()` and the module-level
`merge_update()` must never raise, even on malformed input, so the caller
(plugin.py's read loop) can feed them directly off the wire.

Wire facts, verified live 2026-08-31 on Protect 7.2.105 (see
docs/API-REFERENCE.md, "`/subscribe/devices` WebSocket"):

- The envelope is `{"type": "add"|"update"|"remove", "item": {...}}`, with
  `item.id` and `item.modelKey` always present on a well-formed frame.
- `add` carries the FULL object for the created device.
- `remove` carries a BARE reference: just `id` and `modelKey`, nothing else.
- `update` is partial, but the partial-ness is TOP-LEVEL ONLY: the frame
  carries `id`/`modelKey` plus whichever top-level keys actually changed --
  but a nested settings object (e.g. `ledSettings`) arrives WHOLE, not
  diffed down to the one field that moved. A live `PATCH
  ledSettings.isEnabled` produced an `update` frame whose `ledSettings` held
  `isEnabled`, `welcomeLed`, AND `floodLed`, even though only `isEnabled`
  was patched. `merge_update()` below embodies exactly this: a plain
  top-level `dict.update()`, which replaces a changed nested object wholesale
  rather than deep-merging it -- deep-merging would silently keep a stale
  value under a key the controller never actually sent in this frame.

`modelKey` values the spec knows about: `nvr, camera, chime, light, viewer,
speaker, bridge, sensor, aiprocessor, aiport, linkstation`. This plugin only
acts on the five it already has Indigo device types for -- see
HANDLED_MODEL_KEYS. Everything else (an observed `bridge` update frame
carried only `id`/`modelKey`/`guid` in the same capture) is parsed
successfully but not something this router hands back to the caller; see
`ignored_model_counts`.
"""

HANDLED_MODEL_KEYS = frozenset({"camera", "sensor", "light", "chime", "nvr"})

_FRAME_TYPES = frozenset({"add", "update", "remove"})

# Key `ignored_model_counts` is bucketed under when item.modelKey was absent
# or not a string -- distinct from any real (if unhandled) modelKey string.
MISSING_MODEL_KEY = "<missing>"

# Cap on distinct keys tracked by ignored_model_counts, mirroring
# event_tracker.MAX_IGNORED_TYPE_KEYS -- a stream sending an unbounded
# variety of modelKey strings (or a misbehaving one) would otherwise grow
# this dict without bound; past the cap, every NEW key is folded into
# OTHER_MODEL_KEY instead.
MAX_IGNORED_MODEL_KEYS = 64
OTHER_MODEL_KEY = "<other>"


class DeviceUpdateRouter:
    """Classifies one `/subscribe/devices` frame at a time.

    `route()` never raises. A frame that cannot be parsed at all (the
    message/item isn't a dict, `id` is missing/empty/non-string, or `type`
    is missing/not one of add/update/remove) is counted in `malformed_count`
    and `route()` returns None. A frame that parses fine but names a
    `modelKey` this plugin doesn't act on (viewer, speaker, bridge,
    aiprocessor, aiport, linkstation, or an absent/non-string value) is
    counted separately in `ignored_model_counts`, keyed by that modelKey --
    it is NOT malformed, it just isn't a device class this plugin has an
    Indigo device type for.
    """

    def __init__(self) -> None:
        self._malformed_count = 0
        self._ignored_model_counts: dict = {}

    @property
    def malformed_count(self) -> int:
        """Count of frames that could not be parsed at all: the message or
        `item` wasn't a dict, `item.id` was missing/empty/non-string, or the
        message's own `type` was missing/not one of add/update/remove."""
        return self._malformed_count

    @property
    def ignored_model_counts(self) -> dict:
        """Count of well-formed frames whose `item.modelKey` is not in
        HANDLED_MODEL_KEYS, keyed by that modelKey (`"<missing>"` when
        absent or not a string). NOT malformed -- these frames parsed fine,
        they just aren't a class this plugin has an Indigo device type for.
        Capped at `MAX_IGNORED_MODEL_KEYS` distinct keys -- mirroring
        event_tracker.ignored_type_counts's own cap -- with anything past
        that folded into `OTHER_MODEL_KEY`. A copy, so callers can't mutate
        router-internal state through it."""
        return dict(self._ignored_model_counts)

    def route(self, message):
        """Classify one frame.

        Returns `(kind, model_key, device_id, item)` -- `kind` is
        "add"/"update"/"remove", `model_key` is one of HANDLED_MODEL_KEYS,
        `device_id` is `item["id"]` -- for a well-formed, handled frame.
        Returns None for anything malformed or unhandled (see class
        docstring for the distinction). Never raises.
        """
        try:
            return self._route(message)
        except Exception:  # pylint: disable=broad-except
            # Belt-and-braces: every branch below is already guarded with
            # isinstance checks, so this should be unreachable, but a
            # method that promises "never raises" must not depend on every
            # future edit of its own body remembering that promise.
            self._malformed_count += 1
            return None

    def _route(self, message):
        if not isinstance(message, dict):
            self._malformed_count += 1
            return None

        frame_type = message.get("type")
        if not isinstance(frame_type, str) or frame_type not in _FRAME_TYPES:
            self._malformed_count += 1
            return None

        item = message.get("item")
        if not isinstance(item, dict):
            self._malformed_count += 1
            return None

        device_id = item.get("id")
        if not isinstance(device_id, str) or not device_id:
            self._malformed_count += 1
            return None

        model_key = item.get("modelKey")
        if model_key not in HANDLED_MODEL_KEYS:
            key = model_key if isinstance(model_key, str) and model_key else MISSING_MODEL_KEY
            if (key not in self._ignored_model_counts
                    and len(self._ignored_model_counts) >= MAX_IGNORED_MODEL_KEYS):
                key = OTHER_MODEL_KEY
            self._ignored_model_counts[key] = self._ignored_model_counts.get(key, 0) + 1
            return None

        return frame_type, model_key, device_id, item


def merge_update(cached, item):
    """Apply one `update` frame's `item` onto a cached device object.

    Returns a NEW dict -- `cached` with `item`'s top-level keys overwriting.
    A nested object (e.g. `ledSettings`) is REPLACED WHOLE when `item`
    carries it, never deep-merged -- this matches the verified wire
    behaviour (see module docstring): the controller sends the whole
    sub-object on any change within it, not a diff of the one field that
    moved, so deep-merging here would risk keeping a stale nested key the
    controller has already told us is gone.

    Never mutates `cached` or `item`. Never raises: a non-dict `item`
    degrades to an unchanged copy of `cached` (or `{}` if `cached` itself
    isn't a dict either) rather than discarding the caller's cache or
    raising out of the WS read loop.
    """
    base = dict(cached) if isinstance(cached, dict) else {}
    if not isinstance(item, dict):
        return base
    base.update(item)
    return base
