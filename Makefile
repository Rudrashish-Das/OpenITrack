# OpenITrack developer tasks. Run `make help` for the list.
#
# Recipes only call Python, so they behave the same whether make runs them
# through cmd.exe or sh, on Windows or Linux/macOS.
#
# Keep this file in sync with setup/test/run/build changes. See AGENTS.md.

VENV ?= .venv

ifeq ($(OS),Windows_NT)
  PYTHON ?= python
  VENV_BIN := $(VENV)/Scripts
  EXE := .exe
else
  PYTHON ?= python3
  VENV_BIN := $(VENV)/bin
  EXE :=
endif

PY := $(VENV_BIN)/python$(EXE)
STAMP := $(VENV)/.installed
TRACKIR := openitrack/clients/TrackIR.exe

.DEFAULT_GOAL := help
.PHONY: help setup venv test trackir gui run calibrate build build-force clean distclean

help: ## Show this help
	@$(PYTHON) -c "import re; [print(f'  make {m[0]:<12} {m[1]}') for m in re.findall(r'^([a-z-]+):.*?## (.*)$$', open('Makefile').read(), re.M)]"

$(PY):
	$(PYTHON) -m venv $(VENV)

$(STAMP): $(PY) pyproject.toml
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e .[dev,build]
	$(PY) -c "open('$(STAMP)', 'w').close()"

venv: $(PY) ## Create the virtualenv only

setup: $(STAMP) ## Create .venv and install the package with dev + build extras

test: $(STAMP) ## Run the test suite (pass extra args with ARGS="-k phone")
	$(PY) -m pytest $(ARGS)

$(TRACKIR): packaging/trackir_dummy/TrackIR.cs | $(STAMP)
	$(PY) packaging/build.py trackir

trackir: $(TRACKIR) ## Build the stand-in TrackIR.exe into openitrack/clients (gui/run/build do it too)

gui: $(STAMP) $(TRACKIR) ## Open the OpenITrack window
	$(PY) -m openitrack gui $(ARGS)

run: $(STAMP) $(TRACKIR) ## Start the tracker in the console (pass extra args with ARGS="...")
	$(PY) -m openitrack run $(ARGS)

calibrate: $(STAMP) $(TRACKIR) ## Start the tracker with the calibration overlay (ARGS="--phone" etc.)
	$(PY) -m openitrack calibrate $(ARGS)

build: test ## Run the tests, then build the exe and release zip (stops if tests fail)
	$(PY) packaging/build.py

build-force: $(STAMP) ## Build the exe and release zip without running the tests
	$(PY) packaging/build.py

clean: ## Remove build output and caches
	$(PYTHON) -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['build', 'dist', '.pytest_cache', *[p for d in ['openitrack', 'tests', 'packaging'] for p in pathlib.Path(d).rglob('__pycache__')], *map(str, pathlib.Path('.').glob('*.egg-info'))]]; [pathlib.Path(f).unlink(missing_ok=True) for f in ['packaging/version_info.txt', '$(TRACKIR)']]"

distclean: clean ## Also remove the virtualenv
	$(PYTHON) -c "import shutil; shutil.rmtree('$(VENV)', ignore_errors=True)"
