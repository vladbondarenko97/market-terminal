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
        self.attrs_of_id = {}           # id -> the element's attributes
        self.ancestor_tags_of_id = {}   # id -> tags of the elements that enclose it
        self.aside_ancestors = []       # for every <aside>: the tags that enclose it
        self.script_srcs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.script_srcs.append(attrs["src"])
        if tag == "aside":
            self.aside_ancestors.append([t for t, _ in self.stack])
        if attrs.get("id"):
            self.parents_of_id[attrs["id"]] = [i for _, i in self.stack if i]
            self.classes_of_id[attrs["id"]] = (attrs.get("class") or "").split()
            self.attrs_of_id[attrs["id"]] = attrs
            self.ancestor_tags_of_id[attrs["id"]] = [t for t, _ in self.stack]
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


def css_text():
    """The whole stylesheet with the comments removed."""
    return re.sub(r"/\*.*?\*/", "", STYLES.read_text(encoding="utf-8"), flags=re.S)


def css_rule(css, selector):
    """Declarations of the first rule whose selector list is exactly `selector` (whitespace-normalised), or None."""
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if " ".join(m.group(1).split()) == selector:
            return m.group(2)
    return None


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
                       "gexZero", "slvGexZero", "copyModalText", "btnDumpAll", "dumpBtnText", "dumpProgress", "scanTicker",
                       "scanVol", "scanDTE", "scanPremium", "menuBtn", "sideDrawer", "drawerBackdrop", "drawerBody",
                       "btnMinConsole", "btnMaxConsole", "consoleDock", "consoleDockLine", "tabPanels", "tabConsole"):
            self.assertIn(needed, ids, needed)


class TerminalDrawer(unittest.TestCase):
    """The side drawer that holds the Macro Triggers and the Custom Whale Hunter form (every screen width)."""

    DRAWER_IDS = ("btnRescan", "rescanBtnText", "btnDumpAll", "dumpBtnText", "dumpProgress", "scanTicker", "scanVol", "scanDTE", "scanPremium")

    def test_there_is_no_sidebar_in_the_page_flow(self):
        page = scan_page()
        for aside in page.aside_ancestors:
            self.assertNotIn("main", aside)                                      # no <aside> column next to the tabs
        for gone in ("sidebar", "sidebarToggle", "sidebarBody"):
            self.assertNotIn(gone, page.parents_of_id)
        for css in (STYLES.read_text(encoding="utf-8"),):
            for gone in ("sidebar-open", "sidebar-toggle", "sidebar-body", "sidebar-chevron"):
                self.assertNotIn(gone, css)
        self.assertNotIn("md:contents", TEMPLATE.read_text(encoding="utf-8"))

    def test_drawer_holds_every_trigger_and_the_whale_hunter_form(self):
        page = scan_page()
        self.assertNotIn("main", page.ancestor_tags_of_id["sideDrawer"])         # a body-level, fixed element
        self.assertNotIn("main", page.ancestor_tags_of_id["drawerBackdrop"])
        for el in self.DRAWER_IDS:
            self.assertIn("sideDrawer", page.parents_of_id[el], el)
        html = TEMPLATE.read_text(encoding="utf-8")
        for label in ("RE-SCAN ALL DATA", "SCAN SILVER", "DUMP ALL DATA", "8:31 AM", "2:00 PM", "INJECT"):
            self.assertIn(label, html[html.index('id="sideDrawer"'):], label)
        attrs = page.attrs_of_id["sideDrawer"]
        self.assertEqual(attrs.get("role"), "dialog")
        self.assertEqual(attrs.get("aria-modal"), "true")
        self.assertEqual(attrs.get("aria-labelledby"), "drawerTitle")
        self.assertIn("drawerTitle", page.parents_of_id)
        self.assertIn("sideDrawer", page.parents_of_id["drawerTitle"])
        self.assertIn("sideDrawer", page.parents_of_id["drawerClose"])           # the title row has a close button

    def test_menu_button_is_first_in_the_header_with_aria_state(self):
        page = scan_page()
        html = TEMPLATE.read_text(encoding="utf-8")
        attrs = page.attrs_of_id["menuBtn"]
        self.assertEqual(attrs.get("aria-label"), "Menu")
        self.assertEqual(attrs.get("aria-expanded"), "false")                    # closed on every page load
        self.assertEqual(attrs.get("aria-controls"), "sideDrawer")
        self.assertIn("toggleDrawer()", attrs.get("onclick", ""))
        self.assertIn("header", page.ancestor_tags_of_id["menuBtn"])
        header = html[html.index("<header"):]
        self.assertLess(header.index('id="menuBtn"'), header.index(">MARKET</span> TERMINAL"))   # before the logo
        self.assertRegex(css_rule(css_text(), ".menu-btn") or "", r"width:\s*40px[^}]*height:\s*40px")
        self.assertNotIn("open", page.classes_of_id["sideDrawer"])

    def test_drawer_is_off_canvas_and_slides_in_over_a_backdrop(self):
        css = css_text()
        drawer = css_rule(css, ".side-drawer") or ""
        self.assertRegex(drawer, r"position:\s*fixed")
        self.assertRegex(drawer, r"width:\s*min\(320px,\s*85vw\)")
        self.assertRegex(drawer, r"top:\s*0;\s*bottom:\s*0")                      # the full viewport height
        self.assertRegex(drawer, r"transform:\s*translateX\(-100%\)")
        self.assertRegex(drawer, r"visibility:\s*hidden")                        # closed: not focusable, not announced
        self.assertRegex(drawer, r"transition:\s*transform \.2s")                # about 200 ms
        self.assertRegex(css_rule(css, "body.drawer-open .side-drawer") or "", r"transform:\s*none[^}]*visibility:\s*visible")
        self.assertRegex(css_rule(css, ".drawer-backdrop") or "", r"position:\s*fixed;\s*inset:\s*0")
        self.assertRegex(css_rule(css, "body.drawer-open .drawer-backdrop") or "", r"opacity:\s*1")
        self.assertRegex(css_rule(css, ".drawer-body") or "", r"overflow-y:\s*auto")           # its own scroll
        self.assertRegex(css_rule(css, "body.drawer-open") or "", r"overflow:\s*hidden")      # the page behind does not scroll
        reduced = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{[^{}]*\.side-drawer[^{}]*\{[^}]*transition:\s*none", css)
        self.assertIsNotNone(reduced)                                            # no slide for people who ask for less motion
        # above the page, below the dialogs (copy dialog and help dialog are at 10000, the panel backdrop at 9000)
        for sel in (".drawer-backdrop", ".side-drawer"):
            z = int(re.search(r"z-index:\s*(\d+)", css_rule(css, sel)).group(1))
            self.assertTrue(100 < z < 9000, sel)

    def test_drawer_script_handles_open_close_focus_and_keys(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for fn in ("openDrawer", "closeDrawer", "toggleDrawer"):
            self.assertEqual(len(re.findall(rf"function\s+{fn}\b", app)), 1, fn)
        opening, closing = js_function(app, "openDrawer"), js_function(app, "closeDrawer")
        self.assertIn("drawer-open", opening)
        self.assertIn("'aria-expanded', 'true'", opening)
        self.assertRegex(opening, r"getElementById\('drawerClose'\)\?\.focus")             # focus moves into the drawer
        self.assertIn("'aria-expanded', 'false'", closing)
        self.assertRegex(closing, r"btn\?\.focus")                                          # and back to the menu button
        self.assertIn("getElementById('menuBtn')", closing)
        self.assertRegex(app, r"drawerBody'\)\?\.addEventListener\('click'")               # any button inside closes it ...
        action = app[app.index("getElementById('drawerBody')?.addEventListener"):][:400]
        self.assertIn("closeDrawer()", action)
        self.assertIn("isMobileLayout()", action)                                # ... and on a phone shows the console
        self.assertIn("switchMobileTab('console')", action)
        self.assertIn("e.key !== 'Tab'", app)                                    # Tab stays inside the open drawer

    def test_drawer_is_not_remembered(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("localStorage.setItem('vladhq_sidebar_open'", app)
        self.assertNotRegex(app, r"localStorage\.getItem\('vladhq_sidebar_open'")
        self.assertRegex(app, r"try \{ localStorage\.removeItem\('vladhq_sidebar_open'\); \} catch")   # the old key is cleaned up
        for fn in ("openDrawer", "closeDrawer"):
            self.assertNotIn("localStorage", js_function(app, fn), fn)
        self.assertNotIn("localStorage", re.search(r"function toggleDrawer\(\)[^\n]*", app).group(0))

    def test_drawer_controls_are_touch_sized_on_phones(self):
        css = css_text()
        self.assertRegex(css_rule(css, ".drawer-body button") or "", r"min-height:\s*40px")
        self.assertRegex(css_rule(css, ".drawer-close") or "", r"width:\s*40px[^}]*height:\s*40px")
        self.assertRegex(phone_css(), r"input,\s*select,\s*textarea\s*\{\s*font-size:\s*16px\s*!important")     # the form's fields too


class TerminalPhoneLayout(unittest.TestCase):
    """The phone layout (viewport below 768 px). What it looks like is checked by hand in a browser; these keep its parts in place."""

    def test_phone_breakpoint_is_the_same_in_css_and_script(self):
        # Tailwind's md: classes switch at 768 px and up; a phone query that also matched 768 px would apply on top of them
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn(f"MOBILE_QUERY = '{PHONE_QUERY}'", app)
        self.assertTrue(phone_css())
        for f in (*STATIC.glob("*.js"), STYLES):
            self.assertNotIn("max-width: 768px", f.read_text(encoding="utf-8"), f.name)

    def test_forecast_lab_tables_stay_inside_their_cards_on_a_phone(self):
        # A table wider than its card used to stick out past the border and make the whole tab scroll sideways.
        css = phone_css()
        self.assertRegex(css_rule(css, "#forecastGrid") or "", r"overflow-x:\s*hidden")
        self.assertRegex(css_rule(css, ".fc-table") or "", r"display:\s*block;[^}]*overflow-x:\s*auto")      # scrolls inside the card
        self.assertRegex(css_rule(css, ".rainbow-card, .rainbow-inner, .fc-body") or "", r"min-width:\s*0")
        # the rule tables become one block per row; the cells get their column names from forecast.js
        self.assertRegex(css_rule(css, ".fc-sig:not(.fc-matrix) td::before") or "", r"content:\s*attr\(data-label\)")
        forecast = (STATIC / "forecast.js").read_text(encoding="utf-8")
        self.assertEqual(forecast.count("fcLabelCells(el);"), 3)                               # Signal Watch, Day Scanner, Edge Lab
        self.assertIn('class="fc-table fc-sig fc-matrix"', forecast)                           # the matrix stays a table and scrolls

    def test_console_on_a_phone_is_a_docked_bar_that_opens_over_the_tabs(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        css = phone_css()
        # OPEN and MAXIMIZED (body.mobile-console-view): the tab bar and every tab are hidden; the tab bar is only in the Panels view
        self.assertRegex(css, r"body\.mobile-console-view \.tab-bar,\s*body\.mobile-console-view \.tab-content\s*\{\s*display:\s*none\s*!important")
        # MINIMIZED: the console itself is hidden and the docked bar (a phone-only element) is shown
        self.assertRegex(css, r"body:not\(\.mobile-console-view\) #consoleSection\s*\{\s*display:\s*none")
        self.assertRegex(css, r"\.console-dock\s*\{[^}]*display:\s*flex")
        self.assertRegex(css, r"body\.mobile-console-view \.console-dock\s*\{\s*display:\s*none")
        self.assertRegex(css_rule(css_text(), ".console-dock") or "", r"display:\s*none")      # off everywhere else
        self.assertNotIn("mobile-tab-hidden", STYLES.read_text(encoding="utf-8"))
        # the entry points keep their names: 'console' opens the console, 'panels' goes back to the docked bar
        switch = js_function(app, "switchMobileTab")
        self.assertIn("phoneConsoleOpen = true", switch)
        self.assertIn("phoneConsoleOpen = false", switch)
        self.assertIn("if (!isMobileLayout()) return", switch)                  # no effect on the wide layout
        self.assertIn("switchMobileTab('panels')", js_function(app, "switchTab"))       # picking a tab leaves the console
        self.assertIn("switchMobileTab('console')", js_function(app, "log"))            # a command still opens it
        self.assertIn("updateConsoleDock(", js_function(app, "log"))                    # the docked bar shows the newest line
        self.assertIn("switchMobileTab('console')", re.search(r'id="consoleDock"[^>]*>', TEMPLATE.read_text(encoding="utf-8")).group(0))

    def test_console_title_row_has_minimize_and_maximize_on_every_layout(self):
        page = scan_page()
        html = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("consoleSection", page.parents_of_id["btnMaxConsole"])
        self.assertIn("consoleSection", page.parents_of_id["btnMinConsole"])
        self.assertIn("toggleConsoleMaximized()", page.attrs_of_id["btnMaxConsole"]["onclick"])
        self.assertIn("toggleConsole()", page.attrs_of_id["btnMinConsole"]["onclick"])
        self.assertIn("[Maximize]", html)
        self.assertEqual(page.attrs_of_id["btnMaxConsole"].get("aria-pressed"), "false")
        self.assertNotRegex(phone_css(), r"#btnMinConsole\s*\{\s*display:\s*none")         # a phone's [Minimize] goes to the docked bar
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for fn in ("toggleConsole", "toggleConsoleMaximized", "setConsoleMaximized", "applyConsoleChrome", "clearConsole"):
            self.assertEqual(len(re.findall(rf"function\s+{fn}\b", app)), 1, fn)
        self.assertIn("[Restore]", js_function(app, "applyConsoleChrome"))
        self.assertIn("[Expand]", js_function(app, "applyConsoleChrome"))

    def test_maximized_console_covers_the_viewport_and_is_never_stored(self):
        css = css_text()
        rule = css_rule(css, "body.console-maximized #consoleSection") or ""
        self.assertRegex(rule, r"position:\s*fixed")
        self.assertRegex(rule, r"inset:\s*0")
        self.assertRegex(rule, r"z-index:\s*8\d\d\d")                     # over the header and the bottom bar, under the dialogs (9000 and up)
        self.assertNotIn("@media", css_rule(css, "body.console-maximized #consoleSection") or "@media")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for fn in ("setConsoleMaximized", "toggleConsoleMaximized", "applyConsoleChrome"):
            self.assertNotIn("localStorage", js_function(app, fn), fn)
        self.assertIn("vladhq_console_min", js_function(app, "toggleConsole"))           # the wide split is still remembered
        for call in re.findall(r"localStorage\.\w+Item\([^)]*\)", app):
            self.assertNotRegex(call.lower(), r"max", call)                      # no stored key is about the maximized state

    def test_escape_restores_a_maximized_console_after_closing_the_drawer(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        handler = re.search(r"addEventListener\('keydown', e => \{\s*if \(e\.key !== 'Escape'\) return;[^\n]*\n(?:[^\n]*\n){1,4}\}\);", app)
        self.assertIsNotNone(handler)
        text = handler.group(0)
        self.assertLess(text.index("closeDrawer()"), text.index("setConsoleMaximized(false)"))       # one Esc closes one thing

    def test_crossing_the_breakpoint_reapplies_the_layout(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertRegex(app, r"matchMedia\(MOBILE_QUERY\)")
        self.assertRegex(app, r"addEventListener\('change', applyLayout\)")
        layout = js_function(app, "applyLayout")
        self.assertIn("applyConsoleChrome()", layout)
        self.assertIn("'md:flex'", layout)                                       # the splitter is put back to the wide layout's state

    def test_dock_text_is_plain_and_short(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("textContent", js_function(app, "updateConsoleDock"))
        self.assertNotIn("innerHTML", js_function(app, "updateConsoleDock"))
        self.assertIn("white-space: nowrap", css_text())
        self.assertRegex(css_rule(css_text(), ".console-dock-line") or "", r"text-overflow:\s*ellipsis")
        for level in ("success", "error", "warn", "cmd"):
            self.assertIn(f'.console-dock-line[data-level="{level}"]', css_text())
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        fn = js_function(app, "consoleDockText")
        probe = ("console.log(JSON.stringify([consoleDockText('a  b\\n c '), consoleDockText('<whale_hunt><contract/><contract /></whale_hunt>'),"
                 "consoleDockText('<physical_arbitrage><listing/></physical_arbitrage>'), consoleDockText(null), consoleDockText('x'.repeat(500)).length]))")
        r = subprocess.run(["node", "-e", fn + "\n" + probe], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), '["a b c","Whale hunt: 2 contracts","Silver Eagles: 1 listing","",300]')

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
