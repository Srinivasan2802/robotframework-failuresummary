"""
RobotFailureSummary.cli

Provides the `rf-failure-summary` console command (installed automatically
via pip), plus the underlying `main()` function that run_tests.py (for
running straight from a git checkout, before installing) also calls.

USAGE (once pip-installed):
    rf-failure-summary tests
    rf-failure-summary tests --pabot --processes 2
    rf-failure-summary tests --outputdir results
    rf-failure-summary tests --pabot --processes 4 -- --include smoke

This works identically on Windows, macOS, and Linux since it's plain Python.
"""

import argparse
import os
import subprocess
import sys

LISTENER_DOTTED_PATH = "RobotFailureSummary.listener"


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="rf-failure-summary",
        description="Run Robot Framework tests (robot or pabot) and auto-generate a failure summary.",
    )
    parser.add_argument("test_path", help="Path to the test file or directory (e.g. 'tests')")
    parser.add_argument("--pabot", action="store_true",
                         help="Run with pabot instead of plain robot (needed for parallel execution)")
    parser.add_argument("--processes", type=int, default=None,
                         help="Number of parallel pabot processes (only used with --pabot)")
    parser.add_argument("--outputdir", "-d", default="results",
                         help="Directory for output.xml, log.html, report.html, failure_summary.html (default: results)")
    parser.add_argument("--pythonpath", default=None,
                         help="Extra --pythonpath to pass to robot/pabot (only needed when running from "
                              "source before installing the package, e.g. 'src')")
    return parser


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    # parse_known_args recognizes our own flags (--pabot, --processes, etc.)
    # no matter where they appear in the command line, and returns anything
    # it doesn't recognize (tags, --include, --variable, ...) as extra_args,
    # in their original order. This means no "--" separator is required,
    # and our own flags can safely appear before OR after the test path.
    args, extra_args = parser.parse_known_args(argv)

    runner = "pabot" if args.pabot else "robot"

    cmd = [runner]
    if args.pabot and args.processes:
        cmd += ["--processes", str(args.processes)]
    if args.pythonpath:
        cmd += ["--pythonpath", args.pythonpath]
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

    from RobotFailureSummary.listener import _generate_summary
    import RobotFailureSummary.listener as listener_module
    listener_module._close_called = False  # allow this explicit call regardless of prior state

    _generate_summary(output_xml_override=os.path.abspath(output_xml),
                       summary_path_override=os.path.abspath(summary_path))

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()