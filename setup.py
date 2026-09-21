"""
setup.py — cx_Freeze Build Configuration
==========================================
Run with: python setup.py build
Output:   build/exe.win-amd64-3.x/ folder

PURPOSE (Viva Talking Point):
------------------------------
This script bundles the entire Python application — including the interpreter,
all third-party packages, and PyQt6's Qt DLLs — into a self-contained folder.
The result is a standalone Whiteboard.exe that any Windows PC can run WITHOUT
having Python, pip, or any virtual environment installed.

WHY cx_Freeze instead of PyInstaller?
  - cx_Freeze handles PyQt6 more reliably on Python 3.11+
  - It produces a folder (not a single file), so DLLs are not extracted to
    a temp directory on every launch — faster startup, no antivirus conflicts.
  - The output is transparent: you can inspect every bundled file.

WHY explicitly list packages_to_include?
  cx_Freeze uses static import analysis to find dependencies. It works well
  for pure-Python imports but misses packages that use:
    - Dynamic imports (importlib, __import__)
    - C-extension modules loaded at runtime (PyQt6 plugins, aiortc codecs)
    - Implicit sub-packages (urllib.parse, html.parser)
  Explicitly listing these forces cx_Freeze to include them unconditionally.

WHY Win32GUI base?
  On Windows, a Python script normally opens a black console window behind
  the GUI. Win32GUI suppresses this — the app appears as a pure GUI app
  with no terminal visible to the end user.
"""

import sys
from cx_Freeze import setup, Executable

# Packages that cx_Freeze needs to explicitly include.
# These were identified by running the build and checking error logs
# for 'ModuleNotFoundError' at runtime.
packages_to_include = [
    "PyQt6",                    # Qt6 Python bindings (GUI framework)
    "PyQt6.QtWidgets",          # Window, button, dialog widgets
    "PyQt6.QtCore",             # Qt core: signals, threads, timers
    "PyQt6.QtGui",              # Painting: QPainter, QPixmap, QColor
    "socketio",                 # python-socketio client
    "engineio",                 # Engine.IO transport layer (used by socketio)
    "aiortc",                   # WebRTC implementation (P2P audio)
    "av",                       # PyAV: FFmpeg bindings (Opus codec for WebRTC)
    "sounddevice",              # PortAudio wrapper (mic input + speaker output)
    "numpy",                    # Audio array manipulation (float32→int16 conversion)
    "aiohttp",                  # Async HTTP (required by aiortc internally)
    "asyncio",                  # Standard async I/O (WebRTC event loop)
    "threading",                # Standard threading (_loop_ready Event, locks)
    "ssl",                      # TLS (required for HTTPS/WSS connections)
    "logging",                  # Standard logging (used by socketio internally)
    "websocket",                # websocket-client (transport for sync socketio)
    "urllib",                   # URL utilities (used by socketio/aiohttp)
    "urllib.parse",
    "urllib.request",
    "urllib.error",
    "html",                     # HTML processing (used by http.client)
    "html.parser",
    "http",                     # HTTP protocol implementation
    "http.client",
    "http.cookies",
    "xml",                      # XML parsing (used by some aiohttp internals)
    "xml.etree",
    "xml.etree.ElementTree",
    "email",                    # Email MIME types (used by http.client)
]

# Additional non-Python files to copy into the build directory.
# Add entries here if your app needs assets like images or config files.
# Example: ("assets/logo.png", "assets/logo.png")
include_files = [
    # Add any extra files your app needs here
    # Example: ("assets/", "assets/")
]

build_options = {
    "packages": packages_to_include,
    "include_files": include_files,
    "excludes": [
        "tkinter",        # Not needed — saves space (Qt replaces it)
        "unittest",       # Testing framework — not needed in distribution
        "pydoc",          # Documentation generator — not needed at runtime
    ],
    "optimize": 1,        # Compile .pyc with basic optimization (removes asserts)
}

# Define the executable(s) to build.
# Only the CLIENT is bundled — the server runs as a plain Python script
# on the host machine and does not need to be distributed.
executables = [
    Executable(
        script="client.py",              # Entry point (imports client_part2.main)
        base="Win32GUI" if sys.platform == "win32" else None,
        # Win32GUI hides the black console window behind the PyQt6 GUI.
        # Change to None if you want to see console/debug output.
        target_name="Whiteboard.exe",    # Output exe name seen by end users
        icon=None,                       # Add path to .ico file if you have one
    )
]

setup(
    name="Collaborative Whiteboard",
    version="1.0",
    description="Collaborative Whiteboard with Voice Chat",
    options={"build_exe": build_options},
    executables=executables,
)
