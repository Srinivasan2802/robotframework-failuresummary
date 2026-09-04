"""
run_tests.py

Cross-platform, one-command runner for Robot Framework suites that also
generates failure_summary.html afterward -- works the same way on Windows,
macOS, and Linux since it's plain Python (no shell-specific syntax).

USAGE
-----
Run with plain `robot`:
    python run_tests.py tests

Run with `pabot` (parallel):
    python run_tests.py tests --pabot --processes 2

Custom output dir:
    python run_tests.py tests --outputdir results

Any extra robot/pabot flags can go after `--`:
    python run_tests.py tests --pabot --processes 4 -- --include smoke

WHAT IT DOES
------------
1. Figures out the repo root (the folder this script lives in) so it works
   no matter which directory you call it from.
2. Adds "src" to PYTHONPATH automatically -- no manual `set PYTHONPATH=src`
   or `$env:PYTHONPATH="src"` needed on any OS.
3. Runs `robot` or `pabot` as a subprocess with your test path/args, always
   passing `--listener RobotFailureSummary.listener` so TRACE-level logging
   still gets forced during execution.
4. Waits for that subprocess to fully finish (for pabot, this includes its
   internal merge of all worker output.xml files into one).
5. Parses the resulting output.xml and writes failure_summary.html --
   exactly once, with the complete picture, regardless of runner.

This script requires no installation beyond Python 3 and either
`robotframework` or `robotframework-pabot` already being installed in your
environment (same as today).
"""

import argparse
import os
import subprocess
import sys

# Resolve paths relative to this script's location, not the caller's cwd,
# so `python run_tests.py ...` works the same from any directory.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(REPO_ROOT, "src")
LISTENER_DOTTED_PATH = "RobotFailureSummary.listener"


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="run_tests.py",
        description="Run Robot Framework tests (robot or pabot) and auto-generate a failure summary.",
    )
    parser.add_argument("test_path", help="Path to the test file or directory (e.g. 'tests')")
    parser.add_argument("--pabot", action="store_true",
                         help="Run with pabot instead of plain robot (needed for parallel execution)")
    parser.add_argument("--processes", type=int, default=None,
                         help="Number of parallel pabot processes (only used with --pabot)")
    parser.add_argument("--outputdir", "-d", default="results",
                         help="Directory for output.xml, log.html, report.html, failure_summary.html (default: results)")
    parser.add_argument("extra_args", nargs=argparse.REMAINDER,
                         help="Any further robot/pabot flags, e.g. -- --include smoke")
    return parser


def _clean_extra_args(extra_args):
    # argparse.REMAINDER keeps a literal leading "--" if the user typed one
    # as a separator; strip it so it isn't passed through as a stray arg.
    if extra_args and extra_args[0] == "--":
        return extra_args[1:]
    return extra_args


def main():
    parser = _build_parser()
    args = parser.parse_args()
    extra_args = _clean_extra_args(args.extra_args)

    runner = "pabot" if args.pabot else "robot"

    # Build the actual command to run.
    cmd = [runner]
    if args.pabot and args.processes:
        cmd += ["--processes", str(args.processes)]
    cmd += ["--pythonpath", SRC_DIR]
    cmd += ["--listener", LISTENER_DOTTED_PATH]
    cmd += ["--outputdir", args.outputdir]
    cmd += extra_args
    cmd += [args.test_path]

    print(f"##[section] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)

    output_xml = os.path.join(args.outputdir, "output.xml")
    summary_path = os.path.join(args.outputdir, "failure_summary.html")

    if not os.path.exists(output_xml):
        print(f"##[warning] Could not find {output_xml} - skipping failure summary generation.")
        sys.exit(result.returncode)

    print(f"##[section] {runner} finished. Generating failure summary from {output_xml} ...")

    # Make src importable so we can call the generator directly, in-process,
    # rather than shelling out again.
    sys.path.insert(0, SRC_DIR)
    from RobotFailureSummary.listener import _generate_summary
    import RobotFailureSummary.listener as listener_module
    listener_module._close_called = False  # allow this explicit call regardless of prior state

    _generate_summary(output_xml_override=os.path.abspath(output_xml),
                       summary_path_override=os.path.abspath(summary_path))

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()