import atexit
import logging
import os
import sys
import glob
import html as html_escaper
from robot.api import ExecutionResult
from robot.parsing import get_model
from urllib3.util.retry import Retry

Retry.DEFAULT = Retry(0)
ROBOT_LISTENER_API_VERSION = 3

_output_dir = None
_close_called = False

# CHANGED: keyword line map is now keyed by source file path, so multiple
# .robot files in a suite (very common under pabot --testlevelsplit) each
# get their own map instead of overwriting a single global dict.
# Structure: { source_file_path: { key: (source_file_path, lineno) } }
_keyword_line_maps = {}

# Toggle verbose file-based debug logging. Off by default so we don't litter
# every user's project with listener_debug.log on every run.
_DEBUG = os.environ.get('ROBOFAILURESUMMARY_DEBUG', '').lower() in ('1', 'true', 'yes')


def _log_debug(msg):
    if not _DEBUG:
        return
    try:
        with open("listener_debug.log", "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


_log_debug("=== LISTENER MODULE LOADED ===")

_SCREENSHOT_PATTERNS = [
    'selenium-screenshot-*.png', 'screenshot-*.png', '*-screenshot-*.png', 'selenium-*.png',
    'browser-screenshot-*.png', 'playwright-screenshot-*.png', 'playwright-*.png',
    'robotframework-browser-screenshot-*.png', 'browser-*.png', '*.webm', 'trace-*.zip',
]

_SELENIUM_LOGGERS = ['SeleniumLibrary', 'selenium.webdriver.remote.remote_connection', 'urllib3.connectionpool']
_BROWSER_LOGGERS = ['Browser', 'Browser.utils', 'Browser.playwright', 'grpc', 'asyncio']


# ---------------------------------------------------------------------------
# Pabot detection
# ---------------------------------------------------------------------------

def _is_pabot_run():
    """Detect whether this process is a pabot subprocess or a pabot-managed run.

    Pabot sets PABOTQUEUEINDEX / PABOTEXECUTIONPOOLID (or PABOTLIBURI when
    PabotLib is enabled) as environment variables in each subprocess it spawns.
    We also fall back to checking argv/sys.modules just in case.
    """
    if os.environ.get('PABOTQUEUEINDEX') is not None:
        return True
    if os.environ.get('PABOTEXECUTIONPOOLID') is not None:
        return True
    if os.environ.get('PABOTLIBURI') is not None:
        return True
    if 'PabotLib' in sys.modules:
        return True
    return any('pabot' in arg.lower() for arg in sys.argv)


def _resolve_output_dir_from_argv():
    global _output_dir
    if _output_dir:
        return _output_dir
    args = sys.argv
    for i, arg in enumerate(args):
        if arg in ('-d', '--outputdir') and i + 1 < len(args):
            resolved = os.path.abspath(args[i + 1])
            _output_dir = resolved
            return resolved
    return None


# ---------------------------------------------------------------------------
# Keyword line mapping (per source file)
# ---------------------------------------------------------------------------

def build_keyword_line_map(suite_file):
    """Parse a .robot file and build a map of ALL keywords (custom + library
    calls) to line numbers, scoped to that file only.

    CHANGED: results are stored per-file in `_keyword_line_maps` instead of
    a single shared dict, and this function is safe to call multiple times
    for different files without one overwriting another.
    """
    suite_file = os.path.abspath(suite_file)

    if suite_file in _keyword_line_maps:
        # Already parsed this file, skip re-parsing.
        return

    file_map = {}
    _log_debug(f"Parsing {suite_file} to build keyword line map...")

    try:
        model = get_model(suite_file)

        for i, section in enumerate(model.sections):
            section_type = type(section).__name__
            _log_debug(f"  Section {i}: {section_type}")

            if not hasattr(section, 'body'):
                continue

            for item in section.body:
                item_type = type(item).__name__

                if item_type == 'Keyword':
                    name = getattr(item, 'name', None)
                    lineno = getattr(item, 'lineno', None)
                    if name and lineno:
                        file_map[name] = (suite_file, lineno)
                        _log_debug(f"  Mapped custom keyword '{name}' -> line {lineno}")

                    if hasattr(item, 'body'):
                        _log_debug(f"    Parsing body of keyword '{name}'...")
                        _extract_keyword_calls(item.body, suite_file, name, file_map, depth=2)

                elif item_type == 'TestCase':
                    test_name = getattr(item, 'name', 'Unknown')
                    _log_debug(f"  Found test case: '{test_name}'")

                    if hasattr(item, 'body'):
                        _extract_keyword_calls(item.body, suite_file, test_name, file_map, depth=1)

        _keyword_line_maps[suite_file] = file_map
        _log_debug(f"Built keyword map for {suite_file} with {len(file_map)} entries")
        _log_debug(f"Map contents: {list(file_map.keys())}")
    except Exception as e:
        _log_debug(f"ERROR parsing {suite_file}: {str(e)}")
        import traceback
        _log_debug(traceback.format_exc())
        # Still register an empty map so we don't retry parsing a broken file
        # over and over for every failure that references it.
        _keyword_line_maps[suite_file] = file_map


def _extract_keyword_calls(items, source_file, context_name, file_map, depth=0):
    """Recursively extract keyword calls from test/keyword body into file_map."""
    indent = "    " * depth

    if not items:
        return

    for i, item in enumerate(items):
        item_type = type(item).__name__
        _log_debug(f"{indent}Item {i}: {item_type}")

        if item_type == 'KeywordCall':
            kw_name = None
            kw_lineno = None

            if hasattr(item, 'name'):
                kw_name = item.name
            elif hasattr(item, 'tokens'):
                for token in item.tokens:
                    if hasattr(token, 'type') and token.type == 'KEYWORD':
                        kw_name = token.value
                        break

            if hasattr(item, 'lineno'):
                kw_lineno = item.lineno

            if kw_name and kw_lineno:
                call_key = f"{context_name}::{kw_name}"
                file_map[call_key] = (source_file, kw_lineno)
                _log_debug(f"{indent}  Mapped keyword call '{kw_name}' -> line {kw_lineno}")

        elif hasattr(item, 'body'):
            _log_debug(f"{indent}  Recursing into {item_type} body...")
            _extract_keyword_calls(item.body, source_file, context_name, file_map, depth + 1)


def get_keyword_lineno(keyword_name, source_file, test_name=None, parent_keywords=None):
    """Get line number for a keyword call, handling library prefix mismatches
    and nested keywords. Looks only inside the map for `source_file`.
    """
    if not source_file:
        return None

    source_file = os.path.abspath(source_file)
    file_map = _keyword_line_maps.get(source_file)
    if not file_map:
        return None

    if not parent_keywords:
        parent_keywords = []

    contexts_to_try = parent_keywords[:]
    if test_name:
        contexts_to_try.append(test_name)

    for context in contexts_to_try:
        call_key = f"{context}::{keyword_name}"
        if call_key in file_map:
            _, lineno = file_map[call_key]
            _log_debug(f"   -> Found exact match in context '{context}' -> line {lineno}")
            return lineno

        if '.' in keyword_name:
            simple_name = keyword_name.split('.')[-1]
            call_key_simple = f"{context}::{simple_name}"
            if call_key_simple in file_map:
                _, lineno = file_map[call_key_simple]
                _log_debug(f"   -> Found with stripped prefix in context '{context}' -> line {lineno}")
                return lineno

    if keyword_name in file_map:
        _, lineno = file_map[keyword_name]
        return lineno

    if '.' in keyword_name:
        simple_name = keyword_name.split('.')[-1]
        if simple_name in file_map:
            _, lineno = file_map[simple_name]
            _log_debug(f"   -> Found custom keyword with stripped prefix: '{simple_name}' -> line {lineno}")
            return lineno

    return None


def find_deepest_failures(item):
    failures = []
    if hasattr(item, 'body') and item.body:
        failing_children = [k for k in item.body if hasattr(k, 'status') and k.status == 'FAIL']
        if not failing_children:
            if hasattr(item, 'status') and item.status == 'FAIL':
                failures.append(item)
        else:
            for child in failing_children:
                failures.extend(find_deepest_failures(child))
    elif hasattr(item, 'status') and item.status == 'FAIL':
        failures.append(item)
    return failures


_TYPE_LABELS = {
    'assertion': 'Assertion',
    'timeout': 'Timeout',
    'browser': 'Browser / Selenium',
    'other': 'Other',
}
_TYPE_ORDER = ['assertion', 'timeout', 'browser', 'other']

_TYPE_ICONS = {
    'timeout': '<svg class="badge-icon" viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M11.0615,0.608967 C12.5233,1.21447 13.7727,2.23985 14.6518,3.55544 C15.5308,4.87103 16.0000001,6.41775 16.00000001,8.00000006 C16.00000001,8.55229 15.5523,9.00000006 15.0000001,9.00000006 C14.4477,9.00000006 14.00000001,8.55229 14.00000001,8.00000006 C14.00000001,6.81332 13.6481,5.65328 12.9888,4.66658 C12.3295,3.67989 11.3925,2.91085 10.2961,2.45673 C9.19975,2.0026 7.99335,1.88378 6.82946,2.11529 C5.66558,2.3468 4.59648,2.91825 3.75736,3.75736 C2.91825,4.59648 2.3468,5.66558 2.11529,6.82946 C1.88378,7.99335 2.0026,9.19975 2.45673,10.2961 C2.91085,11.3925 3.67989,12.3295 4.66658,12.9888 C5.65328,13.6481 6.81332,14.0000001 8.00000006,14.00000001 C8.55229,14.00000001 9.00000006,14.4477 9.00000006,15.0000001 C9.00000006,15.5523 8.55229,16.0000001 8.00000006,16.0000001 C6.41775,16.0000001 4.87104,15.5308 3.55544,14.6518 C2.23985,13.7727 1.21447,12.5233 0.608967,11.0615 C0.00346625,9.59966 -0.15496,7.99113 0.153721,6.43928 C0.462403,4.88743 1.22433,3.46197 2.34315,2.34315 C3.46197,1.22433 4.88743,0.462403 6.43928,0.153721 C7.99113,-0.15496 9.59966,0.00346625 11.0615,0.608967 Z M11.7071,10.2929 L13.0000025,11.5858 L14.2929,10.2929 C14.6834,9.90237 15.3166,9.90237 15.7071,10.2929 C16.0976,10.6834 16.0976,11.3166 15.7071,11.7071 L14.4142,13.0000025 L15.7071,14.2929 C16.0976,14.6834 16.0976,15.3166 15.7071,15.7071 C15.3166,16.0976 14.6834,16.0976 14.2929,15.7071 L13.0000025,14.4142 L11.7071,15.7071 C11.3166,16.0976 10.6834,16.0976 10.2929,15.7071 C9.90237,15.3166 9.90237,14.6834 10.2929,14.2929 L11.5858,13.0000025 L10.2929,11.7071 C9.90237,11.3166 9.90237,10.6834 10.2929,10.2929 C10.6834,9.90237 11.3166,9.90237 11.7071,10.2929 Z M8,3 C8.51283143,3 8.93550653,3.38604429 8.9932722,3.88337975 L9,4 L9,8 C9,8.51283143 8.61395571,8.93550653 8.11662025,8.9932722 L8,9 L5,9 C4.44772,9 4,8.55228 4,8 C4,7.48716857 4.38604429,7.06449347 4.88337975,7.0067278 L5,7 L7,7 L7,4 C7,3.44772 7.44772,3 8,3 Z"/></svg>',
    'assertion': '',
    'browser': '',
    'other': '',
}

_TIMEOUT_HINTS = ('timeoutexception', 'timeout error', 'timed out', 'wait until')
_BROWSER_HINTS = (
    'selenium', 'webdriverexception', 'seleniumlibrary', 'browser.', 'playwright',
    'elementclickinterceptedexception', 'nosuchelementexception', 'staleelementreferenceexception',
    'elementnotinteractableexception', 'invalidselectorexception', 'appiumlibrary',
)
_ASSERTION_HINTS = ('assertionerror', 'should be', 'should contain', 'should not', 'should match')


def classify_failure(fail_msg, exception_lines, failure_path):
    haystack = ' '.join((exception_lines or []) + [fail_msg or '', failure_path or '']).lower()
    if any(h in haystack for h in _TIMEOUT_HINTS):
        return 'timeout'
    if any(h in haystack for h in _BROWSER_HINTS):
        return 'browser'
    if any(h in haystack for h in _ASSERTION_HINTS):
        return 'assertion'
    return 'other'


def get_relative_location(item, cwd, fallback_source=None, test_name=None, parent_keywords=None):
    """Get relative path and line number for an item."""
    source = getattr(item, 'source', None)
    lineno = getattr(item, 'lineno', None)
    item_name = getattr(item, 'name', None) or getattr(item, 'type', 'Unknown')

    _log_debug(f"get_relative_location: name={item_name}, lineno={lineno}, test={test_name}, parents={parent_keywords}")

    # Prefer the item's own source (accurate for multi-file suites); fall
    # back to the test's source file only if the item doesn't carry one.
    lookup_source = source or fallback_source

    if (lineno is None or lineno <= 0) and lookup_source:
        parsed_lineno = get_keyword_lineno(item_name, lookup_source, test_name, parent_keywords)
        if parsed_lineno:
            _log_debug(f"   -> Found in parsed map: line {parsed_lineno}")
            lineno = parsed_lineno
            source = lookup_source
        else:
            _log_debug(f"   -> NOT found in parsed map")

    if not source and fallback_source:
        source = fallback_source

    if not source:
        return "Unknown:?"

    if lineno is None or lineno <= 0:
        lineno_str = "?"
    else:
        lineno_str = str(lineno)

    try:
        rel_path = os.path.relpath(source, cwd)
        result = f"{rel_path}:{lineno_str}"
        _log_debug(f"   -> Returning: {result}")
        return result
    except (ValueError, OSError):
        return f"{source}:{lineno_str}"


def extract_fail_details(failure, cwd, test_source=None):
    """Extract only the failure messages and exception lines."""
    fail_msg = None
    exception_lines = []
    raw_messages = []

    if hasattr(failure, 'messages'):
        raw_messages.extend(list(failure.messages))
    if hasattr(failure, 'body'):
        for item in failure.body:
            if hasattr(item, 'message') and hasattr(item, 'level'):
                raw_messages.append(item)

    for msg in raw_messages:
        level = getattr(msg, 'level', '') or ''
        text = getattr(msg, 'message', '') or ''

        if level == 'FAIL' and not fail_msg:
            fail_msg = text.strip()

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line == 'None':
                continue
            if any(x in line for x in ('Error:', 'Exception:', 'Fault:', 'Warning:')):
                if line not in exception_lines:
                    exception_lines.append(line)

    return fail_msg, exception_lines


def _collect_all_suite_sources(suite, sources):
    """Recursively collect every unique test source file across a suite tree.

    CHANGED: previously only the first test's source file was parsed, which
    silently dropped line numbers for any failure in a different .robot file
    within the same run (very common under pabot --testlevelsplit, or any
    multi-file suite).
    """
    for test in suite.tests:
        test_source = getattr(test, 'source', None) or getattr(suite, 'source', None)
        if test_source and os.path.exists(test_source):
            sources.add(os.path.abspath(test_source))
    for child_suite in suite.suites:
        _collect_all_suite_sources(child_suite, sources)


def _generate_summary(output_xml_override=None, summary_path_override=None):
    global _close_called, _output_dir
    if _close_called:
        return
    _close_called = True

    print("##[section]Generating failure summary...")

    _resolve_output_dir_from_argv()
    output_dir = _output_dir if _output_dir else '.'
    output_xml = output_xml_override or os.path.join(output_dir, 'output.xml')
    summary_path = summary_path_override or os.path.join(output_dir, 'failure_summary.html')

    if not os.path.exists(output_xml):
        print("##[warning]Could not find output.xml at: " + output_xml)
        return

    try:
        result = ExecutionResult(output_xml)
    except Exception as e:
        print("##[error]Could not read output.xml. Error: " + str(e))
        return

    # CHANGED: build the keyword line map for every unique source file in
    # the (possibly pabot-merged) suite tree, not just the first test's file.
    all_sources = set()
    _collect_all_suite_sources(result.suite, all_sources)
    for source_file in all_sources:
        build_keyword_line_map(source_file)

    final_report_data = []
    stats = {'total': 0, 'passed': 0, 'failed': 0, 'skipped': 0}
    cwd = os.getcwd()

    def collect_failures_from_suite(suite):
        for test in suite.tests:
            stats['total'] += 1
            if test.status == 'PASS':
                stats['passed'] += 1
            elif test.status == 'SKIP':
                stats['skipped'] += 1
            elif test.status == 'FAIL':
                stats['failed'] += 1

            if test.status == 'FAIL':
                deepest_failure_objects = []
                if test.setup and test.setup.status == 'FAIL':
                    deepest_failure_objects.extend(find_deepest_failures(test.setup))
                for item in test.body:
                    if hasattr(item, 'status') and item.status == 'FAIL':
                        deepest_failure_objects.extend(find_deepest_failures(item))
                if test.teardown and test.teardown.status == 'FAIL':
                    deepest_failure_objects.extend(find_deepest_failures(test.teardown))

                unique_failures = {failure.id: failure for failure in deepest_failure_objects}.values()

                test_source = getattr(test, 'source', None)
                if not test_source and hasattr(suite, 'source'):
                    test_source = suite.source

                for failure in unique_failures:
                    fail_msg, exception_lines = extract_fail_details(failure, cwd, test_source)

                    # STEP 1: Build the full path from leaf to test (bottom-up)
                    path_nodes = []
                    current = failure
                    while current and hasattr(current, 'parent'):
                        name_or_type = getattr(current, 'name', None) or getattr(current, 'type', 'Unknown')
                        path_nodes.append({'name': name_or_type, 'item': current})
                        if getattr(current, 'id', None) == test.id:
                            break
                        current = current.parent

                    # STEP 2: For each node, its parents are the nodes AFTER it
                    path_entries = []
                    for i, node in enumerate(path_nodes):
                        name = node['name']
                        item = node['item']

                        parents = [n['name'] for n in path_nodes[i + 1:] if n['name'] != test.name]

                        loc = get_relative_location(item, cwd, test_source, test.name, parents)
                        path_entries.append({
                            'name': name,
                            'location': loc,
                        })

                    # STEP 3: Reverse for display (Test -> ... -> Leaf)
                    path_entries.reverse()

                    path_parts = []
                    for entry in path_entries:
                        if entry['location']:
                            path_parts.append(f"{entry['name']} ({entry['location']})")
                        else:
                            path_parts.append(entry['name'])
                    failure_path = ' > '.join(path_parts)

                    final_report_data.append({
                        'test_name': test.name,
                        'test_id': test.id,
                        'failure_path': failure_path,
                        'failure_id': failure.id,
                        'fail_msg': fail_msg,
                        'exception_lines': exception_lines,
                        'failure_type': classify_failure(fail_msg, exception_lines, failure_path),
                    })

        for child_suite in suite.suites:
            collect_failures_from_suite(child_suite)

    collect_failures_from_suite(result.suite)

    try:
        elapsed = result.suite.elapsedtime
        duration_s = elapsed.total_seconds() if hasattr(elapsed, 'total_seconds') else elapsed / 1000.0
        duration_display = f"{duration_s:.2f}s"
    except Exception:
        duration_display = "—"

    if not final_report_data:
        print("##[section]No failures found - all tests passed!")
        if os.path.exists(summary_path):
            try:
                os.remove(summary_path)
            except Exception:
                pass
        return

    try:
        from importlib.resources import files
        html_template = files("RobotFailureSummary").joinpath("templates", "summary.html").read_text(encoding="utf-8")
        javascript_code = files("RobotFailureSummary").joinpath("js", "expander.js").read_text(encoding="utf-8")
    except Exception:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(current_dir, 'templates', 'summary.html'), 'r', encoding='utf-8') as tmpl:
            html_template = tmpl.read()
        with open(os.path.join(current_dir, 'js', 'expander.js'), 'r', encoding='utf-8') as js_file:
            javascript_code = js_file.read()

    type_counts = {t: 0 for t in _TYPE_ORDER}
    for f in final_report_data:
        type_counts[f['failure_type']] += 1

    chips_html = f'<span class="chip active" data-type="all"><span class="dot dot-all"></span>All ({len(final_report_data)})</span>'
    for t in _TYPE_ORDER:
        if type_counts[t]:
            chips_html += f'<span class="chip" data-type="{t}"><span class="dot dot-{t}"></span>{_TYPE_LABELS[t]} ({type_counts[t]})</span>'

    cards_html = ""
    for f in final_report_data:
        path_display = " → ".join([f'<span>{html_escaper.escape(p)}</span>' for p in f['failure_path'].split(' > ') if p])
        link = f"log.html?expand={f['failure_id']}#{f['failure_id']}"
        ftype = f['failure_type']

        message_lines = list(f.get('exception_lines') or [])
        fail_msg = f.get('fail_msg')
        if fail_msg and not any(fail_msg in line for line in message_lines):
            message_lines.insert(0, fail_msg)
        message_block = ""
        if message_lines:
            rows = "".join(f'<div class="msg-line">{html_escaper.escape(line)}</div>' for line in message_lines)
            message_block = f'<div class="msg-block">{rows}</div>'

        badge_icon = _TYPE_ICONS.get(ftype, "")
        cards_html += f"""
        <div class="failure-card type-{ftype}" data-type="{ftype}">
            <div class="card-top">
                <span class="test-name">{html_escaper.escape(f['test_name'])}</span>
                <span class="type-badge">{badge_icon}{_TYPE_LABELS[ftype]}</span>
            </div>
            <div class="path">{path_display}</div>
            {message_block}
            <a class="jump-btn" onclick="openLogTab('{link}')">Jump to Failing Keyword ↗</a>
        </div>
        """

    final_html = (html_template
        .replace("{{JAVASCRIPT}}", javascript_code)
        .replace("{{CARDS}}", cards_html)
        .replace("{{CHIPS}}", chips_html)
        .replace("{{TOTAL}}", str(stats['total']))
        .replace("{{PASSED}}", str(stats['passed']))
        .replace("{{FAILED}}", str(stats['failed']))
        .replace("{{SKIPPED}}", str(stats['skipped']))
        .replace("{{DURATION}}", duration_display))

    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(final_html)
        print("Failure Summary generated successfully: " + summary_path)
    except Exception as e:
        print("##[error]Failed to write failure_summary.html: " + str(e))


# NOTE: intentionally NOT using atexit/close() to auto-generate the report.
#
# Reason: under pabot, the merge of all subprocess output.xml files into one
# final output.xml happens INSIDE PABOT'S OWN PROCESS, *after* every listener
# subprocess (and thus this module, and any atexit/close() hook in it) has
# already exited. There is no point inside a Robot Framework listener where
# "pabot finished merging" is knowable — that information only exists to
# whatever process actually launched `robot`/`pabot` and waited for it to
# return.
#
# So instead of guessing/racing on that, this module exposes `run()` below,
# which launches robot or pabot as a subprocess, blocks until it is 100%
# finished (workers destroyed, merge done, whatever it does internally),
# and only then parses the resulting output.xml and writes the report.
# This is the single supported entry point for both `robot` and `pabot` —
# see the `if __name__ == "__main__":` block at the bottom of this file.


def _mute_noisy_loggers():
    for name in _SELENIUM_LOGGERS + _BROWSER_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)


def start_suite(data, result):
    global _output_dir
    _log_debug(f"start_suite called")
    from robot.running.context import EXECUTION_CONTEXTS
    if EXECUTION_CONTEXTS.current:
        EXECUTION_CONTEXTS.current.output.set_log_level('TRACE')
    if hasattr(result, 'suite') and hasattr(result.suite, 'source'):
        _output_dir = os.path.dirname(result.suite.source)
    if not _output_dir:
        _output_dir = os.environ.get('ROBOT_OUTPUT_DIR', None)
    cleanup_old_files()
    _mute_noisy_loggers()


def start_test(data, result):
    _log_debug(f"start_test called: {getattr(data, 'name', 'N/A')}")
    from robot.running.context import EXECUTION_CONTEXTS
    if EXECUTION_CONTEXTS.current:
        EXECUTION_CONTEXTS.current.output.set_log_level('TRACE')


def output_file(path):
    global _output_dir
    _output_dir = os.path.dirname(path)


def log_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('"defaultLevel":"INFO"', '"defaultLevel":"TRACE"')
        content = content.replace("'defaultLevel':'INFO'", "'defaultLevel':'TRACE'")
        content = content.replace('"minLevel":"INFO"', '"minLevel":"TRACE"')
        content = content.replace("'minLevel':'INFO'", "'minLevel':'TRACE'")
        content = content.replace('reportInLog:true', 'reportInLog:true,defaultLevel:"TRACE"')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"##[warning] Failed to patch log level visibility: {str(e)}")


def close():
    # Intentionally does nothing report-generation-wise now (see NOTE above
    # `_is_pabot_run()` usage further up). Report generation only happens
    # via run()/_cli(), after the real run (robot or pabot, including merge)
    # has fully finished.
    pass


def cleanup_old_files():
    global _output_dir
    output_dir = _output_dir if _output_dir else '.'
    if not os.path.exists(output_dir):
        return
    try:
        for pattern in _SCREENSHOT_PATTERNS:
            for file_path in glob.glob(os.path.join(output_dir, pattern)):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
        for sub in ('browser', 'screenshots', 'playwright-report'):
            sub_path = os.path.join(output_dir, sub)
            if os.path.isdir(sub_path):
                for pattern in _SCREENSHOT_PATTERNS:
                    for file_path in glob.glob(os.path.join(sub_path, pattern)):
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass
        summary_path = os.path.join(output_dir, 'failure_summary.html')
        if os.path.exists(summary_path):
            os.remove(summary_path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Single supported entry point: run()
#
# This launches `robot` or `pabot` itself as a subprocess, BLOCKS until it is
# 100% finished (for pabot, that includes workers being torn down and the
# final output.xml merge being written to disk), and only then parses that
# output.xml and writes failure_summary.html. This is the one guarantee we
# actually need: no report gets written before the real run is truly done,
# for either runner, with no manual second step and no race condition.
#
# USAGE:
#   python -m roboFailureSummary robot --outputdir results tests
#   python -m roboFailureSummary pabot --processes 2 --outputdir results tests
#
# Everything after "robot"/"pabot" is passed straight through unchanged to
# that command, so all normal robot/pabot flags (--listener, --include,
# --variable, etc.) keep working exactly as they do today.
# ---------------------------------------------------------------------------

def _find_outputdir(args):
    for i, arg in enumerate(args):
        if arg in ('-d', '--outputdir') and i + 1 < len(args):
            return args[i + 1]
    return '.'


def run():
    import argparse
    import subprocess

    parser = argparse.ArgumentParser(
        prog="roboFailureSummary",
        description=(
            "Run `robot` or `pabot` and, once it has fully finished "
            "(including pabot's merge step), generate an HTML failure summary."
        ),
    )
    parser.add_argument("runner", choices=["robot", "pabot"],
                         help="Which command to launch: 'robot' or 'pabot'")
    parser.add_argument("runner_args", nargs=argparse.REMAINDER,
                         help="All remaining arguments are passed through to robot/pabot unchanged")
    args = parser.parse_args()

    print(f"##[section] Running {args.runner} ...")
    result = subprocess.run([args.runner] + args.runner_args)

    output_dir = _find_outputdir(args.runner_args)
    output_xml = os.path.join(output_dir, 'output.xml')
    summary_path = os.path.join(output_dir, 'failure_summary.html')

    if not os.path.exists(output_xml):
        print(f"##[warning] Could not find {output_xml} - skipping failure summary generation.")
        sys.exit(result.returncode)

    print(f"##[section] {args.runner} finished. Generating failure summary from {output_xml} ...")

    global _close_called, _output_dir
    _close_called = False  # this process never ran a listener close()/atexit; make sure the guard is open
    _output_dir = os.path.abspath(output_dir)

    _generate_summary(output_xml_override=os.path.abspath(output_xml),
                       summary_path_override=os.path.abspath(summary_path))

    sys.exit(result.returncode)


if __name__ == "__main__":
    run()