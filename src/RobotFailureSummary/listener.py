import atexit
import logging
import os
from robot.api import ExecutionResult
import html as html_escaper
import sys
from urllib3.util.retry import Retry
import glob

Retry.DEFAULT = Retry(0)
ROBOT_LISTENER_API_VERSION = 3

_output_dir = None
_is_pipeline = os.environ.get('RF_SUMMARY_PIPELINE_MODE', 'false').lower() == 'true'
_close_called = False

_SCREENSHOT_PATTERNS = [
    'selenium-screenshot-*.png', 'screenshot-*.png', '*-screenshot-*.png', 'selenium-*.png',
    'browser-screenshot-*.png', 'playwright-screenshot-*.png', 'playwright-*.png',
    'robotframework-browser-screenshot-*.png', 'browser-*.png', '*.webm', 'trace-*.zip',
]

_SELENIUM_LOGGERS = ['SeleniumLibrary', 'selenium.webdriver.remote.remote_connection', 'urllib3.connectionpool']
_BROWSER_LOGGERS = ['Browser', 'Browser.utils', 'Browser.playwright', 'grpc', 'asyncio']


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


def extract_fail_details(failure):
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


def _generate_summary():
    global _close_called, _output_dir
    if _close_called:
        return
    _close_called = True

    print("##[section]Generating failure summary...")
    _resolve_output_dir_from_argv()
    output_dir = _output_dir if _output_dir else '.'
    output_xml = os.path.join(output_dir, 'output.xml')
    summary_path = os.path.join(output_dir, 'failure_summary.html')

    if not os.path.exists(output_xml):
        print("##[warning]Could not find output.xml at: " + output_xml)
        return

    try:
        result = ExecutionResult(output_xml)
    except Exception as e:
        print("##[error]Could not read output.xml. Error: " + str(e))
        return

    final_report_data = []

    def collect_failures_from_suite(suite):
        for test in suite.tests:
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

                for failure in unique_failures:
                    path = []
                    current = failure
                    while current and hasattr(current, 'parent') and current.id != test.id:
                        name_or_type = getattr(current, 'name', None) or getattr(current, 'type', None)
                        if name_or_type:
                            path.insert(0, str(name_or_type))
                        current = current.parent

                    fail_msg, exception_lines = extract_fail_details(failure)
                    final_report_data.append({
                        'test_name': test.name,
                        'test_id': test.id,
                        'failure_path': ' > '.join(path),
                        'failure_id': failure.id,
                        'fail_msg': fail_msg,
                        'exception_lines': exception_lines
                    })
        for child_suite in suite.suites:
            collect_failures_from_suite(child_suite)

    collect_failures_from_suite(result.suite)

    if not final_report_data:
        print("##[section]No failures found - all tests passed!")
        if os.path.exists(summary_path):
            try: os.remove(summary_path)
            except Exception: pass
        return

    # Modern package asset extraction via importlib
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

    cards_html = ""
    for f in final_report_data:
        path_display = " → ".join([f'<span>{html_escaper.escape(p)}</span>' for p in f['failure_path'].split(' > ') if p])
        link = f"log.html?expand={f['failure_id']}#{f['failure_id']}"
        fail_block = f'<div class="detail-block"><div class="detail-label">⛔ Fail Message:</div><div class="fail-text">{html_escaper.escape(f["fail_msg"])}</div></div>' if f.get('fail_msg') else ""
        
        exception_block = ""
        if f.get('exception_lines'):
            exception_rows = "".join(f'<div class="exception-text">{html_escaper.escape(line)}</div>' for line in f['exception_lines'])
            exception_block = f'<div class="detail-block"><div class="detail-label">🔴 Exception / Error:</div>{exception_rows}</div>'

        cards_html += f"""
        <div class="failure-card">
            <span class="test-name">{html_escaper.escape(f['test_name'])}</span>
            <div class="path">{path_display}</div>
            {fail_block}
            {exception_block}
            <a class="jump-btn" onclick="openLogTab('{link}')">Jump to Failing Keyword ↗</a>
        </div>
        """

    final_html = html_template.replace("{{JAVASCRIPT}}", javascript_code).replace("{{CARDS}}", cards_html)

    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(final_html)
        print("Failure Summary generated successfully: " + summary_path)
    except Exception as e:
        print("##[error]Failed to write failure_summary.html: " + str(e))


atexit.register(_generate_summary)

def _mute_noisy_loggers():
    for name in _SELENIUM_LOGGERS + _BROWSER_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

def start_suite(data, result):
    global _output_dir
    from robot.running.context import EXECUTION_CONTEXTS
    if EXECUTION_CONTEXTS.current:
        EXECUTION_CONTEXTS.current.output.set_log_level('TRACE')
    if hasattr(result, 'suite') and hasattr(result.suite, 'source'):
        _output_dir = os.path.dirname(result.suite.source)
    if not _output_dir:
        _output_dir = os.environ.get('ROBOT_OUTPUT_DIR', None)
    if not _is_pipeline:
        cleanup_old_files()
    _mute_noisy_loggers()

def cleanup_old_files():
    global _output_dir
    output_dir = _output_dir if _output_dir else '.'
    if not os.path.exists(output_dir): return
    try:
        for pattern in _SCREENSHOT_PATTERNS:
            for file_path in glob.glob(os.path.join(output_dir, pattern)):
                try: os.remove(file_path)
                except Exception: pass
        for sub in ('browser', 'screenshots', 'playwright-report'):
            sub_path = os.path.join(output_dir, sub)
            if os.path.isdir(sub_path):
                for pattern in _SCREENSHOT_PATTERNS:
                    for file_path in glob.glob(os.path.join(sub_path, pattern)):
                        try: os.remove(file_path)
                        except Exception: pass
        summary_path = os.path.join(output_dir, 'failure_summary.html')
        if os.path.exists(summary_path): os.remove(summary_path)
    except Exception: pass

def start_test(data, result):
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
        content = content.replace('"minLevel":"INFO"', '"minLevel":"TRACE"').replace("'minLevel':'INFO'", "'minLevel':'TRACE'")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception: pass

def close():
    _generate_summary()