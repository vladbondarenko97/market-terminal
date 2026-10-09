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
STYLES = STATIC / "styles.css"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class PageScan(HTMLParser):
    """Collects what the checks need: the ids that enclose each element, and every script src."""

    def __init__(self):
        super().__init__()
        self.stack = []                 # (tag, id) of the open elements
        self.parents_of_id = {}         # id -> ids of the elements that enclose it
        self.classes_of_id = {}         # id -> the element's classes
        self.script_srcs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.script_srcs.append(attrs["src"])
        if attrs.get("id"):
            self.parents_of_id[attrs["id"]] = [i for _, i in self.stack if i]
            self.classes_of_id[attrs["id"]] = (attrs.get("class") or "").split()
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


PHONE_QUERY = "(max-width: 767.98px)"   # just below Tailwind's md breakpoint (768 px), which the page's layout classes use


def phone_css():
    """The rules inside every phone media query of styles.css (comments removed, braces matched)."""
    css = re.sub(r"/\*.*?\*/", "", STYLES.read_text(encoding="utf-8"), flags=re.S)
    bodies, i = [], 0
    while True:
        i = css.find(f"@media {PHONE_QUERY}", i)
        if i < 0:
            return "\n".join(bodies)
        start, depth = css.index("{", i), 0
        for j in range(start, len(css)):
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            if depth == 0:
                bodies.append(css[start + 1:j])
                i = j
                break


def js_function(source, name):
    """Text of `function name(...) { ... }` up to the next top-level function declaration."""
    start = source.index(f"function {name}(")
    nxt = re.search(r"\n(?:async )?function \w+\(", source[start + 1:])
    return source[start:start + 1 + nxt.start()] if nxt else source[start:]


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


class TerminalPhoneLayout(unittest.TestCase):
    """The phone layout (viewport below 768 px). What it looks like is checked by hand in a browser; these keep its parts in place."""

    def test_phone_breakpoint_is_the_same_in_css_and_script(self):
        # Tailwind's md: classes switch at 768 px and up; a phone query that also matched 768 px would apply on top of them
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn(f"MOBILE_QUERY = '{PHONE_QUERY}'", app)
        self.assertTrue(phone_css())
        for f in (*STATIC.glob("*.js"), STYLES):
            self.assertNotIn("max-width: 768px", f.read_text(encoding="utf-8"), f.name)

    def test_sidebar_collapses_behind_a_bar_on_phones_only(self):
        page = scan_page()
        self.assertIn("sidebar", page.parents_of_id["sidebarBody"])
        self.assertIn("sidebar", page.parents_of_id["sidebarToggle"])
        self.assertIn("md:hidden", page.classes_of_id["sidebarToggle"])        # the bar does not exist on wide screens
        self.assertIn("md:contents", page.classes_of_id["sidebarBody"])        # wide screens lay the sections out as before
        self.assertNotIn("sidebar-open", page.classes_of_id["sidebar"])        # closed by default
        css = phone_css()
        self.assertRegex(css, r"\.sidebar-body\s*\{\s*display:\s*none")
        self.assertRegex(css, r"#sidebar\.sidebar-open \.sidebar-body\s*\{[^}]*display:\s*flex")

    def test_sidebar_state_is_remembered_with_guarded_storage(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for fn in ("setSidebarOpen", "toggleSidebar", "collapseSidebar"):
            self.assertEqual(len(re.findall(rf"function\s+{fn}\b", app)), 1, fn)
        self.assertIn("SIDEBAR_KEY", js_function(app, "setSidebarOpen"))
        self.assertRegex(js_function(app, "setSidebarOpen"), r"try\s*\{[^}]*localStorage\.setItem")
        self.assertRegex(app, r"try\s*\{\s*open\s*=\s*localStorage\.getItem\(SIDEBAR_KEY\)")

    def test_console_replaces_whichever_tab_is_active(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertRegex(phone_css(), r"body\.mobile-console-view \.tab-content\s*\{\s*display:\s*none\s*!important")
        switch = js_function(app, "switchMobileTab")
        self.assertIn("mobile-console-view", switch)
        self.assertIn("collapseSidebar()", switch)                              # the open sidebar would cover the console
        self.assertNotIn("panelContainer", switch)                              # not only the Macro grid
        self.assertIn("switchMobileTab('panels')", js_function(app, "switchTab"))   # picking a tab leaves the console
        self.assertIn("switchMobileTab('console')", js_function(app, "log"))       # a command still opens it

    def test_text_fields_are_16px_on_phones_without_disabling_zoom(self):
        self.assertRegex(phone_css(), r"input,\s*select,\s*textarea\s*\{\s*font-size:\s*16px\s*!important")
        page = TEMPLATE.read_text(encoding="utf-8")
        viewport = re.search(r'<meta name="viewport"[^>]*>', page).group(0)
        self.assertNotIn("maximum-scale", viewport)
        self.assertNotIn("user-scalable", viewport)

    def test_tab_labels_stay_on_one_line(self):
        self.assertRegex(phone_css(), r"\.tab-btn\s*\{[^}]*white-space:\s*nowrap")

    def test_small_controls_get_a_touch_sized_box(self):
        css = phone_css()
        self.assertRegex(css, r"\.draggable-panel > \.cursor-move button[^{]*\{[^}]*min-height:\s*32px[^}]*min-width:\s*32px")
        self.assertRegex(css, r"\.fc-icon\s*\{[^}]*width:\s*32px[^}]*height:\s*32px")
        self.assertRegex(css, r"\.war-slider\s*\{[^}]*padding:\s*12px 0")

    def test_war_room_tier_labels_are_smaller_in_full_screen_on_phones(self):
        self.assertRegex(phone_css(), r"\.fullscreen-mode \.war-ticks[^{]*\{[^}]*font-size:\s*10px\s*!important")

    def test_oscillator_dial_shows_a_short_label_not_the_reason(self):
        fn = js_function((STATIC / "app.js").read_text(encoding="utf-8"), "updateOscillator")
        self.assertIn("statusText.innerText = 'No reading'", fn)
        self.assertNotIn("statusText.innerText = reason", fn)
        self.assertIn("partsBox.innerText = reason", fn)                        # the full reason stays under the gauge
        self.assertIn("oscillatorHub", scan_page().parents_of_id)

    def test_inventory_legend_goes_to_the_bottom_of_a_narrow_chart_on_phones(self):
        forecast = (STATIC / "forecast.js").read_text(encoding="utf-8")
        self.assertIn("onResize: fcFitLegend", forecast)
        self.assertNotIn("position: 'right'", forecast)
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        helpers = re.search(r"const FC_LEGEND_SIDE_MIN_WIDTH[^\n]*\nconst fcLegendPosition[^\n]*", forecast).group(0)
        probe = ("const widths = [360, 639, 640, 1000, 0];"
                 "console.log(JSON.stringify({phone: widths.map(w => fcLegendPosition(w, true)), wide: widths.map(w => fcLegendPosition(w, false))}))")
        r = subprocess.run(["node", "-e", helpers + "\n" + probe], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(),            # the wide layout keeps the legend on the right at every width
                         '{"phone":["bottom","bottom","right","right","right"],"wide":["right","right","right","right","right"]}')


if __name__ == "__main__":
    unittest.main()
