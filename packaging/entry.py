"""PyInstaller entry point for both executables.

OpenITrack.exe      the window (built without a console)
OpenITrack-cli.exe  the command line (built with a console), same commands as `openitrack`
"""

import multiprocessing
import sys
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if Path(sys.executable).stem.lower().endswith("-cli"):
        from openitrack.cli import frozen_main  # each exe loads only its own half

        sys.exit(frozen_main())
    from openitrack.gui import main

    sys.exit(main(sys.argv[1:]))
