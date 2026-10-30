"""PyInstaller entry script for JobSearchManager.exe."""
import sys

from jsh.main import main

if __name__ in {"__main__", "__mp_main__"}:
    main(sys.argv[1:])
