# HTML pages

`cameras.html` — a camera picker with snapshot display and live motion state.

## Install

Copy it into Indigo's **user pages** folder:

```
/Library/Application Support/Perceptive Automation/Indigo <version>/Web Assets/static/pages/
```

That is the folder the Domio iOS app scans for user-installed pages. It also
survives plugin upgrades, unlike anything inside a plugin bundle.

```bash
cp cameras.html "/Library/Application Support/Perceptive Automation/Indigo 2025.2/Web Assets/static/pages/"
```

Restart the Domio plugin (or Indigo) and the page appears as **Cameras**.

> The page deliberately does **not** ship inside this plugin's bundle. Domio only
> scans its own plugin folder and `Web Assets/static/pages` — a page inside
> *this* plugin's `Contents/Resources` can never be discovered by it, so
> shipping a copy there would only create two files that drift apart.

## Use in a browser

```
https://<indigo-host>:8176/static/pages/cameras.html?api-key=<your-key>
```

The `?api-key=` form is how the page authenticates outside the Domio app.
Indigo's web server requires auth for `Web Assets`, including the snapshot
JPEGs — the page fetches those with the bearer token and hands the `<img>` a
blob URL, because an `<img src>` tag cannot carry an Authorization header.

## What it does

- Dropdown over every Protect Camera device, discovered live by plugin id
- **Take snapshot** fires the plugin's `takeSnapshot` action for that camera
  via `plugin.executeAction`, so no per-camera action group is needed
- Auto-refresh every 30s (off by default)
- Live motion / person / vehicle / animal state, polled every 5s
- Camera config — status LED, mic (with volume), video mode, HDR, and OSD
  name/date — polled every 5s. Values are live-pushed by the plugin
  (>= 2026.8.0, issue #18); on an older plugin — or before the plugin's
  first real camera read — the values aren't readings, so the page shows
  nothing rather than a confident "off"

Two things it does on purpose:

**It shows how old the picture is**, read from the `Last-Modified` header, and
turns amber past two minutes. A security-camera page showing a silently stale
still is a misrepresentation.

**It warns when the event socket is down.** Motion is then *unknown*, not
absent — Indigo booleans cannot express that, so the page says it in words and
tells you not to trust the motion indicators until it clears.
