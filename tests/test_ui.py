"""Checks on the terminal page and its scripts that need no browser, no server and no network.

The JavaScript is syntax-checked with node (skipped when node is not installed); the HTML is parsed with the
standard library. Behaviour in a real browser is checked by hand (see the "Before you commit" list in AGENTS.md).
"""
import re
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

OPTIONS_WHALE = support.ROOT / "options_whale"
STATIC = OPTIONS_WHALE / "static"
TEMPLATE = OPTIONS_WHALE / "templates" / "terminal.html"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class PageScan(HTMLParser):
    """Collects what the checks need: the ids that enclose each element, and every script src."""

    def __init__(self):
        super().__init__()
        self.stack = []                 # (tag, id) of the open elements
        self.parents_of_id = {}         # id -> ids of the elements that enclose it
        self.script_srcs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.script_srcs.append(attrs["src"])
        if attrs.get("id"):
            self.parents_of_id[attrs["id"]] = [i for _, i in self.stack if i]
        if tag not in VOID_TAGS:
            self.stack.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):        # tolerate an unclosed tag by popping up to the match
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def scan_page():
    scan = PageScan()
    scan.feed(TEMPLATE.read_text(encoding="utf-8"))
    return scan


class TerminalScripts(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_every_script_passes_node_check(self):
        files = sorted(STATIC.glob("*.js"))
        self.assertTrue(files, "no scripts found in options_whale/static")
        for f in files:                                     # one file per call, as in AGENTS.md
            with self.subTest(script=f.name):
                r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_script_calls_the_removed_route(self):
        for f in [*STATIC.glob("*.js"), TEMPLATE]:
            self.assertNotIn("macro_direction", f.read_text(encoding="utf-8"), f.name)

    def test_silver_eagle_scan_is_a_post_and_defined_once(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"function\s+triggerPhysicalArbScan\b", app)), 1)
        calls = re.findall(r"fetch\(([^;]*silver_eagle_prices[^;]*)\)", app)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("method: 'POST'", calls[0])           # the route writes a ledger row, so it is POST only

    def test_dump_all_data_does_not_run_the_silver_eagle_scan(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        dump = app[app.index("async function dumpAllData"):]
        self.assertNotIn("silver_eagle_prices", dump)

    def test_status_check_and_clock_are_scheduled_once(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"setInterval\(checkStatus\b", app)), 1)
        self.assertEqual(len(re.findall(r"getElementById\('clock'\)", app)), 1)


class TerminalPage(unittest.TestCase):
    def test_copy_dialog_is_not_inside_the_macro_tab(self):
        # a hidden tab is display:none, and so would be every dialog inside it
        parents = scan_page().parents_of_id
        self.assertIn("copyDataModal", parents)
        self.assertNotIn("macro-tab", parents["copyDataModal"])
        for tab in ("macro-tab", "arbitrage-tab", "forecast-tab"):
            self.assertNotIn(tab, parents["copyDataModal"], tab)

    def test_modals_used_from_every_tab_are_outside_the_tabs(self):
        parents = scan_page().parents_of_id
        for modal in ("copyDataModal", "helpModal", "modalBackdrop"):
            self.assertIn(modal, parents, modal)
            self.assertFalse({"macro-tab", "arbitrage-tab", "forecast-tab"} & set(parents[modal]), modal)

    def test_cdn_scripts_are_pinned_to_a_version(self):
        cdn = [s for s in scan_page().script_srcs if s.startswith(("http://", "https://"))]
        self.assertTrue(cdn, "the page should load Tailwind and Chart.js from a CDN")
        for src in cdn:
            self.assertRegex(src, r"[@/]\d+\.\d+(\.\d+)?(?:[/?#]|$)", f"{src} has no version")

    def test_local_scripts_exist(self):
        for src in scan_page().script_srcs:
            if src.startswith("/static/"):
                self.assertTrue((OPTIONS_WHALE / src.lstrip("/")).is_file(), src)

    def test_elements_the_scripts_write_to_exist(self):
        ids = set(scan_page().parents_of_id)
        for needed in ("btnRescan", "rescanBtnText", "dpBiasMethod", "arbMissingNote", "wishlistBody", "consoleLog",
                       "gexZero", "slvGexZero", "copyModalText"):
            self.assertIn(needed, ids, needed)


if __name__ == "__main__":
    unittest.main()
