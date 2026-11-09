# Compare profile markers with the launched browser

Date: 2026-11-09

## Decision

Before opening a profile with a readable Chromium `Last Version` marker, compare
that marker with the version of the exact executable Patchright will launch.
`browsers.json` describes an expected install and cannot establish the version
of a stale, replaced, or partially installed executable. On POSIX, read the
binary's `--version` output. On Windows, read its PE version resource without
starting the browser. If the version cannot be determined, refuse to open the
profile and explain that the version check could not be completed.

## Reason

The executable that opens the profile determines whether its stores can be
read. Comparing a package declaration instead can approve a launch whose actual
binary is older. Windows cannot safely use Chromium's `--version` switch,
because it can start a browser window instead of answering. Proceeding with an
unknown version would allow an older browser to rewrite `Last Version`,
destroying the evidence needed by later starts.

## Guard

`refuse_a_downgrade()` checks the selected executable before profile creation or
launch. `tests/test_browser_downgrade.py` covers the actual registry executable,
Windows resource routing, and refusal when the executable version is unknown.
