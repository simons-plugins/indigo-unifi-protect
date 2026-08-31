"""Issue #27: the bundled Cameras page must never drift from its source.

`pages/cameras.html` is the repo's source of truth; a second physical copy
lives at `UniFi Protect.indigoPlugin/Contents/Resources/pages/cameras.html`
so it ships inside the plugin bundle without a symlink (which zip/release
bundling and Indigo may not preserve). This test is the only thing keeping
those two files honest -- see CLAUDE.md for the sync step.
"""

from pathlib import Path

import plugin as plugin_module

REPO_ROOT = Path(__file__).parent.parent
SOURCE = REPO_ROOT / "pages" / "cameras.html"
BUNDLED = (
    REPO_ROOT / "UniFi Protect.indigoPlugin" / "Contents" / "Resources"
    / "pages" / "cameras.html"
)


def test_bundled_path_matches_plugin_modules_constants():
    """Guards plugin.py's hardcoded WEB_PAGE_BUNDLE_DIR/WEB_PAGE_FILENAME
    constants against a future bundle rename: if either ever changes, this
    must be the file that actually moves, or _sync_web_page would be
    installing/comparing against the wrong path with nothing to catch it."""
    constant_path = (
        REPO_ROOT / plugin_module.WEB_PAGE_BUNDLE_DIR / "Contents" / "Resources"
        / "pages" / plugin_module.WEB_PAGE_FILENAME
    )
    assert constant_path == BUNDLED


def test_bundled_cameras_page_matches_repo_source():
    assert SOURCE.is_file(), "repo source page is missing"
    assert BUNDLED.is_file(), (
        "bundled copy is missing -- run "
        f"`cp {SOURCE} {BUNDLED}` to sync it"
    )
    assert SOURCE.read_bytes() == BUNDLED.read_bytes(), (
        "pages/cameras.html and the bundled copy have drifted -- edit "
        "pages/cameras.html, then run "
        f"`cp {SOURCE} {BUNDLED}` to sync the bundled copy"
    )
