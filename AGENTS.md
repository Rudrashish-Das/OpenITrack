# Agent instructions

## Keep the Makefile in sync

The `Makefile` is the single entry point developers use to set up, test, run and
build OpenITrack (`make help` lists the targets). Whenever you change how any of
those steps work, update the `Makefile` in the same change, for example when you:

- add, remove or rename a dependency group in `pyproject.toml`
- change the test command, test paths or required test arguments
- change the CLI entry point or how the app is started
- change `packaging/build.py` or what it produces
- add new build output, caches or generated files that `make clean` should remove

Rules for editing it:

- Recipes must only call Python (`$(PY)` or `$(PYTHON)`), never shell builtins like
  `rm`, `del` or `cp`, so they work under both cmd.exe and sh.
- Give every user-facing target a `## description` comment so it shows in `make help`.
- Keep the Development section of `README.md` consistent with the targets.
- After editing, run `make help` and `make test` to check it still works.

## Imports go at module level

Put every import at the top of the module, in the usual three groups (standard
library, third-party, OpenITrack). Do not import inside functions or methods,
including in tests. The only exceptions, each marked with a short `# why` comment
on the import line:

- **Slow imports on a path that may not run.** Keep these lazy so startup and
  unrelated commands stay fast: OpenCV (`cv2`, and the modules that use it:
  `sources.webcam`, `preview`, `calibration_overlay`), `mediapipe`, `aiohttp`,
  `zeroconf`, `segno`, `engine` (numpy and the tracking stack), `sources.phone`,
  and `gui` (tkinter) from the CLI. Check with `python -X importtime -c "import x"`
  before adding a new one; a few milliseconds is not a reason.
- **Breaking an import cycle**, e.g. `config` -> `profiles` and
  `setup_wizard` -> `gui`.
- **Platform-only modules** (e.g. `winsound`): import them at module level under an
  `if sys.platform == "win32":` guard, not inside the function.

## Never use `make build-force`

Always build with `make build`, which runs the tests first. Do not run
`make build-force` (or call `packaging/build.py` directly) to get past failing
tests. Fix the failures instead, or stop and report them to the user.
`build-force` is only for a human developer who has decided to skip the tests.
