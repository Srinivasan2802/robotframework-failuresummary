# Robot Framework Failure Summary Listener

A lightweight, automated Robot Framework listener that isolates test failures and compiles a clean, interactive, standalone HTML summary report (`failure_summary.html`) inside your output folder.

Tired of digging through massive console outputs or deeply nested standard log files just to find exactly why a step cracked? This tool extracts execution errors, stack traces, and relevant failure screenshots into a single, deep-linking dashboard.

---

## Features

* **Isolated Failures:** Skips the background noise of passing tests and extracts only the failures.
* **Deep Linking:** Jump straight to critical failure points, trace logs, and system error strings.
* **Interactive Dashboard:** Light, searchable, and responsive UI built directly into a standalone HTML file.
* **Zero Overhead:** Runs passively during your test execution hook layers without slowing down execution.
* **Library Agnostic:** Works out of the box with both SeleniumLibrary and Browser Library.
---

https://github.com/user-attachments/assets/adb1abe2-7d5b-4449-919a-326bd23b4706


## Installation

Install the stable release directly from PyPI:






```bash
pip install robotframework-failuresummary

```






