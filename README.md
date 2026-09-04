# Robot Framework Failure Summary

A Robot Framework listener that mutes noisy engine logs (Selenium/Browser internals) and generates a clean, interactive **failure summary report** — with deep links to the failing keyword in `log.html` and IDE-ready `file:line` locations you can paste into VS Code (`Ctrl+P`) or PyCharm (`Ctrl+Shift+N`).

Works with **SeleniumLibrary** and **Browser** library, and with both `robot` and `pabot`.

## Install

```bash
pip install robotframework-failuresummary
```

For parallel runs: `pip install robotframework-pabot`

## Usage

### Plain robot (single process)

these tests can be anything like relative  path to ur .robot file or u can change the cd and run it  

```bash
rf-failure-summary tests 
```
or, if you don't want the wrapper and just want plain `robot` directly (this also works fine, no pabot involved):

```bash
robot --listener RobotFailureSummary.listener --outputdir results tests
```

### Pabot (parallel execution)

```bash
rf-failure-summary tests --pabot --processes 4
```

⚠️ For pabot, you **must** use `rf-failure-summary` (not plain `pabot --listener ...`) — see "Why not just `--listener`?" below for why.

### Other options (work with both)

```bash
rf-failure-summary tests --outputdir results             # custom output folder
rf-failure-summary tests --include smoke                  # pass tags/flags through to robot/pabot
rf-failure-summary tests --pabot --processes 4 --include smoke --exclude wip
```

Open `results/failure_summary.html` after the run finishes.

**Why not just `--listener`?** Under `pabot`, results from all worker processes only get merged *after* they've exited — a listener can't hook into that. `rf-failure-summary` runs `robot`/`pabot` itself, waits for the full run (merge included), then generates one complete report. Plain `robot --listener RobotFailureSummary.listener` still works fine on its own since a single robot process can generate the complete report by itself.

## What you get

Each failure card shows the classification (Assertion/Timeout/Browser/Other), the keyword path with `file:line` for each step, the failure message, and a **Jump to Failing Keyword** button into `log.html`.

## Config

### Debug logging

If a failure card shows `file.robot:?` instead of a real line number, enabling debug logging **won't fix it** — it just records what the tool tried while looking up that keyword, so you can attach `listener_debug.log` when reporting it as a bug (or dig into it yourself if you're comfortable).

**Windows (PowerShell):**
```powershell
$env:ROBOFAILURESUMMARY_DEBUG="1"
rf-failure-summary tests
```

**Windows (cmd):**
```cmd
set ROBOFAILURESUMMARY_DEBUG=1
rf-failure-summary tests
```

**macOS / Linux:**
```bash
export ROBOFAILURESUMMARY_DEBUG=1
rf-failure-summary tests
```

This is off by default — leave it unset for normal use, since it writes a log file on every run.

## Changelog

**2.0.0** — Added `rf-failure-summary` base 1.0.0 features + CLI with pabot support, included line-number resolution across multi-file suites, responsive UI with failure seperation based on label.

**1.0.0** — Initial release.
 deep linking failure nodes navigation in a single click which keeps that particular failure node  open and rest all closed for easier and optimized `log.html` navigation for best usage u need to keep both the base html report tab and failure_summary.html tab in single window as clicking to failure nodes first time opens new tab second time it reuses the already opened tab thats why single window spliting tabs will give you the best output.   
## License

MIT