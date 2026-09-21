"""
client.py — Application Entry Point
=====================================
This file is the single entry point that cx_Freeze compiles into Whiteboard.exe.

WHY a separate entry point file?
  cx_Freeze's Executable(script=...) must point to ONE file. The actual
  application logic is split across client_part1.py and client_part2.py
  for maintainability. This thin launcher imports and calls main() without
  containing any logic itself — making it easy to swap the entry point
  if the structure changes, without touching the core files.

WHY not just point cx_Freeze directly at client_part2.py?
  client_part2.py contains the if __name__ == '__main__' guard and main().
  Either would work, but using a dedicated launcher file makes the build
  configuration explicit and keeps client_part2.py independently runnable
  during development (python client_part2.py also works).
"""

# Import and invoke the full application startup sequence defined in client_part2.
# main() shows LoginDialog, then creates and shows MainWindow, then starts
# the Qt event loop (which blocks until the user closes the window).
from client_part2 import main

if __name__ == "__main__":
    main()
