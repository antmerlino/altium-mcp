from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.utilities.types import Image as MCPImage
from mcp.types import ToolAnnotations
import json
import os
import time
import asyncio
import logging
import logging.handlers
import subprocess
import math
import tempfile
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import sys
import win32gui
import win32ui
import win32con
import win32api
import win32process
from PIL import Image
import glob
import io
import re
import silkscreen
import label_blocks
import script_bundle
import pcblib_file
import uuid

from altium_files import long_path, read_altium_text, spec_with_long_library, write_altium_text
import base64
import ctypes
from typing import Union

# Windows virtualizes coordinates for DPI-unaware processes, so GetWindowRect reports a window SMALLER than it
# really is on any scaled display and every capture comes back truncated - which silently cut the error text
# off Altium's dialog screenshots. Declare awareness before any window is measured.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from exchange_lock import ExchangeLock
from altium_install import find_altium_exe
import routing
import pcb_layout
import screenshot_target
import sandbox
import win_processes
from outjob_parser import parse_outjob

# Set MCP_DIR to the directory of the current Python file
MCP_DIR = Path(__file__).parent
# config.json lives in a per-user folder OUTSIDE the install directory and
# outside AppData. Claude Desktop replaces the extension folder on every
# update, which used to reset a hand-picked Altium path; and being an MSIX
# package it redirects its child processes' AppData writes into its own
# LocalCache. start_server.py passes the folder it chose in ALTIUM_MCP_HOME.
# LEGACY_CONFIG_FILE is read once to migrate.
def _data_dir() -> Path:
    override = os.environ.get("ALTIUM_MCP_HOME")
    return Path(override) if override else Path.home() / ".altium-mcp"


class _RotatingLog(logging.handlers.RotatingFileHandler):
    """Rotates like its base class, but keeps logging when it cannot.

    Windows will not rename a file another process has open, and two servers
    (two Claude sessions) often share this log. The base class then fails
    on every record; this one keeps appending and leaves the rotation to the
    next server that starts alone.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            self.maxBytes = 0
            if self.stream is None:
                self.stream = self._open()


# The log sits beside config.json, for the same reasons, and rotates at
# 1 MB with 3 backups. INFO by default: DEBUG adds a line for every MCP
# protocol message. Set ALTIUM_MCP_LOG_LEVEL=DEBUG to get them back.
LOG_FILE = _data_dir() / "altium_mcp.log"
_log_level = logging.getLevelName(os.environ.get("ALTIUM_MCP_LOG_LEVEL", "INFO").upper())
_log_handlers = [logging.StreamHandler()]  # stderr: stdout carries the MCP protocol
try:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(_RotatingLog(str(LOG_FILE), maxBytes=1_000_000, backupCount=3,
                                      encoding="utf-8"))
except OSError:
    pass  # no log file is no reason not to start
logging.basicConfig(
    level=_log_level if isinstance(_log_level, int) else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=_log_handlers
)
logger = logging.getLogger("AltiumMCPServer")

LEGACY_CONFIG_FILE = MCP_DIR / "config.json"
CONFIG_FILE = _data_dir() / "config.json"
DEFAULT_SCRIPT_PATH = MCP_DIR / "AltiumScript" / "Altium_API.PrjScr"

# Use a fixed exchange directory for request/response JSON files.
# Both the Python MCP server and the Altium DelphiScript need to independently
# resolve to the same directory. C:\Users\Public is writable by all users and
# exists on every Windows machine. This avoids fragile script-project-path
# resolution that breaks when Altium caches stale script projects.
EXCHANGE_DIR = Path("C:/Users/Public/altium_mcp")
EXCHANGE_DIR.mkdir(exist_ok=True)
REQUEST_FILE = EXCHANGE_DIR / "request.json"
RESPONSE_FILE = EXCHANGE_DIR / "response.json"

# Written by the bridge script when it starts a command and, at most once a
# second, while a long command (a batch) is still working
PROGRESS_FILE = EXCHANGE_DIR / "progress.txt"

# Copies of command results (design data) that older bridge scripts left in
# this folder, which every account on the machine can read. Nothing reads
# them. Named one by one: users keep their own script output here too.
LEFTOVER_TEMP_FILES = (
    "temp_board_summary.json", "temp_component_data.json",
    "temp_component_primitives.json", "temp_erc.json",
    "temp_footprint_primitives.json", "temp_json_output.json",
    "temp_layers_data.json", "temp_net_connections.json", "temp_nets_data.json",
    "temp_pins_data.json", "temp_project_components.json",
    "temp_project_info.json", "temp_rules_data.json",
    "temp_schematic_data.json", "temp_search_symbol.json",
    "temp_selected_components.json", "temp_stackup_data.json",
    "temp_symbol_primitives.json", "temp_symbol_reference.json",
)


def _remove_leftover_temp_files(folder: Path = EXCHANGE_DIR):
    for name in LEFTOVER_TEMP_FILES:
        try:
            (folder / name).unlink()
        except OSError:
            pass  # absent, or held open: best effort


_remove_leftover_temp_files()

# Seconds for a running Altium to pick up a request (normally ~1-5 s), for a
# cold Altium launch to get that far, for a started command to go without
# reporting progress, and for any command to finish at all
START_TIMEOUT = 20
START_TIMEOUT_COLD = 240
RESPONSE_TIMEOUT = 120
RESPONSE_TIMEOUT_MAX = 30 * 60

# How long to wait while another server's command holds the exchange folder
LOCK_TIMEOUT = START_TIMEOUT_COLD + RESPONSE_TIMEOUT

# Commands that change the design or write files. When one of them times out
# it may have done some or all of its work, so it must not just be retried.
MUTATING_COMMANDS = {
    "create_net_class", "create_schematic_symbol", "set_component_position",
    "move_components", "place_components", "create_symbols_batch", "build_circuit",
    "create_footprints_batch", "layout_duplicator", "layout_duplicator_apply",
    "run_output_jobs", "create_pcb_footprint",
}

# Present while a script the MCP launched is stuck (paused in Altium's
# debugger). Only then is the debugger stopped, so a script the user is
# running or debugging is never stopped. Every server using this exchange
# folder sees it, including after a restart.
STUCK_MARKER = EXCHANGE_DIR / "mcp_script_stuck.txt"


def _mark_script_stuck(what: str):
    try:
        STUCK_MARKER.write_text(what, encoding="utf-8")
    except OSError:
        pass


def _clear_script_stuck():
    try:
        STUCK_MARKER.unlink()
    except OSError:
        pass


ALTIUM_HUNG_ERROR = (
    "Not run: Altium is not responding. A script or a long command is still running in it, or "
    "a script is stuck in a loop. Nothing can be sent to Altium until it finishes. Scripts "
    "started by run_altium_script stop their while/repeat loops at their deadline; if Altium "
    "stays frozen, only ending X2.EXE frees it, and unsaved work is then lost.")


def _lift_failed_result(response: Dict[str, Any]) -> Dict[str, Any]:
    """Make a command's own failure the response's failure.

    Several bridge commands report failure as their result, e.g.
    {"success": false, "error": "No layer names provided"}, and the bridge
    wraps any result in "success": true. Tools then took the failure for
    success: get_screenshot even captured after its focus step had failed.
    Fields besides the message (per-container results of run_output_jobs,
    for instance) are kept in the error text.
    """
    result = response.get("result")
    if response.get("success") and isinstance(result, dict) and result.get("success") is False:
        error = result.get("error") or result.get("message") or "the command reported failure"
        details = {k: v for k, v in result.items() if k not in ("success", "error", "message")}
        if details:
            error = f"{error} {json.dumps(details)[:2000]}"
        return {**response, "success": False, "error": error}
    return response


# Initialize FastMCP server
mcp = FastMCP("AltiumMCP", instructions="Altium integration through the Model Context Protocol")

READ_ONLY = ToolAnnotations(readOnlyHint=True)


def _tool(read_only: bool = False):
    """Register a tool.

    read_only marks a tool that only reads from Altium, so clients can tell it
    from the tools that change a design. Results stay plain text content: by
    default mcp would infer an output schema from "-> str" and send every
    result twice, once more as structuredContent.
    """
    return mcp.tool(annotations=READ_ONLY if read_only else None, structured_output=False)

class AltiumConfig:
    def __init__(self):
        self.altium_exe_path = ""
        self.script_path = str(DEFAULT_SCRIPT_PATH)
        self.load_config()
    
    def load_config(self):
        """Load configuration, migrating a pre-relocation config.json once."""
        source = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_CONFIG_FILE
        if source.exists():
            try:
                with open(source, "r") as f:
                    config = json.load(f)
                self.altium_exe_path = config.get("altium_exe_path", "")
                # Only a hand-picked script project is persisted. A saved path
                # that no longer exists (old install folder) falls back to the
                # project shipped beside this file instead of prompting.
                saved_script = config.get("script_path", "")
                if saved_script and os.path.exists(saved_script):
                    self.script_path = saved_script
                logger.info(f"Loaded configuration from {source}")
                if source is LEGACY_CONFIG_FILE:
                    self.save_config()
                else:
                    self._saved = self._as_dict()
            except Exception as e:
                logger.error(f"Error loading configuration: {e}")
                self._create_default_config()
        else:
            logger.info("No configuration file found, creating default")
            self._create_default_config()

    def _create_default_config(self):
        """Create a default configuration file, with Altium's path if it can be found"""
        self.altium_exe_path = find_altium_exe()
        self.save_config()
    
    def _as_dict(self):
        config = {"altium_exe_path": self.altium_exe_path}
        # The default script project belongs to whichever install is running
        # (extension folder or a dev checkout), so it is never written down:
        # both share this file and must not steal each other's scripts.
        if Path(self.script_path) != DEFAULT_SCRIPT_PATH:
            config["script_path"] = self.script_path
        return config

    def save_config(self):
        """Save configuration to file, only when something changed"""
        config = self._as_dict()
        if config == getattr(self, "_saved", None) and CONFIG_FILE.exists():
            return

        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            self._saved = config
            logger.info(f"Saved configuration to {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")

    def discover_altium(self) -> bool:
        """Keep the saved Altium path if it still exists, else look for one.

        Never asks the user, so it is safe at import time. Called again
        before commands, so an Altium started after the server, or one
        updated to a new folder, is picked up without a restart.
        """
        if self.altium_exe_path and os.path.exists(self.altium_exe_path):
            return True
        found = find_altium_exe()
        if not found:
            return False
        logger.info(f"Automatically found Altium at: {found}")
        self.altium_exe_path = found
        self.save_config()
        return True

    def verify_paths(self):
        """Verify that the paths in the configuration exist, prompt for input if they don't

        Shows file dialogs, so it must not run on the event loop, and never
        before the server has answered the client's initialize request: a
        dialog nobody sees would hold the startup until the client gives up.
        Messages go to the log only; under the stdio transport stdout is the
        protocol channel.
        """

        # Initialize variables
        root = None
        paths_verified = True

        # Check Altium executable
        if not self.discover_altium():
            root = self._dialog_root()
            logger.info("Altium executable not found in the registry, among running "
                        "processes or under C:\\Program Files\\Altium. Prompting user for selection...")

            self.altium_exe_path = filedialog.askopenfilename(
                parent=root,
                title="Select Altium Executable (X2.EXE)",
                filetypes=[("Executable files", "*.exe")],  # Only allow .exe files
                initialdir="C:/Program Files/Altium"
            )

            if not self.altium_exe_path:
                logger.error("No Altium executable selected. Some functionality may not work.")
                paths_verified = False

        # Check script path
        if not os.path.exists(self.script_path):
            paths_verified = False

            if root is None:
                root = self._dialog_root()

            logger.info(f"Script file not found at {self.script_path}. Prompting user for selection...")

            selected_path = filedialog.askopenfilename(
                parent=root,
                title="Select Altium Project File",
                filetypes=[("Altium Project files", "*.PrjScr")],  # Changed to PrjScr for script project
                initialdir=str(MCP_DIR)
            )

            if selected_path:
                self.script_path = selected_path
            else:
                logger.error("No script file selected. Some functionality may not work.")
                paths_verified = False
        
        # Clean up tkinter root if created
        if root is not None:
            root.destroy()
        
        # Save the updated configuration
        self.save_config()

        return paths_verified

    @staticmethod
    def _dialog_root():
        # A hidden, topmost owner window, so the file dialog opens in front of
        # the client instead of behind it where nobody notices it
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        return root


# Two Altium resources run out over a long session, and at either limit
# Altium fails and then crashes, losing unsaved work:
# - USER objects (windows, menus), capped per process at 10,000 by default.
#   Running a script PROJECT leaked about 105 per call (hidden copies of the
#   forms of the user's menu scripts); the bridge now runs a script file,
#   but the user's own menu scripts still leak.
# - Memory: Altium's script engine leaks a little on every property read of
#   a board object, about 25 MB per silkscreen export on a 40k-object board
#   and 50 MB per full silkscreen check, and never gives it back.
# Calls stop while there is still room to save.
USER_OBJECT_RESERVE = 1000
MEMORY_SHARE_LIMIT = 0.5        # of physical RAM

def altium_resources() -> Optional[dict]:
    """USER objects, their quota, private memory and the machine's RAM (MB)
    for the running Altium, or None when it is not running."""
    import ctypes
    from ctypes import wintypes

    class MemCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    class MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows") as key:
            quota = int(winreg.QueryValueEx(key, "USERProcessHandleQuota")[0])
    except OSError:
        quota = 10000
    kernel32, user32, psapi = ctypes.windll.kernel32, ctypes.windll.user32, ctypes.windll.psapi
    kernel32.OpenProcess.restype = wintypes.HANDLE
    status = MemStatus()
    status.dwLength = ctypes.sizeof(MemStatus)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    found = None
    for pid in win32process.EnumProcesses():
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            name = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if (kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
                    and os.path.basename(name.value).upper() == "X2.EXE"):
                counters = MemCounters()
                counters.cb = ctypes.sizeof(MemCounters)
                psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
                # Launcher instances are short-lived; the main one is the biggest
                here = {"user_objects": user32.GetGuiResources(handle, 1),     # GR_USEROBJECTS
                        "private_mb": counters.PrivateUsage / 2 ** 20}
                if found is None or here["private_mb"] > found["private_mb"]:
                    found = here
        finally:
            kernel32.CloseHandle(handle)
    if found is not None:
        found.update(quota=quota, ram_mb=status.ullTotalPhys / 2 ** 20)
    return found

def altium_limit_problem() -> Optional[str]:
    """Why calling into Altium now would risk a crash, or None."""
    r = altium_resources()
    if r is None:
        return None
    if r["user_objects"] > r["quota"] - USER_OBJECT_RESERVE:
        return (f"Altium is using {r['user_objects']} of its {r['quota']} window handles and will fail, "
                "then crash, near the limit. Ask the user to save their work in Altium and restart it.")
    if r["private_mb"] > MEMORY_SHARE_LIMIT * r["ram_mb"]:
        return (f"Altium is using {r['private_mb'] / 1024:.1f} GB of memory ({r['ram_mb'] / 1024:.0f} GB in the "
                "machine); its script engine leaks memory on every call and it will run out. Ask the user "
                "to save their work in Altium and restart it.")
    return None

class AltiumBridge:
    def __init__(self):
        # Ensure the MCP directory exists
        MCP_DIR.mkdir(exist_ok=True)

        # Load configuration. Finding Altium needs nobody, so it happens now;
        # asking for a path waits for the first command (see ensure_paths)
        self.config = AltiumConfig()
        self.config.discover_altium()
        self._asked_for_paths = False

        # Commands share a single request.json/response.json pair, so
        # concurrent tool calls must be serialized or they clobber each other:
        # within this server by the asyncio lock, and against other servers
        # using the same exchange folder by the file lock
        self._command_lock = asyncio.Lock()
        self._exchange_lock = ExchangeLock(EXCHANGE_DIR / "bridge.lock")

    async def ensure_paths(self, need_script: bool = True) -> Optional[str]:
        """None when Altium (and the bridge script project) can be found,
        else what is missing and where to set it.

        The file dialogs of verify_paths are shown at most once per server
        run, in a worker thread so the event loop keeps answering the
        client; after that a missing path is reported straight away.
        """
        def missing():
            if not self.config.discover_altium():
                return "altium_exe_path"
            if need_script and not os.path.exists(self.config.script_path):
                return "script_path"
            return None

        if missing() and not self._asked_for_paths:
            self._asked_for_paths = True
            await asyncio.to_thread(self.config.verify_paths)
        what = missing()
        if what == "altium_exe_path":
            return (f"Altium Designer was not found (looked in the registry, at running "
                    f"processes and under C:\\Program Files\\Altium). Set \"altium_exe_path\" "
                    f"in {CONFIG_FILE} to the full path of X2.EXE, then retry. The folder "
                    f"holding config.json can be changed with the ALTIUM_MCP_HOME "
                    f"environment variable.")
        if what == "script_path":
            return (f"The bridge script project was not found at {self.config.script_path}. "
                    f"Reinstall the extension, or set \"script_path\" in {CONFIG_FILE} to "
                    f"Altium_API.PrjScr, then retry.")
        return None

    async def execute_command(self, command: str, params: Dict[str, Any], timeout: float = RESPONSE_TIMEOUT) -> Dict[str, Any]:
        """Execute a command in Altium via the bridge script"""
        async with self._command_lock:
            problem = await self.ensure_paths()
            if problem:
                return {"success": False, "error": problem}
            if not await self._exchange_lock.acquire(LOCK_TIMEOUT):
                return {"success": False,
                        "error": f"Not run: another Altium MCP server (another Claude session) "
                                 f"has had a command running in Altium for over {LOCK_TIMEOUT} s. "
                                 f"Retry when it has finished."}
            try:
                # A launch sent to a hung Altium only queues up behind whatever
                # holds its UI thread; say so now instead of after the start
                # timeout. Checked with the exchange lock held: while waiting
                # for it, Altium is busy with another server's command, which
                # is no reason to give up.
                if await asyncio.to_thread(sandbox.altium_hung):
                    return {"success": False, "error": ALTIUM_HUNG_ERROR}
                response = await self._execute_command_locked(command, params, timeout)
            finally:
                self._exchange_lock.release()
        return _lift_failed_result(response)

    @staticmethod
    def _altium_running() -> bool:
        # Read in-process: starting tasklist took about 110 ms on every
        # command and held up the event loop while it ran
        try:
            return bool(win_processes.pids_by_name("X2.EXE"))
        except Exception:
            return True  # unknown: assume running and use the normal timeouts

    async def stop_script_debugger(self):
        """Stop a script of ours paused in Altium's debugger.

        A runtime error leaves a script paused in the debugger, after which
        Altium refuses every new RunScript until it is stopped. Each refused
        launch leaves a modal "Another script executing now" error open, and
        while it is open Altium turns the Stop request away as well ("another
        instance ... is busy"), so Altium's message boxes are closed first.
        Harmless when nothing is paused.
        """
        _dismiss_altium_dialogs(message_boxes_only=True)
        await asyncio.sleep(1)
        logger.warning("Sending -REditScript:Stop to Altium to unblock the script executor")
        try:
            subprocess.Popen(f'"{self.config.altium_exe_path}" -REditScript:Stop', shell=True)
        except Exception as e:
            logger.error(f"Could not send -REditScript:Stop: {e}")

    @staticmethod
    async def _wait_for_start(timeout: float) -> bool:
        """The bridge script deletes request.json as soon as it has read it."""
        start = time.time()
        while time.time() - start < timeout:
            if not REQUEST_FILE.exists() or RESPONSE_FILE.exists():
                return True
            await asyncio.sleep(0.25)
        return not REQUEST_FILE.exists() or RESPONSE_FILE.exists()

    @staticmethod
    async def _read_response_text() -> str:
        """Read response.json, retrying while Altium may still be writing it."""
        text = ""
        for _ in range(12):
            try:
                text = read_altium_text(RESPONSE_FILE)
                json.loads(text, strict=False)
                return text
            except (OSError, json.JSONDecodeError):
                await asyncio.sleep(0.25)
        return text

    async def _execute_command_locked(self, command: str, params: Dict[str, Any], timeout: float = RESPONSE_TIMEOUT) -> Dict[str, Any]:
        try:
            problem = altium_limit_problem()
            if problem:
                return {"success": False, "error": f"Not running '{command}': {problem}"}

            # Holding the exchange lock, any reply or progress mark still
            # here is left over from a command that timed out. A late writer
            # may still hold it open; the request_id check below covers that.
            for leftover in (RESPONSE_FILE, PROGRESS_FILE):
                try:
                    leftover.unlink()
                except OSError:
                    pass

            # The script echoes request_id so a late reply to an earlier,
            # timed-out command is never mistaken for this command's result
            request_id = uuid.uuid4().hex[:12]
            with open(REQUEST_FILE, "w") as f:
                json.dump({
                    "command": command,
                    "request_id": request_id,
                    **params  # Include parameters directly in the main JSON object
                }, f, indent=2)

            logger.info(f"Wrote request file for command: {command} (request_id={request_id})")

            cold_start = not self._altium_running()
            if cold_start:
                logger.info("Altium is not running - launching it, allowing time for startup")

            success = await self.run_altium_script()
            if not success:
                return {"success": False, "error": "Failed to run Altium script"}

            # A running Altium picks the request up within seconds. If it does
            # not and a script of ours is stuck in the debugger, stop it and
            # launch once more. The script consumes request.json, so a queued
            # first launch cannot run it twice. Any other script is left alone:
            # it may be one the user is running or debugging.
            started = await self._wait_for_start(START_TIMEOUT_COLD if cold_start else START_TIMEOUT)
            recovered = False
            if not started and STUCK_MARKER.exists():
                logger.warning(f"Altium did not pick up the request within {START_TIMEOUT}s "
                               f"and a script of ours is stuck - attempting recovery")
                await self.stop_script_debugger()
                await asyncio.sleep(2)
                recovered = True
                if REQUEST_FILE.exists():
                    await self.run_altium_script()
                started = await self._wait_for_start(START_TIMEOUT)

            if not started:
                try:
                    REQUEST_FILE.unlink()  # a launch Altium runs later must find nothing to do
                except OSError:
                    pass
                # Each refused launch left an OK-only "Another script executing
                # now" box in Altium: do not leave it in front of the user
                _dismiss_altium_dialogs(message_boxes_only=True)
                logger.error("Altium never started the bridge script")
                # Tools pass on only "error", so the explanation goes in it
                why = ("A script of ours was stuck in Altium's debugger and stopping it did "
                       "not free Altium." if recovered else
                       "Altium is not accepting script runs: a dialog may be open, Altium may "
                       "be busy or still starting up, or a script of the user's own may be "
                       "running or paused in its debugger. Nothing was stopped.")
                return {"success": False,
                        "error": f"Altium did not start the command (not executed). {why} Check "
                                 f"the Altium window for an open dialog or a running or paused "
                                 f"script, then retry."}
            _clear_script_stuck()

            logger.info("Bridge script started, waiting for response file...")
            # Wait as long as the command keeps reporting progress, up to
            # RESPONSE_TIMEOUT_MAX: a large batch is not stopped mid-way
            start_time = last_progress = time.monotonic()
            progress_mark = None
            response_text = None
            ignored_reply = None
            while response_text is None:
                try:
                    reply_mark = RESPONSE_FILE.stat().st_mtime
                except OSError:
                    reply_mark = None
                if reply_mark is not None and reply_mark != ignored_reply:
                    response_text = await self._read_response_text()
                    try:
                        rid = json.loads(response_text, strict=False).get("request_id")
                    except (json.JSONDecodeError, AttributeError):
                        rid = None
                    if rid is None or rid == request_id:
                        break
                    # Someone else's reply: a late one, or that of a server
                    # still without the exchange lock. Leave it for its owner;
                    # ours overwrites it when it comes.
                    logger.warning(f"Ignoring a reply for request_id={rid}")
                    response_text, ignored_reply = None, reply_mark
                now = time.monotonic()
                try:
                    mark = PROGRESS_FILE.stat().st_mtime
                except OSError:
                    mark = None
                if mark != progress_mark:
                    progress_mark, last_progress = mark, now
                if now - last_progress > timeout or now - start_time > max(RESPONSE_TIMEOUT_MAX, timeout):
                    break
                await asyncio.sleep(0.25)

            if response_text is None:
                logger.error("Timeout waiting for response from Altium")
                # The script started but never finished: it is most likely
                # paused in the debugger now. Unblock it for the next command.
                _mark_script_stuck(f"bridge command {command}")
                await self.stop_script_debugger()
                # A command that changes the design may have done some or all
                # of its work; a plain retry would then do it twice
                outcome = (f" '{command}' changes the design and may have done some or all of "
                           f"its work, so its outcome is unknown: check the design before "
                           f"running it again." if command in MUTATING_COMMANDS else "")
                return {"success": False,
                        "error": f"No response from Altium after "
                                 f"{int(time.monotonic() - start_time)} s, the last "
                                 f"{timeout:g} s without progress: command '{command}' "
                                 f"started but did not finish. It probably hit a runtime error "
                                 f"and paused in Altium's debugger (now stopped, so the next "
                                 f"command can run), or it is still working on a very large "
                                 f"design.{outcome}"}

            if recovered:
                logger.info("Command succeeded after automatic executor recovery")

            # Log the raw response for debugging
            logger.info(f"Raw response (first 200 chars): {response_text[:200]}")

            # Parse the JSON response. strict=False accepts control characters
            # inside strings: JSONEscapeString escapes only CR, LF and TAB, and
            # Altium text (descriptions, parameters) can hold others.
            try:
                response = json.loads(response_text, strict=False)
                logger.info(f"Successfully parsed JSON response")
                return response
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing JSON response: {e}")
                logger.error(f"Error at position {e.pos}, line {e.lineno}, column {e.colno}")
                logger.error(f"Character at error position: '{response_text[e.pos:e.pos+10]}...'")
                return {
                    "success": False, 
                    "error": f"Invalid JSON response: {e}",
                    "raw_response": response_text[:500]  # Include part of the raw response for diagnosis
                }
        
        except Exception as e:
            logger.error(f"Error executing command: {e}")
            return {"success": False, "error": str(e)}
    
    @staticmethod
    def _resolve_msix_path(virtual_path: str) -> str:
        """Resolve an MSIX-virtualized path to the real filesystem path.

        When Claude Desktop is installed via MSIX (the standard .exe installer
        on modern Windows), file paths are virtualized under AppData\\Roaming\\
        but the real files live at AppData\\Local\\Packages\\Claude_*\\
        LocalCache\\Roaming\\. Child processes of the MSIX app (like Python)
        can see the virtualized paths, but external apps (like Altium) cannot.
        This resolves the path so external processes can find the files.
        """
        appdata = os.environ.get('APPDATA', '')
        if not appdata or not virtual_path.startswith(appdata):
            return virtual_path

        localappdata = os.environ.get('LOCALAPPDATA', '')
        packages_dir = os.path.join(localappdata, 'Packages')
        if not os.path.isdir(packages_dir):
            return virtual_path

        try:
            for item in os.listdir(packages_dir):
                if item.startswith('Claude_'):
                    relative = os.path.relpath(virtual_path, appdata)
                    real_path = os.path.join(packages_dir, item, 'LocalCache', 'Roaming', relative)
                    if os.path.exists(real_path):
                        logger.info(f"Resolved MSIX path: {virtual_path} -> {real_path}")
                        return real_path
        except Exception as e:
            logger.warning(f"Error resolving MSIX path: {e}")

        return virtual_path

    async def run_altium_script(self) -> bool:
        """Run the Altium bridge script"""
        if not os.path.exists(self.config.altium_exe_path):
            logger.error(f"Altium executable not found at: {self.config.altium_exe_path}")
            return False

        if not os.path.exists(self.config.script_path):
            logger.error(f"Script file not found at: {self.config.script_path}")
            return False

        try:
            # Resolve MSIX-virtualized path so Altium (an external process
            # outside the MSIX sandbox) can find the script files
            script_path = self._resolve_msix_path(self.config.script_path)

            # Run the units merged into one script FILE: running the script
            # project leaks window handles in Altium (see script_bundle.py).
            # The project stays the source, and the fallback.
            try:
                bundle = script_bundle.ensure_bundle(script_path, EXCHANGE_DIR / "bridge")
                command = (f'"{self.config.altium_exe_path}" -RScriptingSystem:RunScriptFile('
                           f'FileName={bundle}^|ProcName=Run)')
            except (OSError, ValueError) as e:
                logger.warning(f"Could not build the script bundle ({e}); running the project")
                command = (f'"{self.config.altium_exe_path}" -RScriptingSystem:RunScript('
                           f'ProjectName="{script_path}"^|ProcName="Altium_API>Run")')
            
            logger.info(f"Running command: {command}")
            
            # Start the process
            process = subprocess.Popen(command, shell=True)
            
            # Don't wait for completion - Altium will run the script and generate the response
            logger.info(f"Launched Altium with script, process ID: {process.pid}")
            return True
        
        except Exception as e:
            logger.error(f"Error launching Altium: {e}")
            return False

# Create a global bridge instance
altium_bridge = AltiumBridge()

@_tool(read_only=True)
async def get_all_component_property_names(ctx: Context) -> str:
    """
    Get all available component property names (JSON keys) from all components
    
    Returns:
        str: JSON array with all unique property names
    """
    logger.info("Getting all component property names")
    
    # Execute the command in Altium to get component data
    response = await altium_bridge.execute_command(
        "get_all_component_data", 
        {}
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting component data: {error_msg}")
        return json.dumps({"error": f"Failed to get component data: {error_msg}"})
    
    # Get the component data
    components_data = response.get("result", [])
    
    if not components_data:
        logger.info("No component data found")
        return json.dumps({"error": "No component data found"})
    
    try:
        # Parse the data if it's a string
        if isinstance(components_data, str):
            components_list = json.loads(components_data)
        else:
            components_list = components_data
            
        # Extract all unique property names from all components
        property_names = set()
        for component in components_list:
            property_names.update(component.keys())
        
        # Convert set to sorted list for consistent output
        property_list = sorted(list(property_names))
        
        logger.info(f"Found {len(property_list)} unique property names")
        return json.dumps(property_list, indent=2)
    except Exception as e:
        logger.error(f"Error processing component data: {e}")
        return json.dumps({"error": f"Failed to process component data: {str(e)}"})

@_tool(read_only=True)
async def get_component_property_values(ctx: Context, property_name: str) -> str:
    """
    Get values of a specific property for all components
    
    Args:
        property_name (str): The name of the property to get values for
    
    Returns:
        str: JSON array with objects containing designator and property value
    """
    logger.info(f"Getting values for property: {property_name}")
    
    # Execute the command in Altium to get component data
    response = await altium_bridge.execute_command(
        "get_all_component_data", 
        {}
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting component data: {error_msg}")
        return json.dumps({"error": f"Failed to get component data: {error_msg}"})
    
    # Get the component data
    components_data = response.get("result", [])
    
    if not components_data:
        logger.info("No component data found")
        return json.dumps({"error": "No component data found"})
    
    try:
        # Parse the data if it's a string
        if isinstance(components_data, str):
            components_list = json.loads(components_data)
        else:
            components_list = components_data
            
        # Extract the property values along with designators
        property_values = []
        for component in components_list:
            designator = component.get("designator")
            if designator and property_name in component:
                property_values.append({
                    "designator": designator,
                    "value": component.get(property_name)
                })
        
        logger.info(f"Found {len(property_values)} components with property '{property_name}'")
        return json.dumps(property_values, indent=2)
    except Exception as e:
        logger.error(f"Error processing component data: {e}")
        return json.dumps({"error": f"Failed to process component data: {str(e)}"})
    
@_tool(read_only=True)
async def get_symbol_placement_rules(ctx: Context) -> str:
    """
    Get schematic symbol placement rules from a local configuration file
    
    Returns:
        str: JSON object with rules for placing pins on schematic symbols
    """
    logger.info("Getting symbol placement rules")
    
    # Define the rules file path in the MCP directory
    rules_file_path = MCP_DIR / "symbol_placement_rules.txt"
    
    # Check if the rules file exists
    if not rules_file_path.exists():
        logger.info("Symbol placement rules file not found, suggesting creation")
        
        # Default rules content
        default_rules = (
            "Your own pin placement rules, for example where power, ground, "
            "inputs, outputs and no-connect pins go and how pin groups are spaced."
        )
        
        # Create a helpful message for the user
        message = {
            "success": False,
            "error": f"Rules file not found at: {rules_file_path}",
            "message": f"Let the user know that they can optionally update the file {rules_file_path} with custom symbol placement rules. "
                      f"Suggested content: {default_rules}"
        }
        
        return json.dumps(message, indent=2)
    
    # Read the rules file if it exists
    try:
        with open(rules_file_path, "r") as f:
            rules_content = f.read()
        
        logger.info("Successfully read symbol placement rules file")
        if not rules_content.strip():
            rules_content = """No symbol placement rules are configured. Follow the conventions of a comparable symbol in the user's library (find one with search_library_symbol and read it with get_symbol_primitives) and any instructions from the user. Keep pins on a 100 mil grid so wires connect to them."""
        
        # Return the rules with a message about how to modify them
        result = {
            "success": True,
            "message": f"Modify {rules_file_path} with custom symbol placement instructions",
            "rules": rules_content
        }
        
        return json.dumps(result, indent=2)
        
    except Exception as e:
        logger.error(f"Error reading symbol placement rules file: {e}")
        return json.dumps({
            "success": False,
            "error": f"Failed to read rules file: {str(e)}"
        }, indent=2)

@_tool(read_only=True)
async def get_library_symbol_reference(ctx: Context) -> str:
    """
    Get the currently open symbol from a schematic library to use as reference for creating a new symbol.
    This tool should be used before creating a new symbol to understand the structure of existing symbols.
    
    Returns:
        str: JSON object with the reference symbol data including pins, their types, positions, and orientations
    """
    logger.info("Getting library symbol reference data")
    
    # Execute the command in Altium to get symbol reference data
    response = await altium_bridge.execute_command(
        "get_library_symbol_reference", 
        {}
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting symbol reference: {error_msg}")
        return json.dumps({"error": f"Failed to get symbol reference: {error_msg}"})
    
    # Get the symbol reference data
    symbol_data = response.get("result", {})
    
    if not symbol_data:
        logger.info("No symbol reference data found")
        return json.dumps({"error": "No symbol reference data found or no symbol is currently selected in the library"})
    
    logger.info(f"Retrieved symbol reference data")
    return json.dumps(symbol_data, indent=2)

@_tool(read_only=True)
async def search_library_symbol(ctx: Context, symbol_name: str, library_path: str = "") -> str:
    """
    Search for a symbol by name in a schematic library (.SchLib) and navigate to it.
    Supports partial name matching (case-insensitive). Returns all matches and navigates
    to the best match (exact match preferred, otherwise first partial match).

    This tool will automatically open the library file in Altium if a path is provided,
    so no SchLib needs to be open beforehand.

    Args:
        symbol_name (str): Name or partial name of the symbol to search for
        library_path (str): Full file path to the .SchLib file (e.g. "C:\\Libraries\\MyParts.SchLib").
                           The tool will open this file in Altium if it is not already open.
                           If empty, uses the currently open library.
                           If no library is open and no path is provided, ask the user for the file path.

    Returns:
        str: JSON object with search results including matches, navigated symbol, and full symbol list
    """
    library_path = _canon(library_path)
    logger.info(f"Searching for symbol: {symbol_name} in library: {library_path or '(current)'}")

    # Execute the command in Altium
    params = {"symbol_name": symbol_name}
    if library_path:
        params["library_path"] = library_path

    response = await altium_bridge.execute_command(
        "search_library_symbol",
        params
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error searching for symbol: {error_msg}")
        return json.dumps({"error": f"Failed to search for symbol: {error_msg}"})

    # Get the result data
    result = response.get("result", {})

    if not result:
        logger.info("No search results returned")
        return json.dumps({"error": "No results returned from symbol search"})

    logger.info(f"Symbol search complete. Found: {result.get('found', False)}")
    return json.dumps(result, indent=2)

@_tool()
async def create_schematic_symbol(ctx: Context, symbol_name: str, description: str, pins: list, part_count: int = 1, graphics: list = None) -> str:
    r"""
    Before executing, run get_symbol_placement_rules first.

    For Altium API guidance while scripting, use the "altium-script" skill
    (ensure_altium_script_skill reports whether it is installed).

    Also look for a similar existing symbol to use as a style reference
    before drawing: search a company/library .SchLib for a comparable part
    (same category - op-amp, comparator, MCU, regulator, diode...) with
    search_library_symbol, then dump it with get_symbol_primitives and
    mirror its conventions (body style, pin lengths, pin name visibility,
    grid spacing, glyph shapes). This produces symbols consistent with the
    user's library. If no symbol library is available to reference, that is
    fine - skip this step and proceed with the defaults below; do not treat
    a missing reference as an error.

    Create a new schematic symbol in the current library with the specified pins
    Instructions: follow the rules from get_symbol_placement_rules and the
                  reference symbol's conventions; keep pins on a 100 mil grid

    Pin name inversion/overbar: To show an overbar on a pin name (for active-low signals),
                  place a backslash after EACH character that should be overbarred.
                  Examples: R\E\S\E\T\ renders as RESET with overbar.
                           C\S\/A0 renders as CS with overbar followed by /A0 without overbar.
                  Do NOT use ~{...} or other notation — only the backslash-per-character format works in Altium.

    Args:
        symbol_name (str): Name of the symbol to create
        description (str): Description of the schematic symbol
        pins (list): List of pin data in format
                    "pin_number|pin_name|pin_type|pin_orientation|x|y[|owner_part_id[|length[|show_name[|show_designator]]]]"
                    Pin types: eElectricHiZ, eElectricInput, eElectricIO, eElectricOpenCollector,
                               eElectricOpenEmitter, eElectricOutput, eElectricPassive, eElectricPower
                    Pin orientations: eRotate0 (right), eRotate90 (up), eRotate180 (left), eRotate270 (down)
                    x, y is where the pin meets the body; the pin extends from there
                    in its orientation's direction (left-side pins eRotate180, top pins eRotate90)
                    X,Y coordinates in mils
                    owner_part_id (optional): Part number the pin belongs to (1-based).
                               Use 0 for pins shared across all parts (e.g. power/GND).
                               Defaults to 1 if omitted. Only needed for multi-part symbols.
                    length (optional): pin length in mils (default 300)
                    show_name / show_designator (optional): 1 or 0 to show/hide
                               the pin name / number (e.g. op-amp pins often hide names)
        part_count (int): Number of parts in the symbol (default 1).
                         Use >1 for multi-part symbols like quad op-amps or hex buffers.
        graphics (list, optional): Explicit body graphics. When given, the
                    default auto-sized body rectangle is NOT drawn - the
                    graphics fully define the symbol body (triangles for
                    op-amps, diode glyphs, etc.). Entry formats (coordinates
                    in mils; part = owner part id, 1-based; width 0-3 =
                    zero/small/medium/large; solid 1 or 0):
                    - "line|part|width|x1|y1|x2|y2"
                    - "polyline|part|width|x1|y1|x2|y2|..." (any number of vertices)
                    - "polygon|part|width|solid|x1|y1|x2|y2|..." (closed/filled shape)
                    - "rectangle|part|width|solid|x1|y1|x2|y2"
                    - "arc|part|width|cx|cy|radius|start_angle|end_angle" (degrees CCW from 3 o'clock)
                    - "elliptical_arc|part|width|cx|cy|radius|secondary_radius|start_angle|end_angle"
                    - "ellipse|part|width|solid|cx|cy|radius|secondary_radius"
                    - "label|part|x|y|text" (free text annotation)
                    Tip: to reproduce an existing symbol's style, dump it first
                    with get_symbol_primitives and mirror its primitives.

    Returns:
        str: JSON object with the result of the component creation
    """
    logger.info(f"Creating schematic symbol: {symbol_name} with {len(pins)} pins, {part_count} part(s)")

    params = {
        "symbol_name": symbol_name,
        "description": description,
        "part_count": part_count,
        "pins": pins
    }
    if graphics:
        params["graphics"] = graphics

    # Execute the command in Altium to create the symbol
    response = await altium_bridge.execute_command(
        "create_schematic_symbol",
        params
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error creating symbol: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to create symbol: {error_msg}"})
    
    # Get the result data
    result = response.get("result", {})
    
    logger.info(f"Symbol {symbol_name} created successfully with {len(pins)} pins")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def get_schematic_data(ctx: Context, cmp_designators: list) -> str:
    """
    Get schematic data for components in Altium
    
    Args:
        cmp_designators (list): List of designators of the components (e.g., ["R1", "C5", "U3"])
    
    Returns:
        str: JSON object with schematic component data for requested designators
    """
    logger.info(f"Getting schematic data for components: {cmp_designators}")
    
    # Execute the command in Altium to get schematic data
    response = await altium_bridge.execute_command(
        "get_schematic_data",
        {}  # No parameters needed for this command in the Altium script
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting schematic data: {error_msg}")
        return json.dumps({"error": f"Failed to get schematic data: {error_msg}"})
    
    # Get the schematic data
    schematic_data = response.get("result", [])
    
    if not schematic_data:
        logger.info("No schematic data found")
        return json.dumps({"error": "No schematic data found"})
    
    try:
        # Parse the data if it's a string
        if isinstance(schematic_data, str):
            schematic_list = json.loads(schematic_data)
        else:
            schematic_list = schematic_data
        
        # Filter components by designator
        components = []
        missing_designators = []
        
        for designator in cmp_designators:
            found = False
            for component in schematic_list:
                if component.get("designator") == designator:
                    components.append(component)
                    found = True
                    break
            
            if not found:
                missing_designators.append(designator)
        
        result = {
            "components": components,
        }
        
        if missing_designators:
            result["missing_designators"] = missing_designators
            logger.info(f"Some designators not found in schematic data: {missing_designators}")
        
        logger.info(f"Found schematic data for {len(components)} components")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error processing schematic data: {e}")
        return json.dumps({"error": f"Failed to process schematic data: {str(e)}"})
    
@_tool(read_only=True)
async def get_pcb_layers(ctx: Context) -> str:
    """
    Get detailed information about all layers in the current Altium PCB
    
    Returns:
        str: JSON object with detailed layer information including copper layers, 
             mechanical layers, and special layers with their properties
    """
    logger.info("Getting detailed PCB layer information")
    
    # Execute the command in Altium to get all layers data
    response = await altium_bridge.execute_command(
        "get_pcb_layers",
        {}  # No parameters needed
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting PCB layers: {error_msg}")
        return json.dumps({"error": f"Failed to get PCB layers: {error_msg}"})
    
    # Get the layers data
    layers_data = response.get("result", [])
    
    if not layers_data:
        logger.info("No PCB layers found")
        return json.dumps({"message": "No PCB layers found in the current document"})
    
    logger.info(f"Retrieved PCB layers data")
    return json.dumps(layers_data, indent=2)

@_tool()
async def set_pcb_layer_visibility(ctx: Context, layer_names: list, visible: bool, exclusive: bool = True) -> str:
    """
    Set visibility for specified PCB layers

    Showing layers is exclusive by default: the listed layers are shown and
    every other layer is hidden, which suits a screenshot of just those
    layers. Pass exclusive=False to show the listed layers and leave the rest
    as they are. Hiding only ever hides the listed layers.

    Args:
        layer_names (list): List of layer names to modify (e.g., ["Top Layer", "Bottom Layer", "Mechanical 1"])
        visible (bool): Whether to show (True) or hide (False) the specified layers
        exclusive (bool): With visible=True, hide every layer not listed (default True)
        
    Returns:
        str: JSON object with the result of the operation
    """
    logger.info(f"Setting layers visibility: {layer_names} to {visible}")
    
    # Execute the command in Altium to set layer visibility
    response = await altium_bridge.execute_command(
        "set_pcb_layer_visibility",
        {
            "layer_names": layer_names,
            "visible": visible,
            "exclusive": exclusive
        }
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error setting layer visibility: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to set layer visibility: {error_msg}"})
    
    # Get the result data
    result = response.get("result", {})
    
    logger.info(f"Layer visibility set successfully")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def get_component_data(ctx: Context, cmp_designators: list) -> str:
    """
    Get all data for components in Altium
    
    Args:
        cmp_designators (list): List of designators of the components (e.g., ["R1", "C5", "U3"])
    
    Returns:
        str: JSON object with all component data for requested designators
    """
    logger.info(f"Getting data for components: {cmp_designators}")
    
    # Execute the command in Altium to get all component data
    response = await altium_bridge.execute_command(
        "get_all_component_data",
        {}  # No parameters needed for this command in the Altium script
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting component data: {error_msg}")
        return json.dumps({"error": f"Failed to get component data: {error_msg}"})
    
    # Get the component data
    component_data = response.get("result", [])
    
    if not component_data:
        logger.info("No component data found")
        return json.dumps({"error": "No component data found"})
    
    try:
        # Parse the data if it's a string
        if isinstance(component_data, str):
            component_list = json.loads(component_data)
        else:
            component_list = component_data
        
        # Filter components by designator
        components = []
        missing_designators = []
        
        for designator in cmp_designators:
            found = False
            for component in component_list:
                if component.get("designator") == designator:
                    components.append(component)
                    found = True
                    break
            
            if not found:
                missing_designators.append(designator)
        
        result = {
            "components": components,
        }
        
        if missing_designators:
            result["missing_designators"] = missing_designators
            logger.info(f"Some designators not found: {missing_designators}")
        
        logger.info(f"Found data for {len(components)} components")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error processing component data: {e}")
        return json.dumps({"error": f"Failed to process component data: {str(e)}"})

@_tool(read_only=True)
async def get_selected_components_coordinates(ctx: Context) -> str:
    """
    Get coordinates and positioning information for selected components in Altium layout
    
    Returns:
        str: JSON array with positioning data (designator, x, y, rotation, width, height)
    """
    logger.info("Getting coordinates for selected components")
    
    # Execute the command in Altium to get selected components coordinates
    response = await altium_bridge.execute_command(
        "get_selected_components_coordinates",
        {}  # No parameters needed
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting selected components coordinates: {error_msg}")
        return json.dumps({"error": f"Failed to get selected components coordinates: {error_msg}"})
    
    # Get the components coordinates data
    components_coords = response.get("result", [])
    
    if not components_coords:
        logger.info("No selected components found")
        return json.dumps({"message": "No components are currently selected in the layout"})
    
    logger.info(f"Retrieved positioning data for selected components")
    return json.dumps(components_coords, indent=2)

@_tool(read_only=True)
async def get_all_designators(ctx: Context) -> str:
    """
    Get all component designators from the current Altium board
    
    Returns:
        str: JSON array of all component designators on the current board
    """
    logger.info("Getting all component designators")
    
    # Execute the command in Altium to get all component data
    response = await altium_bridge.execute_command(
        "get_all_component_data",
        {}  # No parameters needed
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting component data: {error_msg}")
        return json.dumps({"error": f"Failed to get component data: {error_msg}"})
    
    # Get the component data
    component_data = response.get("result", [])
    
    if not component_data:
        logger.info("No component data found")
        return json.dumps({"error": "No component data found"})
    
    try:
        # Parse the data if it's a string
        if isinstance(component_data, str):
            component_list = json.loads(component_data)
        else:
            component_list = component_data
        
        # Extract designators
        designators = [comp.get("designator") for comp in component_list if "designator" in comp]
        
        logger.info(f"Found {len(designators)} designators")
        return json.dumps(designators)
    except Exception as e:
        logger.error(f"Error processing component data: {e}")
        return json.dumps({"error": f"Failed to process component data: {str(e)}"})

@_tool(read_only=True)
async def get_component_pins(ctx: Context, cmp_designators: list) -> str:
    """
    Get pin data for components in Altium

    Args:
        cmp_designators (list): List of designators of the components (e.g., ["R1", "C5", "U3"])

    Returns:
        str: JSON array, one entry per component with its placement info
             (x/y in mils relative to the board origin, rotation in degrees
             counterclockwise, layer) and a "pins" list. Per pin:
             - x/y: absolute pad position (mils, relative to board origin)
             - dx/dy: pad offset from the component origin in the footprint's
               rotation-0 frame. To predict a pad position for a planned
               placement: mirror dx (dx = -dx) if placing on the bottom layer,
               rotate (dx, dy) counterclockwise by the planned rotation, then
               add the planned component x/y.
             - rotation: the pad's own rotation (NOT the component rotation)
             - net, layer, width, height, shape
    """
    logger.info(f"Getting pin data for components: {cmp_designators}")
    
    # Execute the command in Altium to get pin data
    response = await altium_bridge.execute_command(
        "get_component_pins",
        {"designators": cmp_designators}  # Pass the list of designators
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting pin data: {error_msg}")
        return json.dumps({"error": f"Failed to get pin data: {error_msg}"})
    
    # Get the components pins data
    pins_data = response.get("result", [])
    
    if not pins_data:
        logger.info(f"No pin data found for designators: {cmp_designators}")
        return json.dumps({"message": "No pin data found for the specified components"})
    
    logger.info(f"Retrieved pin data for components")
    return json.dumps(pins_data, indent=2)

SANDBOX_DIR = MCP_DIR / "SandboxScript"
SANDBOX_PAS = SANDBOX_DIR / "Sandbox.pas"
# Run files live in the exchange folder: Altium runs outside Claude Desktop's
# MSIX package and cannot see the virtualized AppData path (issue #19)
SANDBOX_RUNS = EXCHANGE_DIR / "runs"
# Pictures of the dialogs a run answered (their text is often not readable)
SANDBOX_DIALOGS = EXCHANGE_DIR / "dialogs"


def _window_process_name(hwnd) -> str:
    """Executable file name of the process that owns a window ('' if unknown)."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return ""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _dismiss_altium_dialogs(message_boxes_only: bool = False):
    """Close Altium modal popups that would otherwise block a script run.

    Altium uses two kinds: Win32 task dialogs (#32770) and Delphi TMessageForm
    error/warning boxes. It also shows "Select Item To Run"
    (TForm_RunScriptParameters) when it cannot find the script project; that
    one stays open and blocks every later script run. Only windows owned by
    the Altium process (X2.EXE) are touched, so dialogs of other applications
    are never closed. With message_boxes_only, only the OK-only Error and
    Information boxes are closed: the ones a refused script launch raises.

    Returns (handle, class name, title) of the windows that were closed, so
    the caller can tell the user (a closed dialog may have been one the user
    had open).
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value not in ("#32770", "TMessageForm", "TForm_RunScriptParameters"):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if cls.value == "TMessageForm" and title not in ("Error", "Warning", "Information", "Confirm"):
            return True
        if message_boxes_only and (cls.value != "TMessageForm" or title not in ("Error", "Information")):
            return True
        if _window_process_name(hwnd).lower() != "x2.exe":
            return True
        found.append((hwnd, cls.value, title))
        return True

    user32.EnumWindows(cb, 0)
    for h, _, _ in found:
        user32.PostMessageW(h, 0x0010, 0, 0)
    return found


def _sandbox() -> sandbox.Sandbox:
    return sandbox.Sandbox(altium_exe=altium_bridge.config.altium_exe_path,
                           template=SANDBOX_PAS, runs_dir=SANDBOX_RUNS)


@_tool()
async def run_altium_script(ctx: Context, script: str, timeout_seconds: int = 120,
                            skip_lint: bool = False) -> str:
    """
    Run a DelphiScript snippet inside an isolated Altium sandbox and report
    what happened, step by step.

    Use this to develop and verify Altium API code before relying on it.
    Each call writes the script into a fresh copy of SandboxScript/Sandbox.pas
    with a unique file name and runs it with ScriptingSystem:RunScriptFile, so
    a script that failed earlier can never be re-run from Altium's editor.
    When Altium reports a compile or run-time error, the tool returns Altium's
    message with the line in YOUR script, answers the dialog, stops the
    debugger and confirms with a probe run that the next call will work. No
    manual Ctrl+F3 or -REditScript:Stop is needed. Only scripts the MCP
    started are ever stopped: a script the user is running or debugging in
    Altium is left alone, and the tool says Altium is busy instead.

    Writing the script:
    - Call SandboxLog('...') before each risky statement. The log is flushed
      after every call, so the last logged line identifies what failed.
    - Assign findings to the string variable ResultText - it is returned.
    - Variables: either start the script with your own block, e.g.
          var
              Doc, Comp : IDispatch;
              X, Y : Integer;
      or reuse the scratch variables S1..S9 (String), I1..I9 and B1..B3
      (Integer), F1..F4 (Double), Obj1..Obj9 (IDispatch), List1..List3
      (TStringList, created empty by the sandbox - reuse with .Clear, never
      Free them), IntMan, DbDoc. Assigning an undeclared plain name is
      refused before Altium is called. Do not assign to a for-loop variable
      inside its loop (compile error).
    - Calls known to crash Altium (GetState_DescriptionString, reading
      .Corner or .BoundingRectangle through an IDispatch variable, ...) and
      a PreProcess without PostProcess are refused before Altium is called:
      a script that dies between PreProcess and PostProcess leaves Altium's
      undo history broken until Altium is restarted.
    - Every dialog the tool answers is saved as a picture first, listed in
      "dialog_snapshots": Altium's own message boxes cannot be read from
      outside, so open the picture to see the message.
    - Helpers: MM(mm) converts millimetres to internal units;
      OpenSchDoc(path) returns the ISch_Document, opening the file if needed
      (GetSchDocumentByPath alone returns nil for a closed file, and a call on
      nil stops the script); SaveDoc(path) marks a document modified and saves it.
    - Errors inside the script (undeclared identifiers, calls on nil, bad
      conversions) are caught by the sandbox and come back as "script error"
      with Altium's message and the last step; no try/except is needed.
    - Loops: a running script cannot be stopped from outside, so a loop that
      never ends would freeze Altium until it is killed. Every while and
      repeat loop is therefore guarded: about 10 s before timeout_seconds all
      loops exit, the rest of the script still runs (so a PostProcess after
      the loop is not skipped), SaveDoc no longer saves, and the tool returns
      "loop guard" with the loop's line. Make loops that delete or retry
      objects bounded anyway (a fixed count, never the same object twice).
      for loops are not guarded.
    - The sandbox is standalone: helpers and constants from the production
      units (TrimJSON, AddJSONProperty, ...) are NOT available; REPLACEALL is.
    - Undo: wrap each edit in SchServer.ProcessControl.PreProcess(Doc, '') /
      PostProcess(Doc, '') (PCBServer.PreProcess / PostProcess on a PCB), and
      report changes to the robot manager (SCHM_PrimitiveRegistration for a new
      object, SCHM_BeginModify / SCHM_EndModify around a change). An undo group
      left open makes the user's own later edits in that document impossible
      to undo until Altium restarts, so the sandbox closes any group the script
      leaves open, even after an error, and reports it as
      "undo_groups_closed": fix the script when you see it.
    - Never register objects into a library document and never write to shared
      or network library paths. Verify the target document kind first
      (ObjectID 32 = schematic, 33 = symbol library).
    - Copper: add routed tracks with route_track instead of computing segment
      ends here, and run check_routing after moving tracks, vias or parts.

    For API guidance - interfaces, object models, worked examples - use the
    "altium-script" skill. ensure_altium_script_skill reports whether that
    skill is installed and can install it.

    Args:
        script (str): DelphiScript statements to execute (body only, optionally
            preceded by a var block).
        timeout_seconds (int): How long to wait for completion (default 120).
        skip_lint (bool): Run even though the known-crash check refused the
            script. Only for a false positive.

    Returns:
        str: JSON with success, the step log and the script's ResultText; on
             failure Altium's error message and its location in the script,
             the last step reached, and whether the executor was recovered.
    """
    logger.info(f"run_altium_script: {len(script.splitlines())} lines")

    if not SANDBOX_PAS.exists():
        return json.dumps({"success": False, "error": f"sandbox template missing: {SANDBOX_PAS}"})

    problem = altium_limit_problem()
    if problem:
        return json.dumps({"success": False, "error": problem})

    split = sandbox.split_var_block(script)
    problems = sandbox.check_script(split.body, sandbox.declared_names(split.decls), split.body_offset)
    if problems:
        return json.dumps({"success": False,
                           "error": "refused before execution: the script would not compile",
                           "problems": problems, "executor_wedged": False}, indent=2)
    if not skip_lint:
        problems = sandbox.lint_script(split.body, split.body_offset, split.decls)
        if problems:
            return json.dumps({"success": False,
                               "error": "refused before execution: the script uses a call known to "
                                        "crash Altium or leaves a document edit open",
                               "problems": problems,
                               "note": "Altium was not touched. Pass skip_lint=True only for a false positive.",
                               "executor_wedged": False}, indent=2)

    problem = await altium_bridge.ensure_paths(need_script=False)
    if problem:
        return json.dumps({"success": False, "error": problem, "executor_wedged": False}, indent=2)

    if await asyncio.to_thread(sandbox.altium_hung):
        return json.dumps({"success": False, "error": ALTIUM_HUNG_ERROR, "executor_wedged": False}, indent=2)

    sb = _sandbox()
    report = {}
    # A script-error dialog left open blocks every new run. Answer it only
    # when a script of ours is stuck; one from the user's own script is theirs.
    if any(d.is_script_error for d in sandbox.altium_dialogs()):
        if not STUCK_MARKER.exists():
            return json.dumps({
                "success": False,
                "error": "not run: Altium is showing a script error dialog from a script the MCP "
                         "did not start. Answer or close it in Altium, then retry.",
                "executor_wedged": False}, indent=2)
        report["recovered_before_run"] = await asyncio.to_thread(sb.recover)
        if report["recovered_before_run"]["recovered"]:
            _clear_script_stuck()

    # The loops stop early enough for the script to finish and its result to
    # arrive before this tool gives up on it
    guard_seconds = max(5, timeout_seconds - 10)
    try:
        files = sb.new_run_files()
        built = sb.write(files, script, guard_seconds)
    except Exception as e:
        return json.dumps({"success": False, "error": f"could not write the run file: {e}"})
    sb.launch(files)
    report["run_file"] = str(files.script)

    start = time.time()
    # Keyed by window: a dialog that ignores WM_CLOSE (Yes/No boxes do) is
    # found again on every pass and must be reported once
    dismissed = {}
    snapshots = {}
    error_dialog = None

    def snapshot(d):
        if d.hwnd not in snapshots:
            try:
                snapshots[d.hwnd] = sandbox.snapshot_dialog(d, SANDBOX_DIALOGS)
            except Exception as e:
                logger.warning(f"dialog snapshot failed: {e}")
                snapshots[d.hwnd] = None

    while not files.result.exists() and time.time() - start < timeout_seconds:
        await asyncio.sleep(0.5)
        for d in sandbox.altium_dialogs():
            # A syntax error stops the script before 'sandbox start' is
            # logged and shows Altium's Error box instead of the
            # "Continue execution?" dialog
            if d.is_script_error or (d.cls == "TMessageForm" and d.title == "Error"
                                     and not files.log.exists()):
                error_dialog = d
                snapshot(d)
                sandbox.answer_dialog(d)
                break
            if time.time() - start > 6:
                snapshot(d)
                sandbox.answer_dialog(d)
                dismissed[d.hwnd] = d
        if error_dialog:
            break

    report["dialogs_dismissed"] = len(dismissed)
    report["dismissed_dialog_titles"] = [d.title for d in dismissed.values()]
    report["dialog_snapshots"] = [p for p in snapshots.values() if p]
    steps = (sandbox.read_when_ready(files.log, read_altium_text).splitlines()
             if files.log.exists() else [])

    if files.result.exists():
        _clear_script_stuck()  # our script ran to the end: Altium is free
        result_text = sandbox.read_when_ready(files.result, read_altium_text).strip()
        sb.prune()
        closed = [s[len(sandbox.UNDO_PREFIX):] for s in steps if s.startswith(sandbox.UNDO_PREFIX)]
        if closed:
            report["undo_groups_closed"] = closed
            report["undo_warning"] = (
                "The script called PreProcess without a matching PostProcess (or failed between "
                "them). The sandbox closed the group so the user can still undo their own edits "
                "in that document; pair every PreProcess with a PostProcess.")
        guard = next((s for s in steps if s.startswith(sandbox.GUARD_PREFIX)), None)
        if guard is not None:
            m = re.search(r"line (\d+)", guard)
            return json.dumps({
                "success": False,
                "error": "loop guard",
                "altium_message": guard[len(sandbox.GUARD_PREFIX):],
                "line": int(m.group(1)) if m else None,
                "diagnosis": f"A while or repeat loop was still running after {guard_seconds} s, "
                             "so every loop was cut short and the script ran to its end; Altium is "
                             "free again. Statements after the loops ran on partial results and "
                             "SaveDoc did not save: check the document before saving it. Make the "
                             "loop bounded (a fixed count, never retrying the same object).",
                "result": result_text, "steps": steps, "executor_wedged": False, **report}, indent=2)
        exc = next((s for s in steps if s.startswith(sandbox.EXCEPTION_PREFIX)), None)
        if exc is not None:
            before = steps[:steps.index(exc)]
            return json.dumps({
                "success": False,
                "error": "script error",
                "altium_message": exc[len(sandbox.EXCEPTION_PREFIX):],
                "last_step_reached": before[-1] if before else None,
                "diagnosis": "The statement after 'last_step_reached' failed (a call on nil is "
                             "reported as 'Undeclared identifier: <method>'). The script was "
                             "stopped cleanly; the executor is not wedged.",
                "steps": steps, "executor_wedged": False, **report}, indent=2)
        return json.dumps({"success": True, "result": result_text, "steps": steps, **report}, indent=2)

    if error_dialog:
        # The dialog came from our run, so the stuck script is ours
        _mark_script_stuck("run_altium_script")
        recovery = await asyncio.to_thread(sb.recover)
        if recovery["recovered"]:
            _clear_script_stuck()
            # The script stopped before its end, so the template could not
            # close the undo groups it had opened; a group left open makes
            # the user's own edits in that document impossible to undo
            closed = await asyncio.to_thread(sb.close_leftover_undo, files)
            if closed:
                report["undo_groups_closed"] = closed
        started = bool(steps)
        return json.dumps({
            "success": False,
            "error": "run-time error" if started else "compile error",
            "altium_message": error_dialog.text or (
                "Altium showed an Error box before the script started; its text cannot be read "
                "from outside Altium - open the picture in 'dialog_snapshots'. This is usually "
                "a syntax error (for example a construct DelphiScript does not support, such "
                "as 'on E: Exception do')."),
            "location": sandbox.map_error_line(built, error_dialog.text),
            "last_step_reached": steps[-1] if steps else None,
            "diagnosis": ("The script started; the statement at 'location' failed while running "
                          "(a call on nil is reported as 'Undeclared identifier: <method>')."
                          if started else "The script did not compile; nothing in it ran."),
            "steps": steps,
            "executor_wedged": not recovery["recovered"],
            "recovery": recovery, **report}, indent=2)

    if any(d.cls == "TForm_RunScriptParameters" for d in dismissed.values()):
        return json.dumps({
            "success": False,
            "error": "Altium could not find the run file",
            "diagnosis": f"Altium was asked to run {files.script} and showed 'Select Item To "
                         "Run' instead, so it cannot open that path.",
            "executor_wedged": False, **report}, indent=2)

    # Still running: Stop and the recovery probe are window messages Altium
    # is not reading, and each probe launch would only queue up behind it
    if steps and await asyncio.to_thread(sandbox.altium_hung):
        _mark_script_stuck("run_altium_script")
        return json.dumps({
            "success": False,
            "error": f"no result after {timeout_seconds} s: the script is still running and "
                     "Altium is not responding",
            "last_step_reached": steps[-1],
            "diagnosis": f"The script's while/repeat loops stop at {guard_seconds} s, so the time "
                         "is going into a for loop or a single long Altium call. A running script "
                         "cannot be stopped from outside. Wait for Altium; if it stays frozen, only "
                         "ending X2.EXE frees it (unsaved work is lost). Do not retry until "
                         "Altium responds.",
            "steps": steps, "executor_wedged": True, **report}, indent=2)

    # No result and no error dialog. If our script started, it is ours: stop
    # it. If it never started, something else holds Altium - possibly a
    # script the user is running or debugging - so stop only when a script
    # of ours is known to be stuck.
    if steps or STUCK_MARKER.exists():
        _mark_script_stuck("run_altium_script")
        recovery = await asyncio.to_thread(sb.recover)
        if recovery["recovered"]:
            _clear_script_stuck()
        return json.dumps({
            "success": False,
            "error": f"no result after {timeout_seconds} s",
            "last_step_reached": steps[-1] if steps else None,
            "diagnosis": ("The script started but did not finish: it was still running or "
                          "stopped without a dialog. The debugger has been stopped, so it is no "
                          "longer running." if steps else
                          "Altium never started the script: a script of ours was stuck and has "
                          "been stopped."),
            "steps": steps,
            "executor_wedged": not recovery["recovered"],
            "recovery": recovery, **report}, indent=2)
    return json.dumps({
        "success": False,
        "error": f"no result after {timeout_seconds} s: Altium never started the script",
        "diagnosis": "Altium may be busy, showing a dialog, or running or debugging a script of "
                     "the user's own. Nothing was stopped.",
        "steps": steps, **report}, indent=2)


@_tool()
async def ensure_altium_script_skill(ctx: Context, install: bool = False) -> str:
    """
    Check whether the "altium-script" skill is installed, and optionally
    install it.

    That skill documents the Altium DelphiScript API - interfaces, object
    models, worked examples, conventions, and how to discover undocumented
    processes - and is the reference to consult before writing scripts for
    run_altium_script or debugging Altium API calls.

    Source: https://github.com/coffeenmusic/altium-scripts-skill

    Installing writes into the user's skills directory, so it only happens when
    install=True is passed explicitly. Skills load at client startup, so a
    newly installed skill becomes available after restarting the client.

    Args:
        install (bool): Install the skill if missing (requires git on PATH).

    Returns:
        str: JSON with installed (bool), the path checked, and the next step.
    """
    skill_dir = Path.home() / ".claude" / "skills" / "altium-script"
    repo = "https://github.com/coffeenmusic/altium-scripts-skill"

    if (skill_dir / "SKILL.md").exists():
        return json.dumps({"installed": True, "path": str(skill_dir),
                           "note": "Use the altium-script skill for API guidance."},
                          indent=2)

    if not install:
        return json.dumps({
            "installed": False,
            "path": str(skill_dir),
            "source": repo,
            "next_step": "Call again with install=true to clone it, or install manually "
                         "into the path above."}, indent=2)

    try:
        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["git", "clone", "--depth", "1", repo, str(skill_dir)],
                              capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            return json.dumps({
                "installed": False, "path": str(skill_dir),
                "error": (proc.stderr or proc.stdout)[:400],
                "hint": f"Requires git on PATH; otherwise download {repo} and extract "
                        "to the path above."}, indent=2)
        return json.dumps({"installed": True, "path": str(skill_dir), "source": repo,
                           "next_step": "Restart the client so the skill is loaded."},
                          indent=2)
    except Exception as e:
        return json.dumps({"installed": False, "path": str(skill_dir),
                           "error": str(e)[:300]}, indent=2)


@_tool(read_only=True)
async def get_footprint_primitives(ctx: Context, library_path: str = "", footprint_name: str = "") -> str:
    """
    Read the primitives of footprints in a PCB library (.PcbLib).

    Modes:
    - footprint_name omitted: inventory of every footprint with per-type
      primitive counts (pads, tracks, arcs, fills, texts, regions, vias,
      component_bodies)
    - footprint_name given (exact, case-insensitive): full geometry dump -
      pads (position, rotation, layer, sizes/shape per stack, hole size/
      type/width/rotation, plating, paste_mode/paste_expansion,
      mask_mode/mask_expansion, locked), tracks, arcs, fills, texts,
      regions (outline vertices). Coordinates in mils relative to the
      footprint origin (also for the footprint shown in the editor, which
      Altium holds offset by the library origin); shapes and hole types as
      raw Altium enum ints; layers as names. 3D component bodies are listed
      apart in bodies_3d, not among the primitives, so the
      create_footprints_batch round-trip is unchanged: model_file,
      model_embedded, model_x/model_y (offset from the footprint origin),
      standoff_height and overall_height (mils), layer, and
      rotation_x/y/z (degrees) and model_z_offset (mils). The rotations and
      offset come from the saved library file (Altium's script API cannot
      return them); "placement" says so, and they are null for a body that
      is not saved yet.
    - footprint_name "*": full dump of every footprint

    Use as the reference when recreating or validating footprints, and to
    survey what a library requires.

    Args:
        library_path (str, optional): Full path to the .PcbLib. Omit to use
            the currently focused PCB library (an already-open library is
            only focused, never reloaded).
        footprint_name (str, optional): Exact footprint name, or "*".

    Returns:
        str: JSON - inventory: {library_name, footprint_count, footprints:
             [{name, description, <type counts>}]}; dump: primitives and
             bodies_3d per footprint
    """
    logger.info(f"Getting footprint primitives (library={library_path}, footprint={footprint_name})")

    response = await altium_bridge.execute_command(
        "get_footprint_primitives",
        {"library_path": library_path, "footprint_name": footprint_name}
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        return json.dumps({"success": False, "error": f"Failed to get footprint primitives: {error_msg}"})

    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result
    if isinstance(result, dict):
        pcblib_file.add_body_placements(result)
    return json.dumps(result, indent=2)

@_tool()
async def create_footprints_batch(ctx: Context, spec_file: str) -> str:
    """
    Create many PCB footprints in a single Altium script run.

    The batch equivalent of create_pcb_footprint with far broader coverage:
    through-hole and SMD pads (full pad stack, holes, slots, plating,
    rotation), tracks, arcs, fills, texts, and regions on any layer.
    Verified by exact round-trips of complete production footprint
    libraries. Prefer this for bulk imports/migrations; use
    get_footprint_primitives on an existing footprint to learn the exact
    field conventions.

    Args:
        spec_file (str): Path to a plain-text spec file, one record per line
            (coords in mils, layers as names, shapes/hole types as raw
            Altium enum ints, booleans as 1/0):
            FPLIB|<path to .PcbLib>   (optional first line: opens/focuses)
            FOOTPRINT|<name>|<description>
            EDITFOOTPRINT|<name>      add MODEL3D records to an existing
                                      footprint (never creates one; other
                                      records under it are reported and
                                      ignored)
            PAD|name|x|y|rot|layer|plated|hole_size|hole_type|hole_width|hole_rot|top_x|top_y|top_shape[|corner_pct[|mode|mid_x|mid_y|mid_shape|bot_x|bot_y|bot_shape[|paste_mode|paste_expansion|mask_mode|mask_expansion]]]
                Leave a field empty to skip it and still give a later one.
                paste_mode/mask_mode are TMaskExpansionMode ints: 1 rule,
                2 manual (with the expansion in mils). 0 (no mask) cannot
                be set on a pad by script and is reported as a problem.
                Omitted: rule. Every pad the tool creates is locked.
            TRACK|x1|y1|x2|y2|width|layer
            ARC|cx|cy|radius|start_angle|end_angle|width|layer
            FILL|x1|y1|x2|y2|rotation|layer
            TEXT|x|y|size|width|rotation|layer|mirror|ttf|text
            REGION|layer|kind|x1|y1|x2|y2|...
            MODEL3D|<STEP file>|<layer>|<rot_x>|<rot_y>|<rot_z>|<z_offset>[|<x>|<y>[|<embed 1/0>]]
                add a 3D body from a STEP file: rotations in degrees,
                z_offset (model height above the board) and x|y (offset
                from the footprint origin) in mils, embedded in the
                library unless embed is 0. Read the result back with
                get_footprint_primitives (bodies_3d); its rotations and
                z offset appear only once the library is saved.

    The library is left modified and unsaved; the user saves it.

    Returns:
        str: JSON with created and edited counts, models_added,
             primitive_errors, failed names and problems (e.g. a missing
             model file, mask mode 0, a record EDITFOOTPRINT ignores)
    """
    logger.info(f"Creating footprints batch from {spec_file}")

    response = await altium_bridge.execute_command(
        "create_footprints_batch",
        {"spec_file": spec_file}
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        return json.dumps({"success": False, "error": f"Failed batch footprint creation: {error_msg}"})

    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

@_tool()
async def build_schematic(ctx: Context, parts: list, wires: list = None,
                          junctions: list = None, net_labels: list = None,
                          power_ports: list = None, notes: list = None) -> str:
    """
    Build a wired schematic on a NEW sheet from a circuit description.

    READ dev/SCHEMATIC_CONVENTIONS.md BEFORE CALLING. This tool places exactly
    what it is given; it does not lay out or correct spacing. The conventions
    file records the drafting rules that make the result readable, each one
    learned by getting it wrong. The ones that most often produce a plausible
    but wrong sheet:

      * A pin's connection point is NOT Pin.Location - it is PinLength further
        along the pin. Do not guess coordinates. Call with parts only first,
        read the returned pin_map, then call again with wires routed from those
        measured coordinates.
      * Never run a riser in a pin's connection column; it drives through every
        pin sharing that column.
      * Give a shunt part one grid of wire between its pin and the node it taps,
        rather than putting the junction on the pin.
      * A net label that does not TOUCH its wire names nothing.
      * A shunt part occupies the band below its rail. Do not route another net
        through that band - the wire will pass through the symbol body.
      * A ground port's "GND" label is always drawn (ShowNetName cannot hide it)
        and occupies ~300 mil below the port. Keep wires out of that band.

    Once the sheet is saved, run check_schematic on it and fix what it finds.

    Args:
        parts: component dicts. Required keys: designator, symbol_library
            (full path of a .SchLib file; .IntLib is not supported),
            symbol, x, y (mils). Optional: design_item_id, orientation (0-3),
            mirror (0/1), comment, description, footprint,
            parameters ({name: value}).
        wires: each a flat list of alternating x,y in mils, e.g.
            [3900, 3000, 5300, 3000, 5300, 4900] - two segments, three points.
        junctions: [{"x":..,"y":..}] - only where 3+ branches actually meet.
        net_labels: [{"x":..,"y":..,"orientation":0,"text":"LED+"}] - the
            coordinate must lie on a wire.
        power_ports: [{"x":..,"y":..,"orientation":3,"style":5,"text":"GND",
            "show_net_name":false}]. Harvest style/orientation from an existing
            sheet rather than guessing: 5 = digital ground, 2 = supply bar.
        notes: [{"x":..,"y":..,"text":".."}] free text.

        All coordinates are mils and keep their fractions, so a metric sheet
        can be drawn exactly (1 mm = 39.3700787 mil). The pin_map has the
        same precision: route from its values unrounded, or a wire ends up
        a few microns out of level and turns diagonal when its part moves.

    Returns:
        JSON with the created sheet name, counts, and the pin map - every
        placed pin's true connection point, for routing a follow-up call.
    """
    logger.info(f"Building schematic: {len(parts)} parts")

    # The spec carries Altium coords (10000 per mil). Whole mils missed a
    # metric grid by up to half a mil, and a wire that far out of level turns
    # diagonal when its part is dragged.
    def c(v):
        return str(int(round(float(v) * 10000)))

    lines = []
    for p in parts:
        for key in ("designator", "symbol_library", "symbol", "x", "y"):
            if key not in p:
                return json.dumps({"success": False,
                                   "error": f"part missing required key '{key}': {p}"})
        lines.append("PART|{}|{}|{}|{}|{}|{}|{}|{}".format(
            p["designator"], p["symbol_library"], p["symbol"],
            p.get("design_item_id", ""), c(p["x"]), c(p["y"]),
            int(p.get("orientation", 0)), 1 if p.get("mirror") else 0))
        if p.get("comment"):
            lines.append(f"COMMENT|{p['comment']}")
        if p.get("footprint"):
            lines.append(f"FOOTPRINT|{p['footprint']}")
        if p.get("description"):
            lines.append(f"DESCRIPTION|{p['description']}")
        for name, val in (p.get("parameters") or {}).items():
            lines.append(f"PARAM|{name}|{val}")

    # The bridge can only read .SchLib files, and asked to open a path that
    # does not exist Altium creates an empty library of that name instead -
    # which used to give an empty sheet reported as success (issue #19)
    for lib in sorted({str(p["symbol_library"]) for p in parts}):
        if not lib.lower().endswith(".schlib") or not os.path.isfile(lib):
            return json.dumps({"success": False,
                               "error": "symbol_library must be the full path of an existing "
                                        ".SchLib file (integrated libraries such as .IntLib "
                                        f"are not supported): {lib}"})

    for route in (wires or []):
        if len(route) < 4 or len(route) % 2:
            return json.dumps({"success": False,
                               "error": f"wire needs an even count of >=4 coords: {route}"})
        lines.append("WIRE|" + "|".join(c(v) for v in route))
    for j in (junctions or []):
        lines.append(f"JUNCTION|{c(j['x'])}|{c(j['y'])}")
    for n in (net_labels or []):
        lines.append("NETLABEL|{}|{}|{}|{}".format(
            c(n["x"]), c(n["y"]), int(n.get("orientation", 0)), n["text"]))
    for pw in (power_ports or []):
        lines.append("POWER|{}|{}|{}|{}|{}|{}".format(
            c(pw["x"]), c(pw["y"]), int(pw.get("orientation", 3)),
            int(pw.get("style", 5)), pw["text"],
            1 if pw.get("show_net_name") else 0))
    for nt in (notes or []):
        lines.append(f"NOTE|{c(nt['x'])}|{c(nt['y'])}|{nt['text']}")

    spec_path = Path("C:/Users/Public/altium_mcp/circuit_spec.txt")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    # Part descriptions carry characters that are not plain ASCII
    write_altium_text(spec_path, "\n".join(lines) + "\n")

    response = await altium_bridge.execute_command("build_circuit", {})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": response.get("error", "unknown error")})

    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result

    # Hand back the measured pin map so the caller can route a second pass
    pin_map = {}
    pm = Path("C:/Users/Public/altium_mcp/pin_map.txt")
    if pm.is_file():
        for line in read_altium_text(pm).splitlines():
            f = line.strip().split("|")
            if len(f) == 5 and f[0] == "PIN":
                pin_map.setdefault(f[1], {})[f[2]] = [int(f[3]) / 10000, int(f[4]) / 10000]
    result["pin_map"] = pin_map
    result["pin_map_note"] = ("Absolute electrical connection points. Route "
                              "wires from these, not from predicted offsets.")
    return json.dumps(result, indent=2)

async def _schematic_pins(sheet_path: str, designators=None):
    """The sheet's placed pins from Altium (internal units), or (None, error)."""
    result, error = await _bridge_json("get_schematic_pins", {
        "sheet_path": sheet_path,
        "designators": "|" + "|".join(designators) + "|" if designators else ""})
    if result is None:
        return None, error
    pins = result.get("pins", [])
    # Some libraries pad pin numbers and names with spaces ("B4   "): a
    # reference such as "U1.B4" must still find the pin
    for p in pins:
        for key in ("pin", "name", "designator"):
            if isinstance(p.get(key), str):
                p[key] = p[key].strip()
    return pins, ""

@_tool(read_only=True)
async def get_schematic_pins(ctx: Context, sheet_path: str, designators: list = None, unit: str = "mil") -> str:
    """
    The pins of a schematic sheet's parts, live from Altium: each pin's
    connection point (the outer end, where a wire must end), number, name,
    electrical type and whether it is hidden. Only the part and display mode
    each component shows are listed. Unsaved changes in Altium are seen.

    Args:
        sheet_path (str): Full path of the .SchDoc; opened if not open.
        designators (list): Only these components; empty for all.
        unit (str): "mil" (default) or "mm" for the coordinates.

    Returns:
        str: JSON with the pins: designator, part, pin, name, x, y,
             electrical, hidden.
    """
    import sch_edit

    sheet_path = long_path(sheet_path)
    if unit not in sch_edit.COORD_PER:
        return json.dumps({"success": False, "error": "unit is mil or mm"})
    if designators and any("|" in str(d) for d in designators):
        return json.dumps({"success": False, "error": "a designator cannot contain '|'"})
    pins, error = await _schematic_pins(sheet_path, [str(d).strip().upper() for d in designators or []])
    if pins is None:
        return json.dumps({"success": False, "error": error})
    scale = sch_edit.COORD_PER[unit]
    for p in pins:
        p["x"], p["y"] = round(p["x"] / scale, 4), round(p["y"] / scale, 4)
    return json.dumps({"success": True, "unit": unit, "count": len(pins), "pins": pins}, indent=2)

@_tool()
async def edit_schematic(ctx: Context, sheet_path: str, operations: list, unit: str = "mil",
                         save: bool = False) -> str:
    """
    Change an existing schematic sheet: move, rotate, delete or place parts,
    set parameters, move designators and parameter text, add wires, net
    labels, power ports, junctions and no-ERC markers, and delete objects.

    The whole edit is one undo step in Altium (Ctrl+Z takes it all back),
    and the user's own edits stay undoable. Operations run in order; one
    that fails is reported in "errors" and the rest still run.

    Coordinates are absolute sheet coordinates, Altium's origin at the
    bottom left, in `unit` ("mil" or "mm"). check_schematic reports
    positions and suggestions in the sheet's own unit (mm for a metric
    grid), so pass the same unit to apply its suggestions directly.

    Operations, each a dict with "op":
      move_component    designator, [part], [x, y], [rotation 0/90/180/270],
                        [mirror true/false]. x, y is the part's origin.
      delete_component  designator, [part]
      place_component   library (.SchLib path), symbol, designator, x, y,
                        [rotation], [mirror], [comment]
      set_parameter     designator, name, value, [part], [visible]
                        (creates the parameter when missing)
      move_text         designator, name ("Designator", "Comment" or a
                        parameter), [part], [x, y], [justification 0-8:
                        bottom-left, bottom-centre, bottom-right, middle-left,
                        ..., top-right], [orientation 0-3], [visible]
      add_wire          points [[x, y] or "U20.VCC", ...]: a point may name
                        a pin, by number ("U20.3") or name ("U20.VCC")
      add_net_label     x, y (on a wire), text, [orientation], [justification]
      add_power_port    x, y, text, [style: bar, arrow, circle, wave,
                        power_ground, signal_ground, earth], [orientation
                        0-3, default 1 = up], [show_net_name]
      add_junction      x, y
      add_no_erc        x, y
      add_port          x, y (its left end, or bottom end when vertical),
                        name, [io_type: unspecified, output, input,
                        bidirectional], [style: right (default), left,
                        left_right, none, and vertical: top, bottom,
                        top_bottom, none_vertical], [width]:
                        a width turns auto size off, so the port keeps its
                        length and stays on its wire when renamed
      delete_at         kind (wire, net_label, power_port, junction, no_erc,
                        port, note), x, y, [text]: removes a wire passing
                        through the point, or the object located there
    "part" picks one part of a multi-part component (1 = A, ...).
    The add_* operations with x, y also take "at": "U20.VCC" instead, to
    place a label, port, power port, junction or no-ERC on a pin. Pins are
    read live from the sheet (see get_schematic_pins) before anything is
    changed; an unknown pin or a name several pins share is an error. So a
    reference to a part that an earlier operation in the same call moves,
    places or deletes is refused: refer to it in a second call. On a
    multi-part part, "U1A.3" names pin 3 of part A; a pin common to every
    part (often the power pins) needs the part letter.

    Args:
        sheet_path (str): Full path of the .SchDoc; opened if not open.
        operations (list): The operations above, applied in order.
        unit (str): "mil" (default) or "mm".
        save (bool): Save the sheet afterwards (needed before
            check_schematic, which reads the saved file). Otherwise it is
            left modified for the user to review and save.

    Returns:
        str: JSON with how many operations were applied and the errors.
    """
    import sch_edit

    # Long paths, or Altium opens a second copy of a sheet or library that
    # is already open
    sheet_path = long_path(sheet_path)
    operations = [dict(op, library=long_path(op["library"]))
                  if isinstance(op, dict) and op.get("op") == "place_component" and op.get("library") else op
                  for op in (operations or [])]
    if not sheet_path.lower().endswith(".schdoc"):
        return json.dumps({"success": False, "error": "sheet_path must be a .SchDoc"})
    designators = sch_edit.pin_designators(operations)
    if designators:
        pins, error = await _schematic_pins(sheet_path, designators)
        if pins is None:
            return json.dumps({"success": False, "error": f"Could not read the pins: {error}"})
        operations, error = sch_edit.resolve_pins(operations, pins, unit)
        if error:
            return json.dumps({"success": False, "error": error})
    lines, error = sch_edit.encode_operations(operations, unit)
    if error:
        return json.dumps({"success": False, "error": error})
    logger.info(f"Editing {sheet_path}: {len(lines)} operations")
    # Part names and parameter values may hold text that is not plain ASCII
    write_altium_text(EXCHANGE_DIR / "sch_edit_spec.txt", "\n".join(lines) + "\n")
    response = await altium_bridge.execute_command("edit_schematic", {
        "sheet_path": sheet_path, "save": bool(save)})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to edit the sheet: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    if result.get("errors"):
        result["success"] = False
    return json.dumps(result, indent=2)

# Changes the design: a timeout leaves its outcome unknown, so no blind retry
MUTATING_COMMANDS.add("edit_schematic")

@_tool(read_only=True)
async def check_schematic(ctx: Context, path: str, max_issues_per_sheet: int = 150) -> str:
    """
    Check a schematic sheet for drafting problems that make it hard to read.

    Run it after drawing or editing a sheet (build_schematic, run_altium_script
    or by hand), fix what it reports, and run it again. It reads the SAVED
    file and does not need Altium, so save the sheet first; unsaved edits are
    not seen.

    Issue types, most important first:
      * pin_tap - a two-pin part (decoupling cap, pull-up) joins its net AT its
        own pin. Move the part so a short wire (a couple of grids) runs from
        the pin to a junction on the net; suggest.junction_at shows where.
      * label_not_at_end - a net label sits along a wire, away from the wire's
        free end. Put it at the end (suggest gives the point).
      * label_justification - a label at the free end whose text hangs off
        the end of the wire. Justify it so the text runs back along the wire:
        bottom-right (2) when the wire ends to the right, bottom-left (0)
        when it ends to the left. A middle-justified (3/5) label anchored on
        the end, reading outward like a port, is also accepted.
      * sheet_entry_margin - a sheet symbol's first entry sits well below its
        top edge. Altium's default is one grid (100 mil; 3 mm on a 1 mm grid);
        extra room at the bottom is fine.
      * sheet_symbol_text - a sheet symbol's designator or file name is not
        above it. Both go above: file name on the top edge, designator a line
        (100 mil) above it, both bottom-left justified at the left edge.
      * label_orientation, label_off_wire, dangling_wire, wire_through_body.
      * pin_off_grid - a part whose pins sit a few microns (up to 1 mil) off
        the grid. Every wire drawn to them comes out slanted. When the part
        itself is on the grid its drawing was shifted from its origin. When
        every pin is off by the same amount, suggest gives the fix:
        move_component_to (an on-grid position for edit_schematic's
        move_component) when the whole part is that far off the grid, else
        shift_contents_by. Pins off by different amounts get no suggestion:
        the symbol needs fixing in its library.
      * slanted_wire - a wire segment a little (1 um to half a grid) out of
        level or plumb. It looks straight, but dragging a part on it turns it
        diagonal. Usually a symbol whose pins are off the grid (then the
        issue names the pin) or coordinates rounded to whole mils.
        suggest.move_end/to give the end to move, at full precision:
        delete_at the wire and add_wire it again with the new end.
      * text_overlap - a designator, parameter, net label or port text
        overlapping a wire, a pin, a part or other text. For part text the
        issue carries suggest.texts: positions (x, y, justification) for the
        designator and each visible parameter that collide with nothing, e.g.
        designator left and value right of a vertical cap.

    Coordinates are in the sheet's units ("unit": mm for a metric grid,
    otherwise mil) with Altium's origin at the bottom left.

    Args:
        path: a .SchDoc, a .PrjPcb (every sheet it lists), or a folder (every
            .SchDoc in it, not recursive).
        max_issues_per_sheet: cap on issues listed per sheet; counts are
            always complete.

    Returns:
        JSON: per sheet, the issue counts and the issues.
    """
    import schematic_check

    p = Path(path)
    if p.is_dir():
        sheets = sorted(p.glob("*.SchDoc"))
    elif p.suffix.lower() == ".prjpcb" and p.is_file():
        sheets = schematic_check.project_sheets(p)
    elif p.suffix.lower() == ".schdoc" and p.is_file():
        sheets = [p]
    else:
        return json.dumps({"success": False,
                           "error": f"not a .SchDoc, .PrjPcb or folder: {path}"})

    results, totals = [], {}
    for sheet in sheets:
        if not Path(sheet).is_file():
            results.append({"sheet": str(sheet),
                            "error": "listed in the project but not on disk - save it in Altium first"})
            continue
        try:
            r = schematic_check.check_sheet(sheet)
        except Exception as e:
            results.append({"sheet": str(sheet), "error": f"{type(e).__name__}: {e}"})
            continue
        for k, v in r["issue_counts"].items():
            totals[k] = totals.get(k, 0) + v
        if len(r["issues"]) > max_issues_per_sheet:
            r["issues"] = r["issues"][:max_issues_per_sheet]
            r["truncated"] = True
        results.append(r)
    return json.dumps({"success": True, "sheets": len(sheets), "issue_counts": totals,
                       "results": results}, indent=1)

@_tool(read_only=True)
async def check_hierarchy(ctx: Context, project: str, min_severity: str = "warning",
                          max_issues: int = 300) -> str:
    """
    Check that a project's sheets are connected: every sheet entry against the
    ports of its child sheet, harness types, and harness definitions.

    Design >> Synchronize Sheet Entries and Ports does this for one sheet
    symbol, by exact name. This covers the whole project at once, pairs that
    probably belong together under different names, and the harness errors
    the ERC reports without a location. It reads the SAVED files and does not
    need Altium, so save first.

    Issue types, by severity:
      error
      * probable_pair - an entry and a port that do not connect but belong
        together (same harness type, same name apart from case/separators, or
        a similar name). suggest renames the port to the entry's name, or the
        entry when the child sheet is used by several sheet symbols;
        alternative gives the other way round.
      * harness_type_mismatch - entry and port of the same name with
        different harness types.
      * harness_conflict - one harness type defined with different entries on
        two sheets (the ERC's fatal "Conflicting harness definition");
        variants lists which entries only one side has.
      * harness_type_undefined - an entry or port uses a harness type no
        harness connector defines.
      * signal_harness_untyped - a signal harness that reaches no harness
        connector and no typed port or entry, with its location (the ERC's
        fatal "Missing harness type on harness", which gives none).
      * signal_harness_type_conflict - a signal harness joining two types.
      * child_sheet_missing, sheet_symbol_without_file.
      warning
      * entry_without_port, port_without_entry - unmatched on one side.
        linked_on_other_instance marks a port another sheet symbol using the
        same child does link (the ERC is silent about this instance).
      * io_mismatch, io_unspecified - I/O types of entry and port differ
        (not for harness entries and ports, which have none in Altium).
      * name_case, duplicate_entry, child_not_in_project.
      * entry_unwired - nothing attached to the entry on the parent sheet.
      * harness_file_stale, harness_file_missing - a <sheet>.Harness file
        that disagrees with the sheet's harness connectors.
      info
      * entry_matches_power_port - no port for the entry, but a power port
        of that name on the child sheet (power ports connect globally).
      * harness_order, top_sheet_ports, sheet_not_in_hierarchy,
        harness_file_not_in_project.

    Locations are in mils, Altium's origin at the bottom left. Sheet symbols
    are given by designator and location (symbol_at), since several symbols
    may share a designator.

    Args:
        project: the .PrjPcb.
        min_severity: "error", "warning" (default) or "info": the least severe
            issues listed. Counts always cover every severity.
        max_issues: cap on issues listed.

    Returns:
        JSON: settings, top sheets, issue counts by severity, and the issues.
    """
    import hierarchy_check

    p = Path(project)
    if p.suffix.lower() != ".prjpcb" or not p.is_file():
        return json.dumps({"success": False, "error": f"not a .PrjPcb: {project}"})
    if min_severity not in hierarchy_check.SEVERITY_ORDER:
        return json.dumps({"success": False, "error": "min_severity must be error, warning or info"})
    try:
        result = hierarchy_check.check_hierarchy(p, max_issues=max_issues, min_severity=min_severity)
    except Exception as e:
        return json.dumps({"success": False, "error": f"{type(e).__name__}: {e}"})
    return json.dumps({"success": True, **result}, indent=1, ensure_ascii=False)

@_tool()
async def create_symbols_batch(ctx: Context, spec_file: str) -> str:
    """
    Create many schematic symbols, or edit existing ones, in a single
    Altium script run.

    Use instead of repeated create_schematic_symbol calls when creating
    more than a handful of symbols (bulk library imports/migrations): one
    script launch instead of one per symbol, and pipe-delimited plain text
    instead of JSON so field text (commas, brackets, spaces) is preserved
    exactly. Verified by exact round-trips of a complete 284-symbol
    production library.

    Args:
        spec_file (str): Path to a plain-text spec file, one record per line:
            LIBRARY|<path to .SchLib>   (optional first line: opens/focuses;
                                         an already-open library is only
                                         focused, never reloaded)
            SYMBOL|<name>|<description>|<part_count>
            PIN|<same pipe fields as create_schematic_symbol pins>
            GRAPHIC|<same entry format as create_schematic_symbol graphics>
            EDITSYMBOL|<name>   edit an existing symbol instead (its pins
                                and graphics are left alone; a missing
                                symbol is reported as failed, never created)
            COMMENT|<text>[|<visible 1/0>]   set the Comment (empty text
                                is allowed); hidden unless visible is 1
            DESCRIPTION|<text>  set the Description
            SYMPARAM|<name>|<value>|<visible 1/0>[|x|y[|justification[|font]]]
                                add a symbol parameter, or replace one with
                                that name. By default, visible ones without
                                x|y go below the body's bottom-left corner,
                                left-aligned, 100 mil apart in record order
                                (leave a field empty to skip it and still
                                give a later one). justification:
                                bottom_left, bottom_center, bottom_right,
                                center_left, center, center_right, top_left,
                                top_center or top_right. font: <font
                                name>:<size>, e.g. "Arial:10" (resolved by
                                name, so it works in any document). Omitted
                                fields keep the parameter's own. A parameter
                                the tool positions gets Autoposition off, so
                                Altium does not move it later.
            PINPARAM|<pin number>|<name>|<value>   add (or replace) a hidden
                                parameter on every pin with that number
            PINDESC|<pin number>|<text>   set the Description of every pin
                                with that number
            PINSYMBOL|<pin number>|<edge>|<symbol>   set an IEEE symbol of
                                every pin with that number. edge: inside,
                                inside_edge, outside_edge or outside.
                                symbol: a TIeeeSymbol name without the
                                leading "e", any case - none, dot, clock,
                                activelowinput, activelowoutput, schmitt,
                                analogsignalin, digitalsignalin, opencollector,
                                hiz, ... (the full list is in the
                                altium-script skill, SCH_API_Reference.md,
                                TIeeeSymbol). An unknown edge or symbol is
                                reported under problems.
            PINFUNC|<pin number>|<F1>/<F2>/...   optional: replace the
                                Functions list (Properties panel > Pin >
                                Functions) of every pin with that number,
                                in the order given, e.g.
                                PINFUNC|23|ETH1_MDIO/USART2_TX/TIM2_CH3.
                                Use it only when the user asks for pin
                                functions; nothing sets them otherwise, not
                                even from a pin name with '/' in it. Names
                                cannot contain '/' or '|'. A script cannot
                                clear the list, so an empty one is reported
                                under problems; Altium merges repeated names,
                                and a stored list that differs is reported.
            These records work after SYMBOL and EDITSYMBOL alike.
            Each SYMBOL or EDITSYMBOL line starts a new symbol; the other
            records belong to the most recent one.

    Every created or edited symbol is one undo step and marks the library
    as modified; nothing is saved - the user saves the library.

    Returns:
        str: JSON object with created and edited counts, a failed name list
             and problems (e.g. a pin number not in the symbol, an unknown
             IEEE symbol or justification)
    """
    logger.info(f"Creating symbols batch from {spec_file}")

    # The script opens or focuses the LIBRARY| path as written, so a short
    # (8.3) or forward-slash path would open a second copy of a library that
    # is open. Altium gets a copy with the long path; the caller's file is
    # left as it is.
    copy = None
    try:
        fixed = spec_with_long_library(read_altium_text(spec_file))
    except OSError:
        fixed = None  # missing or unreadable: the script reports it
    if fixed is not None:
        fd, copy = tempfile.mkstemp(prefix="symbols_spec_", suffix=".txt", dir=EXCHANGE_DIR)
        os.close(fd)
        write_altium_text(copy, fixed)
    try:
        response = await altium_bridge.execute_command(
            "create_symbols_batch",
            {"spec_file": copy or spec_file}
        )
    finally:
        if copy:
            try:
                os.remove(copy)
            except OSError:
                pass

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error in batch symbol creation: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed batch creation: {error_msg}"})

    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

@_tool(read_only=True)
async def get_symbol_primitives(ctx: Context, library_path: str = "", symbol_name: str = "") -> str:
    """
    Read the graphic primitives of symbols in a schematic library (.SchLib).

    Two modes:
    - symbol_name omitted: inventory of every symbol in the library with
      per-type primitive counts (pins, rectangles, lines, polylines,
      polygons, arcs, ellipses, beziers, labels, ...). Use this to survey
      what drawing features a library's symbols require.
    - symbol_name given (exact match, case-insensitive): full geometry dump
      of that symbol - every primitive with coordinates in mils, plus pin
      details (number, name, electrical type, orientation, length,
      owner_part_id). Use this as the reference/spec when recreating or
      validating a symbol.

    Args:
        library_path (str, optional): Full path to the .SchLib file to open.
            Omit to use the schematic library currently focused in Altium.
        symbol_name (str, optional): Exact symbol (LibReference) name to dump.

    Returns:
        str: JSON object - inventory mode: {library_name, symbol_count,
             symbols: [{name, description, part_count, <type counts>}]};
             dump mode: {library_name, symbol_name, description, part_count,
             comment: {text, visible}, parameters: [{name, value, visible,
             x, y, justification, autoposition, font: {name, size}}],
             primitives: [...]} - x and y are where Altium holds the
             parameter now. Pins also carry
             description, symbol_inside, symbol_inside_edge,
             symbol_outside_edge and symbol_outside (IEEE symbol names as
             PINSYMBOL takes them), their parameters [{name, value}]
             when they have any, and functions ["ETH1_MDIO", ...] (the
             Functions list, as PINFUNC sets it) when they have any
    """
    logger.info(f"Getting symbol primitives (library={library_path}, symbol={symbol_name})")

    response = await altium_bridge.execute_command(
        "get_symbol_primitives",
        {"library_path": library_path, "symbol_name": symbol_name}
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting symbol primitives: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to get symbol primitives: {error_msg}"})

    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

@_tool(read_only=True)
async def get_all_nets(ctx: Context) -> str:
    """
    Return every unique net name in the active PCB document.

    Returns
    -------
    str :
        A JSON array of net names, e.g. ["GND", "VCC33", "USB_D+", ...]
    """
    logger.info("Getting all nets")

    response = await altium_bridge.execute_command("get_all_nets", {})

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting nets: {error_msg}")
        return json.dumps({"error": f"Failed to get nets: {error_msg}"})

    # Result is already a JSON‑serialisable Python list
    return json.dumps(response.get("result", []), indent=2)

@_tool()
async def create_net_class(ctx: Context, class_name: str, net_names: list) -> str:
    """
    Create a new net class and add specified nets to it
    
    Args:
        class_name (str): Name of the net class to create or modify
        net_names (list): List of net names to add to the class
    
    Returns:
        str: JSON object with the result of the operation. nets_added counts
        the nets that were not in the class before; names the board has no
        net for are listed in nets_not_found and not added.
    """
    logger.info(f"Creating net class '{class_name}' with {len(net_names)} nets")
    
    # Execute the command in Altium to create the net class
    response = await altium_bridge.execute_command(
        "create_net_class",
        {
            "class_name": class_name,
            "net_names": net_names
        }
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error creating net class: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to create net class: {error_msg}"})
    
    # Get the result data
    result = response.get("result", {})
    
    logger.info(f"Net class '{class_name}' created/modified successfully")
    return json.dumps(result, indent=2)
    
@_tool()
async def set_component_position(ctx: Context, cmp_designator: str, x: float, y: float, rotation: float = -1) -> str:
    """
    Set a component's absolute position in the PCB layout
    
    Args:
        cmp_designator (str): Designator of the component to position (e.g., "R1", "C5", "U3")
        x (float): Absolute X position in mils
        y (float): Absolute Y position in mils
        rotation (float): Rotation angle in degrees (0-360), use -1 to keep current rotation
    
    Returns:
        str: JSON object with the result of the position operation
    """
    logger.info(f"Setting component {cmp_designator} position to X:{x}, Y:{y}, Rotation:{rotation}")
    
    response = await altium_bridge.execute_command(
        "set_component_position",
        {
            "designator": cmp_designator,
            "x": x,
            "y": y,
            "rotation": rotation
        }
    )
    
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error setting component position: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to set component position: {error_msg}"})
    
    result = response.get("result", {})
    logger.info(f"Component position set successfully")
    return json.dumps({"success": True, "result": result}, indent=2)

@_tool()
async def move_components(ctx: Context, cmp_designators: list, x_offset: float, y_offset: float, rotation: float = 0) -> str:
    """
    Move components by RELATIVE offset from their current position (not absolute positioning)
    
    IMPORTANT: This moves components BY the offset amount, not TO a position.
    For absolute positioning, use set_component_position instead.
    
    Args:
        cmp_designators (list): List of designators of the components to move (e.g., ["R1", "C5", "U3"])
        x_offset (float): X offset distance in mils (positive = right, negative = left)
        y_offset (float): Y offset distance in mils (positive = up, negative = down)
        rotation (float): New absolute rotation angle in degrees (0-360), if 0 the rotation is not changed
    
    Returns:
        str: JSON object with the result of the move operation
    """
    logger.info(f"Moving components: {cmp_designators} by X:{x_offset}, Y:{y_offset}, Rotation:{rotation}")
    
    # Execute the command in Altium to move components
    response = await altium_bridge.execute_command(
        "move_components",
        {
            "designators": cmp_designators,
            "x_offset": x_offset,
            "y_offset": y_offset,
            "rotation": rotation
        }
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error moving components: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to move components: {error_msg}"})
    
    # Get the result data
    result = response.get("result", {})

    logger.info(f"Components moved successfully")
    return json.dumps({"success": True, "result": result}, indent=2)

@_tool()
async def place_components(ctx: Context, placements: list, board_path: str = "") -> str:
    """
    Place multiple components at absolute positions in a single Altium transaction.

    This is the batch version of set_component_position - prefer it whenever
    placing more than one component, since every tool call is a full round
    trip into Altium. The whole batch is one undo step.

    Coordinate conventions: x/y are in mils relative to the board origin;
    rotation is in degrees counterclockwise.

    Args:
        placements (list): One dict per component:
            - designator (str, required): e.g. "R1"
            - x (float, required): absolute X position in mils
            - y (float, required): absolute Y position in mils
            - rotation (float, optional): absolute rotation in degrees (0-360).
              Omit (or pass -1) to keep the current rotation.
            - layer (str, optional): "top" or "bottom" to set the board side
              (the footprint is mirrored when flipped). Omit to keep the
              current side.
            - locked (bool, optional): lock (true) or unlock (false) the
              component; omit to leave it as it is.
            - designator_text, comment_text (dict, optional): put the
              designator or comment text at {x, y (its anchor, as get_layout
              reports it), rotation, height, stroke_width, visible,
              autoposition ("manual" by default, or top_left ... bottom_right
              as get_layout reports it)} after the move. Omit to let it move
              with the part. (set_designator_positions places by centre.)
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used. Give it whenever more
            than one board is open, so the parts move on the right one.

    Example:
        placements=[{"designator": "R1", "x": 1000, "y": 2000, "rotation": 90},
                    {"designator": "C5", "x": 1050, "y": 2000, "layer": "bottom"}]

    Returns:
        str: JSON object with placed_count, missing_designators, and the final
             x/y/rotation/layer of each placed component as Altium reports them
    """
    logger.info(f"Placing {len(placements)} components")

    # Flatten each placement into a pipe-delimited string
    # ('Designator|X|Y|Rotation|Layer') - the DelphiScript side parses the
    # request line by line, so nested JSON objects are not safe to send
    entries = []
    errors = []
    for idx, placement in enumerate(placements):
        if not isinstance(placement, dict):
            errors.append(f"placements[{idx}] must be an object")
            continue

        designator = str(placement.get("designator", "")).strip()
        x = placement.get("x")
        y = placement.get("y")

        if not designator or x is None or y is None:
            errors.append(f"placements[{idx}] must include designator, x, and y")
            continue
        if "|" in designator:
            errors.append(f"placements[{idx}] designator must not contain '|'")
            continue

        rotation = placement.get("rotation", -1)
        layer = str(placement.get("layer", "") or "").strip().lower()
        if layer not in ("", "top", "bottom"):
            errors.append(f"placements[{idx}] layer must be 'top' or 'bottom'")
            continue

        locked = placement.get("locked")
        if locked is not None and not isinstance(locked, bool):
            errors.append(f"placements[{idx}] locked must be true or false")
            continue
        lock = "" if locked is None else ("1" if locked else "0")
        try:
            texts = "|".join(pcb_layout.text_spec(placement.get(k), f"placements[{idx}] {k}")
                             for k in ("designator_text", "comment_text"))
        except pcb_layout.LayoutError as e:
            errors.append(str(e))
            continue
        try:
            entry = f"{designator}|{float(x)}|{float(y)}|{float(rotation)}|{layer}|{lock}|{texts}"
        except (TypeError, ValueError):
            errors.append(f"placements[{idx}] x, y, and rotation must be numbers")
            continue
        entries.append(entry)

    if errors:
        return json.dumps({"success": False, "error": "; ".join(errors)})
    if not entries:
        return json.dumps({"success": False, "error": "No placements provided"})

    params = {"placements": entries}
    if board_path:
        params["board_path"] = long_path(board_path)
    response = await altium_bridge.execute_command("place_components", params)

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error placing components: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to place components: {error_msg}"})

    result = response.get("result", {})
    logger.info(f"Placed components successfully")
    return json.dumps({"success": True, "result": result}, indent=2)

def _mst_length(points: list) -> float:
    """Minimum-spanning-tree length over (x, y) points (Prim's algorithm)."""
    n = len(points)
    if n < 2:
        return 0.0
    import math
    in_tree = [False] * n
    best = [float("inf")] * n
    best[0] = 0.0
    total = 0.0
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best[i])
        in_tree[u] = True
        total += best[u]
        ux, uy = points[u]
        for v in range(n):
            if not in_tree[v]:
                d = math.dist((ux, uy), points[v])
                if d < best[v]:
                    best[v] = d
    return total

@_tool(read_only=True)
async def get_net_connections(ctx: Context, cmp_designators: list = None, max_pads_per_net: int = 40) -> str:
    """
    Get net connectivity and airline (unrouted connection) lengths for the
    nets touching the given components.

    Use this to plan or score a placement: it shows every pad on each net -
    including pads of OTHER components outside the given set (e.g. an input
    filter the cluster must connect to) - plus the net's minimum-spanning-tree
    airline length in mils. Shorter airlines on critical nets (switching
    loops, input/output capacitors) mean a better placement; non-critical
    nets (enables, set resistors, feedback dividers) may be lengthened to
    buy routing space. Plane nets like GND have many pads and a meaningless
    airline - judge them by proximity to plane connections instead.

    Args:
        cmp_designators (list, optional): Components whose nets to analyze
            (e.g. ["U12", "R42"]). Omit to use the current Altium selection.
        max_pads_per_net (int): Nets with more pads than this (e.g. GND)
            return only pads belonging to the given components, plus the
            total pad_count. Their airline is also skipped. Default 40.

    Returns:
        str: JSON object with one entry per net: pad_count,
             airline_mst_mils (None for large nets), and pads
             [{designator, pin, x, y}, ...] in mils relative to the board
             origin. Large nets set pads_truncated=true.
    """
    logger.info(f"Getting net connections (designators={cmp_designators})")

    params = {}
    if cmp_designators:
        params["designators"] = cmp_designators

    response = await altium_bridge.execute_command("get_net_connections", params)

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting net connections: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to get net connections: {error_msg}"})

    result = response.get("result", {})
    if isinstance(result, str):
        result = json.loads(result)

    target_set = set(cmp_designators or result.get("targets", []))

    # Group the flat pad list by net and compute airline lengths
    nets = {}
    for pad in result.get("pads", []):
        nets.setdefault(pad["net"], []).append(pad)

    out_nets = []
    for name in result.get("net_names", []):
        pads = nets.get(name, [])
        entry = {"net": name, "pad_count": len(pads)}
        if len(pads) <= max_pads_per_net:
            entry["airline_mst_mils"] = round(_mst_length([(p["x"], p["y"]) for p in pads]), 1)
            entry["pads"] = [
                {"designator": p["designator"], "pin": p["pin"], "x": p["x"], "y": p["y"]}
                for p in pads
            ]
        else:
            entry["airline_mst_mils"] = None
            entry["pads_truncated"] = True
            entry["pads"] = [
                {"designator": p["designator"], "pin": p["pin"], "x": p["x"], "y": p["y"]}
                for p in pads
                if not target_set or p["designator"] in target_set
            ]
        out_nets.append(entry)

    out_nets.sort(key=lambda e: (e["airline_mst_mils"] is None, -(e["airline_mst_mils"] or 0)))

    logger.info(f"Net connections: {len(out_nets)} nets")
    return json.dumps({"net_count": len(out_nets), "nets": out_nets}, indent=2)

def _rotate_offset(dx: float, dy: float, degrees: float) -> tuple:
    """Rotate a rotation-0 pad offset counterclockwise by the given angle."""
    import math
    rad = math.radians(degrees)
    return (dx * math.cos(rad) - dy * math.sin(rad),
            dx * math.sin(rad) + dy * math.cos(rad))

@_tool(read_only=True)
async def check_orientation(ctx: Context, cmp_designators: list = None, min_improvement_mils: float = 25) -> str:
    """
    Advisory check: find 2-pad passives whose rotation could be improved.

    For each 2-pad component in the target set, every orthogonal rotation
    (0/90/180/270) is scored in place, summing one term per pad:
    - routable nets (few pads): the net's total airline (MST) length with
      this pad at the candidate position - so a pad that merely slides along
      a pass-through flow (e.g. a bulk cap in a power chain) scores as
      nearly free, while a genuine detour costs its real length
    - plane nets (many pads, e.g. GND): distance to the nearest same-net
      pad - a capacitor's ground-return loop is local, so proximity to the
      IC's GND/thermal pad is what matters
    A component is reported when a different rotation beats the current one
    by at least min_improvement_mils.

    This is advisory, not pass/fail: it measures geometry only and knows
    nothing about net criticality. Suggestions matter for loop-critical
    parts (capacitor ground returns, snubbers, input/output filters) and
    should usually be ignored for parts whose orientation is electrically
    arbitrary (pull-ups, strapping resistors, enables) or where a datasheet
    layout recommendation says otherwise. Weigh suggestions with judgement
    rather than applying them blindly. If a suggestion with
    bbox_changes=true is applied (a 90-degree change alters the body
    outline), re-run check_placement afterwards.

    Args:
        cmp_designators (list, optional): Components to check. Omit to use
            the current Altium selection. Components with more or fewer than
            2 pads are skipped.
        min_improvement_mils (float): Only report components where the best
            rotation improves the connection score by at least this many
            mils (default 25).

    Returns:
        str: JSON object with checked_count and suggestions, each having
             designator, current_rotation, suggested_rotation,
             improvement_mils, bbox_changes, and a per-pad breakdown of
             nearest same-net distances at the current vs suggested rotation.
    """
    import math

    logger.info(f"Checking orientation (designators={cmp_designators})")

    # Resolve the target designators from the selection when not given
    if not cmp_designators:
        sel_resp = await altium_bridge.execute_command("get_selected_components_coordinates", {})
        if not sel_resp.get("success", False):
            return json.dumps({"success": False, "error": sel_resp.get("error", "Unknown error")})
        sel = sel_resp.get("result", [])
        if isinstance(sel, str):
            sel = json.loads(sel)
        cmp_designators = [c["designator"] for c in sel if "designator" in c]
        if not cmp_designators:
            return json.dumps({"success": False, "error": "No components selected and no designators given"})

    pins_resp = await altium_bridge.execute_command("get_component_pins", {"designators": cmp_designators})
    if not pins_resp.get("success", False):
        return json.dumps({"success": False, "error": pins_resp.get("error", "Unknown error")})
    comps = pins_resp.get("result", [])
    if isinstance(comps, str):
        comps = json.loads(comps)

    nets_resp = await altium_bridge.execute_command("get_net_connections", {"designators": cmp_designators})
    if not nets_resp.get("success", False):
        return json.dumps({"success": False, "error": nets_resp.get("error", "Unknown error")})
    net_data = nets_resp.get("result", {})
    if isinstance(net_data, str):
        net_data = json.loads(net_data)

    # net name -> list of (designator, x, y) for every pad on the net
    net_pads = {}
    for p in net_data.get("pads", []):
        net_pads.setdefault(p["net"], []).append((p["designator"], p["x"], p["y"]))

    PLANE_NET_PAD_COUNT = 40  # nets above this are treated as planes

    suggestions = []
    checked = 0
    for comp in comps:
        pins = comp.get("pins", [])
        if len(pins) != 2 or "x" not in comp:
            continue
        mirror = comp.get("layer") == "Bottom Layer"

        def score(rotation):
            """Hybrid per-pad score (see docstring); None if no pad scores."""
            total, detail = 0.0, []
            for pin in pins:
                net = pin.get("net", "")
                cands = [(x, y) for (d, x, y) in net_pads.get(net, [])
                         if d != comp["designator"]] if net else []
                if not cands:
                    continue
                dx = -pin["dx"] if mirror else pin["dx"]
                ox, oy = _rotate_offset(dx, pin["dy"], rotation)
                px, py = comp["x"] + ox, comp["y"] + oy
                if len(cands) > PLANE_NET_PAD_COUNT:
                    value = min(math.dist((px, py), c) for c in cands)
                    metric = "nearest_return_mils"
                else:
                    value = _mst_length(cands + [(px, py)])
                    metric = "net_airline_mils"
                total += value
                detail.append({"pin": pin["name"], "net": net, metric: round(value, 1)})
            return (total, detail) if detail else None

        current = comp.get("rotation", 0) % 360
        cur = score(current)
        if cur is None:
            continue
        checked += 1

        best_rot, best = current, cur
        for r in (0, 90, 180, 270):
            s = score(r)
            if s is not None and s[0] < best[0]:
                best_rot, best = r, s

        improvement = cur[0] - best[0]
        if best_rot != current and improvement >= min_improvement_mils:
            suggestions.append({
                "designator": comp["designator"],
                "current_rotation": current,
                "suggested_rotation": best_rot,
                "improvement_mils": round(improvement, 1),
                "bbox_changes": (best_rot - current) % 180 != 0,
                "current_pads": cur[1],
                "suggested_pads": best[1],
            })

    suggestions.sort(key=lambda s: -s["improvement_mils"])
    logger.info(f"Orientation check: {len(suggestions)} suggestions from {checked} components")
    return json.dumps({
        "checked_count": checked,
        "min_improvement_mils": min_improvement_mils,
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
    }, indent=2)

@_tool(read_only=True)
async def check_placement(ctx: Context, cmp_designators: list = None, clearance_mils: float = 6) -> str:
    """
    Verify component placement: find overlaps and clearance violations.

    Checks each target component against every other component on the same
    side of the board. Bounding boxes (which include silkscreen) are used as
    a fast prefilter; close pairs are then measured precisely with Altium's
    primitive-to-primitive distance, so reported distances are true minimum
    distances between any primitives (pads, silk, etc.) of the two parts.

    Run this after placing components - a screenshot is not verification.

    Args:
        cmp_designators (list, optional): Components to check (e.g. ["U12", "R42"]).
            Omit to check the components currently selected in Altium.
        clearance_mils (float): Minimum allowed primitive-to-primitive distance
            in mils (default 6). Pairs closer than this are reported.

    Returns:
        str: JSON object with checked_count, violation_count, and a violations
             list. Each violation has a/b designators, type
             ("bounding_box_overlap" = the parts' outlines intersect, or
             "clearance" = distance below threshold), distance_mils (0 =
             touching/overlapping copper or silk), overlap sizes when boxes
             intersect, and the other part's x/y position. An empty violations
             list means the placement is clean at the given clearance.
    """
    logger.info(f"Checking placement (designators={cmp_designators}, clearance={clearance_mils})")

    params = {"clearance_mils": clearance_mils}
    if cmp_designators:
        params["designators"] = cmp_designators

    response = await altium_bridge.execute_command("check_placement", params)

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error checking placement: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to check placement: {error_msg}"})

    result = response.get("result", {})
    logger.info(f"Placement check complete: {result.get('violation_count', '?')} violations")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def check_routing(ctx: Context, nets: list = None, angle_tolerance_deg: float = 0.1,
                        max_findings: int = 50) -> str:
    """
    Check the routed copper of the focused PCB for segments that are not 0,
    45 or 90 degrees, and for right-angle and acute corners.

    Run this after changing tracks or vias - a screenshot does not show a
    stub that is 2 degrees off 45. Such stubs come from a diagonal whose end
    was snapped to a grid on its own, or from a track end dragged along
    when a via or component moved. Only tracks with a net that are not part
    of a component or polygon pour count. A corner is where exactly two
    track ends of one net meet on one layer; under a via or pad it is not
    a corner.

    Args:
        nets (list, optional): Check only these nets (exact names, any case).
        angle_tolerance_deg (float): How far a segment may be from a
            multiple of 45 degrees (default 0.1).
        max_findings (int): Most entries listed per kind (default 50). The
            counts always cover every segment.

    Returns:
        str: JSON with clean (true when nothing was found), the segment
             count, segment_angles (orthogonal / 45 / off-angle counts),
             corner_turns (straight / 45 / 90 / acute counts), and the
             off_angle segments and sharp_corners: net, layer, positions in
             mils relative to the board origin, angle or turn in degrees.
    """
    logger.info(f"Checking routing (nets={nets}, tolerance={angle_tolerance_deg})")

    response = await altium_bridge.execute_command("get_routing_geometry", {})
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error reading routing: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to read the routing: {error_msg}"})

    geo = routing.parse_geometry(str(response.get("result", "")))
    report = routing.check(geo, angle_tolerance_deg, nets)
    report["clean"] = not report["off_angle"] and not report["sharp_corners"]
    report["routed_arcs"] = len(geo["arcs"])
    for key in ("off_angle", "sharp_corners"):
        report[f"{key}_count"] = len(report[key])
        report[key] = report[key][:max(0, max_findings)]
    logger.info(f"Routing check: {report['off_angle_count']} off-angle, "
                f"{report['sharp_corners_count']} sharp corners in {report['segments']} segments")
    return json.dumps(report, indent=2)

@_tool()
async def place_polygon_pour(ctx: Context, outline: list, layer: str, net: str = "", name: str = "",
                             hatch: str = "solid", pour_over_same_net: bool = True,
                             remove_dead_copper: bool = True, track_width_mils: float = 0,
                             grid_mils: float = 0, board_path: str = "") -> str:
    """
    Add a polygon pour (copper area, e.g. a ground pour) to a PCB and pour it.

    The pour keeps the board's clearance rules from other nets and joins
    same-net pads and tracks as the polygon connect rule says. Cut holes in
    it with place_cutout.

    Args:
        outline (list): Corners [[x, y], ...] in mils relative to the board
            origin, in order around the shape (at least three; it closes by
            itself). Straight edges only.
        layer (str): Signal layer, by its stack name or Altium's name, e.g.
            "Top Layer".
        net (str): Net to pour; it must exist on the board. Empty for an
            unconnected pour.
        name (str): Polygon name; empty lets Altium name it.
        hatch (str): "solid" (default), "hatched_45", "hatched_90", or
            "none" (outline only).
        pour_over_same_net (bool): Pour over same-net tracks and pads.
        remove_dead_copper (bool): Remove islands not connected to the net.
        track_width_mils (float): Hatch track width; 0 for Altium's default.
        grid_mils (float): Hatch grid; 0 for Altium's default.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with the polygon's name, layer, net and what it poured:
             poured_regions, one per separate piece of solid copper, and
             hatch_tracks for a hatched pour. When nothing was poured it
             says why it may be: no room, or every piece touched nothing on
             its net and was removed as dead copper.
    """
    import pcb_shapes

    board_path = long_path(board_path)
    points, error = pcb_shapes.outline_points(outline)
    if error:
        return json.dumps({"success": False, "error": error})
    if hatch not in ("solid", "hatched_45", "hatched_90", "none"):
        return json.dumps({"success": False, "error": "hatch is solid, hatched_45, hatched_90 or none"})
    if track_width_mils < 0 or grid_mils < 0:
        return json.dumps({"success": False, "error": "track_width_mils and grid_mils cannot be negative"})
    logger.info(f"Placing a polygon pour on {layer} ({net or 'no net'})")
    response = await altium_bridge.execute_command("create_polygon_pour", {
        "outline": pcb_shapes.encode(points), "layer": layer, "net": net, "name": name,
        "hatch": hatch, "pour_over_same_net": "true" if pour_over_same_net else "false",
        "remove_dead_copper": "true" if remove_dead_copper else "false",
        "track_size": int(round(track_width_mils * pcb_shapes.COORD_PER_MIL)),
        "grid": int(round(grid_mils * pcb_shapes.COORD_PER_MIL)), "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to add the polygon: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    if hatch != "none" and not result.get("poured_regions") and not result.get("hatch_tracks"):
        result["warning"] = ("Nothing was poured: there is no room inside the outline, or every piece "
                             "touched nothing on its net and was removed as dead copper "
                             "(remove_dead_copper).")
    return json.dumps(result, indent=2)

@_tool()
async def place_cutout(ctx: Context, outline: list, kind: str = "polygon", layer: str = "",
                       board_path: str = "") -> str:
    """
    Add a cutout to a PCB: a hole in polygon pours, or in the board itself.

    kind "polygon" keeps polygon pours out of the area on one signal layer
    (or on every signal layer with layer "all"), as Altium's Polygon Pour
    Cutout does. kind "board" cuts the area out of the board (a slot or
    opening). Either way the polygons the cutout overlaps are repoured.

    Args:
        outline (list): Corners [[x, y], ...] in mils relative to the board
            origin, in order around the shape (at least three).
        kind (str): "polygon" (default) or "board".
        layer (str): For a polygon cutout, the signal layer ("Top Layer",
            a stack name) or "all". Ignored for a board cutout.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with how many cutout regions were added and how many
             polygons were repoured.
    """
    import pcb_shapes

    board_path = long_path(board_path)
    points, error = pcb_shapes.outline_points(outline)
    if error:
        return json.dumps({"success": False, "error": error})
    if kind not in ("polygon", "board"):
        return json.dumps({"success": False, "error": "kind is polygon or board"})
    if kind == "polygon" and not layer.strip():
        return json.dumps({"success": False, "error": "a polygon cutout needs a layer, or \"all\""})
    logger.info(f"Placing a {kind} cutout")
    response = await altium_bridge.execute_command("create_cutout", {
        "outline": pcb_shapes.encode(points), "kind": kind, "layer": layer, "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to add the cutout: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

@_tool()
async def repour_polygons(ctx: Context, names: list = None, net: str = "", layer: str = "",
                          area: list = None, include_shelved: bool = False, board_path: str = "") -> str:
    """
    Repour polygon pours on a PCB: all of them, or the ones the filters pick.

    Repour after moving parts, tracks or vias through a pour, or after
    changing clearance or polygon connect rules; Altium does not repour by
    itself. The whole repour is one undo step.

    Filters combine; with none, every polygon on the board is repoured.

    Args:
        names (list): Polygon names, any case. Names need not be unique (a
            "GND" pour on each layer is common), so add layer to pick one.
        net (str): Only polygons on this net.
        layer (str): Only polygons on this signal layer, by stack name or
            Altium's name ("Top Layer").
        area (list): [x1, y1, x2, y2] in mils from the board origin: only
            polygons overlapping it, e.g. around an edit. Overlapping, so a
            whole-board pour counts for any area on it.
        include_shelved (bool): Also repour shelved polygons, which
            unshelves them. By default they are left shelved and listed.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with how many polygons were repoured and, for each, its
             name, layer, net and poured_regions (separate pieces of
             copper) and hatch_tracks. shelved_skipped lists the shelved
             polygons that matched and were left alone.
    """
    import pcb_shapes

    board_path = long_path(board_path)
    names, encoded, error = pcb_shapes.polygon_names(names)
    if error:
        return json.dumps({"success": False, "error": error})
    box, error = pcb_shapes.area_box(area)
    if error:
        return json.dumps({"success": False, "error": error})
    logger.info(f"Repouring polygons (names={names or 'any'}, net={net or 'any'}, layer={layer or 'any'})")
    response = await altium_bridge.execute_command("repour_polygons", {
        "names": encoded, "net": net, "layer": layer, "area": box,
        "include_shelved": "true" if include_shelved else "false", "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to repour: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    missing = pcb_shapes.unmatched_names(names, result)
    if missing:
        result["names_not_matched"] = missing
    if not result.get("repoured") and not result.get("shelved_skipped"):
        result["warning"] = "No polygon matched the filters."
    return json.dumps(result, indent=2)

# Change the design: a timeout leaves their outcome unknown, so no blind retry
MUTATING_COMMANDS.update({"create_polygon_pour", "create_cutout", "repour_polygons"})

@_tool(read_only=True)
async def get_pour_report(ctx: Context, names: list = None, net: str = "", layer: str = "",
                          area: list = None, sliver_sq_mils: float = 100, board_path: str = "") -> str:
    """
    Check what polygon pours poured: islands, slivers, and pours with no net
    or no copper. Read only; repour first (repour_polygons) to check the
    copper as the rules now make it.

    Each polygon is listed with its pieces of solid copper and their sizes,
    and flags:
      * islands - poured into more than one separate piece;
      * slivers - a piece smaller than sliver_sq_mils;
      * no_net - the polygon has no net (dead copper);
      * no_copper - poured nothing (no room, or all removed as dead copper);
      * shelved - shelved, so it has no copper now.
    A piece's area is its outline, holes not subtracted: an upper bound.

    Args:
        names, net, layer, area: Filters, as for repour_polygons; with none,
            every polygon on the board.
        sliver_sq_mils (float): A piece below this area is a sliver
            (default 100 sq mil).
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with each polygon (name, layer, net, pieces, smallest
             piece, copper area, slivers, flags) and the flagged ones first.
    """
    import pcb_shapes

    names, encoded, error = pcb_shapes.polygon_names(names)
    if error:
        return json.dumps({"success": False, "error": error})
    box, error = pcb_shapes.area_box(area)
    if error:
        return json.dumps({"success": False, "error": error})
    if (not isinstance(sliver_sq_mils, (int, float)) or isinstance(sliver_sq_mils, bool)
            or not sliver_sq_mils >= 0 or sliver_sq_mils == float("inf")):
        return json.dumps({"success": False, "error": "sliver_sq_mils must be a number of at least 0"})
    response = await altium_bridge.execute_command("pour_report", {
        "names": encoded, "net": net, "layer": layer, "area": box,
        # Altium reads it as a 32-bit integer; no piece of copper is near this
        "sliver_sq_mils": int(round(min(sliver_sq_mils, 2e9))), "board_path": long_path(board_path)})
    if not response.get("success", False):
        return json.dumps({"success": False, "error": response.get("error", "unknown error")})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    polys = result.get("polygons", [])
    polys.sort(key=lambda p: (not p.get("flags"), p.get("layer", ""), p.get("name", "")))
    result["flagged"] = sum(1 for p in polys if p.get("flags"))
    return json.dumps(result, indent=2, ensure_ascii=False)

async def _edit_pcb_objects(params: dict, board_path: str) -> str:
    response = await altium_bridge.execute_command("edit_pcb_objects", {**params, "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False, "error": response.get("error", "unknown error")})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    if not result.get("matched"):
        result["warning"] = "No object matched the filters."
    elif result.get("matched", 0) > len(result.get("objects", [])):
        result["note"] = f"objects lists the first {len(result.get('objects', []))} of {result['matched']}."
    return json.dumps(result, indent=2)

@_tool()
async def delete_pcb_objects(ctx: Context, kinds: list = None, net: str = "", layer: str = "",
                             area: list = None, designators: list = None, margin_mils: float = 40,
                             include_locked: bool = False, dry_run: bool = False, board_path: str = "") -> str:
    """
    Delete copper from a PCB: tracks, arcs, vias, polygon pours, copper
    regions, fills or pour cutouts. Use it to rip up routing.

    Objects are picked by every filter given: kinds, net, layer and area,
    or a group's copper by designators. At least one must narrow the match;
    deleting all the copper on the board is refused. Only copper is ever
    touched: never pads, anything belonging to a component, keepouts, or
    objects on other layers (board outline, silkscreen, mechanical). The
    copper a pour poured goes with its pour. Locked objects are left alone
    and listed, unless include_locked. One undo step in Altium. Use dry_run
    first to see what would go.

    Deleting copper inside a pour leaves the pour as it was: repour it.
    Remove a via stitching group with Altium's own command rather than
    here, so the group goes too.

    Args:
        kinds (list): Some of "track", "arc", "via", "polygon", "region",
            "fill", "cutout" (a pour cutout). Empty: all but cutouts.
        net (str): Only objects on this net.
        layer (str): Only objects on this copper layer, by stack name or
            Altium's name ("Top Layer"); a via matches every layer it spans.
        area (list): [x1, y1, x2, y2] in mils from the board origin; only
            objects lying inside it.
        designators (list): Rip up a group's copper, as get_layout lists it:
            what lies inside the group's extent (grown by margin_mils, or
            area when given) plus the copper of its own nets (nets with no
            pad on another part) wherever it runs.
        margin_mils (float): See designators (default 40).
        include_locked (bool): Delete locked objects too.
        dry_run (bool): List what matches without deleting.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with matched, deleted, the first 50 objects (kind, net,
             layer, position in mils), and locked_skipped with the first
             20 locked objects left alone.
    """
    import pcb_edit

    board_path = long_path(board_path)
    params, error = pcb_edit.filters(kinds, net, layer, area, designators, margin_mils, include_locked)
    if error:
        return json.dumps({"success": False, "error": error})
    if not pcb_edit.narrowed(kinds, net, layer, area, designators):
        return json.dumps({"success": False, "error": (
            "Give at least one filter (kinds, net, layer, area or designators): this would delete all the "
            "copper on the board.")})
    logger.info(f"Deleting PCB objects {params} (dry run: {dry_run})")
    return await _edit_pcb_objects({**params, "action": "delete", "dry_run": "true" if dry_run else "false"},
                                   board_path)

@_tool()
async def modify_pcb_objects(ctx: Context, kinds: list = None, net: str = "", layer: str = "",
                             area: list = None, designators: list = None, margin_mils: float = 40,
                             width_mils: float = None, via_diameter_mils: float = None,
                             via_hole_mils: float = None, new_net: str = None, new_layer: str = "",
                             include_locked: bool = False, dry_run: bool = False, board_path: str = "") -> str:
    """
    Change copper on a PCB: the width of tracks and arcs, the size of vias,
    the net, or the layer.

    Objects are picked as for delete_pcb_objects, and at least one filter
    must narrow the match. Each change applies where it makes sense:
    width_mils to tracks and arcs, via_diameter_mils and via_hole_mils to
    vias, new_layer to tracks, arcs, fills and regions, new_net to all ("" removes
    the net). A via whose hole would not fit inside its diameter is left as
    it is and counted. A polygon given another net pours again. Locked
    objects are left alone unless include_locked. One undo step in Altium.

    Changed copper inside a pour leaves the pour as it was: repour it, and
    run DRC after changing widths or via sizes.

    Args:
        kinds, net, layer, area, designators, margin_mils, include_locked:
            The filters, as for delete_pcb_objects.
        width_mils (float): New width of tracks and arcs.
        via_diameter_mils, via_hole_mils (float): New via size; either alone
            is checked against each via's other size.
        new_net (str): New net; "" removes the net; leave out to keep it.
        new_layer (str): Copper layer to move tracks, arcs, fills and
            regions to.
        dry_run (bool): List what matches without changing it.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with matched, changed, unchanged (matched objects none of
             the changes applies to), vias_left_as_hole_would_not_fit,
             locked_skipped, and the first 50 objects as they were.
    """
    import pcb_edit

    board_path = long_path(board_path)
    params, error = pcb_edit.filters(kinds, net, layer, area, designators, margin_mils, include_locked)
    if error:
        return json.dumps({"success": False, "error": error})
    if not pcb_edit.narrowed(kinds, net, layer, area, designators):
        return json.dumps({"success": False, "error": (
            "Give at least one filter (kinds, net, layer, area or designators): this would change all the "
            "copper on the board.")})
    change, error = pcb_edit.changes(width_mils, via_diameter_mils, via_hole_mils, new_net, new_layer)
    if error:
        return json.dumps({"success": False, "error": error})
    logger.info(f"Modifying PCB objects {params}: {change} (dry run: {dry_run})")
    return await _edit_pcb_objects({**params, **change, "action": "modify",
                                    "dry_run": "true" if dry_run else "false"}, board_path)

MUTATING_COMMANDS.add("edit_pcb_objects")

@_tool()
async def route_track(ctx: Context, net_name: str, layer: str, width_mils: float, points: list,
                      diagonal_first: bool = False, corner_chamfer_mils: float = 0,
                      dry_run: bool = False) -> str:
    """
    Add a routed track through waypoints on the focused PCB, every segment
    exactly 0, 45 or 90 degrees.

    Between two waypoints that are not in line, the track takes a straight
    leg and a 45 degree diagonal. The diagonal is computed from its own
    start point, so its x and y runs are equal: nothing is snapped on its
    own, which is what leaves stubs a few degrees off 45. Use this instead
    of placing track segments from run_altium_script, and run
    check_routing afterwards. It adds tracks only: no vias, and no DRC.

    Args:
        net_name (str): Net of the track; it must exist on the board.
        layer (str): Layer name as get_component_pins and check_routing
            report it, e.g. "Top Layer" or "Bottom Layer".
        width_mils (float): Track width in mils.
        points (list): Waypoints [[x, y], ...] in mils relative to the
            board origin, at least two: usually a pad centre, any bends,
            and the far pad or via centre.
        diagonal_first (bool): Put the diagonal at the start of each
            stretch instead of its end (default: straight, then diagonal).
        corner_chamfer_mils (float): Cut each right-angle corner between two
            straight legs with a 45 degree segment, setting back this far on
            each leg (at most half the shorter leg). 0 leaves them square.
        dry_run (bool): Only compute and return the segments.

    Returns:
        str: JSON with the segments in mils ([x1, y1, x2, y2]) and, unless
             dry_run, how many tracks were added.
    """
    logger.info(f"Routing {net_name} on {layer} through {len(points or [])} points")

    def coord(v):
        return int(round(float(v) * routing.COORD_PER_MIL))

    try:
        waypoints = [(coord(p[0]), coord(p[1])) for p in points]
    except (TypeError, ValueError, IndexError):
        return json.dumps({"success": False, "error": "points must be a list of [x, y] pairs in mils"})
    if len(waypoints) < 2:
        return json.dumps({"success": False, "error": "points needs at least two waypoints"})
    if width_mils <= 0:
        return json.dumps({"success": False, "error": "width_mils must be positive"})

    segments = routing.route(waypoints, diagonal_first, coord(corner_chamfer_mils))
    if not segments:
        return json.dumps({"success": False, "error": "the waypoints are all the same point"})
    result = {"segments_mils": [[routing.to_mils(v) for v in s] for s in segments]}
    if dry_run:
        return json.dumps({"success": True, "dry_run": True, **result}, indent=2)

    response = await altium_bridge.execute_command("create_route_tracks", {
        "net_name": net_name,
        "layer": layer,
        "width": coord(width_mils),
        "segments": ";".join("|".join(str(v) for v in s) for s in segments),
    })
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error routing track: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to add the track: {error_msg}", **result})

    created = response.get("result", {})
    if isinstance(created, dict):
        result.update({k: v for k, v in created.items() if k != "success"})
    return json.dumps({"success": True, **result}, indent=2)


# Board snapshot for the silkscreen tools. An export takes a few seconds, so
# views and option queries reuse it. Moves made through
# set_designator_positions are written into it, check_silkscreen always
# refreshes it, and it is re-exported once older than SILK_SNAPSHOT_SECONDS.
# Edits made by hand in Altium are seen after that, or with refresh=True.
SILK_SNAPSHOT_SECONDS = 900
_silk_snapshot = {"board": None, "time": 0.0}

async def _silk_board(refresh: bool = False):
    """The board's silkscreen geometry, exported from Altium when needed."""
    snap = _silk_snapshot
    if refresh or snap["board"] is None or time.time() - snap["time"] > SILK_SNAPSHOT_SECONDS:
        response = await altium_bridge.execute_command("export_silkscreen_data", {})
        if not response.get("success", False):
            raise RuntimeError(response.get("error", "Unknown error"))
        result = response.get("result", {})
        if isinstance(result, str):
            result = json.loads(result)
        text = read_altium_text(result["file"])
        snap["board"], snap["time"] = silkscreen.Board(text), time.time()
    return snap["board"]

async def _check_silk_drc(designators: list = None) -> dict:
    params = {"designators": designators} if designators else {}
    response = await altium_bridge.execute_command("check_silkscreen", params)
    if not response.get("success", False):
        raise RuntimeError(response.get("error", "Unknown error"))
    result = response.get("result", {})
    return json.loads(result) if isinstance(result, str) else result

def _group_silk_violations(drc: dict) -> dict:
    """Altium's per-object silk violations as {designator: [description]}."""
    grouped = {}
    for v in drc.get("violations", []):
        what = v.get("object", "")
        if v.get("owner"):
            what += f" {v['owner']}" + (f"-{v['detail']}" if v.get("object") == "Pad" and v.get("detail") else "")
        elif v.get("detail"):
            what += f" '{v['detail']}'"
        if "distance_mils" in v:
            what += f" ({v['distance_mils']} mil)"
        grouped.setdefault(v["designator"], []).append(f"{v['rule']}: {what}")
    return dict(sorted(grouped.items()))

def _silk_side(side, designators, board) -> str:
    """'T' or 'B' from a side argument, else from the first designator."""
    if side:
        s = str(side).strip().lower()
        if s in ("top", "t"):
            return "T"
        if s in ("bottom", "b", "bot"):
            return "B"
        raise ValueError("side must be 'top' or 'bottom'")
    for d in designators or ():
        c = board.components.get(d)
        if c is not None and c.side in ("T", "B"):
            return c.side
    return "T"

def _current_boxes(board, side=None) -> dict:
    return {d: (c.text_box, c.text_rotation) for d, c in board.components.items()
            if c.visible and c.side in ("T", "B") and (side is None or c.side == side)}

def _focus_around(board, designators, side, min_size=300.0):
    """Area showing the parts, their current labels, and some surroundings."""
    bb = silkscreen.focus_box(board, designators, side)
    if bb is None:
        return None
    for d in designators:
        c = board.components.get(d)
        if c is not None and c.side == side and c.visible:
            bb = silkscreen._union(bb, c.text_box)
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    hw, hh = max((bb[2] - bb[0]) / 2, min_size / 2), max((bb[3] - bb[1]) / 2, min_size / 2)
    return (cx - hw, cy - hh, cx + hw, cy + hh)

def _box_info(rect, rotation) -> dict:
    return {"x": round((rect[0] + rect[2]) / 2, 2), "y": round((rect[1] + rect[3]) / 2, 2),
            "rotation": round(rotation) % 360,
            "width": round(rect[2] - rect[0], 1), "height": round(rect[3] - rect[1], 1)}

SILK_VIEW_LEGEND = ("grid lines labelled in mils (board origin); copper = solder mask openings "
                    "(dark = vias); white = silk; grey names = part centres; yellow = designators; "
                    "red = designators with problems; cyan = the designators asked about, with a line "
                    "to their part; green numbered = options")

AUTOPLACE_POSITIONS = ["TopCenter", "CenterRight", "BottomCenter", "CenterLeft",
                       "TopLeft", "TopRight", "BottomLeft", "BottomRight"]
AUTOPLACE_ROTATIONS = {"component": 0, "horizontal": 1, "along_side": 2,
                       "along_axis": 3, "along_pins": 4, "klc": 5}
# The script's GUI defaults, used when there is no saved settings file
AUTOPLACE_DEFAULTS = {
    "failed_action": "center", "avoid_vias": True, "rotation_strategy": 5,
    "try_altered_rotation": True, "second_pass": True, "unhide_all": False,
    "auto_hide": [], "text_height_mils": 31.496, "stroke_width_mils": 5.906,
    "position_delta_mils": 16.535, "positions": AUTOPLACE_POSITIONS[:4],
}

def _to_mils(text: str):
    """'30mil', '0.8mm', '0.05in' or a bare number (mils) -> mils."""
    m = re.match(r"\s*([-+]?\d*\.?\d+)\s*(mil|mm|in)?\s*$", str(text), re.I)
    if not m:
        return None
    value, unit = float(m.group(1)), (m.group(2) or "mil").lower()
    return value * {"mil": 1.0, "mm": 1000 / 25.4, "in": 1000.0}[unit]

def _saved_autoplacer_settings() -> dict:
    """Options saved by the AutoPlaceSilkscreen GUI (AutoPlaceSilkscreen.ini in
    Altium's application data folder), in this tool's terms. Empty if none."""
    import configparser
    files = glob.glob(os.path.join(os.environ.get("APPDATA", ""), "Altium", "*", "AutoPlaceSilkscreen.ini"))
    if not files:
        return {}
    ini = configparser.ConfigParser()
    try:
        ini.read(max(files, key=os.path.getmtime))
        g = ini["General"]
    except (configparser.Error, KeyError):
        return {}
    flag = lambda key, default: g.get(key, "1" if default else "0").strip() in ("1", "true", "True")
    out = {
        "failed_action": {"0": "center", "1": "hide", "2": "restore"}.get(g.get("FailedPlacementOptions", "0"), "center"),
        "avoid_vias": flag("AvoidVias", True),
        "rotation_strategy": int(g.get("RotationStrategy", "5")),
        "try_altered_rotation": flag("TryAlteredRotation", True),
        "second_pass": flag("WiggleEnabled", True),
        "unhide_all": flag("UnhideAllDesignators", False),
        "auto_hide": [x.strip() for x in g.get("AutoHideList", "").split(",") if x.strip()]
                     if flag("AutoHideEnabled", False) else [],
        "text_height_mils": (_to_mils(g.get("FixedSize", "")) or 0) if flag("FixedSizeEnabled", True) else 0,
        "stroke_width_mils": (_to_mils(g.get("FixedWidth", "")) or 0) if flag("FixedWidthEnabled", True) else 0,
        "position_delta_mils": _to_mils(g.get("PositionDelta", "")) or AUTOPLACE_DEFAULTS["position_delta_mils"],
        "positions": [p for i, p in enumerate(AUTOPLACE_POSITIONS, 1) if flag(f"Position{i}", i <= 4)],
    }
    return out

@_tool()
async def auto_place_silkscreen(ctx: Context, designators: list = None, selected_only: bool = False,
                                failed_action: str = None, avoid_vias: bool = None,
                                rotation_strategy: str = None, positions: list = None,
                                try_altered_rotation: bool = None, second_pass: bool = None,
                                unhide_all: bool = None, auto_hide: list = None,
                                allow_under: list = None, text_height_mils: float = None,
                                stroke_width_mils: float = None, position_delta_mils: float = None,
                                component_outline_layer: str = None,
                                use_saved_settings: bool = True) -> str:
    """
    Bulk-place designators with the Silkscreen Auto Placer script - step 1 of
    silkscreen cleanup. Then finish by hand with the other silkscreen tools.

    Runs a headless port of the AutoPlaceSilkscreen script inside Altium. For
    every part, smallest first, it tries the enabled autopositions on an
    offset grid with shrinking text, then a retry pass (rotation flip, wider
    grid) and a 2nd-pass "wiggle" search up to 100 mil out. It avoids pads,
    component bodies, other silk and (optionally) vias, and keeps the Board
    Outline Clearance rule's distance from the board edge. Typically 80-95%
    of designators end up placed; the rest are handled per failed_action.
    Everything is one undo step. A whole-board run moves every designator:
    ask the user to save the board first.

    It is fast and good at the bulk, but it does not judge readability:
    labels can land far from their part (2nd-pass label blocks), next to a
    neighbour, or over open vias when avoid_vias is off. Always follow up:
    1. check_silkscreen - lists what needs work (failed designators, DRC,
       ambiguous or distant labels).
    2. view_silkscreen / get_designator_options - look at each problem area
       and find legal spots.
    3. set_designator_positions - place them (dry_run first for neighbours).

    Options default to the settings last saved by the script's GUI
    (AutoPlaceSilkscreen.ini) when use_saved_settings is on, else the GUI's
    defaults. Any argument given here overrides them.

    Args:
        designators (list, optional): Only place these designators. Omit for
            the whole board (or the selection with selected_only).
        selected_only (bool): Place only the components selected in Altium.
        failed_action (str): What to do with designators that could not be
            placed: "center" (centre them on their part, where
            check_silkscreen flags them), "hide", or "restore" (put them back
            where they were).
        avoid_vias (bool): Treat vias as obstacles.
        rotation_strategy (str): "component", "horizontal", "along_side",
            "along_axis", "along_pins" or "klc" (KLC style).
        positions (list): Autopositions to try, in order: TopCenter,
            CenterRight, BottomCenter, CenterLeft, TopLeft, TopRight,
            BottomLeft, BottomRight.
        try_altered_rotation (bool): Also try the text turned 90 degrees.
        second_pass (bool): Run the wider "wiggle" search for failures.
        unhide_all (bool): Show hidden designators first so they get placed.
        auto_hide (list): Designator prefixes to hide instead of placing,
            e.g. ["TP", "MT", "FID"]; [] hides none.
        allow_under (list): Designators whose bodies silk may cover.
        text_height_mils (float): Fixed text height; 0 = size from the part
            (shrinks to 25 mil when needed).
        stroke_width_mils (float): Fixed stroke width; 0 = from the height.
        position_delta_mils (float): Extra gap between autoposition and part.
        component_outline_layer (str): Mechanical layer holding component
            bodies (default "Mechanical 13" or a "Component Outline" layer).
        use_saved_settings (bool): Start from the GUI's saved settings.

    Returns:
        str: JSON with placed/failed counts per pass, the failed designators,
             the settings used and the next step.
    """
    settings = dict(AUTOPLACE_DEFAULTS)
    source = "script defaults"
    if use_saved_settings:
        saved = _saved_autoplacer_settings()
        if saved:
            settings.update(saved)
            source = "saved GUI settings (AutoPlaceSilkscreen.ini)"
    overrides = {"failed_action": failed_action, "avoid_vias": avoid_vias,
                 "try_altered_rotation": try_altered_rotation, "second_pass": second_pass,
                 "unhide_all": unhide_all, "auto_hide": auto_hide,
                 "text_height_mils": text_height_mils, "stroke_width_mils": stroke_width_mils,
                 "position_delta_mils": position_delta_mils, "positions": positions}
    settings.update({k: v for k, v in overrides.items() if v is not None})
    if rotation_strategy is not None:
        key = str(rotation_strategy).strip().lower().replace(" ", "_")
        if key.isdigit() and 0 <= int(key) <= 5:
            settings["rotation_strategy"] = int(key)
        elif key in AUTOPLACE_ROTATIONS:
            settings["rotation_strategy"] = AUTOPLACE_ROTATIONS[key]
        else:
            return json.dumps({"success": False, "error": f"rotation_strategy must be one of {list(AUTOPLACE_ROTATIONS)}"})
    if settings["failed_action"] not in ("center", "hide", "restore"):
        return json.dumps({"success": False, "error": "failed_action must be center, hide or restore"})
    order = {p.lower(): p for p in AUTOPLACE_POSITIONS}
    try:
        settings["positions"] = [order[str(p).strip().lower()] for p in settings["positions"]]
    except KeyError as e:
        return json.dumps({"success": False, "error": f"unknown position {e}; use {AUTOPLACE_POSITIONS}"})
    if not settings["positions"]:
        return json.dumps({"success": False, "error": "enable at least one position"})

    params = {k: v for k, v in settings.items() if k not in ("auto_hide", "positions",
                                                             "text_height_mils", "stroke_width_mils")}
    params.update({
        "positions": settings["positions"],
        "fixed_size_mils": float(settings["text_height_mils"] or 0),
        "fixed_width_mils": float(settings["stroke_width_mils"] or 0),
        "selected_only": bool(selected_only),
    })
    # Arrays are only sent when non-empty: the script reads '"key": []' as absent
    for key, value in (("designators", designators), ("auto_hide", settings["auto_hide"]),
                       ("allow_under", allow_under)):
        if value:
            params[key] = [str(v) for v in value]
    if component_outline_layer:
        params["outline_layer"] = component_outline_layer

    logger.info(f"auto_place_silkscreen ({source}): {params}")
    response = await altium_bridge.execute_command("auto_place_silkscreen", params, timeout=1800)
    _silk_snapshot["board"] = None      # every designator may have moved
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Auto placer failed: {response.get('error', 'Unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        result = json.loads(result)
    if not result.get("outline_layer_found"):
        result["warning"] = ("no component outline layer found - component bodies were not treated "
                             "as obstacles; pass component_outline_layer")
    result.pop("outline_layer_found", None)
    names = {v: k for k, v in AUTOPLACE_ROTATIONS.items()}
    result["settings"] = dict(settings, rotation_strategy=names[settings["rotation_strategy"]], source=source)
    result["next"] = ("Run check_silkscreen, then fix what it lists with view_silkscreen, "
                      "get_designator_options and set_designator_positions.")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def check_silkscreen(ctx: Context, designators: list = None, preview: bool = False):
    """
    Find the designators that need work. Start and finish silkscreen cleanup
    with this.

    Two parts:
    - drc: each designator is tested with Altium's own Silk To Silk
      Clearance, Silk To Solder Mask Clearance and Board Outline Clearance
      rules (Rule.ActualCheck, the test a batch DRC runs) against nearby
      silk, pads, vias, mask objects and the board edge. Ground truth for
      fabrication.
    - quality: problems DRC does not flag - text far from its part or closer
      to another part of the same type, over another part's body or under its
      own (e.g. left on top of its part by an auto-placer), off the board, or
      rotated to read upside down.
    needs_attention lists every designator with either kind of problem;
    hidden lists designators whose display is turned off.

    Args:
        designators (list, optional): Designators to check. Omit for all.
        preview (bool): Also return a whole-board view with the problem
            designators in red (default False).

    Returns:
        JSON with needs_attention, drc and quality (both grouped by
        designator), hidden, plus optional view image(s).
    """
    try:
        drc = await _check_silk_drc(designators)
        board = await _silk_board(refresh=True)
    except Exception as e:
        return json.dumps({"success": False, "error": f"Failed to check silkscreen: {e}"})

    scope = designators or list(board.components)
    boxes = {d: b for d, b in _current_boxes(board).items() if d in scope}
    quality = {d: p for d, p in silkscreen.evaluate(board, boxes, geometry=False).items() if p}
    # Labels in an intact label block are linked to their parts: distance and
    # "reads as" problems do not apply. A disturbed block is a problem itself.
    blocks = []
    for rec in _load_blocks(board.file):
        broken = label_blocks.check_record(board, rec)
        blocks.append({"parts": rec["members"], "link": rec["link"], "intact": not broken,
                       **({"marker": rec["marker"]} if rec.get("marker") else {}),
                       **({"problems": broken} if broken else {})})
        for d in rec["members"]:
            if d not in scope:
                continue
            if broken:
                quality.setdefault(d, []).append("label block broken: " + "; ".join(broken))
            elif d in quality:
                keep = [p for p in quality[d] if not label_blocks.is_association_problem(p)]
                if keep:
                    quality[d] = keep
                else:
                    del quality[d]
    grouped = _group_silk_violations(drc)
    attention = sorted(set(grouped) | set(quality))
    summary = {
        "board": board.file,
        "checked_count": drc.get("checked_count"),
        "needs_attention_count": len(attention),
        "needs_attention": attention,
        "drc_violation_count": drc.get("violation_count"),
        "drc": grouped,
        "quality": quality,
        "hidden": sorted(d for d in scope if d in board.components and not board.components[d].visible),
    }
    if blocks:
        summary["label_blocks"] = blocks
    if drc.get("missing_designators"):
        summary["missing_designators"] = drc["missing_designators"]
    text = json.dumps(summary, indent=2)
    if not preview:
        return text
    images = []
    for side in ("T", "B"):
        side_boxes = {d: b for d, b in boxes.items() if board.components[d].side == side}
        if side_boxes:
            images.append(MCPImage(data=silkscreen.render_view(
                board, side_boxes, side=side, problems=attention, max_px=1600), format="png"))
    return [text] + images

@_tool(read_only=True)
async def view_silkscreen(ctx: Context, designators: list = None, region: list = None,
                          side: str = None, grid_mils: float = 0, refresh: bool = False):
    """
    Look at the silkscreen around some designators or in a region.

    Renders one board side cleanly - no Altium UI, no copper clutter - with a
    grid labelled in mils (board-origin coordinates, the same ones
    set_designator_positions takes), so a free spot can be read straight off
    the image. Shows solder mask openings, silk, part names at their centres,
    every designator box, and marks designators whose current spot has a
    problem in red. The designators asked about are cyan with a line to
    their part.

    Uses the board snapshot (refreshed by check_silkscreen, updated by
    set_designator_positions, at most 5 minutes old). Pass refresh=True after
    editing the board by hand in Altium.

    Args:
        designators (list, optional): Zoom to these parts and highlight them.
        region (list, optional): [x_min, y_min, x_max, y_max] in mils to show
            instead. Omit both for the whole board.
        side (str, optional): "top" or "bottom". Default: the side of the
            first designator, else top.
        grid_mils (float): Grid spacing; 0 picks one to suit the zoom.
        refresh (bool): Re-export the board from Altium first.

    Returns:
        JSON (view bounds, grid, legend, problems of the designators in view)
        plus the image.
    """
    try:
        board = await _silk_board(refresh)
        s = _silk_side(side, designators, board)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})

    if region:
        if len(region) != 4:
            return json.dumps({"success": False, "error": "region must be [x_min, y_min, x_max, y_max]"})
        x0, y0, x1, y1 = (float(v) for v in region)
        focus, margin = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), 0.0
    elif designators:
        focus, margin = _focus_around(board, designators, s), 60.0
        if focus is None:
            return json.dumps({"success": False, "error": f"none of {designators} is on the {s} side"})
    else:
        focus, margin = None, 40.0

    boxes = _current_boxes(board, s)
    if focus is not None:
        view = (focus[0] - margin, focus[1] - margin, focus[2] + margin, focus[3] + margin)
        boxes = {d: b for d, b in boxes.items()
                 if not (b[0][2] < view[0] or b[0][0] > view[2] or b[0][3] < view[1] or b[0][1] > view[3])}
    problems = _block_exempt(board, {d: p for d, p in silkscreen.evaluate(board, boxes).items() if p})
    png = silkscreen.render_view(board, _current_boxes(board, s), focus=focus, side=s,
                                 targets=designators or (), problems=problems,
                                 margin=margin, grid=grid_mils or None)
    info = {
        "side": "top" if s == "T" else "bottom",
        "designators_in_view": len(boxes),
        "legend": SILK_VIEW_LEGEND,
        "problems": problems,
    }
    if focus is not None:
        info["view_mils"] = [round(v, 1) for v in view]
    return [json.dumps(info, indent=2), MCPImage(data=png, format="png")]

@_tool(read_only=True)
async def get_designator_options(ctx: Context, designators: list, count: int = 5,
                                 max_gap_mils: float = 50, min_height_mils: float = 0,
                                 refresh: bool = False):
    """
    Legal spots for designators, ranked, drawn as numbered boxes.

    For each designator: its current spot and what is wrong with it, then up
    to `count` distinct legal positions around its part - clear of silk, solder
    mask openings, untented vias, the board edge and every other designator
    where it is now, readable, and nearer its own part than any other part of
    the same type. Ranked by: close to the part, above > left/right > below,
    text along the part's long axis, centred. Pick one (or use the image to
    judge a better spot) and apply it with set_designator_positions using the
    option's x, y and rotation.

    The designators asked about are not obstacles to each other: when placing
    neighbours, check the chosen spots together with
    set_designator_positions(dry_run=True) first. When a designator has no
    legal spot, blocked_by says what is in the way: ask again together with
    the neighbouring designators (so they can move to make room), allow
    smaller text (min_height_mils), or place its row's designators as a
    block with get_label_block_options. Do not hide designators to get rid
    of problems; only mounting holes and fiducials go without one.

    Args:
        designators (list): Designators to find spots for.
        count (int): Options per designator (default 5).
        max_gap_mils (float): Furthest the text may sit from its part's pads
            and silk (default 50).
        min_height_mils (float): Also offer smaller text, down to this height,
            in 5 mil steps (0 = current size only).
        refresh (bool): Re-export the board from Altium first.

    Returns:
        JSON per designator (current spot + problems, options with n, x, y,
        rotation, side, gap_mils and height when reduced, or blocked_by) plus
        an image per board side with the options numbered.
    """
    try:
        board = await _silk_board(refresh)
    except Exception as e:
        return json.dumps({"success": False, "error": f"Failed to export silkscreen data: {e}"})
    scope, skipped = silkscreen.split_scope(board, designators or [])
    if not scope:
        return json.dumps({"success": False, "error": "no placeable designators", "skipped": skipped})

    opts = silkscreen.Options(max_gap=max_gap_mils, min_height=min_height_mils or None)
    found = silkscreen.options_for(board, scope, count=max(1, count), options=opts)
    current = silkscreen.evaluate(board, {d: (board.components[d].text_box,
                                              board.components[d].text_rotation) for d in scope})
    result = {}
    for d in scope:
        c = board.components[d]
        entry = {"current": dict(_box_info(c.text_box, c.text_rotation), problems=current[d])}
        entry["options"] = []
        for i, cand in enumerate(found[d]["options"], 1):
            o = {"n": i, "x": round(cand.center[0], 2), "y": round(cand.center[1], 2),
                 "rotation": round(cand.rotation) % 360,
                 "side": silkscreen.SIDE_NAMES.get(cand.side_name, cand.side_name),
                 "gap_mils": round(silkscreen._rect_rect_dist(cand.rect, c.extent), 1)}
            if cand.note == "reduced height":
                o["height"] = cand.height
                o["stroke_width"] = cand.stroke
            entry["options"].append(o)
        if found[d]["blocked_by"]:
            entry["blocked_by"] = found[d]["blocked_by"]
        result[d] = entry
    out = {"designators": result, "legend": SILK_VIEW_LEGEND}
    if skipped:
        out["skipped"] = skipped
    images = []
    for side in ("T", "B"):
        on_side = [d for d in scope if board.components[d].side == side]
        if not on_side:
            continue
        png = silkscreen.render_view(board, _current_boxes(board, side),
                                     focus=_focus_around(board, on_side, side), margin=60.0, side=side,
                                     targets=on_side, problems=[d for d in on_side if current[d]],
                                     options={d: found[d]["options"] for d in on_side})
        images.append(MCPImage(data=png, format="png"))
    return [json.dumps(out, indent=2)] + images

@_tool()
async def set_designator_positions(ctx: Context, placements: list, dry_run: bool = False,
                                   verify: bool = True) -> str:
    """
    Place designators - by coordinates or relative to their part - and get a
    verdict for each.

    Each placement is either
    - absolute: x, y = centre of the text box in mils (board origin), as read
      off view_silkscreen or taken from get_designator_options, or
    - relative: side = "above" | "below" | "left" | "right" | "inside" of its
      own part, with gap (mils from the part's pads/silk; default just clear
      of them) and offset (slide along that side, +x or +y).
    Optional on both: rotation (0/90 top, 0/270 bottom read correctly;
    default keeps a readable current rotation), height and stroke_width
    (mils), visible (show/hide - hide only mounting holes and fiducials;
    designators with no room go in a label block, see
    get_label_block_options). A placement with only visible (or only
    rotation/height) keeps the current centre.

    dry_run=True changes nothing: every placement is checked against silk,
    mask openings, the board edge, other designators where they are now and
    each other, plus placement quality. Use it to test a set of neighbours
    together before applying.

    Otherwise all moves are one undo step, and with verify (default) each
    moved designator is then checked with Altium's own silk rules and for
    quality. ok=true means clean. The moves only go to the board the
    snapshot was read from: if another PCB has been focused since, nothing
    moves and the error names both boards.

    Args:
        placements (list): Dicts with designator plus x/y or side (gap,
            offset), and optional rotation, height, stroke_width, visible.
        dry_run (bool): Only check the placements.
        verify (bool): After moving, run Altium's silk DRC on the moved
            designators (default True).

    Returns:
        str: JSON with one verdict per designator: final x, y, rotation,
             ok and problems.
    """
    try:
        board = await _silk_board()
    except Exception as e:
        return json.dumps({"success": False, "error": f"Failed to export silkscreen data: {e}"})

    errors, resolved, entries = [], {}, []
    for idx, p in enumerate(placements or []):
        des = str(p.get("designator", "")).strip() if isinstance(p, dict) else ""
        if not des or des not in board.components:
            errors.append(f"placements[{idx}]: unknown designator {des!r}")
            continue
        if "|" in des:
            errors.append(f"placements[{idx}]: designator must not contain '|'")
            continue
        if ("x" in p) != ("y" in p):
            errors.append(f"placements[{idx}]: give both x and y, or neither")
            continue
        try:
            rect, rot, height, stroke = silkscreen.resolve_placement(board, p)
        except (TypeError, ValueError) as e:
            errors.append(f"placements[{idx}]: {e}")
            continue
        visible = p.get("visible")
        resolved[des] = (rect, rot, visible)
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        size = f"{height:g}" if p.get("height") is not None else ""
        width = f"{stroke:g}" if p.get("height") is not None or p.get("stroke_width") is not None else ""
        vis = "" if visible is None else ("1" if visible else "0")
        entries.append(f"{des}|{cx:.3f}|{cy:.3f}|{rot:g}|{size}|{width}|{vis}")
    if errors:
        return json.dumps({"success": False, "error": "; ".join(errors)})
    if not entries:
        return json.dumps({"success": False, "error": "No placements provided"})

    shown = {d: (r, rot) for d, (r, rot, vis) in resolved.items()
             if vis is not False and (vis or board.components[d].visible)}
    if dry_run:
        problems = silkscreen.evaluate(board, shown, hiding=set(resolved) - set(shown))
        verdicts = {d: dict(_box_info(r, rot), ok=not problems.get(d), problems=problems.get(d, []))
                    for d, (r, rot) in shown.items()}
        for d in set(resolved) - set(shown):
            verdicts[d] = {"hidden": True}
        return json.dumps({"dry_run": True, "all_ok": all(v.get("ok", True) for v in verdicts.values()),
                           "designators": verdicts}, indent=2)

    params = {"placements": entries}
    if board.file:
        # Altium refuses the moves if another PCB is focused by now
        params["board"] = board.file.replace("\\", "/")
    response = await altium_bridge.execute_command("place_designators", params)
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Failed to move designators: {response.get('error', 'Unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        result = json.loads(result)

    # Keep the snapshot in step with what Altium reports
    final = {}
    for item in result.get("designators", []):
        c = board.components.get(item["designator"])
        if c is None:
            continue
        cx, cy, w, h = item["cx"], item["cy"], item["width"], item["height"]
        c.text_rotation = item["rotation"]
        c.text_height = item.get("text_height", c.text_height)
        c.stroke = item.get("stroke_width", c.stroke)
        c.text_box = silkscreen.ink_box((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2),
                                        c.stroke, c.truetype)
        c.visible = item.get("visible", c.visible)
        c.autopos = 0
        final[c.designator] = c

    verdicts = {}
    moved = sorted(d for d, c in final.items() if c.visible)
    grouped = {}
    if verify and moved:
        try:
            grouped = _group_silk_violations(await _check_silk_drc(moved))
        except Exception as e:
            grouped = {d: [f"DRC check failed: {e}"] for d in moved}
    quality = silkscreen.evaluate(board, {d: (final[d].text_box, final[d].text_rotation) for d in moved},
                                  geometry=False)
    for d, c in sorted(final.items()):
        if not c.visible:
            verdicts[d] = {"hidden": True}
            continue
        problems = grouped.get(d, []) + quality.get(d, [])
        verdicts[d] = dict(_box_info(c.text_box, c.text_rotation), ok=not problems, problems=problems)
        if c.text_height:
            verdicts[d]["text_height"] = c.text_height
    out = {"moved_count": len(final), "verified": bool(verify),
           "all_ok": all(v.get("ok", True) for v in verdicts.values()), "designators": verdicts}
    if result.get("missing_designators"):
        out["missing_designators"] = result["missing_designators"]
    return json.dumps(out, indent=2)

# Label blocks applied with place_label_block, per board file, so that
# check_silkscreen can tell a block's labels from labels that wandered off.
# Kept in the exchange folder, never next to the board.
SILK_BLOCKS_FILE = EXCHANGE_DIR / "silk_label_blocks.json"
_block_options_cache = {}

def _load_blocks(board_file: str) -> list:
    try:
        data = json.loads(SILK_BLOCKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get(board_file.lower(), [])

def _save_blocks(board_file: str, records: list):
    try:
        data = json.loads(SILK_BLOCKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[board_file.lower()] = records
    SILK_BLOCKS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")

def _update_snapshot_graphics(removed_records: list, added_option):
    """Keep the board snapshot in step with link graphics just drawn or
    deleted, instead of exporting the whole board again (every export leaks
    memory in Altium's script engine)."""
    board = _silk_snapshot["board"]
    if board is None:
        return
    for r in removed_records:
        label_blocks.drop_graphics(board, r["side"], r["graphics"])
    if added_option is not None and added_option.graphics:
        board._parse("\n".join(label_blocks.graphics_lines(added_option.side, added_option.graphics)))

def _replaced_graphics(records: list, members: list) -> list:
    """Link graphics of earlier blocks of these parts (placing replaces them)."""
    return [g for r in records if set(r["members"]) & set(members) for g in r["graphics"]]

def _block_members(board, designators: list, whole_row: bool = True) -> list:
    """The parts of a block: one designator stands for its whole row unless
    whole_row is off."""
    names = [str(d).strip() for d in designators or []]
    missing = [d for d in names if d not in board.components]
    if missing:
        raise ValueError(f"not on the board: {missing}")
    if len(names) == 1 and whole_row:
        return label_blocks.find_row(board, names[0])[1]
    return names

def _block_exempt(board, problems: dict) -> dict:
    """problems without the distance and "reads as" problems of labels in
    intact label blocks (their link answers those)."""
    out = dict(problems)
    for rec in _load_blocks(board.file):
        if label_blocks.check_record(board, rec):
            continue
        for d in rec["members"]:
            if d in out:
                keep = [p for p in out[d] if not label_blocks.is_association_problem(p)]
                if keep:
                    out[d] = keep
                else:
                    del out[d]
    return out

def _block_image(board, option) -> bytes:
    side = option.side
    boxes = {d: b for d, b in _current_boxes(board, side).items() if d not in option.members}
    for d, (rect, rot, _, _) in option.labels.items():
        boxes[d] = (rect, rot)
    focus = label_blocks._bounds(
        [label_blocks._bounds(board.components[d].extent for d in option.members), option.rect] +
        [r for g in option.graphics for r in label_blocks._graphic_rects(g)])
    return silkscreen.render_view(board, boxes, focus=focus, side=side, targets=option.members,
                                  margin=60.0, graphics=option.graphics)

@_tool(read_only=True)
async def get_label_block_options(ctx: Context, designators: list, count: int = 3,
                                  min_height_mils: float = 25, max_distance_mils: float = 500,
                                  link: str = "auto", whole_row: bool = True, refresh: bool = False):
    """
    Place a row of parts' designators together as a block, when they do not
    fit next to their parts one by one. Use this instead of hiding
    designators (only mounting holes and fiducials go without one).

    Parts in a row get their designators in a row too, in the same order,
    even some way off. Give one designator to use the whole row it belongs
    to (parts side by side along x or y, similar size across the row, gaps
    up to 50 mil), or list the parts yourself. For a block of just one part
    (its label alone, linked to it), give it with whole_row=False.

    Each option is a block of labels - across the row (in line with each
    part when the pitch allows, else side by side) or end to end along it -
    clear of silk, mask openings, the board edge, other designators and part
    bodies, plus a link when it is not right against the row:
    - leader: a silk line with an arrowhead from the block to the parts;
    - index: matching markers - a capital letter in a circle (then square,
      then triangle) - next to the block and next to the parts.
    Ranked: no link, then leader, then index; nearer and larger text first.
    Apply one with place_label_block.

    Args:
        designators (list): One designator (its row is used) or the block's parts.
        count (int): Options to return (default 3).
        min_height_mils (float): Smallest text allowed (default 25).
        max_distance_mils (float): Furthest the block may sit from the parts (default 500).
        link (str): "auto" (cheapest), "none", "leader" or "index".
        whole_row (bool): With one designator, use its whole row (default)
            or just that part.
        refresh (bool): Re-export the board from Altium first.

    Returns:
        JSON with the parts in order and the numbered options, plus an image
        per option (block labels cyan with lines to their parts, link magenta).
    """
    if link not in ("auto", "none", "leader", "index"):
        return json.dumps({"success": False, "error": "link must be auto, none, leader or index"})
    try:
        board = await _silk_board(refresh)
        members = _block_members(board, designators, whole_row)
        records = _load_blocks(board.file)
        used = label_blocks.used_markers(board, records)
        axis, members, options = label_blocks.block_options(
            board, members, count=max(1, count), min_height=min_height_mils,
            max_distance=max_distance_mils, link=link, used_markers=used,
            ignore=_replaced_graphics(records, members))
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
    _block_options_cache[(board.file.lower(), tuple(members))] = {
        "time": _silk_snapshot["time"], "options": options,
        "params": dict(min_height=min_height_mils, max_distance=max_distance_mils, link=link)}
    out = {"parts": members, "row_axis": axis,
           "options": [o.to_dict(i) for i, o in enumerate(options, 1)]}
    if not options:
        out["note"] = ("no block fits; allow a longer max_distance_mils or smaller text, or move "
                       "other designators out of the way first")
    return [json.dumps(out, indent=2)] + [MCPImage(data=_block_image(board, o), format="png") for o in options]

@_tool()
async def place_label_block(ctx: Context, designators: list, option: int = 1,
                            whole_row: bool = True) -> str:
    """
    Apply a label block from get_label_block_options: move the parts'
    designators into the block (shown, at the block's text size) and draw
    its link on the silkscreen, then check both with Altium's rules. An
    earlier block of any of these parts is replaced (its link removed).

    Args:
        designators (list): The same designators given to get_label_block_options.
        option (int): Which option (default 1).
        whole_row (bool): As given to get_label_block_options.

    Returns:
        JSON: the block's parts, link and marker, ok, and any Altium
        violations of the labels or the link graphics.
    """
    try:
        board = await _silk_board()
        members = _block_members(board, designators, whole_row)
        axis = label_blocks.row_axis(board, members)
        members = sorted(members, key=lambda d: label_blocks._reading_key(board.components[d], axis))
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
    records = _load_blocks(board.file)
    key = (board.file.lower(), tuple(members))
    cached = _block_options_cache.get(key)
    note = None
    if cached is None or cached["time"] != _silk_snapshot["time"]:
        params = cached["params"] if cached else {}
        _, _, options = label_blocks.block_options(
            board, members, count=max(3, option), used_markers=label_blocks.used_markers(board, records),
            ignore=_replaced_graphics(records, members), **params)
        note = "board changed since the options were listed - recomputed them"
    else:
        options = cached["options"]
    if not 1 <= option <= len(options):
        return json.dumps({"success": False, "error": f"option {option} does not exist ({len(options)} options)",
                           "note": note})
    opt = options[option - 1]

    old = [r for r in records if set(r["members"]) & set(members)]
    items = []
    for r in old:
        items += label_blocks.to_items(r["side"], r["graphics"], "-")
    items += label_blocks.to_items(opt.side, opt.graphics, "+")

    moved = json.loads(await set_designator_positions(ctx, opt.placements(), verify=False))
    if "designators" not in moved:
        return json.dumps({"success": False, "error": moved.get("error", "moving the labels failed")})
    graphic_problems = []
    if items:
        response = await altium_bridge.execute_command(
            "edit_silk_graphics", {"items": items, "board": board.file.replace("\\", "/")})
        if not response.get("success", False):
            _silk_snapshot["board"] = None
            return json.dumps({"success": False, "error": f"labels moved, but drawing the link failed: "
                                                          f"{response.get('error', 'Unknown error')}"})
        result = response.get("result", {})
        if isinstance(result, str):
            result = json.loads(result)
        graphic_problems = [f"{v['rule']}: {v['object']} {v.get('owner', '')} {v.get('detail', '')}".strip()
                            for v in result.get("violations", [])]
        if result.get("not_found"):
            graphic_problems.append(f"old link graphics not found: {result['not_found']}")
        _update_snapshot_graphics(old, opt)
    try:
        label_problems = _group_silk_violations(await _check_silk_drc(list(members)))
    except Exception as e:
        label_problems = {d: [f"DRC check failed: {e}"] for d in members}

    records = [r for r in records if r not in old] + [label_blocks.record(opt)]
    _save_blocks(board.file, records)
    out = {"parts": members, "summary": opt.summary, "link": opt.link,
           "ok": not label_problems and not graphic_problems,
           "label_problems": label_problems, "link_problems": graphic_problems}
    if opt.marker:
        out["marker"] = {"letter": opt.marker[0], "shape": opt.marker[1]}
    if old:
        out["replaced_blocks"] = [r["members"] for r in old]
    if note:
        out["note"] = note
    return json.dumps(out, indent=2)

@_tool()
async def remove_label_block(ctx: Context, designators: list) -> str:
    """
    Remove the label block of these parts: its link graphics (leader or
    index markers) are deleted; the designators stay where they are, ready
    to be placed again.

    Args:
        designators (list): Any of the block's parts.

    Returns:
        JSON with the removed block's parts and link.
    """
    try:
        board = await _silk_board()
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
    records = _load_blocks(board.file)
    names = {str(d).strip() for d in designators or []}
    old = [r for r in records if names & set(r["members"])]
    if not old:
        return json.dumps({"success": False, "error": f"no label block holds any of {sorted(names)}"})
    items = [i for r in old for i in label_blocks.to_items(r["side"], r["graphics"], "-")]
    if items:
        response = await altium_bridge.execute_command(
            "edit_silk_graphics", {"items": items, "board": board.file.replace("\\", "/")})
        if not response.get("success", False):
            _silk_snapshot["board"] = None
            return json.dumps({"success": False, "error": response.get("error", "Unknown error")})
        _update_snapshot_graphics(old, None)
    _save_blocks(board.file, [r for r in records if r not in old])
    return json.dumps({"removed": [{"parts": r["members"], "link": r["link"]} for r in old]}, indent=2)

@_tool()
async def place_vias(ctx: Context, vias: list, diameter_mils: float, hole_mils: float,
                     net: str = "", start_layer: str = "Top Layer", end_layer: str = "Bottom Layer",
                     board_path: str = "") -> str:
    """
    Add vias to a PCB: stitching, fan-out, or a via at the end of a track.

    Args:
        vias (list): Via positions in mils relative to the board origin,
            each [x, y] or an object {"x": .., "y": .., "net": ..,
            "diameter_mils": .., "hole_mils": .., "start_layer": ..,
            "end_layer": ..} whose keys override the defaults below for
            that via.
        diameter_mils (float): Via pad diameter.
        hole_mils (float): Drill diameter, smaller than diameter_mils.
        net (str): Net of the vias; it must exist on the board. Empty for
            vias without a net.
        start_layer (str): Layer the via starts on, e.g. "Top Layer".
        end_layer (str): Layer it ends on, e.g. "Bottom Layer". Set both for
            a blind or buried via.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Every net and layer is checked before anything is added, so a bad entry
    adds nothing. Run run_drc afterwards: a via is not checked against
    clearance when it is placed.

    Returns:
        str: JSON with how many vias were added.
    """
    board_path = long_path(board_path)
    records, error = routing.via_records(vias, diameter_mils, hole_mils, net, start_layer, end_layer)
    if error:
        return json.dumps({"success": False, "error": error})

    logger.info(f"Placing {len(records)} vias")
    response = await altium_bridge.execute_command("create_vias", {
        "vias": ";".join(records), "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to add the vias: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

# Changes the design: a timeout leaves its outcome unknown, so no blind retry
MUTATING_COMMANDS.add("create_vias")


def _check_area(area, what="area"):
    """[x1, y1, x2, y2] in mils as floats, or an error message."""
    if not isinstance(area, (list, tuple)) or len(area) != 4:
        return None, f"{what} must be [x1, y1, x2, y2] in mils"
    try:
        a = [float(v) for v in area]
    except (TypeError, ValueError):
        return None, f"{what} must hold four numbers"
    if not all(math.isfinite(v) and abs(v) <= pcb_layout.LIMIT_MILS for v in a):
        return None, f"{what} must lie within ±{pcb_layout.LIMIT_MILS} mil"
    if a[0] >= a[2] or a[1] >= a[3]:
        return None, f"{what} must have x1 < x2 and y1 < y2"
    return a, ""


def _names(values, what):
    """A list of names as strings, or an error message."""
    if values is None:
        return [], ""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
        return None, f"{what} must be a list of names"
    if any(";" in v or "|" in v for v in values):
        return None, f"{what} may not contain ';' or '|'"
    return [v.strip() for v in values], ""


@_tool(read_only=True)
async def get_layout(ctx: Context, designators: list = None, area: list = None, margin_mils: float = 40,
                     include_touching: bool = False, include_local_nets: bool = True, include_pads: bool = False,
                     board_path: str = "", out_file: str = "") -> str:
    """
    Read the layout of a group of components or of an area: the components'
    placement and the copper around them (tracks, arcs, vias, fills, copper
    regions and polygon pours, each pour with its full outline, arc edges
    included, and its pour settings). Read-only.

    The result is in exactly the form place_copper takes, so it can be
    edited and placed again, on this board or moved with place_copper's
    transform (to repeat a channel, for example). The components come in
    place_components' form.

    Args:
        designators (list): Components to read, e.g. ["U5", "C13", "Y1"].
            Without an area, the area is their extent (pads and bodies)
            grown by margin_mils.
        area (list): [x1, y1, x2, y2] in mils from the board origin. With
            no designators, the components wholly inside it are listed.
        margin_mils (float): How far the area reaches beyond the components
            (default 40).
        include_touching (bool): Also list copper that only reaches into the
            area, such as a track leaving the group. By default only copper
            wholly inside the area is listed: the group's own routing.
        include_local_nets (bool): With designators, also list the copper of
            their own nets (nets with no pad on any other part) wherever it
            runs, such as a route between two of them that leaves the area
            (default true).
        include_pads (bool): Also list every pad reaching into the area
            (designator, pad, x, y, layer, shape, size [x, y], rotation,
            hole, net), e.g. to see what new copper must keep clear of.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.
        out_file (str): Write the layout to this JSON file and return only a
            summary; for large areas.

    Never listed: copper that belongs to a component or that a pour poured
    (the pour itself is), keepouts, and objects on non-copper layers
    (counted in skipped).

    Returns:
        JSON: area; components (designator, x, y, rotation, layer, locked,
        footprint, designator_text and comment_text: {x, y, rotation,
        height, stroke_width, visible, autoposition}); tracks (net, layer,
        width, start, end); arcs (net, layer, width, center, radius,
        start_angle, end_angle: counter-clockwise from start to end); vias
        (net, x, y, diameter, hole, start_layer, end_layer); fills; regions;
        cutouts (layer, outline); pours (name, net, layer, hatch, pour_over,
        remove_dead_copper, remove_islands, island_area_sq_mils,
        remove_necks, neck_width_mils, grid, track_width, arc_approximation,
        pour_index, locked, obey_cutouts, optimal_void_rotation, outline:
        vertices {x, y} with "arc" {center, radius, start_angle, end_angle}
        when the edge to the next vertex is an arc); counts and
        missing_designators. All in mils and degrees.
    """
    names, err = _names(designators, "designators")
    if err:
        return json.dumps({"success": False, "error": err})
    box = None
    if area is not None:
        box, err = _check_area(area)
        if err:
            return json.dumps({"success": False, "error": err})
    if not names and box is None:
        return json.dumps({"success": False, "error": "Give designators, an area, or both"})
    try:
        margin = float(margin_mils)
        if not math.isfinite(margin) or not 0 <= margin <= 10000:
            raise ValueError
    except (TypeError, ValueError):
        return json.dumps({"success": False, "error": "margin_mils must be a number from 0 to 10000"})
    params = {"layout_designators": ";".join(names), "layout_margin": int(round(margin * pcb_layout.COORD_PER_MIL)),
              "layout_touching": bool(include_touching), "layout_local_nets": bool(include_local_nets),
              "layout_pads": bool(include_pads),
              "layout_area": "" if box is None else "|".join(str(int(round(v * pcb_layout.COORD_PER_MIL))) for v in box)}
    if board_path:
        params["board_path"] = long_path(board_path)
    response = await altium_bridge.execute_command("get_layout", params)
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Failed to read the layout: {response.get('error', 'unknown error')}"})
    text = response.get("result", "")
    if not isinstance(text, str) or text.startswith("ERROR"):
        return json.dumps({"success": False, "error": str(text)[:500]})
    layout = pcb_layout.parse_layout(text)
    found = {d.upper() for d in layout.pop("found")}
    layout["missing_designators"] = [d for d in names if d.upper() not in found]
    layout["counts"] = {k: len(layout[k]) for k in ("components", "tracks", "arcs", "vias", "fills", "regions", "cutouts",
                                                     "pours", "pads")}
    if not include_pads:
        layout.pop("pads")
        layout["counts"].pop("pads")
    layout["success"] = True
    if out_file:
        path = Path(out_file)
        try:
            path.write_text(json.dumps(layout, indent=1), encoding="utf-8")
        except OSError as e:
            return json.dumps({"success": False, "error": f"Could not write {out_file}: {e}"})
        return json.dumps({"success": True, "out_file": str(path), "area": layout["area"], "counts": layout["counts"],
                           "missing_designators": layout["missing_designators"], "board": layout["board"]}, indent=2)
    return json.dumps(layout, indent=1)


@_tool()
async def place_copper(ctx: Context, tracks: list = None, arcs: list = None, vias: list = None, fills: list = None,
                       regions: list = None, cutouts: list = None, pours: list = None, layout: dict = None,
                       transform: dict = None,
                       rename_nets: dict = None, board_path: str = "") -> str:
    """
    Add copper to a PCB in one undo step: tracks and arcs at any angle, vias,
    fills, copper regions and polygon pours (arc edges, island and neck
    removal, pour order). Everything is checked first (each net must exist,
    each layer must be a signal layer), so a bad entry adds nothing. Pours go
    in last, in pour_index order, and are poured around the copper added
    with them. The nets the copper is on are analyzed afterwards, so the
    ratsnest is current (this can join straight track segments that continue
    each other; the copper stays the same). Run run_drc or
    get_unrouted_connections afterwards.

    Each list takes entries in get_layout's form (mils, degrees):
        tracks: {net, layer, width, start: [x, y], end: [x, y]}
        arcs:   {net, layer, width, center: [x, y], radius, start_angle,
                 end_angle}  (counter-clockwise from start to end)
        vias:   {net, x, y, diameter, hole, start_layer, end_layer}
                (layers default to Top Layer and Bottom Layer)
        fills:  {net, layer, corner1: [x, y], corner2: [x, y], rotation}
        regions: {net, layer, outline: [[x, y], ...]}
        cutouts: {layer, outline}  a polygon pour cutout, on a signal layer
                 or "Multi Layer" (all of them); pours with obey_cutouts
                 leave it free
        pours:  {net, layer, outline, name, hatch ("solid", "hatched_45",
                 "hatched_90", "vertical", "horizontal", "none"), pour_over
                 ("same_net", "same_net_polygons", "none"),
                 remove_dead_copper, remove_islands (below island_area_sq_mils,
                 default 2500), remove_necks (narrower than neck_width_mils,
                 default 5), grid, track_width, arc_approximation,
                 pour_index, locked, obey_cutouts (default true),
                 optimal_void_rotation (default false)}
                outline vertices are [x, y] or {x, y, arc: {center, radius,
                start_angle, end_angle}} for an arc edge to the next vertex.
    net "" is no net. Coordinates are mils from the board origin.

    Args:
        tracks, arcs, vias, fills, regions, cutouts, pours (list): Copper
            to add.
        layout (dict): A whole get_layout result; its copper lists are added
            too (its components are not moved: use place_components).
        transform (dict): Move everything: {"dx", "dy", "rotation" (degrees,
            about "about": [x, y], default the origin), "mirror" (mirror x
            before rotating)}. Fills only shift.
        rename_nets (dict): {old net: new net}, e.g. to repeat a channel.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        JSON: counts added (tracks, arcs, vias, fills, regions, cutouts) and each pour
        with the pieces of copper it poured (0 means it poured nothing), or
        the problems that stopped it.
    """
    merged = {k: list(v or []) for k, v in (("tracks", tracks), ("arcs", arcs), ("vias", vias), ("fills", fills),
                                            ("regions", regions), ("cutouts", cutouts), ("pours", pours))}
    if layout is not None:
        if not isinstance(layout, dict):
            return json.dumps({"success": False, "error": "layout must be a get_layout result"})
        for k in merged:
            merged[k].extend(layout.get(k) or [])
    if transform is not None and not isinstance(transform, dict):
        return json.dumps({"success": False, "error": "transform must be an object {dx, dy, rotation, about, mirror}"})
    if rename_nets is not None and not isinstance(rename_nets, dict):
        return json.dumps({"success": False, "error": "rename_nets must be an object {old: new}"})
    try:
        lines = pcb_layout.encode_copper(merged, transform, rename_nets)
    except (pcb_layout.LayoutError, KeyError, TypeError) as e:
        what = f"missing field {e}" if isinstance(e, KeyError) else str(e)
        return json.dumps({"success": False, "error": f"Nothing was added: {what}"})
    if not lines:
        return json.dumps({"success": False, "error": "Nothing to add"})
    fd, spec = tempfile.mkstemp(prefix="copper_spec_", suffix=".txt", dir=EXCHANGE_DIR)
    os.close(fd)
    try:
        write_altium_text(spec, "\n".join(lines) + "\n")
        params = {"copper_spec_file": spec}
        if board_path:
            params["board_path"] = long_path(board_path)
        response = await altium_bridge.execute_command("place_copper", params,
                                                       timeout=max(RESPONSE_TIMEOUT, 60 + len(lines) / 20))
    finally:
        try:
            os.remove(spec)
        except OSError:
            pass
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Failed to add the copper: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

# Changes the design: a timeout leaves its outcome unknown, so no blind retry
MUTATING_COMMANDS.add("place_copper")


@_tool()
async def get_unrouted_connections(ctx: Context, designators: list = None, nets: list = None, area: list = None,
                                   margin_mils: float = 40, max_connections: int = 200,
                                   board_path: str = "") -> str:
    """
    List the connections still to be routed (Altium's ratsnest lines): for a
    group of components, some nets, or the whole board. Unlike run_drc it
    has no 500-violation limit and needs no DRC run, so it is the quick way
    to see what a group still lacks after placing and routing it.

    Args:
        designators (list): Components whose nets to check. Their pads name
            the ends of each connection (from_pad, to_pad), and unless area is
            given, the area is their pads' extent grown by margin_mils.
        nets (list): Nets to check (exact names, any case). With neither
            designators nor nets, every net.
        area (list): [x1, y1, x2, y2] in mils from the board origin; each
            connection says whether both its ends lie in it (inside).
        margin_mils (float): See designators (default 40).
        max_connections (int): Most connections listed (default 200); the
            counts cover them all.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    With designators, only connections that end at one of their pads or lie
    wholly inside the area are listed: a group's own connections and the
    ones leaving it, not the rest of a board-wide net such as GND.

    Altium does not update its ratsnest after copper added by a script, so
    the nets are analyzed first. Analyzing can join straight track segments
    that continue each other into one (the copper stays the same), as
    Altium does after interactive routing.

    Returns:
        JSON: connection_count, inside_count (both ends in the area),
        by_net counts, area, and connections (net, from, to, from_pad,
        to_pad, length in mils, inside), longest first.
    """
    names, err = _names(designators, "designators")
    if err:
        return json.dumps({"success": False, "error": err})
    net_names, err = _names(nets, "nets")
    if err:
        return json.dumps({"success": False, "error": err})
    box = None
    if area is not None:
        box, err = _check_area(area)
        if err:
            return json.dumps({"success": False, "error": err})
    try:
        margin = float(margin_mils)
        limit = int(max_connections)
    except (TypeError, ValueError):
        return json.dumps({"success": False, "error": "margin_mils and max_connections must be numbers"})
    params = {"unrouted_nets": ";".join(net_names), "unrouted_designators": ";".join(names)}
    if board_path:
        params["board_path"] = long_path(board_path)
    response = await altium_bridge.execute_command("get_unrouted_connections", params)
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Failed to read the connections: {response.get('error', 'unknown error')}"})
    text = response.get("result", "")
    if not isinstance(text, str) or text.startswith("ERROR"):
        return json.dumps({"success": False, "error": str(text)[:500]})
    pads, conns, board = pcb_layout.parse_unrouted(text)
    if box is None and pads:
        xs, ys = [p["x"] for p in pads], [p["y"] for p in pads]
        box = [min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin]
    labelled = pcb_layout.label_connections(pads, conns, box)
    if names:
        labelled = [c for c in labelled if c["from_pad"] or c["to_pad"] or c["inside"]]
    labelled.sort(key=lambda c: -c["length"])
    by_net = {}
    for c in labelled:
        by_net[c["net"]] = by_net.get(c["net"], 0) + 1
    found = {p["designator"].upper() for p in pads}
    missing = [d for d in names if d.upper() not in found]
    return json.dumps({"success": True, "board": board, "connection_count": len(labelled),
                       "inside_count": sum(1 for c in labelled if c["inside"]),
                       "by_net": dict(sorted(by_net.items(), key=lambda kv: -kv[1])),
                       "area": [round(v, 2) for v in box] if box else None,
                       "missing_designators": missing, "connections": labelled[:max(0, limit)]}, indent=1)


def _exact_segments(layer, net, width, pts):
    """place_copper tracks through pts (mils), every segment exactly 0, 45 or
    90 degrees: routing.route adds a short leg where an end point snapped
    onto the grid leaves a bend off 45."""
    ipts = [(int(round(x * routing.COORD_PER_MIL)), int(round(y * routing.COORD_PER_MIL))) for x, y in pts]
    segs = routing.route(ipts)
    c = routing.COORD_PER_MIL
    return [{"net": net, "layer": layer, "width": width, "start": [x1 / c, y1 / c], "end": [x2 / c, y2 / c]}
            for x1, y1, x2, y2 in segs if (x1, y1) != (x2, y2)]


def _route_params(width_mils, clearance_mils, via_diameter_mils, via_hole_mils, grid_mils):
    for name, v in (("width_mils", width_mils), ("clearance_mils", clearance_mils),
                    ("via_diameter_mils", via_diameter_mils), ("via_hole_mils", via_hole_mils), ("grid_mils", grid_mils)):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 < v < 1000:
            return f"{name} must be a positive number of mils"
    if via_hole_mils >= via_diameter_mils:
        return "via_hole_mils must be smaller than via_diameter_mils"
    if not 0.5 <= grid_mils <= 25:
        return "grid_mils must be from 0.5 to 25"
    return ""


async def _net_rules(nets, layer, board_path):
    """{net: {"min", "preferred", "max", "rule", "clearance"}} in mils, from the
    board's width and clearance rules for a track of each net on layer."""
    nets = sorted({n for n in nets if n and ";" not in n and "|" not in n})
    if not nets:
        return {}
    params = {"net_rules_nets": ";".join(nets), "net_rules_layer": layer}
    if board_path:
        params["board_path"] = long_path(board_path)
    response = await altium_bridge.execute_command("net_rules", params)
    text = response.get("result", "") if response.get("success") else ""
    out = {}
    for line in (text if isinstance(text, str) else "").splitlines():
        f = line.split("|")
        if f[0] == "N" and len(f) >= 7:
            mil = lambda v: int(v) / routing.COORD_PER_MIL if v.strip().lstrip("-").isdigit() else None
            out[f[1]] = {"min": mil(f[2]), "preferred": mil(f[3]), "max": mil(f[4]), "rule": f[5].strip(),
                         "clearance": mil(f[6]) if mil(f[6]) and mil(f[6]) > 0 else None}
    return out


@_tool()
async def route_connections(ctx: Context, designators: list = None, nets: list = None, area: list = None,
                            layers: list = None, width_mils: float = None, clearance_mils: float = None,
                            via_diameter_mils: float = 24, via_hole_mils: float = 12, grid_mils: float = 2.5,
                            include_leaving: bool = False, max_connections: int = 100, dry_run: bool = False,
                            board_path: str = "") -> str:
    """
    Route unrouted connections automatically: a maze router finds a path for
    each connection, every segment 0, 45 or 90 degrees, keeping the clearance
    from other nets' copper and changing layer through a via where a route
    needs it. For a group's local routing after it is placed (and fanned out
    with fanout_pads). Each net gets the preferred width of its width rule
    (the minimum where the preferred width finds no path) and the gap of
    its clearance rule, unless width_mils or clearance_mils is given. Wider
    nets are routed first, then shortest first; each route becomes an
    obstacle for the next. Polygon pours are not obstacles: they pour again
    around new copper (repour_polygons). Everything is added in one undo
    step; check with run_drc and get_unrouted_connections.

    Args:
        designators (list): Route the connections of these components that
            lie wholly inside their area (their pads' extent, as
            get_unrouted_connections gives it); include_leaving adds those
            leaving the group.
        nets (list): Route the connections of these nets (inside area when
            given).
        area (list): [x1, y1, x2, y2] in mils from the board origin.
        layers (list): Layers routes may use, e.g. ["Top Layer"] (default)
            or ["Top Layer", "Bottom Layer"]; with more than one, vias
            (through, Top to Bottom) change layer.
        width_mils (float): One width for every net, instead of the rules.
        clearance_mils (float): Gap to other nets' copper, instead of the
            largest clearance rule gap of the nets routed.
        via_diameter_mils, via_hole_mils (float): Size of a layer-change via.
        grid_mils (float): Routing grid (default 2.5); finer finds paths
            between fine-pitch pads but is slower.
        include_leaving (bool): See designators.
        max_connections (int): Most connections routed in one call.
        dry_run (bool): Find the routes and return them without adding them.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        JSON: routed and failed connections (net, from, to, pads, width, and
        why a route failed), tracks and vias added, the widths used per
        net, and the connections still unrouted afterwards. With dry_run,
        the tracks and vias themselves.
    """
    import router

    layers = layers or ["Top Layer"]
    if not isinstance(layers, list) or not all(isinstance(l, str) and l.strip() for l in layers):
        return json.dumps({"success": False, "error": "layers must be a list of layer names"})
    err = _route_params(width_mils or 6, clearance_mils or 6, via_diameter_mils, via_hole_mils, grid_mils)
    if err:
        return json.dumps({"success": False, "error": err})
    if not designators and not nets:
        return json.dumps({"success": False, "error": "Give designators or nets to route"})
    un = json.loads(await get_unrouted_connections(ctx, designators=designators, nets=nets, area=area,
                                                   max_connections=100000, board_path=board_path))
    if not un.get("success"):
        return json.dumps(un)
    conns = [c for c in un["connections"] if include_leaving or c["inside"] or (not designators and area is None)]
    conns.sort(key=lambda c: c["length"])
    conns = conns[:max(0, int(max_connections))]
    if not conns:
        return json.dumps({"success": True, "routed": 0, "failed": [], "note": "Nothing to route",
                           "unrouted": un.get("connection_count", 0)})
    rules = await _net_rules({c["net"] for c in conns}, layers[0], board_path)
    clearance = clearance_mils or max([r["clearance"] for r in rules.values() if r.get("clearance")] or [6.0])

    def widths(net):
        """The widths to try for a net: preferred, then minimum."""
        if width_mils:
            return [float(width_mils)]
        r = rules.get(net) or {}
        pref, low = r.get("preferred") or 6.0, r.get("min") or r.get("preferred") or 6.0
        return [pref] if abs(pref - low) < 1e-6 else [pref, low]

    xs = [p[0] for c in conns for p in (c["from"], c["to"])]
    ys = [p[1] for c in conns for p in (c["from"], c["to"])]
    margin = 150.0
    box = [min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin]
    lay = json.loads(await get_layout(ctx, area=box, include_touching=True, include_local_nets=False,
                                      include_pads=True, board_path=board_path))
    if not lay.get("success"):
        return json.dumps(lay)
    signal = sorted({p["layer"] for p in lay["pads"] if p["layer"] != "Multi Layer"} |
                    {t["layer"] for t in lay["tracks"]} | {a["layer"] for a in lay["arcs"]} | set(layers))
    base_shapes = router.shapes_from_layout(lay, signal)

    def end_layers(layer):
        return list(layers) if layer == "Multi Layer" else [layer]

    def attempt(order):
        """One routing pass over the connections in this order, in memory."""
        new_shapes, routed, failed, tracks, vias = [], [], [], [], []
        pending = [(c, 0) for c in order]   # (connection, index into widths(net))
        used = {}
        while pending:
            width = max(widths(c["net"])[k] for c, k in pending)
            batch = [(c, k) for c, k in pending if abs(widths(c["net"])[k] - width) < 1e-6]
            pending = [(c, k) for c, k in pending if abs(widths(c["net"])[k] - width) >= 1e-6]
            try:
                grid = router.Board(box, grid_mils, signal, width, clearance, via_diameter_mils,
                                    {c["net"] for c, _ in batch})
            except router.RouteError as e:
                return json.dumps({"success": False, "error": str(e)})
            for s in base_shapes + new_shapes:
                grid.add(s)
            for c, k in batch:
                info = {key: c[key] for key in ("net", "from", "to", "from_pad", "to_pad")}
                sl = [l for l in end_layers(c["from_layer"]) if l in layers]
                el = [l for l in end_layers(c["to_layer"]) if l in layers]
                if not sl or not el:
                    failed.append(dict(info, why=f"an end is on {c['from_layer'] if not sl else c['to_layer']}, "
                                                 "not on a routing layer"))
                    continue
                i1, j1 = grid.cell(min(c["from"][0], c["to"][0]) - 100, min(c["from"][1], c["to"][1]) - 100)
                i2, j2 = grid.cell(max(c["from"][0], c["to"][0]) + 100, max(c["from"][1], c["to"][1]) + 100)
                window = (max(0, i1), max(0, j1), min(grid.nx - 1, i2), min(grid.ny - 1, j2))
                found = router.route_one(grid, c["net"], c["from"], c["to"], sl, el, window=window) or \
                    router.route_one(grid, c["net"], c["from"], c["to"], sl, el)
                if found is None:
                    if k + 1 < len(widths(c["net"])):
                        pending.append((c, k + 1))   # try again at the rule's minimum width
                    else:
                        failed.append(dict(info, width=width, why="no path keeps the clearance on the routing layers"))
                    continue
                legs, via_pts = found
                for layer, pts in legs:
                    segs = _exact_segments(layer, c["net"], width, pts)
                    tracks.extend(segs)
                    for s in segs:
                        shape = router.Shape("seg", c["net"], [layer], ax=s["start"][0], ay=s["start"][1],
                                             bx=s["end"][0], by=s["end"][1], hw=width / 2)
                        grid.add(shape)
                        new_shapes.append(shape)
                for vx, vy in via_pts:
                    vias.append({"net": c["net"], "x": round(vx, 4), "y": round(vy, 4), "diameter": via_diameter_mils,
                                 "hole": via_hole_mils, "start_layer": "Top Layer", "end_layer": "Bottom Layer"})
                    shape = router.Shape("circle", c["net"], signal, cx=vx, cy=vy, r=via_diameter_mils / 2)
                    grid.add(shape)
                    new_shapes.append(shape)
                used[c["net"]] = width
                routed.append(dict(info, width=width))
        return routed, failed, tracks, vias, used

    # A connection that fails is often walled in by routes made before it:
    # route again with the failures first, and keep the best pass
    order, best = list(conns), None
    for _ in range(3):
        outcome = attempt(order)
        if isinstance(outcome, str):
            return outcome
        if best is None or len(outcome[1]) < len(best[1]):
            best = outcome
        if not outcome[1]:
            break
        failed_keys = {(f["net"], tuple(f["from"]), tuple(f["to"])) for f in outcome[1]}
        key = lambda c: (c["net"], tuple(c["from"]), tuple(c["to"]))
        order = [c for c in conns if key(c) in failed_keys] + [c for c in conns if key(c) not in failed_keys]
    routed, failed, tracks, vias, used = best
    result = {"success": True, "routed": len(routed), "failed": failed, "tracks": len(tracks), "vias": len(vias),
              "clearance_mils": clearance, "widths": used}
    if dry_run:
        result.update({"dry_run": True, "track_list": tracks, "via_list": vias})
        return json.dumps(result, indent=1)
    if tracks or vias:
        placed = json.loads(await place_copper(ctx, tracks=tracks, vias=vias, board_path=board_path))
        if not placed.get("success"):
            return json.dumps({"success": False, "error": f"Routes found but not added: {placed.get('error')}",
                               "problems": placed.get("problems")})
    after = json.loads(await get_unrouted_connections(ctx, designators=designators, nets=nets, area=area,
                                                      max_connections=20, board_path=board_path))
    result["unrouted_after"] = after.get("connection_count")
    result["inside_unrouted_after"] = after.get("inside_count")
    return json.dumps(result, indent=1)

MUTATING_COMMANDS.add("route_connections")


@_tool()
async def fanout_pads(ctx: Context, designators: list, nets: list = None, pads: list = None,
                      width_mils: float = None, clearance_mils: float = None, via_diameter_mils: float = 24,
                      via_hole_mils: float = 12, max_distance_mils: float = 80, grid_mils: float = 2.5,
                      dry_run: bool = False, board_path: str = "") -> str:
    """
    Fan out pads: a short track from each pad to a via next to it, e.g. to
    take an IC's ground and power pads down to their planes, or to free a
    pad for routing on another layer. Each via goes outward from the
    component first (then the other 45 degree directions), as near as
    clearance_mils from other nets' copper allows, within max_distance_mils.
    Pads that already have a track or via on them are left alone. One undo
    step; vias are through (Top to Bottom).

    Args:
        designators (list): Components whose pads to fan out.
        nets (list): Only pads on these nets, e.g. ["GND", "3V3"].
        pads (list): Only these pads, as "U1-5" or pad names.
        width_mils, clearance_mils (float): Track width and gap to other
            nets' copper. By default each net's width rule (its preferred
            width, but no wider than the pad and never below the rule's
            minimum) and the largest clearance rule gap of the nets.
        via_diameter_mils, via_hole_mils (float): Via size.
        max_distance_mils (float): Farthest a via may sit from its pad's
            centre (default 80).
        grid_mils (float): Grid for the clearance check (default 2.5).
        dry_run (bool): Return the vias and tracks without adding them.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        JSON: fanned out pads with their via positions, pads skipped (and
        why), and the counts added.
    """
    import router

    names, err = _names(designators, "designators")
    if err or not names:
        return json.dumps({"success": False, "error": err or "Give designators"})
    err = _route_params(width_mils or 8, clearance_mils or 6, via_diameter_mils, via_hole_mils, grid_mils)
    if err:
        return json.dumps({"success": False, "error": err})
    lay = json.loads(await get_layout(ctx, designators=names, margin_mils=max_distance_mils + 60,
                                      include_touching=True, include_local_nets=False, include_pads=True,
                                      board_path=board_path))
    if not lay.get("success"):
        return json.dumps(lay)
    want_nets = {n.upper() for n in (nets or [])}
    want_pads = {str(p).upper() for p in (pads or [])}
    upper = {n.upper() for n in names}
    comps = {c["designator"].upper(): c for c in lay["components"]}
    sel = [p for p in lay["pads"] if p["designator"].upper() in upper and p["net"]
           and (not want_nets or p["net"].upper() in want_nets)
           and (not want_pads or p["pad"].upper() in want_pads
                or f"{p['designator']}-{p['pad']}".upper() in want_pads)]
    signal = sorted({p["layer"] for p in lay["pads"] if p["layer"] != "Multi Layer"} |
                    {t["layer"] for t in lay["tracks"]} | {"Top Layer", "Bottom Layer"})
    rules = await _net_rules({p["net"] for p in sel}, "Top Layer", board_path)
    clearance = clearance_mils or max([r["clearance"] for r in rules.values() if r.get("clearance")] or [6.0])

    def width_for(p):
        if width_mils:
            return float(width_mils)
        r = rules.get(p["net"]) or {}
        pref, low = r.get("preferred") or 8.0, r.get("min") or 6.0
        return max(low, min(pref, min(p["size"])))

    widest = max([width_for(p) for p in sel] or [width_mils or 8.0])
    try:
        grid = router.Board(lay["area"], grid_mils, signal, widest, clearance, via_diameter_mils,
                            {p["net"] for p in sel})
    except router.RouteError as e:
        return json.dumps({"success": False, "error": str(e)})
    shapes = router.shapes_from_layout(lay, signal)
    for s in shapes:
        grid.add(s)
    # Copper already on a pad: a track end or a via (not the pads themselves)
    ends = {}
    for s in shapes:
        if s.g.get("pad"):
            continue
        if s.kind == "seg":
            ends.setdefault(s.net, []).extend([(s.g["ax"], s.g["ay"]), (s.g["bx"], s.g["by"])])
        elif s.kind == "circle":
            ends.setdefault(s.net, []).append((s.g["cx"], s.g["cy"]))
    done, skipped, tracks, vias = [], [], [], []
    for p in sorted(sel, key=lambda p: (p["net"], p["designator"], p["pad"])):
        name = f"{p['designator']}-{p['pad']}"
        if p["layer"] == "Multi Layer":
            skipped.append({"pad": name, "why": "through-hole pad: it already reaches every layer"})
            continue
        pad_shape = router.shapes_from_layout({"pads": [p]}, signal)[0]
        if any(pad_shape.dist(x, y) <= 0.01 for x, y in ends.get(p["net"], [])):
            skipped.append({"pad": name, "why": "already has a track or via on it"})
            continue
        comp = comps.get(p["designator"].upper())
        center = (comp["x"], comp["y"]) if comp else (p["x"], p["y"])
        site = router.fanout_site(grid, p, p["net"], width_for(p), via_diameter_mils, max_distance_mils, center,
                                  [p["layer"]])
        if site is None:
            skipped.append({"pad": name, "why": f"no via site within {max_distance_mils} mil keeps the clearance"})
            continue
        (vx, vy), legs = site
        w = width_for(p)
        for layer, pts in legs:
            segs = _exact_segments(layer, p["net"], w, pts)
            tracks.extend(segs)
            for s in segs:
                grid.add(router.Shape("seg", p["net"], [layer], ax=s["start"][0], ay=s["start"][1],
                                      bx=s["end"][0], by=s["end"][1], hw=w / 2))
        vias.append({"net": p["net"], "x": round(vx, 4), "y": round(vy, 4), "diameter": via_diameter_mils,
                     "hole": via_hole_mils, "start_layer": "Top Layer", "end_layer": "Bottom Layer"})
        grid.add(router.Shape("circle", p["net"], signal, cx=vx, cy=vy, r=via_diameter_mils / 2))
        done.append({"pad": name, "net": p["net"], "via": [round(vx, 2), round(vy, 2)]})
    result = {"success": True, "fanned_out": done, "skipped": skipped, "tracks": len(tracks), "vias": len(vias)}
    if dry_run or not vias:
        if dry_run:
            result.update({"dry_run": True, "track_list": tracks, "via_list": vias})
        return json.dumps(result, indent=1)
    placed = json.loads(await place_copper(ctx, tracks=tracks, vias=vias, board_path=board_path))
    if not placed.get("success"):
        return json.dumps({"success": False, "error": f"Fanout found but not added: {placed.get('error')}",
                           "problems": placed.get("problems")})
    return json.dumps(result, indent=1)

MUTATING_COMMANDS.add("fanout_pads")


def _bring_to_front(hwnd):
    """Give a window the foreground from this background process.

    Windows usually refuses SetForegroundWindow from a background process.
    While attached to the foreground thread, BringWindowToTop does the job
    without a key press. A synthetic Alt also lifts the refusal, but it lands
    on whichever window is in front: usually the program the user is typing
    in, and if that is Altium, its menu bar stays active for the next keys.
    """
    this_thread = win32api.GetCurrentThreadId()
    front = win32gui.GetForegroundWindow()
    front_thread = win32process.GetWindowThreadProcessId(front)[0] if front else 0
    attached = False
    try:
        if front_thread and front_thread != this_thread:
            win32process.AttachThreadInput(this_thread, front_thread, True)
            attached = True
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            logger.debug(f"SetForegroundWindow refused: {e}")
        win32gui.BringWindowToTop(hwnd)
    except Exception as e:
        logger.debug(f"Could not bring the window to the front: {e}")
    finally:
        if attached:
            win32process.AttachThreadInput(this_thread, front_thread, False)


def _return_foreground(previous_window):
    """Give the window that was in front back the foreground, when Altium
    still has it. Starting any command in Altium brings Altium up, so this is
    due again after every command, not only after the capture."""
    try:
        previous_pid = win32process.GetWindowThreadProcessId(previous_window)[1] if previous_window else 0
        front = win32gui.GetForegroundWindow()
        front_pid = win32process.GetWindowThreadProcessId(front)[1] if front else 0
        if screenshot_target.should_return_foreground(
                previous_pid, front_pid, set(win_processes.pids_by_name("X2.EXE"))):
            _bring_to_front(previous_window)
    except Exception as e:
        logger.debug(f"Could not give the foreground back: {e}")


def _grab_window_image(hwnd, width: int, height: int) -> Image.Image:
    """Copy a window's pixels, frame included, into an RGB image.

    Every GDI object is released even when a step fails, so a failed capture
    does not leak device contexts or the bitmap.
    """
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = save_dc = bitmap = None
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)
        info = bitmap.GetInfo()
        return Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']),
                                bitmap.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
    finally:
        # The memory DC goes first: a bitmap still selected into a DC is not deleted
        if save_dc is not None:
            save_dc.DeleteDC()
        if bitmap is not None and bitmap.GetHandle():
            win32gui.DeleteObject(bitmap.GetHandle())
        if mfc_dc is not None:
            mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def _save_debug_screenshot(label: str, png_bytes: bytes):
    """Path of a debug copy of a capture, or None. A copy only on request, and
    with the per-user data: the install folder is replaced on every update
    (non-fatal if it fails)."""
    if os.environ.get("ALTIUM_MCP_DEBUG_SCREENSHOTS", "") in ("", "0"):
        return None
    try:
        debug_path = _data_dir() / f"screenshot_{label}.png"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_bytes(png_bytes)
        logger.info(f"Saved debug screenshot to {debug_path}")
        return str(debug_path)
    except Exception as save_error:
        logger.warning(f"Could not save debug screenshot: {save_error}")
        return None


@_tool(read_only=True)
async def get_screenshot(ctx: Context, target: str = None, view_type: str = None,
                         zoom_to: list = None, open_if_closed: bool = False):
    """
    Take a screenshot of exactly one thing in Altium, returned as viewable image content.

    Say what to capture with `target`:
        "current"            the Altium window as it is now (the default).
                             Nothing is switched or opened.
        "document"           the editing area of the active document only.
        "document:<name>"    the editing area of that document: a file name
                             ("PD_Mon.SchDoc") or a full path. It must already
                             be open unless open_if_closed is true. The document
                             that was active before is brought back afterwards,
                             and one opened for the capture is closed again
                             (only when it has no unsaved changes).
        "dialog"             the topmost Altium dialog or popup menu.
        "panel:<name>"       one panel, e.g. "panel:Projects", "panel:Components".
        "region:x,y,w,h"     a rectangle inside Altium's main window, in
                             pixels of a "current" capture (0,0 = its top
                             left corner). A region reaching outside the
                             window is refused: nothing outside Altium is
                             ever captured.

    Altium comes to the front for about a second, because it only paints what
    is on screen; the window that was in front then gets the foreground back.

    Args:
        target (str): What to capture, see above. Default "current".
        view_type (str): Old form, kept for compatibility: 'pcb' or 'sch' brings
            some document of that kind to the front (opening one if none is
            open) and captures the whole window. Ignored when target is given.
        zoom_to (list, optional): Component designators (e.g. ["U12", "R42"]).
            PCB only: Altium zooms to them before the capture. Works with
            "document" / "document:<board>" and view_type 'pcb'; given on its
            own it means view_type 'pcb'.
        open_if_closed (bool): Let "document:<name>" open a document of the
            open projects that is not open yet. Default false.

    Returns:
        JSON metadata (what was captured: target, rectangle, captured_kind,
        captured_document, view_type, whether anything was switched, opened or
        closed) plus the PNG image.
    """
    view_type = screenshot_target.implied_view_type(target, view_type, zoom_to)
    try:
        resolved = screenshot_target.resolve(target, view_type)
    except screenshot_target.TargetError as e:
        return json.dumps({"success": False, "error": str(e)})
    if resolved is None:
        return await _view_type_screenshot(view_type, zoom_to)
    if zoom_to and resolved.kind != "document":
        return json.dumps({"success": False,
                           "error": "zoom_to works with target 'document' or 'document:<board>' only"})
    return await _target_screenshot(resolved, zoom_to, open_if_closed)


def _altium_main_window():
    """(hwnd, title) of Altium's main frame, or (None, None)."""
    found = []

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and "Altium" in win32gui.GetWindowText(hwnd) \
                and not win32gui.IsIconic(hwnd):
            found.append(screenshot_target.Win(
                hwnd=hwnd, cls="", text=win32gui.GetWindowText(hwnd),
                rect=_visible_rect(hwnd),
                pid=win32process.GetWindowThreadProcessId(hwnd)[1]))
        return True
    win32gui.EnumWindows(collect, 0)
    frame = screenshot_target.pick_main_window(found, set(win_processes.pids_by_name("X2.EXE")))
    return (frame.hwnd, frame.text) if frame else (None, None)


def _dwm_attribute(hwnd, attribute, value):
    import ctypes
    from ctypes import wintypes
    try:
        return ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0
    except Exception:
        return False


def _visible_rect(hwnd):
    """The part of a top-level window that is drawn. GetWindowRect also counts
    the invisible resize border: 8 px around a maximised window, which then
    reaches onto the next monitor and over the taskbar. Child windows have no
    such border; for them DWM fails and GetWindowRect is right."""
    from ctypes import wintypes
    r = wintypes.RECT()
    if _dwm_attribute(hwnd, 9, r) and r.right > r.left and r.bottom > r.top:  # DWMWA_EXTENDED_FRAME_BOUNDS
        return (r.left, r.top, r.right, r.bottom)
    return tuple(win32gui.GetWindowRect(hwnd))


def _is_cloaked(hwnd):
    """Visible to Windows but not drawn (e.g. apps on another virtual desktop)."""
    from ctypes import wintypes
    cloaked = wintypes.DWORD(0)
    return _dwm_attribute(hwnd, 14, cloaked) and cloaked.value != 0  # DWMWA_CLOAKED


def _win(hwnd, parent_cls=""):
    return screenshot_target.Win(hwnd=hwnd, cls=win32gui.GetClassName(hwnd),
                                 text=win32gui.GetWindowText(hwnd),
                                 rect=_visible_rect(hwnd), parent_cls=parent_cls)


def _windows_above(hwnd, pid):
    """Drawn top-level windows of other programs above hwnd, topmost last."""
    out = []
    h = win32gui.GetWindow(hwnd, win32con.GW_HWNDPREV)
    for _ in range(10000):
        if not h:
            break
        try:
            if win32gui.IsWindowVisible(h) and not win32gui.IsIconic(h) and not _is_cloaked(h)                     and win32process.GetWindowThreadProcessId(h)[1] != pid:
                w = _win(h)
                w.pid = win32process.GetWindowThreadProcessId(h)[1]
                if w.width > 0 and w.height > 0:
                    out.append(w)
        except Exception:
            pass  # the window went away meanwhile
        h = win32gui.GetWindow(h, win32con.GW_HWNDPREV)
    return out


def _altium_top_windows(pid):
    """Altium's visible top-level windows, topmost first."""
    out = []

    def collect(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            out.append(_win(hwnd))
        return True
    win32gui.EnumWindows(collect, 0)
    return out


def _visible_children(hwnd):
    out = []

    def collect(child, _):
        if win32gui.IsWindowVisible(child):
            parent = win32gui.GetParent(child)
            out.append(_win(child, win32gui.GetClassName(parent) if parent else ""))
        return True
    try:
        win32gui.EnumChildWindows(hwnd, collect, 0)
    except Exception:
        pass  # EnumChildWindows raises when the callback list is empty
    return out


def _virtual_screen():
    x, y = win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77)
    return (x, y, x + win32api.GetSystemMetrics(78), y + win32api.GetSystemMetrics(79))


def _grab_screen_rect(rect) -> Image.Image:
    """Copy a rectangle of the screen (physical pixels). CAPTUREBLT takes
    layered windows along, which popup menus and some dialogs are."""
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    desktop = win32gui.GetDesktopWindow()
    hwnd_dc = win32gui.GetWindowDC(desktop)
    mfc_dc = save_dc = bitmap = None
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (left, top), win32con.SRCCOPY | 0x40000000)
        info = bitmap.GetInfo()
        return Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']),
                                bitmap.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
    finally:
        if save_dc is not None:
            save_dc.DeleteDC()
        if bitmap is not None and bitmap.GetHandle():
            win32gui.DeleteObject(bitmap.GetHandle())
        if mfc_dc is not None:
            mfc_dc.DeleteDC()
        win32gui.ReleaseDC(desktop, hwnd_dc)


def _run_dpi_aware(fn):
    """Run fn with this thread per-monitor DPI aware, so window rectangles
    and the screen copy are both in physical pixels (see get_screenshot)."""
    import ctypes
    set_dpi_context = ctypes.windll.user32.SetThreadDpiAwarenessContext
    set_dpi_context.restype = ctypes.c_void_p
    set_dpi_context.argtypes = [ctypes.c_void_p]
    previous = set_dpi_context(-4) or set_dpi_context(-3)
    try:
        return fn()
    finally:
        if previous:
            set_dpi_context(previous)


async def _target_screenshot(t, zoom_to, open_if_closed):
    logger.info(f"Screenshot of {t.describe()} (zoom_to={zoom_to}, open_if_closed={open_if_closed})")
    previous_window = win32gui.GetForegroundWindow()
    info = {"success": True, "target": t.describe(), "switched_document": False,
            "opened_document": False, "closed_again": False, "restored_document": None}
    restore_path = opened_path = None
    focused_kind = ""

    # Only a named document (or a zoom) needs Altium itself; everything else
    # is read from the windows, so it also works while a dialog is open
    if t.kind == "document" and (t.name or zoom_to):
        response = await altium_bridge.execute_command(
            "focus_document", screenshot_target.focus_params(t, zoom_to, open_if_closed))
        if not response.get("success", False):
            await asyncio.to_thread(_return_foreground, previous_window)
            return json.dumps({"success": False, "target": t.describe(),
                               "error": response.get("error", "Could not bring the document to the front")})
        focus = response.get("result", {})
        focus = focus if isinstance(focus, dict) else {}
        before = focus.get("previous_document") or ""
        now = focus.get("focused_document") or ""
        focused_kind = focus.get("focused_kind") or ""
        info["document"] = now or None
        info["switched_document"] = bool(before and now and before.lower() != now.lower())
        info["opened_document"] = bool(focus.get("opened"))
        if "zoomed_component_count" in focus:
            info["zoomed_component_count"] = focus["zoomed_component_count"]
        if info["switched_document"]:
            restore_path = before
        if info["opened_document"]:
            opened_path = now

    def capture():
        hwnd, title = _altium_main_window()
        if not hwnd:
            return {"success": False, "error": "No Altium window found"}
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]

        def altium_in_front():
            fg = win32gui.GetForegroundWindow()
            return bool(fg) and win32process.GetWindowThreadProcessId(fg)[1] == pid

        # Always raise the main frame, even when some Altium window already
        # has the foreground: a document Altium has just opened is not painted
        # until its frame is brought up (seen with a freshly opened board)
        try:
            _bring_to_front(hwnd)
        except Exception as e:
            logger.warning(f"Could not bring Altium to the front: {e}")
        activated = False
        deadline = time.time() + 1.5
        while time.time() < deadline and not activated:
            time.sleep(0.1)
            activated = altium_in_front()
        # Let a switched or freshly shown view paint
        time.sleep(1.0 if info["switched_document"] or info["opened_document"] else 0.4)

        try:
            window = None
            if t.kind == "current":
                window = _win(hwnd)
            elif t.kind == "dialog":
                window = screenshot_target.pick_dialog(_altium_top_windows(pid))
                if window is None:
                    return {"success": False, "error": "No Altium dialog is open"}
            elif t.kind == "panel":
                children = _visible_children(hwnd)
                window = screenshot_target.pick_panel(children, t.name)
                if window is None:
                    return {"success": False,
                            "error": f"Panel '{t.name}' is not visible. Visible panels: "
                                     + (", ".join(screenshot_target.panel_names(children)) or "none")}
            elif t.kind == "document":
                children = _visible_children(hwnd)
                wanted = info.get("document") or ""
                window = screenshot_target.pick_document_view(children, wanted)
                if window is None:
                    views = [v.text for v in screenshot_target.document_views(children)]
                    return {"success": False,
                            "error": f"No visible editing area for '{wanted or 'the active document'}'. "
                                     f"Visible documents: {', '.join(views) or 'none'}"}
            if t.kind == "region":
                # Relative to the main window exactly as "current" captures it
                frame = screenshot_target.clip(_win(hwnd).rect, _virtual_screen())
                if frame is None:
                    return {"success": False, "error": "Altium's main window is off the screen"}
                try:
                    rect = screenshot_target.region_in_window(t.region, frame)
                except screenshot_target.TargetError as e:
                    return {"success": False, "error": str(e)}
                info["window_class"] = None
                info["window_title"] = None
            else:
                rect = screenshot_target.clip(window.rect, _virtual_screen())
                if rect is None:
                    return {"success": False, "error": f"'{t.describe()}' is off the screen"}
                info["window_class"] = window.cls
                info["window_title"] = window.text
            if t.kind in ("current", "document"):
                views = screenshot_target.document_views(_visible_children(hwnd))
                info["active_document_view"] = views[0].text if views else None
            # The capture is a copy of the screen: refuse it when a window of
            # another program lies over the area, before and after the copy
            top = window.hwnd if t.kind == "dialog" else hwnd

            def covered():
                return screenshot_target.covering(rect, _windows_above(top, pid))

            def refuse(cover):
                programs = [os.path.basename(win_processes.image_path(w.pid)) for w in cover]
                return {"success": False, "error": screenshot_target.covering_error(programs)}
            cover = covered()
            if cover:
                return refuse(cover)
            img = _grab_screen_rect(rect)
            # A view that was just opened or switched to can still be unpainted
            # (all one colour): give it up to about 5 s more
            tries = 0
            while screenshot_target.is_blank(img) and tries < 10 and t.kind != "region":
                time.sleep(0.5)
                tries += 1
                img = _grab_screen_rect(rect)
            cover = covered()
            if cover:
                return refuse(cover)
            info["blank"] = screenshot_target.is_blank(img)
            info["paint_retries"] = tries
        finally:
            _return_foreground(previous_window)
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        png_bytes = buffer.getvalue()
        info.update(screenshot_target.captured_metadata(
            t, info.get("document"), focused_kind, info.get("active_document_view")))
        info.update({"rect": list(rect), "width": rect[2] - rect[0], "height": rect[3] - rect[1],
                     "foreground_confirmed": activated, "image_format": "PNG",
                     "debug_file": _save_debug_screenshot(t.kind, png_bytes)})
        return {"success": True, "png": png_bytes}

    try:
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_run_dpi_aware, capture), timeout=20)
        except asyncio.TimeoutError:
            result = {"success": False, "error": "Screenshot operation timed out"}
    finally:
        # Put Altium back the way it was, also when the capture failed
        if restore_path or opened_path:
            back = await altium_bridge.execute_command(
                "focus_document", {"path": restore_path or "", "close_path": opened_path or "",
                                   "open_if_closed": "false"})
            ok = back.get("success", False)
            res = back.get("result", {}) if ok else {}
            res = res if isinstance(res, dict) else {}
            info["restored_document"] = restore_path if (restore_path and ok) else None
            info["closed_again"] = bool(res.get("closed"))
            if not ok:
                info["restore_error"] = back.get("error", "unknown error")
            # That command brought Altium up again
            await asyncio.to_thread(_return_foreground, previous_window)

    if not result.get("success", False):
        out = {"success": False, "target": t.describe(), "error": result.get("error", "unknown error")}
        for k in ("restored_document", "closed_again", "restore_error"):
            if info.get(k):
                out[k] = info[k]
        return json.dumps(out)
    return [json.dumps(info), MCPImage(data=result["png"], format="png")]


async def _view_type_screenshot(view_type: str, zoom_to: list = None):
    """The old get_screenshot: view_type 'pcb'/'sch' focuses some document of
    that kind (opening one when none is open) and captures the whole window."""
    logger.info(f"Taking screenshot of Altium {view_type} window (zoom_to={zoom_to})")
    previous_window = win32gui.GetForegroundWindow()

    try:
        # First, execute the Altium command to ensure the right document type
        # is focused, optionally zooming to the requested components
        params = {"view_type": view_type.lower()}
        if zoom_to:
            params["designators"] = zoom_to
        response = await altium_bridge.execute_command(
            "take_view_screenshot",
            params
        )
        
        # Check for success
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error focusing {view_type} document: {error_msg}")
            return json.dumps({"success": False, "error": f"Failed to focus the correct document type: {error_msg}"})

        # Altium reports which document ended up focused. If it is not the
        # kind that was asked for (typically: no document of that kind is
        # open), say so instead of returning the wrong editor labelled as
        # the requested view.
        focus_info = response.get("result", {})
        focus_info = focus_info if isinstance(focus_info, dict) else {}
        focused_kind = str(focus_info.get("focused_kind", "") or "").upper()
        focused_document = focus_info.get("focused_document", "")
        wanted_kind = {"pcb": "PCB", "sch": "SCH"}.get(view_type.lower())
        if wanted_kind and focused_kind and focused_kind != wanted_kind:
            logger.error(f"Requested {view_type} view but {focused_kind} document is focused")
            return json.dumps({
                "success": False,
                "error": f"Requested a '{view_type}' view but Altium has a {focused_kind} "
                         f"document focused ({focused_document}). Is a "
                         f"{wanted_kind} document open in the active project?",
                "requested_view_type": view_type,
                "focused_kind": focused_kind,
                "focused_document": focused_document})

        # The capture runs in a worker thread: it waits for Altium to come to
        # the front and paint, which must not block the server's event loop
        import ctypes

        def capture_in_physical_pixels():
            # Work in physical pixels. Python is not DPI aware, so on a
            # scaled display Windows hands this process window rects
            # divided by the scale factor (1080x902 for a 2160x1804
            # window at 200%), while the BitBlt copies the window's real
            # pixels: the capture came back as only the top-left part of
            # the window. Per-monitor awareness for this thread gives
            # GetWindowRect the physical size. The worker thread is pooled
            # and reused, so its previous setting is restored afterwards.
            # V2 (-4) needs Windows 10 1703; V1 (-3) is the fallback.
            set_dpi_context = ctypes.windll.user32.SetThreadDpiAwarenessContext
            set_dpi_context.restype = ctypes.c_void_p
            set_dpi_context.argtypes = [ctypes.c_void_p]
            previous_dpi_context = set_dpi_context(-4) or set_dpi_context(-3)
            if not previous_dpi_context:
                logger.warning("Could not make the capture thread DPI aware; on a scaled display the capture may show only part of the window")
            try:
                return capture_screenshot()
            finally:
                if previous_dpi_context:
                    set_dpi_context(previous_dpi_context)

        def capture_screenshot():
            try:
                # Find Altium windows
                altium_windows = []
                altium_fallback_windows = []
                
                def collect_altium_windows(hwnd, _):
                    if win32gui.IsWindowVisible(hwnd):
                        title = win32gui.GetWindowText(hwnd)
                        
                        # First, look for windows with Altium and .PrjPcb in the title
                        if "Altium" in title and ".PrjPcb" in title:
                            altium_windows.append({
                                "handle": hwnd,
                                "title": title,
                                "class_name": win32gui.GetClassName(hwnd),
                                "rect": win32gui.GetWindowRect(hwnd)
                            })
                        # Collect any window with Altium in the title as fallback
                        elif "Altium" in title:
                            altium_fallback_windows.append({
                                "handle": hwnd,
                                "title": title,
                                "class_name": win32gui.GetClassName(hwnd),
                                "rect": win32gui.GetWindowRect(hwnd)
                            })
                    return True
                
                win32gui.EnumWindows(collect_altium_windows, 0)
                
                # If no specific Altium .PrjPcb windows found, use the fallback
                if not altium_windows and altium_fallback_windows:
                    altium_windows = altium_fallback_windows
                
                if not altium_windows:
                    return {
                        "success": False,
                        "error": f"No Altium windows found for {view_type} view"
                    }

                # Altium owns several top-level windows with its name in the
                # title, and while it switches documents a small transient one
                # can come first. Take the largest window that is not
                # minimized: that is the main frame.
                def area(w):
                    l, t, r, b = w["rect"]
                    return max(0, r - l) * max(0, b - t)
                candidates = [w for w in altium_windows if not win32gui.IsIconic(w["handle"])]
                window = max(candidates or altium_windows, key=area)
                hwnd = window["handle"]
                
                # Bring Altium to the front and let it paint. Altium only
                # renders a schematic view once it has actually been shown on
                # screen, so a capture taken while it sits behind another
                # window comes back with a blank canvas. Letters typed while
                # it is in front go to its menus (W then H tiles the
                # documents), so whoever was in front gets the foreground back.
                activated = False
                altium_pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                previous_pid = win32process.GetWindowThreadProcessId(previous_window)[1] if previous_window else 0

                def altium_is_foreground():
                    # Altium owns several top-level windows; any of them
                    # being foreground means Altium is in front.
                    fg = win32gui.GetForegroundWindow()
                    return bool(fg) and win32process.GetWindowThreadProcessId(fg)[1] == altium_pid

                try:
                    _bring_to_front(hwnd)
                    deadline = time.time() + 1.5
                    while time.time() < deadline:
                        time.sleep(0.1)
                        if altium_is_foreground():
                            activated = True
                            break
                except Exception as e:
                    logger.warning(f"Could not bring window to foreground: {e}")
                if not activated:
                    logger.warning("Altium is not the foreground window; the capture may show an unpainted view")
                # A freshly switched-to document needs a moment to render.
                time.sleep(1.0)

                # Measure AFTER activation: restoring or switching can change
                # the frame's size.
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width = right - left
                height = bottom - top
                if width < 200 or height < 150:
                    return {"success": False, "error": f"Altium window is too small to capture ({width}x{height}); is it minimized?"}

                # Take screenshot using GDI functions instead of ImageGrab
                try:
                    try:
                        img = _grab_window_image(hwnd, width, height)
                    finally:
                        # Not when the user has already moved on by themselves
                        if previous_pid not in (0, altium_pid) and altium_is_foreground():
                            _bring_to_front(previous_window)

                    # Encode once: the same PNG bytes are returned and, when
                    # asked for, kept as a debug copy
                    buffer = io.BytesIO()
                    img.save(buffer, format='PNG')
                    png_bytes = buffer.getvalue()
                    debug_filename = _save_debug_screenshot(view_type, png_bytes)

                    return {
                        "success": True,
                        "width": width,
                        "height": height,
                        "window_title": window["title"],
                        "window_class": window["class_name"],
                        "view_type": view_type,
                        "foreground_confirmed": activated,
                        "image_format": "PNG",
                        "debug_file": debug_filename,
                        "image_data": png_bytes
                    }

                except Exception as e:
                    import traceback
                    trace = traceback.format_exc()
                    logger.error(f"GDI screenshot error: {e}\n{trace}")
                    return {
                        "success": False,
                        "error": f"GDI screenshot failed: {str(e)}",
                        "traceback": trace
                    }

            except Exception as e:
                import traceback
                return {
                    "success": False,
                    "error": f"Screenshot thread error: {str(e)}",
                    "traceback": traceback.format_exc()
                }

        # A capture that hangs (Altium never painting, a stuck window call)
        # gives up after 10 s; the worker thread then finishes on its own
        try:
            result = await asyncio.wait_for(asyncio.to_thread(capture_in_physical_pixels), timeout=10)
        except asyncio.TimeoutError:
            logger.error("Screenshot thread timed out")
            return json.dumps({"success": False, "error": "Screenshot operation timed out"})

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            logger.error(f"Screenshot error: {error_msg}")
            return json.dumps({"success": False, "error": error_msg})

        logger.info(f"Screenshot taken successfully, size: {result['width']}x{result['height']}")

        # Return the PNG as a proper MCP image content block instead of inline
        # base64 text: raw base64 in the text result exceeds client token
        # limits (a full-window capture is ~300 KB), while image blocks are
        # rendered natively by clients
        png_bytes = result.pop("image_data")
        zoom_info = response.get("result", {})
        if isinstance(zoom_info, dict) and "zoomed_component_count" in zoom_info:
            result["zoomed_component_count"] = zoom_info["zoomed_component_count"]
        # What was actually captured, as opposed to what was requested
        result["captured_kind"] = focused_kind or None
        result["captured_document"] = focused_document or None
        return [
            json.dumps(result),
            MCPImage(data=png_bytes, format="png"),
        ]

    except Exception as e:
        logger.error(f"Error in screenshot function: {str(e)}")
        return json.dumps({"success": False, "error": f"Failed to take screenshot: {str(e)}"})
    
@_tool()
async def layout_duplicator(ctx: Context) -> str:
    """
    First step of layout duplication. Selects source components and returns data to match with destination components.
    
    Returns:
        str: JSON object with source and destination component data for matching
    """
    logger.info("Starting layout duplication - selection phase")
    
    # Execute the command in Altium to get component data
    response = await altium_bridge.execute_command(
        "layout_duplicator", 
        {}
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error in layout duplication selection: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to start layout duplication: {error_msg}"})
    
    # Get the component data
    components_data = response.get("result", {})
    
    if not components_data:
        logger.info("No component data found")
        return json.dumps({"success": False, "error": "No component data returned from Altium"})
    
    # Parse the result to check if no source components were selected
    try:
        if isinstance(components_data, str):
            result_json = json.loads(components_data)
            if not result_json.get("success", True):
                logger.info(f"Source component selection issue: {result_json.get('message', 'Unknown issue')}")
                return json.dumps(result_json)
    except Exception as e:
        logger.error(f"Error parsing layout duplicator result: {e}")
    
    logger.info(f"Retrieved layout duplicator component data")
    return json.dumps(components_data, indent=2)

@_tool()
async def layout_duplicator_apply(ctx: Context, source_designators: list, destination_designators: list) -> str:
    """
    Second step of layout duplication. Applies the layout of source components to destination components.
    
    Args:
        source_designators (list): List of source component designators (e.g., ["R1", "C5", "U3"])
        destination_designators (list): List of destination component designators (e.g., ["R10", "C15", "U7"])
    
    Returns:
        str: JSON object with the result of the layout duplication
    """
    logger.info(f"Applying layout duplication from {source_designators} to {destination_designators}")
    
    # Execute the command in Altium to apply layout duplication
    response = await altium_bridge.execute_command(
        "layout_duplicator_apply",
        {
            "source_designators": source_designators,
            "destination_designators": destination_designators
        }
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error applying layout duplication: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to apply layout duplication: {error_msg}"})
    
    # Get the result data
    result = response.get("result", {})
    
    logger.info(f"Layout duplication applied successfully")
    return json.dumps(result, indent=2)
    
@_tool(read_only=True)
async def get_pcb_rules(ctx: Context) -> str:
    """
    Get all design rules from the current Altium PCB
    
    Returns:
        str: JSON array of PCB design rules: name, enabled, priority (1 is
             the highest within a rule kind), descriptor, rule_kind and the
             two scope expressions (filter1, filter2)
    """
    logger.info("Getting PCB design rules")
    
    # Execute the command in Altium to get rule data
    response = await altium_bridge.execute_command(
        "get_pcb_rules",
        {}  # No parameters needed
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting PCB rules: {error_msg}")
        return json.dumps({"error": f"Failed to get PCB rules: {error_msg}"})
    
    # Get the rules data
    rules_data = response.get("result", [])
    
    if not rules_data:
        logger.info("No PCB rules found")
        return json.dumps({"message": "No PCB rules found in the current document"})
    
    logger.info(f"Retrieved PCB rules data")
    return json.dumps(rules_data, indent=2)

@_tool()
async def run_drc(ctx: Context, board_path: str = "", max_violations: int = 200, rules: list = None,
                  nets: list = None, area: list = None) -> str:
    """
    Run the design rule check (DRC) on a PCB and list the violations.

    Runs Altium's own batch DRC with the board's rules and the options last
    set in its Design Rule Checker dialog. The dialog is answered from
    outside Altium: it may flash on screen. Altium writes its usual report
    beside the board (or in the project's output folder) and may open it.
    The violations stay on the board, marked as Altium marks them.

    A cancelled check leaves no violations, exactly like a clean board, so
    "ran" says whether the check really ran: the Run button was pressed and
    the dialog closed. "report" (when Altium wrote one) gives the per-rule
    counts and confirms it independently.

    Args:
        board_path (str): Full path of the .PcbDoc to check. It is opened if
            needed and brought to the front. Empty: the focused board, or
            the one the PCB tools last used.
        rules (list): Only violations of these rules: each is a rule's own
            name or a rule kind ("Clearance", "Width", "Short-Circuit",
            "Un-Routed Net", "Hole To Hole Clearance", ...), whole, in any
            case and with or without spaces. "Clearance" is the electrical
            clearance rules only, not hole-to-hole or silk clearances. The
            whole check still runs: Altium checks every enabled rule.
        nets (list): Only violations with an object on one of these nets.
        area (list): Only violations located in [x1, y1, x2, y2] (mils from
            the board origin), by their own or an object's position.
            With any filter, matching_count counts the matching violations;
            listed_by_rule always counts the ones listed.
        max_violations (int): How many violations to list in full.
            violation_count counts every violation the check found, but
            Altium stops the check at its own violation limit (500 unless
            changed in its Design Rule Checker dialog); "complete": false
            then says the board may have more.

    Returns:
        str: JSON with ran, violation_count, the violations (rule,
             description, x_mils/y_mils relative to the board origin, and
             the primitives involved with their net and layer), and the
             report's per-rule counts.
    """
    import drc

    # The filters are checked before the check runs, which takes a while.
    # A lone name counts as a list of one, not as letters.
    rules = [rules] if isinstance(rules, str) else rules
    nets = [nets] if isinstance(nets, str) else nets
    for label, names in (("rules", rules), ("nets", nets)):
        if names is not None and (not isinstance(names, (list, tuple))
                                  or not all(isinstance(n, str) for n in names)):
            return json.dumps({"success": False, "error": f"{label} is a list of names"})
    rules = [r for r in rules or () if r.strip()]
    nets = [n for n in nets or () if n.strip()]
    if area is not None and (not isinstance(area, (list, tuple)) or len(area) != 4 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in area)):
        return json.dumps({"success": False, "error": "area is [x1, y1, x2, y2] in mils from the board origin"})
    filtered = bool(rules or nets or area is not None)

    board_path = long_path(board_path)
    logger.info(f"Running DRC on {board_path or 'the focused board'}")
    started = time.time()
    outcome = drc.DialogOutcome()
    driver = asyncio.create_task(drc.drive_dialog(outcome, PROGRESS_FILE))
    try:
        response = await altium_bridge.execute_command(
            "run_drc", {"board_path": board_path,
                        # filtered: the violations Altium kept (500 unless its
                        # limit was raised), up to a cap, then filter
                        "max_violations": drc.FILTER_FETCH if filtered else int(max_violations)})
    finally:
        driver.cancel()
        try:
            await driver
        except asyncio.CancelledError:
            pass

    if not response.get("success", False):
        return json.dumps({"success": False, "ran": outcome.pressed,
                           "error": f"DRC failed: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result, strict=False)
        except ValueError:
            return json.dumps({"success": False, "error": f"unexpected reply: {result[:200]}"})

    out = {"success": True, "ran": outcome.pressed}
    if not outcome.pressed:
        out["success"] = False
        out["error"] = ("The check did not run: " +
                        ("the Design Rule Checker dialog could not be started and was closed. "
                         if outcome.seen else "the Design Rule Checker dialog never appeared. ") +
                        "violation_count is what was already on the board, not a result.")
        if outcome.notes:
            out["notes"] = outcome.notes
    out.update({k: v for k, v in result.items() if k != "success"})
    # By how much each violation fails, as numbers; then the filters
    for v in out.get("violations", []):
        v.update(drc.measurement(v.get("description", "")))
    if filtered:
        fetched = out.get("violations", [])
        picked = [v for v in fetched if drc.matches(v, rules, nets, area)]
        out["matching_count"] = len(picked)
        out["violations"] = picked[:max(0, int(max_violations))]
        if out.get("violation_count", 0) > len(fetched):
            out["filter_note"] = (f"The filters looked at the first {len(fetched)} of "
                                  f"{out['violation_count']} violations; matching_count may be low.")
    by_rule = {}
    for v in out.get("violations", []):
        by_rule[v.get("rule", "")] = by_rule.get(v.get("rule", ""), 0) + 1
    out["listed_by_rule"] = by_rule
    report = drc.find_report(result.get("board", board_path), started)
    if report is not None:
        try:
            out["report"] = {"path": str(report),
                             **drc.parse_report(read_altium_text(report))}
        except OSError:
            pass
    if out.get("report", {}).get("stopped"):
        out["complete"] = False
        out["note"] = ("Altium stopped the check at its violation limit (set in its Design Rule "
                       "Checker dialog), so the board may have more violations than violation_count.")
    return json.dumps(out, indent=2)

@_tool(read_only=True)
async def get_pcb_layer_stackup(ctx: Context) -> str:
    """
    Get the detailed layer stackup information from the current Altium PCB including
    copper thickness, dielectric materials, constants, and heights
    
    Returns:
        str: JSON object with detailed layer stackup information
    """
    logger.info("Getting PCB layer stackup information")
    
    # Execute the command in Altium to get layer stackup data
    response = await altium_bridge.execute_command(
        "get_pcb_layer_stackup",
        {}  # No parameters needed
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting PCB layer stackup: {error_msg}")
        return json.dumps({"error": f"Failed to get PCB layer stackup: {error_msg}"})
    
    # Get the stackup data
    stackup_data = response.get("result", {})
    
    if not stackup_data:
        logger.info("No PCB layer stackup found")
        return json.dumps({"message": "No PCB layer stackup found in the current document"})
    
    logger.info(f"Retrieved PCB layer stackup data")
    return json.dumps(stackup_data, indent=2)

@_tool()
async def create_pcb_rule(ctx: Context, rule_type: str, name: str, values: dict,
                          scope1: str = "All", scope2: str = "All", comment: str = "",
                          replace: bool = False, board_path: str = "") -> str:
    """
    Add a design rule to a PCB.

    Values per rule_type, distances in mils:
      clearance              gap_mils (between scope1 and scope2)
      width                  min_mils, max_mils, preferred_mils (all signal layers)
      hole_size              min_mils, max_mils
      routing_via            min/max/preferred_diameter_mils,
                             min/max/preferred_hole_mils
      solder_mask_expansion  expansion_mils (negative shrinks the opening)
      paste_mask_expansion   expansion_mils
      polygon_connect        connect_style ("relief", "direct", "none"), and
                             optionally conductor_width_mils, air_gap_mils,
                             entries (2 or 4)

    Scopes are Altium query expressions, e.g. "All", "InNet('GND')",
    "InNetClass('Power')", "IsVia", "HasFootprint('0402')". A one-scope
    rule (width, hole size, via, mask, ...) uses scope1 only.

    PRIORITY: Altium makes a rule added this way priority 1, the highest
    of its kind, and moves the others down; a script cannot set priority.
    So a new rule wins wherever its scope applies. Give it a narrow scope
    (a net, class or object kind), or reorder priorities in Altium's PCB
    Rules and Constraints Editor afterwards. get_pcb_rules lists all rules
    with their priorities. Check the result's "descriptor": it is what
    Altium stored.

    Args:
        rule_type (str): One of the kinds above.
        name (str): Rule name, unique on the board.
        values (dict): The values for that kind, e.g. {"gap_mils": 8}.
        scope1 (str): First (or only) object scope.
        scope2 (str): Second scope, for two-object rules such as clearance.
        comment (str): Optional rule comment.
        replace (bool): Replace an existing rule of the same name instead
            of refusing.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with the rule's name, descriptor, scopes, priority and
             whether it replaced an existing rule.
    """
    import pcb_rules

    board_path = long_path(board_path)
    encoded, error = pcb_rules.encode_values(rule_type, values)
    if error:
        return json.dumps({"success": False, "error": error})
    if not name.strip():
        return json.dumps({"success": False, "error": "name is empty"})
    logger.info(f"Creating {rule_type} rule {name}")
    response = await altium_bridge.execute_command("create_pcb_rule", {
        "rule_kind": rule_type, "rule_name": name.strip(), "scope1": scope1 or "All",
        "scope2": scope2 or "All", "comment": comment, "values": encoded,
        "replace": bool(replace), "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to create the rule: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    others = result.get("other_rules_of_kind", 0)
    if others and (scope1 or "All").strip().lower() == "all" and (scope2 or "All").strip().lower() == "all":
        result["warning"] = (f"This rule applies to everything at priority 1, so it now overrides the "
                             f"{others} other {rule_type} rule(s) on the board. Narrow its scope or "
                             f"move it down in Altium's rules editor if that is not intended.")
    return json.dumps(result, indent=2)

# Changes the design: a timeout leaves its outcome unknown, so no blind retry
MUTATING_COMMANDS.add("create_pcb_rule")

@_tool(read_only=True)
async def get_output_job_containers(ctx: Context, outjob_path: str = "") -> str:
    """
    Get all available output job containers from a specified OutJob file

    Args:
        outjob_path (str): Full path to the OutJob file (optional). When empty,
            the first OutJob found in the open projects is used, in project
            order, regardless of which document has focus.

    Returns:
        str: JSON with outjob_path (the file actually used) and the containers
    """
    logger.info(f"Getting output job containers from {outjob_path or 'the first open OutJob'}")

    # Execute the command in Altium to get output job containers
    response = await altium_bridge.execute_command(
        "get_output_job_containers",
        {"outjob_path": outjob_path}
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting output job containers: {error_msg}")
        return json.dumps({"error": f"Failed to get output job containers: {error_msg}"})
    
    # Get the containers data
    containers_data = response.get("result", [])
    
    if not containers_data:
        logger.info("No output job containers found")
        return json.dumps({"message": "No output job containers found"})
    
    logger.info(f"Retrieved output job containers data")
    return containers_data  # Already in JSON format

@_tool(read_only=True)
async def get_outjob_settings(ctx: Context, outjob_path: str, include_raw_settings: bool = False) -> str:
    """
    Read an .OutJob file from disk and return its outputs, containers and settings.

    Works without Altium and on OutJobs that are not open. It reads the SAVED
    file: changes not yet saved in Altium are not visible.

    Notes for interpreting the result:
    - An output is generated when it is connected to a container
      (connected_containers non-empty). The per-output "enabled" flag mirrors
      OutputEnabledN in the file and does not always match the UI.
    - Gerber layers are decoded from Altium V7 layer IDs; unknown IDs are
      reported as "Unknown layer 0x...".

    Args:
        outjob_path (str): Full path to the .OutJob file
        include_raw_settings (bool): Also return raw_settings, the output's
            settings as the file has them, for outputs with a decoded
            summary. It doubles the reply. Outputs without a summary always
            have raw_settings.

    Returns:
        str: JSON with path, group_name, variant, containers (name, type,
             output paths, file name expression, add_to_project) and outputs
             (type, name, document_path, variant, connected_containers and a
             decoded summary for Gerber, NC Drill, test points, pick and
             place and print outputs, or sections by name for a BOM)
    """
    try:
        data = parse_outjob(outjob_path, include_raw=include_raw_settings)
    except FileNotFoundError:
        return json.dumps({"error": f"OutJob not found: {outjob_path}"})
    except Exception as e:
        return json.dumps({"error": f"Could not parse OutJob: {e}"})
    return json.dumps(data, indent=2, ensure_ascii=False)


async def _simple_command(command: str, params: dict, what: str) -> str:
    """Run a bridge command and return its result as JSON text."""
    response = await altium_bridge.execute_command(command, params)
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting {what}: {error_msg}")
        return json.dumps({"error": f"Failed to get {what}: {error_msg}"})
    result = response.get("result", {})
    return result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False)


async def _bridge_json(command: str, params: dict, timeout: Optional[float] = None) -> Tuple[Optional[dict], str]:
    """(result, "") of a bridge command that returns JSON, or (None, error).
    One helper for the tools that work with the reply instead of passing it
    on; strict=False because Altium text can hold control characters."""
    if timeout is None:
        response = await altium_bridge.execute_command(command, params)
    else:
        response = await altium_bridge.execute_command(command, params, timeout=timeout)
    if not response.get("success", False):
        return None, response.get("error", "unknown error")
    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result, strict=False)
        except ValueError:
            return None, result if result.startswith("ERROR") else f"unexpected reply: {result[:300]}"
    if not isinstance(result, dict):
        return None, f"unexpected reply: {str(result)[:300]}"
    return result, ""


@_tool(read_only=True)
async def get_project_info(ctx: Context) -> str:
    """
    Get the focused Altium project: parameters, documents and variants.

    Read-only. Use it to check that nothing has unsaved changes before
    generating outputs, and to read project parameters and variants.

    Returns:
        str: JSON with project_path, parameters [{name, value}],
             schematic_sheet_count, modified_document_count, documents
             [{path, kind, state}] where state is "closed", "open" or
             "modified" (unsaved changes in memory), and variants
             [{name, variations [{designator, kind}]}] where kind is
             "not_fitted" or "alternate"
    """
    return await _simple_command("get_project_info", {}, "project info")


def _project_path(project_path: str):
    """A compile tool's project_path in its long form, and an error ('' when
    fine): empty for the focused project, else an existing .PrjPcb. Altium
    would open any project file it is given, a script project too."""
    if not project_path:
        return "", ""
    path = long_path(project_path)
    if not path.lower().endswith(".prjpcb"):
        return path, f"project_path must be a .PrjPcb file: {project_path}"
    if not os.path.isfile(path):
        return path, f"project_path not found: {project_path}"
    return path, ""


@_tool(read_only=True)
async def get_project_components(ctx: Context, include_parameters: bool = False, designators: list = None,
                                 sheet: str = "", offset: int = 0, limit: int = 0,
                                 parameter_names: list = None, project_path: str = "") -> str:
    """
    List the components of the compiled project (schematic side).

    Compiles the project in memory first (nothing is saved): project_path,
    or the focused project, as for get_erc_violations. Unlike
    the PCB component tools, this returns the schematic Comment of each
    instance, which is where values of generic passives usually live.

    A large project does not fit in one reply. A reply stops at about 60,000
    characters: when next_offset is not null, call again with
    offset=next_offset and the same filters for the rest. Filter first when
    you can: all parameters of every part can come to megabytes.

    Args:
        include_parameters (bool): Also return every component parameter
            (e.g. Manufacturer_Part_Number). Default False - the output is
            much larger with parameters.
        designators (list): Only these designators. Wildcards work and case
            does not matter, e.g. ["R*", "U1", "C1?"].
        sheet (str): Only this sheet, by file name ("Power.SchDoc");
            wildcards work.
        offset (int): Skip this many matching components.
        limit (int): At most this many components; 0 for as many as fit.
        parameter_names (list): Return only these parameters, e.g.
            ["Manufacturer_Part_Number"]. Implies include_parameters.
        project_path (str): Full path of the .PrjPcb to compile.

    Returns:
        str: JSON with project_path, component_count (the whole project),
             matched, offset, returned, next_offset and components
             [{designator, comment, footprint, library_reference, sheet,
             parameters?}]
    """
    import project_components

    project_path, error = _project_path(project_path)
    if error:
        return json.dumps({"success": False, "error": error})
    text = await _simple_command("get_project_components",
                                 {"include_parameters": include_parameters or bool(parameter_names),
                                  "project_path": project_path},
                                 "project components")
    try:
        data = json.loads(text, strict=False)
    except ValueError:
        return text
    if "components" not in data:
        return text
    return project_components.compact_json(project_components.select(
        data, designators=designators, sheet=sheet, offset=offset, limit=limit,
        parameter_names=parameter_names))


@_tool(read_only=True)
async def get_erc_violations(ctx: Context, compile: bool = True, project_path: str = "") -> str:
    """
    Compile a project and list its ERC/compiler violations.

    Give project_path to compile exactly that project (it is opened if
    needed): without it the focused project is compiled, which is whatever
    document last had focus, and "Free Documents" is refused. The result
    names the project it compiled.

    Compiling reads each sheet from disk, except a sheet open in Altium with
    unsaved changes: that one is compiled from Altium's copy and listed in
    unsaved_in_altium. Save it (or close it unsaved) for the file to count.
    Sheets the project lists but that are not on disk are in
    missing_on_disk. Compiling does not save any document (Altium may
    refresh the derived .PrjPcbStructure file).

    Args:
        compile (bool): Compile before reading (default True). False returns
            the violations of the last compilation.
        project_path (str): Full path of the .PrjPcb to compile.

    Returns:
        str: JSON with project_path, unsaved_in_altium, missing_on_disk,
             total, fatal, errors, warnings and violations
             [{level, type, detail}]
    """
    project_path, error = _project_path(project_path)
    if error:
        return json.dumps({"success": False, "error": error})
    return await _simple_command("get_erc_violations", {"compile": compile, "project_path": project_path},
                                 "ERC violations")


@_tool(read_only=True)
async def get_board_summary(ctx: Context) -> str:
    """
    Summarise the focused PCB for fabrication checks.

    Read-only. Coordinates are millimetres relative to the board origin.

    Returns:
        str: JSON with pcb_path, board origin (absolute), board_outline
             (bounding box, width, height, vertex count), dimensions (cotas)
             with their extents, via_count and vias_by_hole, components per
             side, layer_contents (primitive count per layer, split into free
             objects and objects inside components) and overlay_texts (free
             texts on Top/Bottom Overlay, e.g. board name and revision)
    """
    return await _simple_command("get_board_summary", {}, "board summary")


@_tool(read_only=True)
async def get_component_primitives(ctx: Context, designators: list) -> str:
    """
    List the primitives of PCB components with the layer each one is on.

    Read-only. Useful to check footprints that were flipped to the bottom
    side (e.g. fiducials: the copper and the pad that opens the solder mask
    must end up on the same layer) and pad mask expansions.

    Args:
        designators (list): Component designators, e.g. ["FD1", "FD3"]

    Returns:
        str: JSON with components [{designator, footprint, layer, x_mm, y_mm,
             rotation, primitives [{type, layer, ...}]}] and
             missing_designators
    """
    return await _simple_command("get_component_primitives", {"designators": designators},
                                 "component primitives")


@_tool()
async def run_output_jobs(ctx: Context, container_names: list, outjob_path: str = "") -> str:
    """
    Run specified output job containers

    The OutJob's project must be open in Altium. A PDF (Publish) container
    reports output_file and output_verified: true once Altium has written
    that file. output_verified is false for generated-file containers and
    for PDFs that Altium names itself (after each output, or from an
    expression). When a PDF already exists and the container prompts before
    overwriting (the default), Altium asks in a dialog and the run waits for
    the answer.

    Args:
        container_names (list): List of container names to run
        outjob_path (str): Full path to the OutJob file (optional). When empty,
            the first OutJob found in the open projects is used, in project
            order, regardless of which document has focus.

    Returns:
        str: JSON object with outjob_path (the file actually used) and the
             results of running the output jobs
    """
    logger.info(f"Running output jobs from {outjob_path or 'the first open OutJob'}")
    logger.info(f"Containers to run: {container_names}")

    # Execute the command in Altium to run output jobs
    response = await altium_bridge.execute_command(
        "run_output_jobs",
        {"outjob_path": outjob_path, "container_names": container_names}
    )
    
    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error running output jobs: {error_msg}")
        return json.dumps({"error": f"Failed to run output jobs: {error_msg}"})
    
    # Get the result data
    result_data = response.get("result", {})
    
    logger.info(f"Output jobs execution completed")
    
    # If result_data is a string, it's already in JSON format
    if isinstance(result_data, str):
        return result_data
    
    # Otherwise, convert to JSON
    return json.dumps(result_data, indent=2)

@_tool()
async def create_pcb_footprint(ctx: Context, footprint_name: str, description: str, pads: list, courtyard_x_mm: float = 0, courtyard_y_mm: float = 0) -> str:
    """
    Create a new PCB footprint in the currently active PcbLib document.
    The PcbLib (e.g. Discrete.PcbLib) must be the focused document in Altium.

    Pad format: each element is "pad_number|x_mm|y_mm|width_mm|height_mm|shape"
                shape options: Rect (default), Round, Oval
                Coordinates are in mm relative to component origin (0,0).
                Pin 1 is indicated by a gap in the top-left silkscreen corner.
                Pads are created locked.

    Courtyard & silkscreen are auto-generated from pad extents + 0.25 mm margin
    unless courtyard_x_mm / courtyard_y_mm are provided explicitly (half-dimensions).

    Args:
        footprint_name (str): Footprint name as it will appear in the library
        description (str): Description string
        pads (list): List of pad definitions, e.g. ["1|-0.9|0.55|1.0|0.8|Rect", ...]
        courtyard_x_mm (float): Half-width of courtyard in mm (0 = auto)
        courtyard_y_mm (float): Half-height of courtyard in mm (0 = auto)

    Returns:
        str: JSON object with result
    """
    logger.info(f"Creating PCB footprint: {footprint_name} with {len(pads)} pads")

    response = await altium_bridge.execute_command(
        "create_pcb_footprint",
        {
            "footprint_name": footprint_name,
            "description": description,
            "pads": pads,
            "courtyard_x_mm": courtyard_x_mm,
            "courtyard_y_mm": courtyard_y_mm,
        }
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error creating footprint: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to create footprint: {error_msg}"})

    result = response.get("result", {})
    logger.info(f"Footprint {footprint_name} created successfully")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def get_server_status(ctx: Context) -> str:
    """Get the current status of the Altium MCP server"""
    altium_bridge.config.discover_altium()
    status = {
        "server": "Running",
        "altium_exe": altium_bridge.config.altium_exe_path,
        "script_path": altium_bridge.config.script_path,
        "altium_found": os.path.exists(altium_bridge.config.altium_exe_path),
        "script_found": os.path.exists(altium_bridge.config.script_path),
    }
    
    return json.dumps(status, indent=2)



# ---- Verge local tools on upstream's sandbox ----------------------------------------------------------------
# These tools were written against the local branch's own sandbox runner. They run their DelphiScript through
# run_altium_script, so they inherit upstream's pre-run checks, loop guard, undo-group closing and Altium-only
# dialog handling. SandboxEmit lines (logged with this prefix) come back as "emitted".
SANDBOX_EMIT_PREFIX = "EMIT: "


async def _run_sandbox(script: str, timeout_seconds: int = 120):
    """Run a DelphiScript body in the sandbox for the local tools. Returns (payload_dict, images)."""
    raw = await run_altium_script(None, script, timeout_seconds)
    try:
        payload = json.loads(raw)
    except Exception:
        return {"success": False, "error": str(raw)}, []
    steps = payload.get("steps") or []
    emitted = [s[len(SANDBOX_EMIT_PREFIX):] for s in steps if s.startswith(SANDBOX_EMIT_PREFIX)]
    if emitted:
        payload["emitted"] = emitted
        payload["steps"] = [s for s in steps if not s.startswith(SANDBOX_EMIT_PREFIX)]
    images = []
    for p in payload.get("dialog_snapshots") or []:
        try:
            images.append(base64.b64encode(Path(p).read_bytes()).decode("ascii"))
        except OSError:
            pass
    return payload, images

EXECUTOR_LOCK_FILE = EXCHANGE_DIR / "executor.lock"


_EXECUTOR_ASYNC_LOCK = asyncio.Lock()


def _pid_is_alive(pid: int) -> bool:
    """True if a process with this PID currently exists.

    os.kill(pid, 0) is the POSIX idiom for this and must NOT be used here:
    on Windows os.kill TerminateProcess()es the target for any signal other
    than CTRL_C_EVENT/CTRL_BREAK_EVENT, so the "harmless" probe would kill
    the other session's server.
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True  # exists but unreadable - assume alive
    finally:
        kernel32.CloseHandle(handle)


def _running_altium_paths():
    """Executable paths of every running X2.EXE, lower-cased."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Process -Name X2 -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Path"],
            capture_output=True, text=True, timeout=20)
        return [l.strip().lower() for l in (r.stdout or "").splitlines() if l.strip()]
    except Exception as e:
        logger.warning(f"could not list running Altium processes: {e}")
        return None


def _require_running_altium(exe_path):
    """None if the configured Altium is running; else an error string.

    `X2.EXE -RScriptingSystem:RunScript(...)` LAUNCHES Altium when it is not
    already running. That is never what a tool call means, and it is how a
    script dispatch started a half-uninstalled Altium and crashed it. The
    check also catches a different Altium than the configured one running -
    the script would go to the wrong product.
    """
    running = _running_altium_paths()
    if running is None:
        return None  # could not tell; do not block on a broken probe
    want = str(exe_path).lower()
    if not running:
        return ("Altium is not running. Start it (the configured one is %s) and "
                "retry - this server will not launch Altium itself." % exe_path)
    if want not in running:
        return ("A different Altium is running than the one configured: running %s, "
                "configured %s. Fix altium_exe_path in config.json or start the "
                "configured one." % (running, exe_path))
    return None


def _capture_hwnd_png(hwnd):
    """Base64 PNG of a window, rendered with PrintWindow.

    PrintWindow asks the window to paint itself into a DC, so it works even
    when the window is obscured or the session is disconnected - unlike
    BitBlt screen-scraping, which returns blanks there.
    """
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return None
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfcDC, w, h)
        saveDC.SelectObject(bmp)
        ctypes.windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        info = bmp.GetInfo()
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                               bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(bmp.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning(f"could not capture dialog window: {e}")
        return None


def _split_dialog_images(dialogs):
    """Separate JSON-safe dialog records from their PNG attachments."""
    records, images = [], []
    for d in dialogs:
        png = d.pop("_png", None)
        if png:
            d = {**d, "image_attached": True}
            images.append(png)
        records.append(d)
    return records, images


def _sandbox_return(payload, images):
    """Return JSON alone, or JSON plus the dialog screenshots as image content."""
    text = json.dumps(payload, indent=2)
    if not images:
        return text
    return [text] + [MCPImage(data=base64.b64decode(p), format="png") for p in images]


def _read_dialog_text(hwnd):
    """Read a modal dialog's message text via UI Automation.

    This is the ONLY place a compile/runtime error message exists: Altium
    reports "Undeclared identifier: eSchDoc" or "EVariantTypeCastError: ..."
    in a modal box and nowhere else - not in the log, not in a return code.
    Delphi dialogs are custom-drawn, so EnumChildWindows yields class names
    rather than the message; UI Automation is what actually reaches the text.
    Same approach as dev/sandbox_runner.py.
    """
    try:
        ps = (
            "Add-Type -AssemblyName UIAutomationClient;"
            "Add-Type -AssemblyName UIAutomationTypes;"
            f"$el=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd});"
            "$all=$el.FindAll([System.Windows.Automation.TreeScope]::Descendants,"
            "[System.Windows.Automation.Condition]::TrueCondition);"
            "foreach($e in $all){if($e.Current.Name){Write-Output $e.Current.Name}}"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=15)
        # Button captions are not the message. Leaving them in made a Yes/No
        # confirmation look "readable", which suppressed the screenshot and
        # left no record of what was actually being asked.
        buttons = {"ok", "cancel", "yes", "no", "abort", "retry", "ignore",
                   "close", "help"}
        texts = [l.strip() for l in r.stdout.splitlines()
                 if l.strip() and l.strip().lower() not in buttons
                 and not l.strip().startswith(("XPPanels.", "TXP", "Panel"))]
        return " | ".join(texts[:6])
    except Exception as e:
        return f"(could not read: {e})"


def _competing_platform():
    """CircuitStudio (DXP.EXE) running alongside Altium Designer takes the
    RunScript dispatch, and the script dies there with 'Undeclared identifier:
    GetCurrentSchDocument'. Measured 2026-09-19. Returns a message or None."""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DXP.EXE", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    if "DXP.EXE" in out.upper():
        return ("CircuitStudio (DXP.EXE) is running and would receive the script dispatch "
                "instead of Altium Designer. Close it, or stop its script with "
                "\"C:\\Program Files (x86)\\Altium\\CS\\DXP.EXE\" -REditScript:Stop and close it.")
    return None


# ObjectId values, verified on AD21 - see dev/ALTIUM_SCRIPT_API.md.
SCH_DOC_OBJECTID = 32          # 33 is a schematic LIBRARY; never edit that here


_SCH_KIND_EMITTERS = {
    "netlabel": ("eNetLabel",
                 "'netlabel|' + Obj3.Text + '|' + IntToStr(Obj3.Location.X) + '|' "
                 "+ IntToStr(Obj3.Location.Y) + '|' + IntToStr(Obj3.Orientation)"),
    "power": ("ePowerObject",
              "'power|' + Obj3.Text + '|' + IntToStr(Obj3.Location.X) + '|' "
              "+ IntToStr(Obj3.Location.Y) + '|' + IntToStr(Obj3.Style)"),
    "port": ("ePort",
             "'port|' + Obj3.Name + '|' + IntToStr(Obj3.Location.X) + '|' "
             "+ IntToStr(Obj3.Location.Y) + '|' + Obj3.HarnessType + '|' "
             "+ IntToStr(Obj3.Width) + '|' + IntToStr(Obj3.Height) + '|' "
             "+ IntToStr(Obj3.IOType) + '|' + IntToStr(Obj3.Style) + '|' "
             "+ IntToStr(Obj3.Alignment)"),
    "component": ("eSchComponent",
                  "'component|' + Obj3.Designator.Text + '|' + Obj3.LibReference + '|' "
                  "+ IntToStr(Obj3.Location.X) + '|' + IntToStr(Obj3.Location.Y) + '|' "
                  "+ IntToStr(Obj3.CurrentPartID) + '|' + IntToStr(Obj3.PartCount)"),
    "junction": ("eJunction",
                 "'junction|' + IntToStr(Obj3.Location.X) + '|' + IntToStr(Obj3.Location.Y)"),
    "noerc": ("eNoERC",
              "'noerc|' + IntToStr(Obj3.Location.X) + '|' + IntToStr(Obj3.Location.Y)"),
}


_SCH_KINDS = list(_SCH_KIND_EMITTERS) + ["pin", "wire", "paramset", "harness", "signalharness",
                                          "blanket", "sheetsymbol"]


_PIN_BRANCH = """        if I3 = ePin then
        begin
            S1 := '?';
            S2 := '?';
            if Obj3.Container <> nil then
                if Obj3.Container.ObjectId = eSchComponent then
                begin
                    S1 := Obj3.Container.LibReference;
                    S2 := Obj3.Container.Designator.Text;
                end;
            List1.Add('pin|' + Obj3.Designator + '|' + Obj3.Name + '|' +
                      IntToStr(Obj3.Location.X) + '|' + IntToStr(Obj3.Location.Y) +
                      '|' + IntToStr(Obj3.OwnerPartId) + '|' +
                      IntToStr(Obj3.Orientation) + '|' + IntToStr(Obj3.PinLength) +
                      '|' + S1 + '|' + S2);
        end;"""


_WIRE_BRANCH = """        if I3 = eWire then
        begin
            S3 := 'wire';
            for I2 := 1 to Obj3.VerticesCount do
                S3 := S3 + '|' + IntToStr(Obj3.Vertex[I2].X) + ',' +
                          IntToStr(Obj3.Vertex[I2].Y);
            List1.Add(S3);
        end;"""


# A parameter set (directive) is a CONTAINER: its Name is the visible label and
# its meaning lives in hidden child parameters - a net class directive carries
# ClassName=<class>, a differential-pair directive DifferentialPair=True plus
# DifferentialPairClassName=<pair class>. Measured on the X9 comms sheet.
_PARAMSET_BRANCH = """        if I3 = eParameterSet then
        begin
            S3 := 'paramset|' + Obj3.Name + '|' + IntToStr(Obj3.Location.X) + '|' +
                  IntToStr(Obj3.Location.Y) + '|' + IntToStr(Obj3.Orientation) + '|' +
                  IntToStr(Obj3.Color);
            Obj4 := Obj3.SchIterator_Create;
            try
                Obj4.AddFilter_ObjectSet(MkSet(eParameter));
                Obj5 := Obj4.FirstSchObject;
                while Obj5 <> nil do
                begin
                    S3 := S3 + '|' + Obj5.Name + '=' + Obj5.Text;
                    if Obj5.IsHidden then S3 := S3 + '=H' else S3 := S3 + '=V';
                    Obj5 := Obj4.NextSchObject;
                end;
            finally
                Obj3.SchIterator_Destroy(Obj4);
            end;
            List1.Add(S3);
        end;"""


# Harness objects. A harness connector (ObjectId 53) is a container: Location
# is its TOP-LEFT corner, entries (54) hang at Location.Y - DistanceFromTop on
# Side 1 (right edge) or Side 0 (left edge), and the signal harness (56, a
# polyline) meets it at PrimaryConnectionPosition below the top on the
# opposite edge. These live in the .SchDoc "Additional" stream, which is why
# an offline parser that reads only FileHeader sees no harnesses at all.
_HARNESS_BRANCH = """        if I3 = eHarnessConnector then
        begin
            S3 := 'harness|' + Obj3.HarnessType + '|' + IntToStr(Obj3.Location.X) + '|' +
                  IntToStr(Obj3.Location.Y) + '|' + IntToStr(Obj3.XSize) + '|' +
                  IntToStr(Obj3.YSize) + '|' + IntToStr(Obj3.PrimaryConnectionPosition);
            Obj4 := Obj3.SchIterator_Create;
            try
                Obj4.AddFilter_ObjectSet(MkSet(eHarnessEntry));
                Obj5 := Obj4.FirstSchObject;
                while Obj5 <> nil do
                begin
                    S3 := S3 + '|' + Obj5.Name + ',' + IntToStr(Obj5.Location.X) + ',' +
                          IntToStr(Obj5.Location.Y) + ',' + IntToStr(Obj5.Side) + ',' +
                          IntToStr(Obj5.DistanceFromTop);
                    Obj5 := Obj4.NextSchObject;
                end;
            finally
                Obj3.SchIterator_Destroy(Obj4);
            end;
            List1.Add(S3);
        end;"""


_SIGNAL_HARNESS_BRANCH = """        if I3 = eSignalHarness then
        begin
            S3 := 'signalharness';
            for I2 := 1 to Obj3.VerticesCount do
                S3 := S3 + '|' + IntToStr(Obj3.Vertex[I2].X) + ',' +
                          IntToStr(Obj3.Vertex[I2].Y);
            List1.Add(S3);
        end;"""


# A sheet symbol (eSheetSymbol = 41) is the parent-sheet stand-in for a child
# sheet: SheetName.Text is the displayed name, SheetFileName.Text the child
# document. Its entries (eSheetEntry = 40) are what a child sheet's PORTS must
# match - name and harness type - or the project compiles with a harness or
# net-name conflict. `FileName` is undeclared; it is `SheetFileName`.
_SHEET_SYMBOL_BRANCH = """        if I3 = eSheetSymbol then
        begin
            S3 := 'sheetsymbol|' + Obj3.SheetName.Text + '|' + Obj3.SheetFileName.Text + '|' +
                  IntToStr(Obj3.Location.X) + '|' + IntToStr(Obj3.Location.Y) + '|' +
                  IntToStr(Obj3.XSize) + '|' + IntToStr(Obj3.YSize);
            Obj4 := Obj3.SchIterator_Create;
            try
                Obj4.AddFilter_ObjectSet(MkSet(eSheetEntry));
                Obj5 := Obj4.FirstSchObject;
                while Obj5 <> nil do
                begin
                    S3 := S3 + '|' + Obj5.Name + ',' + IntToStr(Obj5.Side) + ',' +
                          IntToStr(Obj5.DistanceFromTop) + ',' + IntToStr(Obj5.IOType) + ',' +
                          Obj5.HarnessType;
                    Obj5 := Obj4.NextSchObject;
                end;
            finally
                Obj3.SchIterator_Destroy(Obj4);
            end;
            List1.Add(S3);
        end;"""


# A blanket (eBlanket = 61) is the dashed rectangle whose border directive marks
# every net inside it. `Corner` is undeclared through the API; BoundingRectangle
# gives the extent (Left/Bottom/Right/Top).
_BLANKET_BRANCH = """        if I3 = eBlanket then
            List1.Add('blanket|' + IntToStr(Obj3.BoundingRectangle.Left) + '|' +
                      IntToStr(Obj3.BoundingRectangle.Bottom) + '|' +
                      IntToStr(Obj3.BoundingRectangle.Right) + '|' +
                      IntToStr(Obj3.BoundingRectangle.Top));"""


def _canon(path):
    """The one spelling Altium recognises: absolute, Windows backslashes.

    Client.IsDocumentOpen and GetDocumentByPath compare the path STRING. A
    forward-slash spelling of a document that is open under backslashes is
    "not open", so OpenDocument loads a SECOND instance of the same file from
    disk - measured: two live copies of one .SchLib with different contents,
    one per spelling. Every path handed to Altium goes through here.
    """
    if not path:
        return path
    return str(Path(str(path)).resolve()) if Path(str(path)).exists() else         str(Path(str(path))).replace("/", "\\")


def _pas_str(value) -> str:
    """A DelphiScript string literal - single quotes are doubled to escape."""
    return "'" + str(value).replace("'", "''") + "'"


def _target_document(script, document=None):
    """Point a generated sheet script at `document` (full path) instead of the
    focused document.

    GetCurrentSchDocument returns nil whenever the focused document is not a
    schematic - the user clicked another tab, or a compile focused the
    sandbox project - and a nil receiver is reported as "Undeclared
    identifier: DocumentName". GetSchDocumentByPath needs the sheet OPEN, not
    focused, and never changes what the user is looking at.
    """
    if not document:
        return script
    path = _canon(document)
    return script.replace("Obj1 := SchServer.GetCurrentSchDocument;",
                          "Obj1 := SchServer.GetSchDocumentByPath(%s);" % _pas_str(path), 1)


# The project that matters is the one owning the sheet being edited, not
# whatever Altium happens to have focused. After any run_altium_script the
# focused project is the sandbox SCRIPT project; compiling that yields no
# flattened document. Measured 2026-09-19.
# __DOC__ is the target document's full path ('' = use the focused schematic).
# Resolution: the project that owns that document; else the focused project
# unless it is a script project; else the ONLY open .PrjPcb - with several
# open and no owner match, it refuses rather than guessing (it once compiled
# an unrelated ESC project because "last .PrjPcb wins").
_PROJECT_PRELUDE = """Obj1 := nil;
S3 := '__DOC__';
if S3 = '' then Obj5 := SchServer.GetCurrentSchDocument
else Obj5 := SchServer.GetSchDocumentByPath(S3);
if Obj5 <> nil then S3 := Obj5.DocumentName;
I2 := 0;
for I3 := 0 to GetWorkspace.DM_ProjectCount - 1 do
begin
    Obj4 := GetWorkspace.DM_Projects(I3);
    if Pos('.PRJPCB', UpperCase(Obj4.DM_ProjectFullPath)) > 0 then
    begin
        I2 := I2 + 1;
        if Obj3 = nil then Obj3 := Obj4;
    end;
    if S3 <> '' then
        for B1 := 0 to Obj4.DM_LogicalDocumentCount - 1 do
            if UpperCase(Obj4.DM_LogicalDocuments(B1).DM_FullPath) = UpperCase(S3) then Obj1 := Obj4;
end;
if Obj1 = nil then
begin
    Obj1 := GetWorkspace.DM_FocusedProject;
    if Obj1 <> nil then
        if Pos('.PRJSCR', UpperCase(Obj1.DM_ProjectFullPath)) > 0 then Obj1 := nil;
end;
if (Obj1 = nil) and (I2 = 1) then Obj1 := Obj3;
if (Obj1 = nil) and (I2 > 1) then
begin
    S2 := '';
    for I3 := 0 to GetWorkspace.DM_ProjectCount - 1 do
        if Pos('.PRJPCB', UpperCase(GetWorkspace.DM_Projects(I3).DM_ProjectFullPath)) > 0 then
            S2 := S2 + ';' + GetWorkspace.DM_Projects(I3).DM_ProjectFullPath;
    List1.Add('ERR|several PCB projects are open and none owns the target document - pass document=; candidates' + S2);
end;"""


def _project_script(template, document=None):
    """Fill the project prelude's target document into a workspace script."""
    path = _canon(document) if document else ""
    return template.replace("__DOC__", str(path).replace("'", "''"))


def _sch_dump_script(kinds, document=None):
    """Build a read-only sheet dump emitting one pipe-delimited line per object."""
    branches = []
    for kind in kinds:
        if kind in _SCH_KIND_EMITTERS:
            const, expr = _SCH_KIND_EMITTERS[kind]
            branches.append("        if I3 = %s then\n            List1.Add(%s);"
                            % (const, expr))
    if "pin" in kinds:
        branches.append(_PIN_BRANCH)
    if "wire" in kinds:
        branches.append(_WIRE_BRANCH)
    if "paramset" in kinds:
        branches.append(_PARAMSET_BRANCH)
    if "harness" in kinds:
        branches.append(_HARNESS_BRANCH)
    if "signalharness" in kinds:
        branches.append(_SIGNAL_HARNESS_BRANCH)
    if "blanket" in kinds:
        branches.append(_BLANKET_BRANCH)
    if "sheetsymbol" in kinds:
        branches.append(_SHEET_SYMBOL_BRANCH)
    body = "\n".join(branches) if branches else "        ;"
    script = """List1 := TStringList.Create;
Obj1 := SchServer.GetCurrentSchDocument;
if Obj1 = nil then
    List1.Add('doc|(none)|0')
else
begin
    List1.Add('doc|' + Obj1.DocumentName + '|' + IntToStr(Obj1.ObjectId));
    Obj2 := Obj1.SchIterator_Create;
    try
        Obj3 := Obj2.FirstSchObject;
        while Obj3 <> nil do
        begin
            I3 := Obj3.ObjectId;
%s
            Obj3 := Obj2.NextSchObject;
        end;
    finally
        Obj1.SchIterator_Destroy(Obj2);
    end;
end;
ResultText := List1.Text;""" % body
    return _target_document(script, document)


def _pin_connect_point(x, y, orientation, length):
    """The ELECTRICAL end of a pin.

    Pin.Location is the body end; the connection point is PinLength further
    along the pin. Using Location silently misses every connection - see
    dev/SCHEMATIC_CONVENTIONS.md.
    """
    if orientation == 0:
        return x + length, y
    if orientation == 1:
        return x, y + length
    if orientation == 2:
        return x - length, y
    if orientation == 3:
        return x, y - length
    return x, y


def _parse_sch_dump(result_text):
    """Turn the pipe-delimited dump into (document, objects)."""
    doc, objs = {}, []
    for line in (result_text or "").splitlines():
        if not line.strip():
            continue
        f = line.split("|")
        kind = f[0]
        try:
            if kind == "doc":
                doc = {"name": f[1], "object_id": int(f[2]),
                       "is_schematic_sheet": int(f[2]) == SCH_DOC_OBJECTID}
            elif kind == "netlabel":
                objs.append({"kind": kind, "text": f[1], "x": int(f[2]),
                             "y": int(f[3]), "orientation": int(f[4])})
            elif kind == "power":
                objs.append({"kind": kind, "text": f[1], "x": int(f[2]),
                             "y": int(f[3]), "style": int(f[4])})
            elif kind == "port":
                rec = {"kind": kind, "name": f[1], "x": int(f[2]), "y": int(f[3])}
                if len(f) >= 10:
                    rec.update({"harness_type": f[4], "width": int(f[5]),
                                "height": int(f[6]), "io_type": int(f[7]),
                                "style": int(f[8]), "alignment": int(f[9])})
                objs.append(rec)
            elif kind == "sheetsymbol":
                entries = []
                for e in f[7:]:
                    if not e:
                        continue
                    name, side, dft, io, htype = e.rsplit(",", 4)
                    entries.append({"name": name, "side": int(side), "distance_from_top": int(dft),
                                    "io_type": int(io), "harness_type": htype})
                objs.append({"kind": kind, "name": f[1], "file_name": f[2], "x": int(f[3]), "y": int(f[4]),
                             "x_size": int(f[5]), "y_size": int(f[6]), "entries": entries})
            elif kind == "blanket":
                objs.append({"kind": kind, "x0": int(f[1]), "y0": int(f[2]), "x1": int(f[3]), "y1": int(f[4]),
                             "x": int(f[1]), "y": int(f[2])})
            elif kind == "harness":
                entries = []
                for e in f[7:]:
                    if not e:
                        continue
                    name, ex, ey, side, dft = e.rsplit(",", 4)
                    entries.append({"name": name, "x": int(ex), "y": int(ey),
                                    "side": int(side), "distance_from_top": int(dft)})
                objs.append({"kind": kind, "harness_type": f[1], "x": int(f[2]), "y": int(f[3]),
                             "x_size": int(f[4]), "y_size": int(f[5]),
                             "primary_connection_position": int(f[6]), "entries": entries})
            elif kind == "signalharness":
                pts = [tuple(int(v) for v in p.split(",")) for p in f[1:] if p]
                rec = {"kind": kind, "vertices": [{"x": a, "y": b} for a, b in pts]}
                if pts:
                    rec["x"], rec["y"] = pts[0]
                objs.append(rec)
            elif kind == "paramset":
                params = []
                for p in f[6:]:
                    if not p:
                        continue
                    name, _, rest = p.partition("=")
                    text, _, vis = rest.rpartition("=")
                    params.append({"name": name, "text": text, "hidden": vis == "H"})
                objs.append({"kind": kind, "name": f[1], "x": int(f[2]), "y": int(f[3]),
                             "orientation": int(f[4]), "color": int(f[5]),
                             "params": params})
            elif kind == "component":
                objs.append({"kind": kind, "designator": f[1], "lib_reference": f[2],
                             "x": int(f[3]), "y": int(f[4]),
                             "current_part_id": int(f[5]),
                             "part_count": int(f[6]) if len(f) > 6 else 1})
            elif kind == "pin":
                x, y, ori, ln = int(f[3]), int(f[4]), int(f[6]), int(f[7])
                cx, cy = _pin_connect_point(x, y, ori, ln)
                objs.append({"kind": kind, "designator": f[1], "name": f[2],
                             "x": x, "y": y, "owner_part_id": int(f[5]),
                             "orientation": ori, "pin_length": ln,
                             "owner_lib_reference": f[8], "owner_designator": f[9],
                             "connect_x": cx, "connect_y": cy})
            elif kind in ("junction", "noerc"):
                objs.append({"kind": kind, "x": int(f[1]), "y": int(f[2])})
            elif kind == "wire":
                pts = [tuple(int(v) for v in p.split(",")) for p in f[1:] if p]
                rec = {"kind": kind,
                       "vertices": [{"x": a, "y": b} for a, b in pts]}
                # Give a wire the same single anchor the DelphiScript side
                # filters on (ISch_Wire.Location is its first vertex), so an
                # area filter means the same thing in the preview and in the
                # script that actually deletes.
                if pts:
                    rec["x"], rec["y"] = pts[0]
                objs.append(rec)
        except (ValueError, IndexError):
            objs.append({"kind": "unparsed", "raw": line})
    return doc, objs


def _point_on_segment(p, a, b):
    """Exact integer point-on-segment test - schematic coords are integers."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    if (bx - ax) * (py - ay) - (by - ay) * (px - ax) != 0:
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


def _wire_segments(objects):
    segs = []
    for o in objects:
        if o.get("kind") == "wire":
            v = [(p["x"], p["y"]) for p in o["vertices"]]
            segs += list(zip(v, v[1:]))
    return segs


_RENDER_PAS = """Obj3 := Client.GetDocumentByPath('__DOC__');
if (Obj3 = nil) then
    Obj3 := Client.OpenDocument('SCH', '__DOC__');
if (Obj3 = nil) then
begin
    SandboxLog('cannot open __DOC__');
    ResultText := 'ERR|cannot open document';
end
else
begin
    Client.ShowDocument(Obj3);
    Sleep(1200);
    SandboxLog('shown');
__SELECT__
    ResetParameters;
    RunProcess('Sch:Copy');
    ResetParameters;
    RunProcess('Sch:DeSelect');
    SandboxLog('copied to clipboard');
    ResultText := 'OK';
end;"""


_RENDER_SELECT_ALL = """    ResetParameters;
    AddStringParameter('Action', 'All');
    RunProcess('Sch:Select');"""


# Select only the objects inside a rectangle (every vertex for wires and
# harnesses, the anchor otherwise), so the copied metafile - and the picture -
# is one cluster at a legible scale instead of the whole sheet. The iterator
# is filtered to drawable kinds: unfiltered it yields sheet-level objects
# without Location ("Undeclared identifier: Location" - measured).
_RENDER_SELECT_AREA = """    ResetParameters;
    RunProcess('Sch:DeSelect');
    Obj1 := SchServer.GetSchDocumentByPath('__DOC__');
    Obj2 := Obj1.SchIterator_Create;
    Obj2.AddFilter_ObjectSet(MkSet(eWire, eNetLabel, eParameterSet, ePort, eHarnessConnector, eSignalHarness, eSchComponent, ePowerObject, eNoERC, eJunction, eNote, eLabel, eBlanket, ePolyline, eLine, eRectangle, eBus, eBusEntry));
    try
        Obj4 := Obj2.FirstSchObject;
        while Obj4 <> nil do
        begin
            B2 := 1;
            if (Obj4.ObjectId = eWire) or (Obj4.ObjectId = eSignalHarness) or (Obj4.ObjectId = ePolyline) then
            begin
                for I4 := 1 to Obj4.VerticesCount do
                    if (Obj4.Vertex[I4].X < __X0__) or (Obj4.Vertex[I4].X > __X1__) or (Obj4.Vertex[I4].Y < __Y0__) or (Obj4.Vertex[I4].Y > __Y1__) then B2 := 0;
            end
            else if (Obj4.Location.X < __X0__) or (Obj4.Location.X > __X1__) or (Obj4.Location.Y < __Y0__) or (Obj4.Location.Y > __Y1__) then B2 := 0;
            if B2 = 1 then Obj4.Selection := True;
            Obj4 := Obj2.NextSchObject;
        end;
    finally
        Obj1.SchIterator_Destroy(Obj2);
    end;"""


def _render_script(doc, area=None):
    if area:
        x0, y0, x1, y1 = [int(v) for v in area]
        sel = (_RENDER_SELECT_AREA.replace("__X0__", str(min(x0, x1))).replace("__X1__", str(max(x0, x1)))
               .replace("__Y0__", str(min(y0, y1))).replace("__Y1__", str(max(y0, y1))))
    else:
        sel = _RENDER_SELECT_ALL
    return _RENDER_PAS.replace("__SELECT__", sel).replace("__DOC__", str(doc))


# Pull the enhanced metafile off the clipboard and rasterise it. Extracted
# from dev/render_sheet.py, which established that this is the only reliable
# route: Altium paints a schematic view only once it has actually been
# displayed, so window capture returns stale or black pixels while every API
# signal claims the right document is current.
_RENDER_PS = r"""
Add-Type -AssemblyName System.Drawing
$sig = @'
using System;
using System.Runtime.InteropServices;
public class ClipEmf {
  [DllImport("user32.dll")] public static extern bool OpenClipboard(IntPtr h);
  [DllImport("user32.dll")] public static extern bool CloseClipboard();
  [DllImport("user32.dll")] public static extern IntPtr GetClipboardData(uint fmt);
  [DllImport("user32.dll")] public static extern bool IsClipboardFormatAvailable(uint fmt);
  [DllImport("gdi32.dll")] public static extern IntPtr CopyEnhMetaFile(IntPtr h, string f);
  [DllImport("gdi32.dll")] public static extern bool DeleteEnhMetaFile(IntPtr h);
}
'@
Add-Type -TypeDefinition $sig
if (-not [ClipEmf]::IsClipboardFormatAvailable(14)) { Write-Output 'NOEMF'; exit 1 }
[ClipEmf]::OpenClipboard([IntPtr]::Zero) | Out-Null
$h = [ClipEmf]::GetClipboardData(14)
$copy = [ClipEmf]::CopyEnhMetaFile($h, '__EMF__')
[ClipEmf]::DeleteEnhMetaFile($copy) | Out-Null
[ClipEmf]::CloseClipboard() | Out-Null
$img = [System.Drawing.Image]::FromFile('__EMF__')
$w = __W__; $h2 = [int]($w * $img.Height / $img.Width)
$bmp = New-Object System.Drawing.Bitmap $w, $h2
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::White)
$g.SmoothingMode = 'AntiAlias'
$g.DrawImage($img, 0, 0, $w, $h2)
$g.Dispose()
$bmp.Save('__PNG__', [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose(); $img.Dispose()
Write-Output ('rendered ' + $w + 'x' + $h2)
"""


@_tool(read_only=True)
async def render_schematic(ctx: Context, document: str = None, width: int = 2400, area: list = None,
                           timeout_seconds: int = 200):
    """
    Render a schematic sheet to an image you can actually look at.

    This is the reliable mechanism. Window capture does NOT work for
    schematics: Altium paints a view only once it has really been displayed,
    so a background capture returns the previous document's pixels or black
    while every API signal insists the right document is current. This route
    needs no window - Select All + Sch:Copy puts a framed enhanced metafile on
    the clipboard (the same path that pastes a schematic into Word) and that
    is rasterised here, so the image always reflects CURRENT content.

    Use it to check work that geometry cannot: text overlap, net labels
    colliding with wires, parameters sitting on top of each other, and whether
    a sheet simply looks right.

    SIDE EFFECT: overwrites the Windows clipboard. Avoid running it while the
    user is mid copy-paste.

    Args:
        document (str): full path of the sheet. Defaults to the focused one.
        width (int): output width in pixels (default 2400).
        area (list): [x0, y0, x1, y1] in internal units - render only the
            objects inside (wires and harnesses need every vertex inside).
            The way to look at one cluster at a legible scale.
        timeout_seconds (int): how long to wait for Altium.

    Returns:
        The rendered PNG as image content, plus a JSON metadata block.
    """
    doc = _canon(document)
    if not doc:
        payload, images = await _run_sandbox(_sch_dump_script([]), 120)
        if not payload.get("success"):
            return _sandbox_return(payload, images)
        info, _ = _parse_sch_dump(payload.get("result", ""))
        doc = info.get("name")
        if not doc or doc == "(none)":
            return json.dumps({"success": False,
                               "error": "no schematic is focused; pass document="})
    logger.info(f"render_schematic: {doc}")

    payload, images = await _run_sandbox(_render_script(doc, area),
                                         timeout_seconds)
    if not payload.get("success") or str(payload.get("result", "")).startswith("ERR"):
        payload.setdefault("error", "could not copy the sheet to the clipboard")
        payload["document"] = doc
        return _sandbox_return(payload, images)

    emf = EXCHANGE_DIR / "render_sheet.emf"
    png = EXCHANGE_DIR / "render_sheet.png"
    for f in (emf, png):
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass
    ps = (_RENDER_PS.replace("__EMF__", str(emf))
                    .replace("__W__", str(int(width)))
                    .replace("__PNG__", str(png)))
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=200)
    except Exception as e:
        return json.dumps({"success": False, "error": f"rasterise failed: {e}"})
    if "rendered" not in (r.stdout or "") or not png.exists():
        return json.dumps({"success": False, "document": doc,
                           "error": "clipboard held no metafile to rasterise",
                           "detail": ((r.stdout or "") + (r.stderr or ""))[-400:]})

    data = png.read_bytes()
    return [json.dumps({"success": True, "document": doc, "width": int(width),
                        "detail": r.stdout.strip(), "bytes": len(data)}),
            MCPImage(data=data, format="png")]


_NETLIST_PAS = """List1 := TStringList.Create;
Obj3 := nil;
""" + _PROJECT_PRELUDE + """
if Obj1 = nil then
    List1.Add('ERR|no focused project')
else
begin
    SandboxLog('compiling ' + Obj1.DM_ProjectFullPath);
    Obj1.DM_Compile;
    SandboxLog('compiled');
    List1.Add('project|' + Obj1.DM_ProjectFullPath);
    Obj2 := Obj1.DM_DocumentFlattened;
    if Obj2 = nil then
        List1.Add('ERR|compile produced no flattened document')
    else
    begin
        I1 := Obj2.DM_NetCount;
        I2 := Obj2.DM_ComponentCount;
        SandboxLog('nets=' + IntToStr(I1) + ' components=' + IntToStr(I2));
        List1.Add('counts|' + IntToStr(I1) + '|' + IntToStr(I2));
__COMPONENTS__
__NETS__
    end;
end;
ResultText := List1.Text;
SandboxLog('done');"""


_NETLIST_COMPONENTS = """        for B1 := 0 to I2 - 1 do
        begin
            Obj3 := Obj2.DM_Components(B1);
            List1.Add('comp|' + Obj3.DM_PhysicalDesignator + '|' +
                      Obj3.DM_LogicalDesignator + '|' +
                      IntToStr(Obj3.DM_PinCount) + '|' +
                      Obj3.DM_OwnerDocumentName);
        end;"""


_NETLIST_NETS = """        for B1 := 0 to I1 - 1 do
        begin
            Obj3 := Obj2.DM_Nets(B1);
            S1 := 'net|' + Obj3.DM_NetName + '|' + IntToStr(Obj3.DM_PinCount);
            for I3 := 0 to Obj3.DM_PinCount - 1 do
            begin
                Obj4 := Obj3.DM_Pins(I3);
                S1 := S1 + '|' + Obj4.DM_LogicalPartDesignator + '.' + Obj4.DM_PinNumber;
            end;
            List1.Add(S1);
        end;"""


def _parse_netlist(result_text):
    project, counts, nets, comps, errors = None, {}, [], [], []
    for line in (result_text or "").splitlines():
        if not line.strip():
            continue
        f = line.split("|")
        try:
            if f[0] == "ERR":
                errors.append(f[1])
            elif f[0] == "project":
                project = f[1]
            elif f[0] == "counts":
                counts = {"nets": int(f[1]), "components": int(f[2])}
            elif f[0] == "comp":
                comps.append({"designator": f[1], "logical_designator": f[2],
                              "pin_count": int(f[3]), "sheet": f[4],
                              "annotated": "?" not in f[1]})
            elif f[0] == "net":
                pins = [{"designator": p.rsplit(".", 1)[0], "pin": p.rsplit(".", 1)[1]}
                        for p in f[3:] if "." in p]
                nets.append({"name": f[1], "pin_count": int(f[2]), "pins": pins})
        except (ValueError, IndexError):
            errors.append("unparsed: " + line)
    return project, counts, nets, comps, errors


@_tool(read_only=True)
async def get_schematic_netlist(ctx: Context, nets: list = None,
                                designators: list = None, include_components: bool = True,
                                max_nets: int = 0, document: str = None, sheet: str = None,
                                timeout_seconds: int = 300) -> str:
    """
    Compile the focused project and return its netlist: which pins are on
    which net, across all sheets.

    This is Altium's OWN netlist, not geometry. It is the difference between
    editing objects and understanding a circuit, and unlike a geometric walk
    it is correct for mirrored and multi-part components.

    Compiling is what a person does before any netlist-dependent action; it
    does not change the drawing, though Altium may mark documents modified.

    Unannotated components (designator still "U?") compile as such and are
    reported with annotated=false - their nets are usually meaningless until
    annotate_schematic has run.

    Args:
        nets (list): only return these net names (case-insensitive).
        designators (list): only return nets touching these components.
        include_components (bool): include the component inventory.
        max_nets (int): cap the nets returned; 0 means no cap.
        document (str): full path of a sheet in the project to compile. Pass
            it: the focused project is the sandbox script project after any
            script run, and with several PCB projects open the tool refuses
            to guess.
        sheet (str): only nets touching a component on this sheet (file name
            or full path, case-insensitive). Implies include_components.
        timeout_seconds (int): compiling a large project takes a while.

    Returns:
        str: JSON with project, counts, nets[{name, pin_count, pins[]}] and
             components[{designator, pin_count, sheet, annotated}].
    """
    logger.info("get_schematic_netlist")
    if sheet:
        include_components = True
    script = (_project_script(_NETLIST_PAS, document)
              .replace("__COMPONENTS__", _NETLIST_COMPONENTS if include_components else "")
              .replace("__NETS__", _NETLIST_NETS))
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    project, counts, all_nets, comps, errors = _parse_netlist(payload.get("result", ""))
    if errors and not all_nets:
        return json.dumps({"success": False, "errors": errors, "project": project},
                          indent=2)

    sel = all_nets
    if sheet:
        key = str(sheet).lower().replace("/", "\\")
        on_sheet = {c["designator"].upper() for c in comps
                    if c.get("sheet") and (c["sheet"].lower().endswith(key) or key.endswith(c["sheet"].lower()))}
        sel = [n for n in sel if any(p["designator"].upper() in on_sheet for p in n["pins"])]
    if nets:
        want = set(n.lower() for n in nets)
        sel = [n for n in sel if n["name"].lower() in want]
    if designators:
        want = set(d.upper() for d in designators)
        sel = [n for n in sel if any(p["designator"].upper() in want for p in n["pins"])]
    truncated = False
    if max_nets and len(sel) > max_nets:
        sel, truncated = sel[:max_nets], True

    out = {"success": True, "project": project, "counts": counts,
           "nets_returned": len(sel), "truncated": truncated, "nets": sel}
    if include_components:
        out["components"] = comps
    if errors:
        out["parse_errors"] = errors
    return json.dumps(out, indent=2)


@_tool(read_only=True)
async def check_schematic_structure(ctx: Context, timeout_seconds: int = 300) -> str:
    """
    Compile the focused project and report the structural faults that make a
    schematic wrong, rather than merely ugly.

    This is the schematic sibling of check_placement: run it after editing and
    before trusting a sheet. It reports, from Altium's own compiled netlist:

      * single-pin nets - a pin connected to nothing, or a wire that missed
      * unannotated components - designators still containing "?", which make
        every net name provisional
      * duplicate designators - two parts claiming one name
      * pinless components in the netlist

    It does NOT replace Altium's full ERC rule set; it covers the faults that
    an automated edit actually causes.

    Returns:
        str: JSON with counts and the offending items.
    """
    logger.info("check_schematic_structure")
    script = (_NETLIST_PAS
              .replace("__COMPONENTS__", _NETLIST_COMPONENTS)
              .replace("__NETS__", _NETLIST_NETS))
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    project, counts, nets, comps, errors = _parse_netlist(payload.get("result", ""))

    single = [{"net": n["name"], "pins": n["pins"]} for n in nets if n["pin_count"] < 2]
    unannotated = [c for c in comps if not c["annotated"]]
    seen, dupes = {}, []
    for c in comps:
        if c["annotated"]:
            seen.setdefault(c["designator"], []).append(c["sheet"])
    for d, sheets in seen.items():
        if len(sheets) > 1:
            dupes.append({"designator": d, "sheets": sheets})
    pinless = [c for c in comps if c["pin_count"] == 0]

    findings = {
        "single_pin_nets": single,
        "unannotated_components": unannotated,
        "duplicate_designators": dupes,
        "pinless_components": pinless,
    }
    summary = dict((k, len(v)) for k, v in findings.items())
    summary["clean"] = not any(summary.values())
    return json.dumps({"success": True, "project": project, "counts": counts,
                       "summary": summary, "findings": findings,
                       "parse_errors": errors or None}, indent=2)


_DOCSTATE_PAS = """List1 := TStringList.Create;
Obj3 := nil;
""" + _PROJECT_PRELUDE + """
if Obj1 = nil then
    List1.Add('ERR|no focused project')
else
begin
    List1.Add('project|' + Obj1.DM_ProjectFullPath);
    I1 := Obj1.DM_LogicalDocumentCount;
    for B1 := 0 to I1 - 1 do
    begin
        Obj2 := Obj1.DM_LogicalDocuments(B1);
        S1 := 'x';
        if Client.IsDocumentOpen(Obj2.DM_FullPath) then S1 := 'open' else S1 := 'closed';
        List1.Add('doc|' + Obj2.DM_DocumentKind + '|' + Obj2.DM_FullPath + '|' + S1);
    end;
end;
Obj3 := SchServer.GetCurrentSchDocument;
if Obj3 = nil then
    List1.Add('focused_sch|(none)|0')
else
    List1.Add('focused_sch|' + Obj3.DocumentName + '|' + IntToStr(Obj3.ObjectId));
ResultText := List1.Text;"""


@_tool(read_only=True)
async def get_document_state(ctx: Context, timeout_seconds: int = 120) -> str:
    """
    Report the project's documents and which one is focused.

    Every other tool here acts on "whatever is focused", and that changes
    under you - a user clicking a tab, or EnsureDocumentFocused switching
    documents as a side effect. Call this when it matters which sheet an
    edit will land on, and check the focused document before a write.

    A focused ObjectId of 32 is a schematic sheet; 33 is a schematic LIBRARY,
    which the net-label tools refuse to edit.

    Returns:
        str: JSON with the project, its documents (kind, path, open) and the
             focused schematic.
    """
    logger.info("get_document_state")
    payload, images = await _run_sandbox(_DOCSTATE_PAS, timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    project, docs, focused, errors = None, [], None, []
    for line in (payload.get("result") or "").splitlines():
        if not line.strip():
            continue
        f = line.split("|")
        if f[0] == "ERR":
            errors.append(f[1])
        elif f[0] == "project":
            project = f[1]
        elif f[0] == "doc" and len(f) >= 4:
            docs.append({"kind": f[1], "path": f[2], "open": f[3] == "open"})
        elif f[0] == "focused_sch" and len(f) >= 3:
            focused = {"name": f[1], "object_id": int(f[2]),
                       "is_schematic_sheet": f[2] == "32",
                       "is_schematic_library": f[2] == "33"}
    return json.dumps({"success": True, "project": project,
                       "focused_schematic": focused,
                       "document_count": len(docs), "documents": docs,
                       "errors": errors or None}, indent=2)


_SAVE_STATE_PAS = """List1 := TStringList.Create;
Obj3 := nil;
""" + _PROJECT_PRELUDE + """
if Obj1 = nil then
    List1.Add('ERR|no focused project')
else
begin
    List1.Add('project|' + Obj1.DM_ProjectFullPath);
    I1 := Obj1.DM_LogicalDocumentCount;
    for B1 := 0 to I1 - 1 do
    begin
        Obj2 := Obj1.DM_LogicalDocuments(B1);
        S1 := Obj2.DM_FullPath;
        if Client.IsDocumentOpen(S1) then
        begin
            Obj3 := Client.GetDocumentByPath(S1);
            if Obj3 <> nil then
                List1.Add('doc|' + Obj2.DM_DocumentKind + '|' + S1 + '|' +
                          BoolToStr(Obj3.Modified, True));
        end;
    end;
end;
ResultText := List1.Text;"""


def _save_script(paths):
    """Save named documents. DoFileSave('') is verified; Modified goes False."""
    out = ["List1 := TStringList.Create;", "I1 := 0;"]
    for p in paths:
        out.append("Obj2 := Client.GetDocumentByPath(%s);" % _pas_str(p))
        out.append("if Obj2 = nil then")
        out.append("    List1.Add('skip|not open|' + %s)" % _pas_str(p))
        out.append("else")
        out.append("begin")
        out.append("    Obj2.DoFileSave('');")
        out.append("    List1.Add('saved|' + %s + '|still_modified=' +" % _pas_str(p))
        out.append("              BoolToStr(Obj2.Modified, True));")
        out.append("    I1 := I1 + 1;")
        out.append("end;")
    out.append("List1.Add('count|' + IntToStr(I1));")
    out.append("ResultText := List1.Text;")
    return chr(10).join(out)


@_tool()
async def save_documents(ctx: Context, documents: list = None, apply: bool = False,
                         timeout_seconds: int = 240) -> str:
    """
    Save modified Altium documents to disk.

    Nothing else in this server can save. Every edit any tool makes - net
    labels, wires, symbols, placement - leaves the document dirty in Altium
    until someone commits it, which is fine for review and useless for an
    unattended run.

    DEFAULTS TO A PREVIEW (apply=False) listing what is modified. Saving is
    the point where an edit stops being undoable with Ctrl+Z, so look first.

    Args:
        documents (list): full paths to save. Defaults to every modified,
            open document in the focused project.
        apply (bool): False lists what would be saved; True writes to disk.
        timeout_seconds (int): how long to wait for Altium.

    Returns:
        str: JSON with the modified documents and, when applied, what was
             written and whether each came back clean.
    """
    logger.info("save_documents: apply=%s documents=%s" % (apply, documents))
    payload, images = await _run_sandbox(_SAVE_STATE_PAS, timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)

    project, docs, errors = None, [], []
    for line in (payload.get("result") or "").splitlines():
        if not line.strip():
            continue
        f = line.split("|")
        if f[0] == "ERR":
            errors.append(f[1])
        elif f[0] == "project":
            project = f[1]
        elif f[0] == "doc" and len(f) >= 4:
            docs.append({"kind": f[1], "path": f[2], "modified": f[3] == "True"})

    dirty = [d for d in docs if d["modified"]]
    if documents:
        documents = [_canon(p) for p in documents]
        want = set(str(p).lower() for p in documents)
        targets = [d for d in docs if d["path"].lower() in want]
        missing = sorted(want - set(d["path"].lower() for d in docs))
    else:
        targets, missing = dirty, []

    if not apply:
        return json.dumps({"success": True, "applied": False, "project": project,
                           "open_documents": len(docs), "modified": dirty,
                           "would_save": [d["path"] for d in targets],
                           "not_open": missing, "errors": errors or None,
                           "note": "preview only - call again with apply=true. "
                                   "Saving ends Ctrl+Z recovery for these edits."},
                          indent=2)
    if not targets:
        return json.dumps({"success": True, "applied": True, "saved": 0,
                           "note": "nothing to save"}, indent=2)

    payload, images = await _run_sandbox(_save_script([d["path"] for d in targets]),
                                         timeout_seconds)
    if not payload.get("success"):
        payload["warning"] = ("FAILED during save. Some documents may already be "
                              "written - check which.")
        return _sandbox_return(payload, images)

    saved, skipped, still_dirty = [], [], []
    for line in (payload.get("result") or "").splitlines():
        f = line.split("|")
        if f[0] == "saved":
            saved.append(f[1])
            if len(f) > 2 and f[2].endswith("True"):
                still_dirty.append(f[1])
        elif f[0] == "skip":
            skipped.append(f[2] if len(f) > 2 else line)
    return json.dumps({"success": True, "applied": True, "project": project,
                       "saved": saved, "saved_count": len(saved),
                       "skipped_not_open": skipped,
                       "still_modified_after_save": still_dirty or None,
                       "warning": ("These edits are now on disk and no longer "
                                   "revertible with Ctrl+Z.")}, indent=2)


def _annotate_script(assignments):
    """Set designators on components identified by their exact location."""
    out = [_SHEET_GUARD]
    for a in assignments:
        out.append("Obj2 := Obj1.SchIterator_Create;")
        out.append("Obj5 := nil;")
        out.append("try")
        out.append("    Obj3 := Obj2.FirstSchObject;")
        out.append("    while (Obj3 <> nil) and (Obj5 = nil) do")
        out.append("    begin")
        out.append("        if Obj3.ObjectId = eSchComponent then")
        out.append("            if (Obj3.Location.X = %d) and (Obj3.Location.Y = %d) then"
                   % (int(a["x"]), int(a["y"])))
        out.append("                if Obj3.Designator.Text = %s then Obj5 := Obj3;"
                   % _pas_str(a["from"]))
        out.append("        Obj3 := Obj2.NextSchObject;")
        out.append("    end;")
        out.append("finally")
        out.append("    Obj1.SchIterator_Destroy(Obj2);")
        out.append("end;")
        out.append("if Obj5 <> nil then")
        out.append("begin")
        out.append("    SchServer.RobotManager.SendMessage(Obj5.I_ObjectAddress,")
        out.append("        c_BroadCast, SCHM_BeginModify, c_NoEventData);")
        out.append("    Obj5.Designator.Text := %s;" % _pas_str(a["to"]))
        out.append("    SchServer.RobotManager.SendMessage(Obj5.I_ObjectAddress,")
        out.append("        c_BroadCast, SCHM_EndModify, c_NoEventData);")
        out.append("    I1 := I1 + 1;")
        out.append("end;")
    out.append(_SHEET_CLOSE)
    return chr(10).join(out)


@_tool()
async def annotate_schematic(ctx: Context, apply: bool = False,
                             start_at: int = 1, timeout_seconds: int = 300) -> str:
    """
    Give unannotated components on the focused sheet real designators.

    A component still called "U?" makes every net touching it provisional -
    the compiled netlist names those nets NetU?_38 and similar - and blocks
    updating the PCB. This assigns the next free number per prefix.

    Free numbers are computed from the COMPILED PROJECT, not just this sheet,
    so a designator already used on another sheet is never reissued. That
    matters here: this project already contains R22, R23 and TP1 twice.

    MULTI-PART COMPONENTS ARE NOT TOUCHED, and are reported instead. Their
    placements carry independent UniqueIds (measured: a 3-part MCU had
    AWIQLVOH / ZRYKKRQV / WTVLSXNI), so nothing in the object model says
    which placements are one physical part. Deciding that is what Altium's
    own annotator does from your settings; guessing it would silently merge
    or split parts. Use Altium's annotator for those, or set them by hand.

    DEFAULTS TO A PREVIEW (apply=False) showing every proposed designator.

    Args:
        apply (bool): False previews; True assigns.
        start_at (int): lowest number to consider when looking for a free one.
        timeout_seconds (int): compiling a large project takes a while.

    Returns:
        str: JSON with the proposed or applied assignments, and the
             multi-part components left alone.
    """
    logger.info("annotate_schematic: apply=%s" % apply)

    # Project-wide designators, so a number in use on another sheet is not
    # reissued. This is what stops annotation creating fresh duplicates.
    script = (_NETLIST_PAS.replace("__COMPONENTS__", _NETLIST_COMPONENTS)
                          .replace("__NETS__", ""))
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    project, counts, _nets, comps, errors = _parse_netlist(payload.get("result", ""))

    used = {}
    for c in comps:
        m = re.match(r"^([A-Za-z_]+)(\d+)$", c["designator"] or "")
        if m:
            used.setdefault(m.group(1).upper(), set()).add(int(m.group(2)))

    payload, images = await _run_sandbox(_sch_dump_script(["component"]), timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    doc, objs = _parse_sch_dump(payload.get("result", ""))
    if not doc.get("is_schematic_sheet"):
        return json.dumps({"success": False, "document": doc,
                           "error": "focused document is not a schematic sheet "
                                    "(ObjectId %s)" % doc.get("object_id")}, indent=2)

    assignments, multipart, unparsed = [], [], []
    for o in objs:
        if o.get("kind") != "component" or "?" not in (o.get("designator") or ""):
            continue
        if int(o.get("part_count") or 1) > 1:
            multipart.append({"designator": o["designator"],
                              "lib_reference": o["lib_reference"],
                              "part": o.get("current_part_id"),
                              "part_count": o.get("part_count"),
                              "x": o["x"], "y": o["y"]})
            continue
        prefix = (o["designator"] or "").replace("?", "").upper()
        if not prefix:
            unparsed.append(o)
            continue
        taken = used.setdefault(prefix, set())
        n = max(start_at, 1)
        while n in taken:
            n += 1
        taken.add(n)
        assignments.append({"from": o["designator"], "to": "%s%d" % (prefix, n),
                            "lib_reference": o["lib_reference"],
                            "x": o["x"], "y": o["y"]})

    summary = {"to_assign": len(assignments),
               "multipart_left_alone": len(multipart),
               "unrecognised_prefix": len(unparsed)}

    if not apply:
        return json.dumps({"success": True, "applied": False, "project": project,
                           "document": doc, "summary": summary,
                           "assignments": assignments,
                           "multipart_needs_altium_annotator": multipart,
                           "errors": errors or None,
                           "note": "preview only - call again with apply=true"},
                          indent=2)
    if not assignments:
        return json.dumps({"success": True, "applied": True, "assigned": 0,
                           "summary": summary,
                           "multipart_needs_altium_annotator": multipart,
                           "note": "nothing to annotate"}, indent=2)

    payload, images = await _run_sandbox(_annotate_script(assignments), timeout_seconds)
    if not payload.get("success"):
        payload["warning"] = ("FAILED during annotation. Earlier designators are "
                              "already assigned - re-run the preview to see what "
                              "remains.")
        return _sandbox_return(payload, images)
    res = (payload.get("result") or "").strip()
    if res.startswith("ABORT"):
        return json.dumps({"success": False, "error": res}, indent=2)
    parts = res.split("|")
    return json.dumps({"success": True, "applied": True, "document": doc,
                       "assigned": int(parts[1]) if len(parts) > 1 else None,
                       "assignments": assignments,
                       "multipart_needs_altium_annotator": multipart,
                       "summary": summary,
                       "undo": "one Ctrl+Z reverts this whole call"}, indent=2)


_NEW_LIB_PAS = """List1 := TStringList.Create;
SandboxLog('Client.OpenNewDocument SCHLIB');
Obj2 := Client.OpenNewDocument('SCHLIB', '__NAME__', '__DESC__', False);
if Obj2 = nil then
    List1.Add('ERR|OpenNewDocument returned nil')
else
begin
    Client.ShowDocument(Obj2);
    Sleep(800);
    SandboxLog('DoSafeChangeFileNameAndSave -> __PATH__');
    B1 := 0;
    if Obj2.DoSafeChangeFileNameAndSave('__PATH__', 'SCHLIB') then B1 := 1;
    List1.Add('saved|' + IntToStr(B1));
    Obj1 := SchServer.GetCurrentSchDocument;
    if Obj1 <> nil then
        List1.Add('current|' + Obj1.DocumentName + '|' + IntToStr(Obj1.ObjectId));
end;
ResultText := List1.Text;"""


@_tool()
async def create_symbol_library(ctx: Context, path: str, description: str = "",
                                timeout_seconds: int = 120) -> str:
    """
    Create a NEW, empty schematic library (.SchLib) at a path, saved and
    focused, with no dialogs.

    This is the reliable way to get a library to generate symbols into.
    Verified on AD Develop: Client.OpenNewDocument('SCHLIB', ...) followed by
    IServerDocument.DoSafeChangeFileNameAndSave(path, 'SCHLIB'). Two dead
    ends are recorded so they are not retried: DM_CreateNewDocument fails
    with a variant-type error, and the process WorkspaceManager:OpenObject
    opens a "Choose Document to Open" file chooser rather than creating.

    The new library becomes the focused document, which is what
    create_schematic_symbol requires. It is created with Altium's default
    empty "Component_1"; that is normal.

    Refuses to overwrite an existing file.

    Args:
        path (str): where to save it; must end in .SchLib.
        description (str): document description shown in Altium.
        timeout_seconds (int): how long to wait for Altium.

    Returns:
        str: JSON with the saved path and the focused document.
    """
    target = _canon(path)
    if not target.lower().endswith(".schlib"):
        return json.dumps({"success": False, "error": "path must end in .SchLib"})
    if Path(target).exists():
        return json.dumps({"success": False, "error": f"refusing to overwrite {target}"})
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    name = Path(target).name
    script = (_NEW_LIB_PAS.replace("__NAME__", _pas_str(name)[1:-1])
              .replace("__DESC__", _pas_str(description or name)[1:-1])
              .replace("__PATH__", _pas_str(target)[1:-1]))
    logger.info(f"create_symbol_library: {target}")
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    res = payload.get("result") or ""
    saved = "saved|1" in res
    current = next((l.split("|")[1:] for l in res.splitlines() if l.startswith("current|")), None)
    return json.dumps({"success": saved and Path(target).exists(),
                       "path": target, "on_disk": Path(target).exists(),
                       "focused": {"name": current[0], "object_id": int(current[1])} if current else None,
                       "raw": res}, indent=2)


def _hex6(v) -> str:
    """Normalise a colour argument to 'RRGGBB'.

    MCP TRAP (mcp 1.5.0, FuncMetadata.pre_parse_json): every string argument
    is tried as JSON first, and only a result that is itself a string is
    skipped. So the hex colour "800000" reaches the tool as the INTEGER
    800000 while "FFFFFF" (not JSON) and "000000" (leading zero, invalid
    JSON) stay strings. An all-digit colour therefore has to be accepted as
    an int and its decimal digits read back verbatim - zero-padded, since a
    leading zero is the one thing the round trip cannot drop (it never parses).
    """
    if isinstance(v, bool):
        raise ValueError(f"colour must be RRGGBB hex, got {v!r}")
    if isinstance(v, int):
        if not 0 <= v <= 999999:
            raise ValueError(f"colour must be RRGGBB hex, got {v!r}")
        h = f"{v:06d}"
    else:
        h = str(v).strip().lstrip("#").upper()
    if len(h) != 6 or any(c not in "0123456789ABCDEF" for c in h):
        raise ValueError(f"colour must be RRGGBB hex, got {v!r}")
    return h


def _hex_to_bgr(h) -> int:
    """'RRGGBB' (or the int the MCP layer made of it) -> Altium TColor ($00BBGGRR).
    Altium's stock body is FFFFB0/0000FF."""
    h = _hex6(h)
    return (int(h[4:6], 16) << 16) | (int(h[2:4], 16) << 8) | int(h[0:2], 16)


_STYLE_PAS = """List1 := TStringList.Create;
__GETDOC__
if Obj1 = nil then
    ResultText := 'ABORT|library not found or not open'
else if Obj1.ObjectId <> 33 then
    ResultText := 'ABORT|not a schematic library|' + IntToStr(Obj1.ObjectId)
else
begin
    SandboxLog('library=' + Obj1.DocumentName);
    Obj5 := nil;
    Obj4 := Obj1.SchLibIterator_Create;
    try
        Obj3 := Obj4.FirstSchObject;
        while (Obj3 <> nil) and (Obj5 = nil) do
        begin
            if Obj3.ObjectId = eSchComponent then
                if Obj3.LibReference = __NAME__ then Obj5 := Obj3;
            Obj3 := Obj4.NextSchObject;
        end;
    finally
        Obj1.SchIterator_Destroy(Obj4);
    end;
    if Obj5 = nil then
        ResultText := 'ABORT|symbol not found|' + __NAME__
    else
    begin
        I1 := 0; I2 := 0; I3 := 0; B1 := 0;
        if __APPLY__ = 1 then SchServer.ProcessControl.PreProcess(Obj1, '');
        Obj2 := Obj5.SchIterator_Create;
        try
            Obj3 := Obj2.FirstSchObject;
            while Obj3 <> nil do
            begin
                if (Obj3.ObjectId = eRectangle) or (Obj3.ObjectId = ePolygon) or (Obj3.ObjectId = eEllipse) then
                begin
                    I1 := I1 + 1;
                    if __APPLY__ = 1 then begin Obj3.Color := __BODY_LINE__; Obj3.AreaColor := __BODY_FILL__; Obj3.LineWidth := __WIDTH__; end;
                end
                else if (Obj3.ObjectId = eLine) or (Obj3.ObjectId = ePolyline) or (Obj3.ObjectId = eArc) then
                begin
                    I1 := I1 + 1;
                    if __APPLY__ = 1 then begin Obj3.Color := __DIVIDER__; Obj3.LineWidth := __WIDTH__; end;
                end
                else if Obj3.ObjectId = ePin then
                begin
                    I2 := I2 + 1;
                    if __APPLY__ = 1 then Obj3.Color := __PIN__;
                end
                else if Obj3.ObjectId = eLabel then
                begin
                    I3 := I3 + 1;
                    if __APPLY__ = 1 then Obj3.Color := __LABEL__;
                end;
                Obj3 := Obj2.NextSchObject;
            end;
        finally
            Obj5.SchIterator_Destroy(Obj2);
        end;
        if __APPLY__ = 1 then
        begin
            SchServer.ProcessControl.PostProcess(Obj1, '');
            Obj1.GraphicallyInvalidate;
        end;
        ResultText := 'OK|' + IntToStr(I1) + '|' + IntToStr(I2) + '|' + IntToStr(I3);
    end;
end;"""


@_tool()
async def apply_symbol_style(ctx: Context, symbol_name: str, library_path: str = None,
                             body_fill: Union[str, int] = "FFFFB0",
                             body_line: Union[str, int] = "0000FF",
                             divider_color: Union[str, int] = "",
                             pin_color: Union[str, int] = "000000",
                             label_color: Union[str, int] = "0000FF",
                             line_width: int = 1, apply: bool = False,
                             timeout_seconds: int = 180) -> str:
    """
    Restyle one library symbol in place: body fill/outline colour, divider
    (line/polyline/arc) colour, line width, pin colour, label colour.

    The house library measured here uses a DIFFERENT red for dividers and
    labels (980000) than for the body outline (800000), so dividers are
    their own colour; leave divider_color empty to reuse body_line.

    create_schematic_symbol hard-codes Altium's stock palette (yellow body,
    blue outline), so generated symbols never matched a house library. This
    applies a palette AFTER creation - to generated symbols or existing ones -
    without touching geometry, as one undo step.

    Colours are RRGGBB hex (as a designer reads them); they are converted to
    Altium's $00BBGGRR TColor internally. The defaults are Altium's stock
    palette, so calling with no colours is a no-op restyle.

    DEFAULTS TO A PREVIEW (apply=False): counts what would be recoloured.

    Args:
        symbol_name (str): the symbol's LibReference.
        library_path (str): the .SchLib holding it; must already be open.
            Defaults to the focused library. Never a schematic sheet.
        body_fill, body_line, divider_color, pin_color, label_color (str):
            RRGGBB hex. Typed str|int only because the MCP layer turns an
            all-digit hex string such as "800000" into an integer before the
            tool sees it; pass hex strings, they are recovered either way.
        line_width (int): Altium size enum; 1 = small (the house norm).
        apply (bool): False previews; True writes.

    Returns:
        str: JSON with counts of body graphics, pins and labels affected.
    """
    try:
        body_fill, body_line = _hex6(body_fill), _hex6(body_line)
        divider_color = _hex6(divider_color) if divider_color not in ("", None) else body_line
        pin_color, label_color = _hex6(pin_color), _hex6(label_color)
        cols = {k: _hex_to_bgr(v) for k, v in (("BODY_FILL", body_fill), ("BODY_LINE", body_line),
                                                ("DIVIDER", divider_color),
                                                ("PIN", pin_color), ("LABEL", label_color))}
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)})
    lib = _canon(library_path) if library_path else None
    getdoc = (f"Obj1 := SchServer.GetSchDocumentByPath({_pas_str(lib)});" if lib
              else "Obj1 := SchServer.GetCurrentSchDocument;")
    script = _STYLE_PAS.replace("__GETDOC__", getdoc).replace("__NAME__", _pas_str(symbol_name))
    for k, v in cols.items():
        script = script.replace(f"__{k}__", str(v))
    script = script.replace("__WIDTH__", str(int(line_width))).replace("__APPLY__", "1" if apply else "0")
    logger.info(f"apply_symbol_style: {symbol_name} in {lib or 'focused library'} apply={apply}")
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    res = (payload.get("result") or "").strip()
    if res.startswith("ABORT"):
        return json.dumps({"success": False, "error": res}, indent=2)
    f = res.split("|")
    return json.dumps({"success": True, "applied": apply, "symbol": symbol_name,
                       "library": lib or "focused",
                       "body_graphics": int(f[1]), "pins": int(f[2]), "labels": int(f[3]),
                       "style": {"body_fill": body_fill, "body_line": body_line,
                                 "divider_color": divider_color,
                                 "pin_color": pin_color, "label_color": label_color,
                                 "line_width": line_width},
                       "note": None if apply else "preview only - call again with apply=true",
                       "undo": "one Ctrl+Z reverts this call" if apply else None}, indent=2)


@_tool(read_only=True)
async def get_schematic_objects(ctx: Context, kinds: list = None, document: str = None,
                                timeout_seconds: int = 120) -> str:
    """
    Dump objects from the schematic sheet currently focused in Altium.

    Unlike get_schematic_data this needs no designators, so it works on sheets
    that are not annotated yet (components still showing "U?"), and it reports
    the net-naming objects - net labels, power ports, ports - that carry a
    design's net names.

    Coordinates are Altium internal units: 1 mil = 10000.

    Pins include connect_x / connect_y, the ELECTRICAL point, which is
    PinLength along the pin and not Pin.Location. Wires meet that point;
    matching on Location silently misses every connection.

    Args:
        kinds (list): any of netlabel, power, port, component, pin, wire,
            junction, noerc, paramset, harness, signalharness, blanket,
            sheetsymbol. Defaults to
            [netlabel, power, port]. harness = a harness connector (x/y is its
            TOP-LEFT corner) with its entries (each with the electrical point
            and side: 1 = right edge, 0 = left edge); signalharness = the thick
            line from the connector to a port, as vertices.
            Ports report harness_type/width/height/io_type/style/alignment;
            paramset (a directive: net class, differential pair, ...) reports
            its visible name plus the hidden parameters that give it meaning
            (ClassName=..., DifferentialPair=True, ...).
        timeout_seconds (int): how long to wait for Altium.

    Returns:
        str: JSON with the document (name, object_id, is_schematic_sheet),
             per-kind counts, and the objects.
    """
    kinds = [str(k).lower() for k in (kinds or ["netlabel", "power", "port"])]
    if kinds == ["all"]:
        kinds = list(_SCH_KINDS)
    bad = [k for k in kinds if k not in _SCH_KINDS]
    if bad:
        return json.dumps({"success": False, "error": "unknown kinds: %s" % bad,
                           "valid_kinds": _SCH_KINDS})
    logger.info("get_schematic_objects: %s" % kinds)
    payload, images = await _run_sandbox(_sch_dump_script(kinds, document), timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    doc, objs = _parse_sch_dump(payload.get("result", ""))
    counts = {}
    for o in objs:
        counts[o["kind"]] = counts.get(o["kind"], 0) + 1
    return json.dumps({"success": True, "document": doc, "counts": counts,
                       "objects": objs}, indent=2)


@_tool(read_only=True)
async def check_net_labels(ctx: Context, document: str = None, timeout_seconds: int = 120) -> str:
    """
    Report which net labels on the focused sheet actually name a net.

    A net label names something only if its location lies ON a wire - and
    "on" includes mid-segment, not just at a vertex. A label sitting beside a
    pin, or in a tidy column next to a not-yet-wired symbol, is inert
    annotation that contributes no net name. Half the labels on a real sheet
    can be floating with nothing flagging it.

    Run this after renaming or adding labels, and before trusting a sheet's
    net names.

    Returns:
        str: JSON listing floating and attached labels, plus names carried by
             more than one label (those are all one net).
    """
    logger.info("check_net_labels")
    payload, images = await _run_sandbox(_sch_dump_script(["netlabel", "wire"], document),
                                         timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    doc, objs = _parse_sch_dump(payload.get("result", ""))
    segs = _wire_segments(objs)
    attached, floating, names = [], [], {}
    for o in objs:
        if o.get("kind") != "netlabel":
            continue
        rec = {"text": o["text"], "x": o["x"], "y": o["y"]}
        hit = any(_point_on_segment((o["x"], o["y"]), a, b) for a, b in segs)
        (attached if hit else floating).append(rec)
        names.setdefault(o["text"], []).append(rec)
    dupes = dict((n, v) for n, v in names.items() if len(v) > 1)
    return json.dumps({
        "success": True,
        "document": doc,
        "wire_segments": len(segs),
        "summary": {"total": len(attached) + len(floating),
                    "attached": len(attached), "floating": len(floating),
                    "distinct_names": len(names)},
        "floating_names_nothing": floating,
        "attached": attached,
        "duplicate_names_one_net_each": dupes}, indent=2)


_SHEET_GUARD = """Obj1 := SchServer.GetCurrentSchDocument;
if Obj1 = nil then
    ResultText := 'ABORT|no schematic focused|0'
else if Obj1.ObjectId <> 32 then
    ResultText := 'ABORT|not a schematic sheet|' + IntToStr(Obj1.ObjectId)
else
begin
SchServer.ProcessControl.PreProcess(Obj1, '');
I1 := 0;
I2 := 0;
"""


_SHEET_CLOSE = """SchServer.ProcessControl.PostProcess(Obj1, '');
Obj1.GraphicallyInvalidate;
ResultText := 'OK|' + IntToStr(I1) + '|' + IntToStr(I2);
end;"""


def _draw_wires_script(wires, junctions):
    """Wires and junctions onto the focused sheet, as one undo transaction."""
    out = [_SHEET_GUARD]
    for pts in wires:
        flat = [int(v) for v in pts]
        # eCreate_Default, NOT eCreate_GlobalCopy: the global copy clones the
        # last-edited wire's vertex list (measured 2026-09-19: a 3-vertex
        # template gave every new wire a spurious third vertex, fanning 56
        # diagonals across a sheet). The default wire has 0 vertices and the
        # same LineWidth/Color; there is no DeleteVertex in the API.
        out.append("Obj4 := SchServer.SchObjectFactory(eWire, eCreate_Default);")
        out.append("Obj4.Location := Point(%d, %d);" % (flat[0], flat[1]))
        for i in range(0, len(flat), 2):
            n = i // 2 + 1
            out.append("Obj4.InsertVertex := %d;" % n)
            out.append("Obj4.SetState_Vertex(%d, Point(%d, %d));"
                       % (n, flat[i], flat[i + 1]))
        out.append("Obj1.RegisterSchObjectInContainer(Obj4);")
        out.append("SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,")
        out.append("    SCHM_PrimitiveRegistration, Obj4.I_ObjectAddress);")
        out.append("I1 := I1 + 1;")
    for j in junctions:
        out.append("Obj4 := SchServer.SchObjectFactory(eJunction, eCreate_GlobalCopy);")
        out.append("Obj4.Location := Point(%d, %d);" % (int(j["x"]), int(j["y"])))
        out.append("Obj1.RegisterSchObjectInContainer(Obj4);")
        out.append("SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,")
        out.append("    SCHM_PrimitiveRegistration, Obj4.I_ObjectAddress);")
        out.append("I2 := I2 + 1;")
    out.append(_SHEET_CLOSE)
    return chr(10).join(out)


@_tool()
async def draw_schematic_wires(ctx: Context, wires: list = None, junctions: list = None,
                               apply: bool = False, document: str = None, force: bool = False,
                               timeout_seconds: int = 180) -> str:
    """
    Draw wires and junction dots on the schematic sheet already open.

    build_schematic only creates NEW sheets; this is for wiring up a sheet
    that exists - the common case when a symbol has been placed and needs
    connecting.

    DEFAULTS TO A PREVIEW (apply=False). Everything in one call is a single
    undo step.

    Coordinates are internal units: 1 mil = 10000.

    A wire must END on a pin's electrical point to connect. That point is
    PinLength along the pin, NOT Pin.Location - get_schematic_objects reports
    it as connect_x/connect_y. Routing to Pin.Location instead drives the wire
    through the pin and out the far side, which leaves the pin unconnected and
    can silently short it to a neighbour sharing the column. Verify with
    check_schematic_structure afterwards; the preview checks pin endpoints for you.

    Args:
        wires (list): each a flat list of alternating x,y, e.g.
            [3900,3000, 5300,3000, 5300,4900] - two segments, three points.
        junctions (list): [{"x":..,"y":..}] - only where 3+ branches meet.
        apply (bool): False previews; True draws.
        timeout_seconds (int): how long to wait for Altium.

    Returns:
        str: JSON with counts, plus which wire ends land on a pin.
    """
    wires = wires or []
    junctions = junctions or []
    if not wires and not junctions:
        return json.dumps({"success": False, "error": "nothing to draw"})
    for w in wires:
        if len(w) < 4 or len(w) % 2:
            return json.dumps({"success": False,
                               "error": "each wire needs an even count of at least "
                                        "4 coordinates: %s" % (w,)})

    logger.info("draw_schematic_wires: %d wires, %d junctions, apply=%s"
                % (len(wires), len(junctions), apply))

    # Read pins so the preview can say whether an end actually lands on one.
    payload, images = await _run_sandbox(_sch_dump_script(["pin"], document), timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    doc, objs = _parse_sch_dump(payload.get("result", ""))
    if not doc.get("is_schematic_sheet"):
        return json.dumps({"success": False, "document": doc,
                           "error": "focused document is not a schematic sheet "
                                    "(ObjectId %s)" % doc.get("object_id")}, indent=2)
    pin_at = {}
    for o in objs:
        if o.get("kind") == "pin":
            pin_at[(o["connect_x"], o["connect_y"])] = "%s.%s" % (
                o.get("owner_designator", "?"), o.get("designator", "?"))

    ends, warnings = [], []
    for i, w in enumerate(wires):
        for (x, y), which in (((w[0], w[1]), "start"), ((w[-2], w[-1]), "end")):
            hit = pin_at.get((int(x), int(y)))
            ends.append({"wire": i, "which": which, "x": int(x), "y": int(y),
                         "on_pin": hit})
            if not hit:
                warnings.append("wire %d %s (%d,%d) does not land on a pin "
                                "connection point" % (i, which, x, y))

    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc,
                           "plan": {"wires": len(wires), "junctions": len(junctions)},
                           "wire_ends": ends, "warnings": warnings,
                           "note": "preview only - call again with apply=true"},
                          indent=2)

    payload, images = await _run_sandbox(_target_document(_draw_wires_script(wires, junctions), document),
                                         timeout_seconds)
    if not payload.get("success"):
        payload["warning"] = ("FAILED while drawing. A runtime failure leaves earlier "
                              "wires already drawn - inspect the sheet before retrying.")
        return _sandbox_return(payload, images)
    res = (payload.get("result") or "").strip()
    if res.startswith("ABORT"):
        return json.dumps({"success": False, "error": res}, indent=2)
    parts = res.split("|")
    return json.dumps({"success": True, "applied": True, "document": doc,
                       "wires_drawn": int(parts[1]) if len(parts) > 2 else None,
                       "junctions_drawn": int(parts[2]) if len(parts) > 2 else None,
                       "wire_ends": ends, "warnings": warnings,
                       "undo": "one Ctrl+Z reverts this whole call"}, indent=2)


_DELETE_KINDS = {"netlabel": "eNetLabel", "wire": "eWire", "junction": "eJunction",
                 "power": "ePowerObject", "port": "ePort", "noerc": "eNoERC",
                 "component": "eSchComponent"}


def _delete_objects_script(kind_const, match_text, x0, y0, x1, y1, limit):
    """Delete matching objects, restarting the iterator after each removal.

    The iterator does not survive RemoveSchObject, so this finds one, destroys
    the iterator, removes, and looks again.
    """
    text_test = ""
    if match_text is not None:
        text_test = "                if Obj3.Text <> %s then B1 := 1 else B1 := 0;\n" \
                    % _pas_str(match_text)
    area_test = ""
    if x0 is not None:
        area_test = ("                if (Obj3.Location.X < %d) or (Obj3.Location.X > %d) "
                     "or (Obj3.Location.Y < %d) or (Obj3.Location.Y > %d) then B1 := 1;\n"
                     % (int(x0), int(x1), int(y0), int(y1)))
    return (_SHEET_GUARD +
            "for I3 := 1 to %d do\nbegin\n" % int(limit) +
            "    Obj2 := Obj1.SchIterator_Create;\n    Obj5 := nil;\n    try\n"
            "        Obj3 := Obj2.FirstSchObject;\n"
            "        while (Obj3 <> nil) and (Obj5 = nil) do\n        begin\n"
            "            if Obj3.ObjectId = %s then\n            begin\n" % kind_const +
            "                B1 := 0;\n" + text_test + area_test +
            "                if B1 = 0 then Obj5 := Obj3;\n"
            "            end;\n            Obj3 := Obj2.NextSchObject;\n        end;\n"
            "    finally\n        Obj1.SchIterator_Destroy(Obj2);\n    end;\n"
            "    if Obj5 <> nil then\n    begin\n"
            "        Obj1.RemoveSchObject(Obj5);\n"
            "        SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,\n"
            "            SCHM_PrimitiveRegistration, Obj5.I_ObjectAddress);\n"
            "        I1 := I1 + 1;\n    end;\nend;\n" + _SHEET_CLOSE)


@_tool()
async def delete_schematic_objects(ctx: Context, kind: str = None, text: str = None, document: str = None,
                                   area: list = None, limit: int = 50,
                                   apply: bool = False, timeout_seconds: int = 180) -> str:
    """
    Delete objects from the focused schematic sheet, by kind and optionally by
    text or bounding box.

    DEFAULTS TO A PREVIEW (apply=False), which lists exactly what would go.
    Deleting is the least reversible thing here, so look first.

    Args:
        kind (str): one of netlabel, wire, junction, power, port, noerc,
            component.
        text (str): only objects whose Text matches exactly (net labels,
            power ports).
        area (list): [x0, y0, x1, y1] in internal units - only objects whose
            location falls inside.
        limit (int): safety cap on how many to remove (default 50).
        apply (bool): False previews; True deletes.

    Returns:
        str: JSON with the matched objects and, when applied, the count.
    """
    if kind not in _DELETE_KINDS:
        return json.dumps({"success": False, "error": "kind must be one of %s"
                                                      % sorted(_DELETE_KINDS)})
    if area is not None and len(area) != 4:
        return json.dumps({"success": False, "error": "area must be [x0,y0,x1,y1]"})
    logger.info("delete_schematic_objects: kind=%s text=%r area=%s apply=%s"
                % (kind, text, area, apply))

    dump_kind = kind if kind in _SCH_KINDS else "netlabel"
    payload, images = await _run_sandbox(_sch_dump_script([dump_kind], document), timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    doc, objs = _parse_sch_dump(payload.get("result", ""))
    if not doc.get("is_schematic_sheet"):
        return json.dumps({"success": False, "document": doc,
                           "error": "focused document is not a schematic sheet "
                                    "(ObjectId %s)" % doc.get("object_id")}, indent=2)

    matched = []
    for o in objs:
        if o.get("kind") != dump_kind:
            continue
        if text is not None and o.get("text") != text:
            continue
        if area is not None:
            x, y = o.get("x"), o.get("y")
            if x is None or not (area[0] <= x <= area[2] and area[1] <= y <= area[3]):
                continue
        matched.append(o)

    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc,
                           "would_delete": len(matched), "matched": matched[:limit],
                           "capped_at": limit,
                           "note": "preview only - call again with apply=true"},
                          indent=2)
    if not matched:
        return json.dumps({"success": True, "applied": True, "deleted": 0,
                           "note": "nothing matched"}, indent=2)

    a = area or (None, None, None, None)
    script = _delete_objects_script(_DELETE_KINDS[kind], text,
                                    a[0], a[1], a[2], a[3], limit)
    script = _target_document(script, document)
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        payload["warning"] = ("FAILED while deleting. Earlier removals are already "
                              "applied - inspect the sheet before retrying.")
        return _sandbox_return(payload, images)
    res = (payload.get("result") or "").strip()
    if res.startswith("ABORT"):
        return json.dumps({"success": False, "error": res}, indent=2)
    parts = res.split("|")
    return json.dumps({"success": True, "applied": True, "document": doc,
                       "deleted": int(parts[1]) if len(parts) > 1 else None,
                       "undo": "one Ctrl+Z reverts this whole call"}, indent=2)


_PLACE_PAS = """Obj5 := Client.GetDocumentByPath('__TARGET__');
if Obj5 = nil then
    ResultText := 'ABORT|target sheet not open|0'
else
begin
Obj2 := Client.OpenDocument('SchLib', '__LIB__');
if Obj2 = nil then
    ResultText := 'ABORT|cannot open symbol library|0'
else
begin
    Client.ShowDocument(Obj2);
    Sleep(800);
    Obj3 := SchServer.GetCurrentSchDocument;
    SandboxLog('library current: ' + Obj3.DocumentName);
    Obj4 := nil;
    Obj2 := Obj3.SchLibIterator_Create;
    try
        Obj1 := Obj2.FirstSchObject;
        while (Obj1 <> nil) and (Obj4 = nil) do
        begin
            if Obj1.ObjectId = eSchComponent then
                if Obj1.LibReference = '__SYMBOL__' then Obj4 := Obj1;
            Obj1 := Obj2.NextSchObject;
        end;
    finally
        Obj3.SchIterator_Destroy(Obj2);
    end;
    if Obj4 = nil then
        ResultText := 'ABORT|symbol not found in library|0'
    else
    begin
        SandboxLog('replicating ' + Obj4.LibReference);
        Obj1 := Obj4.Replicate;
        Client.ShowDocument(Obj5);
        Sleep(800);
        Obj3 := SchServer.GetCurrentSchDocument;
        SandboxLog('target current: ' + Obj3.DocumentName);
        if Obj3.ObjectId <> 32 then
            ResultText := 'ABORT|target is not a schematic sheet|' + IntToStr(Obj3.ObjectId)
        else
        begin
            SchServer.ProcessControl.PreProcess(Obj3, '');
            Obj1.Designator.Text := '__DESIG__';
            Obj1.Location := Point(__X__, __Y__);
            Obj1.DesignItemID := '__ITEMID__';
            Obj3.RegisterSchObjectInContainer(Obj1);
            SchServer.RobotManager.SendMessage(Obj3.I_ObjectAddress, c_BroadCast,
                SCHM_PrimitiveRegistration, Obj1.I_ObjectAddress);
            SchServer.ProcessControl.PostProcess(Obj3, '');
            Obj3.GraphicallyInvalidate;
            ResultText := 'OK|1|' + Obj1.Designator.Text;
        end;
    end;
end;
end;"""


@_tool()
async def place_schematic_component(ctx: Context, symbol_library: str, symbol: str,
                                    designator: str, x: int, y: int,
                                    target_sheet: str = None, design_item_id: str = None,
                                    timeout_seconds: int = 240) -> str:
    """
    Place a component from a symbol library onto a schematic sheet.

    *** DOES NOT CURRENTLY WORK - DO NOT RELY ON IT. ***
    Measured on a scratch sheet: the replica IS created (the iterator finds
    it, with the right designator, LibReference and Location) but Altium DOES
    NOT DRAW IT, and its pins keep library-relative coordinates instead of
    absolute ones, so any wire routed to them would miss. Every API-level
    signal reports success; only render_schematic reveals the failure. Left
    in place because the remaining gap is small and the diagnosis is
    recorded, but verify with render_schematic before trusting any result.

    Uses the one method that gets closest, per dev/DBLIB_FINDINGS.md:
    open the source .SchLib, find the symbol, Replicate it, and register the
    copy on the target sheet. Both documented alternatives are dead ends -
    IntegratedLibraryManager.PlaceLibraryComponent and the
    Sch:PlaceIntegratedComponentFromDB process each return without error and
    place nothing.

    KNOWN LIMITATION: for a database-linked library this produces an UNLINKED
    shell. The symbol arrives with its library placeholder parameters
    (VALUE=VALUE, PKG_STYLE=PKG_STL, ...) because this path bypasses Altium's
    DbLib machinery; database values are not pulled in. Populating them is a
    separate step.

    Coordinates are internal units: 1 mil = 10000.

    Args:
        symbol_library (str): full path to the .SchLib holding the symbol.
        symbol (str): the symbol's LibReference.
        designator (str): designator for the placed part, e.g. "R12" or "U?".
        x, y (int): placement location in internal units.
        target_sheet (str): sheet to place on; defaults to the focused one.
            It must already be open.
        design_item_id (str): DesignItemID to stamp; defaults to the symbol.

    Returns:
        str: JSON with the placement result.
    """
    target = _canon(target_sheet)
    symbol_library = _canon(symbol_library)
    if not target:
        payload, images = await _run_sandbox(_sch_dump_script([]), 120)
        if not payload.get("success"):
            return _sandbox_return(payload, images)
        info, _ = _parse_sch_dump(payload.get("result", ""))
        if not info.get("is_schematic_sheet"):
            return json.dumps({"success": False, "document": info,
                               "error": "focused document is not a schematic sheet; "
                                        "pass target_sheet="}, indent=2)
        target = info.get("name")

    logger.info("place_schematic_component: %s from %s -> %s" % (symbol, symbol_library, target))
    script = (_PLACE_PAS.replace("__TARGET__", str(target))
                        .replace("__LIB__", str(symbol_library))
                        .replace("__SYMBOL__", str(symbol))
                        .replace("__DESIG__", str(designator))
                        .replace("__X__", str(int(x)))
                        .replace("__Y__", str(int(y)))
                        .replace("__ITEMID__", str(design_item_id or symbol)))
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    res = (payload.get("result") or "").strip()
    if res.startswith("ABORT"):
        return json.dumps({"success": False, "error": res, "target_sheet": target},
                          indent=2)
    parts = res.split("|")
    return json.dumps({"success": True, "target_sheet": target, "symbol": symbol,
                       "designator": parts[2] if len(parts) > 2 else designator,
                       "x": int(x), "y": int(y),
                       "warning": "UNRELIABLE: the replica is created in the object "
                                  "model but measured NOT to render, and its pins keep "
                                  "library-relative coordinates. Verify with "
                                  "render_schematic before building on this.",
                       "note": "database-linked libraries also place as an unlinked "
                               "shell - parameters keep library placeholder values",
                       "undo": "one Ctrl+Z reverts this placement"}, indent=2)


def _set_net_labels_script(renames, add, delete):
    """Build the DelphiScript applying one net-label edit as a single undo
    transaction.

    Split out from the tool so the emitted script can be tested without an
    Altium instance - the expensive way to find a mistake in generated
    DelphiScript is a runtime failure halfway through modifying a sheet.
    """
    lines = ["Obj1 := SchServer.GetCurrentSchDocument;",
             "if Obj1.ObjectId <> %d then" % SCH_DOC_OBJECTID,
             "    ResultText := 'ABORT|not a schematic sheet|' + IntToStr(Obj1.ObjectId)",
             "else", "begin", "List1 := TStringList.Create;"]
    for old, new in renames.items():
        lines.append("List1.Add(%s);" % _pas_str(old + "=" + new))
    lines += ["I1 := 0;", "I2 := 0;", "I3 := 0;",
              "SchServer.ProcessControl.PreProcess(Obj1, '');"]
    if renames:
        lines += [
            "Obj2 := Obj1.SchIterator_Create;", "try",
            "    Obj3 := Obj2.FirstSchObject;", "    while Obj3 <> nil do", "    begin",
            "        if Obj3.ObjectId = eNetLabel then", "        begin",
            "            B1 := List1.IndexOfName(Obj3.Text);",
            "            if B1 >= 0 then", "            begin",
            "                S1 := List1.ValueFromIndex[B1];",
            "                SchServer.RobotManager.SendMessage(Obj3.I_ObjectAddress,",
            "                    c_BroadCast, SCHM_BeginModify, c_NoEventData);",
            "                Obj3.Text := S1;",
            "                SchServer.RobotManager.SendMessage(Obj3.I_ObjectAddress,",
            "                    c_BroadCast, SCHM_EndModify, c_NoEventData);",
            "                I1 := I1 + 1;", "            end;", "        end;",
            "        Obj3 := Obj2.NextSchObject;", "    end;", "finally",
            "    Obj1.SchIterator_Destroy(Obj2);", "end;"]
    for d in delete:
        # Find-one-then-restart: the iterator does not survive a removal.
        lines += [
            "for B1 := 1 to 200 do", "begin",
            "    Obj2 := Obj1.SchIterator_Create;", "    Obj5 := nil;", "    try",
            "        Obj3 := Obj2.FirstSchObject;",
            "        while (Obj3 <> nil) and (Obj5 = nil) do", "        begin",
            "            if Obj3.ObjectId = eNetLabel then",
            "                if Obj3.Text = %s then Obj5 := Obj3;" % _pas_str(d),
            "            Obj3 := Obj2.NextSchObject;", "        end;", "    finally",
            "        Obj1.SchIterator_Destroy(Obj2);", "    end;",
            "    if Obj5 <> nil then", "    begin",
            "        Obj1.RemoveSchObject(Obj5);",
            "        SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,",
            "            SCHM_PrimitiveRegistration, Obj5.I_ObjectAddress);",
            "        I2 := I2 + 1;", "    end;", "end;"]
    for a in add:
        lines += [
            "Obj4 := SchServer.SchObjectFactory(eNetLabel, eCreate_Default);",
            "Obj4.Text := %s;" % _pas_str(a["text"]),
            "Obj4.Location := Point(%d, %d);" % (int(a["x"]), int(a["y"])),
            "Obj4.Orientation := %d;" % int(a.get("orientation", 0)),
            "Obj4.Justification := %d;" % int(a.get("justification", 0)),
            "Obj4.FontID := %d;" % int(a.get("font_id", 1)),
            "Obj1.RegisterSchObjectInContainer(Obj4);",
            "SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,",
            "    SCHM_PrimitiveRegistration, Obj4.I_ObjectAddress);",
            "I3 := I3 + 1;"]
    lines += ["SchServer.ProcessControl.PostProcess(Obj1, '');",
              "ResultText := 'OK|' + IntToStr(I1) + '|' + IntToStr(I2) + '|' "
              "+ IntToStr(I3);",
              "end;"]

    return chr(10).join(lines)


@_tool()
async def set_net_labels(ctx: Context, renames: dict = None, add: list = None,
                         delete: list = None, apply: bool = False, document: str = None,
                         force: bool = False, timeout_seconds: int = 180) -> str:
    """
    Rename, add and delete net labels on the focused schematic sheet.

    DEFAULTS TO A PREVIEW - nothing is written unless apply=True. The preview
    reports what would change and what would collide: renames whose target
    already exists (those MERGE two nets into one), deletes matching nothing,
    and added labels landing where there is no wire (so naming nothing).

    Everything applied in one call is a SINGLE undo step in Altium.

    Refuses to run unless the focused document is a schematic sheet
    (ObjectId 32). A schematic library is 33 and must never be edited this way.

    Args:
        renames (dict): {old_text: new_text}. Every label with that exact text
            is renamed, wherever it sits on the sheet.
        add (list): [{"text":..., "x":..., "y":..., "orientation":0}] in
            internal units (1 mil = 10000). To name a net, x/y must lie ON a
            wire.
        delete (list): label texts to remove entirely.
        apply (bool): False (default) previews; True writes.
        timeout_seconds (int): how long to wait for Altium.

    Returns:
        str: JSON with planned or applied counts plus any collisions.
    """
    renames = dict((str(k), str(v)) for k, v in (renames or {}).items())
    add = add or []
    delete = [str(d) for d in (delete or [])]
    if not renames and not add and not delete:
        return json.dumps({"success": False, "error": "nothing to do"})
    for a in add:
        if not all(k in a for k in ("text", "x", "y")):
            return json.dumps({"success": False,
                               "error": "add entries need text, x and y: %s" % (a,)})

    logger.info("set_net_labels: %d renames, %d adds, %d deletes, apply=%s"
                % (len(renames), len(add), len(delete), apply))

    # Read current state first: the preview, the collision report and the
    # is-this-a-sheet guard all depend on it.
    payload, images = await _run_sandbox(_sch_dump_script(["netlabel", "wire"], document),
                                         timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    doc, objs = _parse_sch_dump(payload.get("result", ""))
    if not doc.get("is_schematic_sheet"):
        return json.dumps({
            "success": False,
            "error": "focused document is not a schematic sheet (ObjectId %s; "
                     "33 is a library)" % doc.get("object_id"),
            "document": doc}, indent=2)

    existing = [o for o in objs if o.get("kind") == "netlabel"]
    by_text = {}
    for o in existing:
        by_text.setdefault(o["text"], []).append(o)
    segs = _wire_segments(objs)

    warnings = []
    for old, new in renames.items():
        if old not in by_text:
            warnings.append("rename source not found on this sheet: %r" % old)
        if new in by_text and new != old:
            warnings.append("rename target %r already exists (%d label(s)) - these "
                            "MERGE into one net" % (new, len(by_text[new])))
        if new in renames and renames[new] != new:
            warnings.append("chain rename: %r is also a rename source" % new)
    for d in delete:
        if d not in by_text:
            warnings.append("delete target not found: %r" % d)
    for a in add:
        p = (int(a["x"]), int(a["y"]))
        if not any(_point_on_segment(p, s, e) for s, e in segs):
            warnings.append("added label %r at %s is not on a wire - it will name "
                            "nothing" % (a["text"], p))
        if any(o["x"] == p[0] and o["y"] == p[1] for o in existing):
            warnings.append("added label %r lands on an existing label at %s"
                            % (a["text"], p))

    plan = {"renames_matching_labels": sum(len(by_text.get(k, [])) for k in renames),
            "deletes_matching_labels": sum(len(by_text.get(d, [])) for d in delete),
            "adds": len(add)}

    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc,
                           "plan": plan, "warnings": warnings,
                           "note": "preview only - call again with apply=true to write"},
                          indent=2)
    blocked = _blocking(warnings, ("is not on a wire",))
    if blocked and not force:
        return json.dumps({"success": False, "applied": False, "document": doc,
                           "error": "refused: these labels would name nothing (pass force=true to "
                                    "place them anyway)", "blocked": blocked, "warnings": warnings}, indent=2)

    script = _target_document(_set_net_labels_script(renames, add, delete), document)
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        payload["warning"] = ("The script FAILED while modifying the sheet. A runtime "
                              "failure leaves earlier changes applied - inspect the "
                              "sheet before retrying.")
        payload["plan"] = plan
        return _sandbox_return(payload, images)

    res = (payload.get("result") or "").strip()
    if res.startswith("ABORT"):
        return json.dumps({"success": False, "error": res}, indent=2)
    parts = res.split("|")
    ok = len(parts) > 3
    return json.dumps({
        "success": True, "applied": True, "document": doc,
        "renamed": int(parts[1]) if ok else None,
        "deleted": int(parts[2]) if ok else None,
        "added": int(parts[3]) if ok else None,
        "plan": plan, "warnings": warnings,
        "undo": "one Ctrl+Z in Altium reverts this whole call",
        "raw": res}, indent=2)


_MATCH_FIELDS = {
    "paramset": ("eParameterSet", {"name": "Name"}),
    "port": ("ePort", {"name": "Name"}),
    "netlabel": ("eNetLabel", {"text": "Text"}),
    "power": ("ePowerObject", {"text": "Text"}),
    "component": ("eSchComponent", {"designator": "Designator.Text",
                                    "lib_reference": "LibReference"}),
    "noerc": ("eNoERC", {}),
    "junction": ("eJunction", {}),
}


def _match_condition(kind, match, var="Obj3"):
    """Pascal boolean selecting objects of `kind` that satisfy `match`.

    match keys: the kind's text field(s) (name / text / designator /
    lib_reference), plus optional exact x / y in internal units.
    """
    if kind not in _MATCH_FIELDS:
        raise ValueError("unknown kind %r (one of %s)" % (kind, sorted(_MATCH_FIELDS)))
    const, fields = _MATCH_FIELDS[kind]
    terms = ["(%s.ObjectId = %s)" % (var, const)]
    for key, member in fields.items():
        if match.get(key) is not None:
            terms.append("(%s.%s = %s)" % (var, member, _pas_str(match[key])))
    if match.get("x") is not None:
        terms.append("(%s.Location.X = %d)" % (var, int(match["x"])))
    if match.get("y") is not None:
        terms.append("(%s.Location.Y = %d)" % (var, int(match["y"])))
    return " and ".join(terms)


def _object_matches(kind, match, obj):
    """Python twin of _match_condition, for the preview."""
    if obj.get("kind") != kind:
        return False
    _, fields = _MATCH_FIELDS[kind]
    for key in fields:
        if match.get(key) is not None and str(obj.get(key)) != str(match[key]):
            return False
    for key in ("x", "y"):
        if match.get(key) is not None and int(obj.get(key, 0)) != int(match[key]):
            return False
    return True


_SCRIPT_HEAD = ["Obj1 := SchServer.GetCurrentSchDocument;",
                "if Obj1.ObjectId <> %d then" % SCH_DOC_OBJECTID,
                "    ResultText := 'ABORT|not a schematic sheet|' + IntToStr(Obj1.ObjectId)",
                "else", "begin",
                "I1 := 0;", "I2 := 0;", "I3 := 0;",
                "SchServer.ProcessControl.PreProcess(Obj1, '');"]


def _script_tail(counters):
    expr = " + '|' + ".join("IntToStr(%s)" % c for c in counters)
    return ["SchServer.ProcessControl.PostProcess(Obj1, '');",
            "Obj1.GraphicallyInvalidate;",
            "ResultText := 'OK|' + %s;" % expr, "end;"]


def _modify_block(var, statements):
    """Pair property writes with begin/end modify (see ALTIUM_SCRIPT_API.md)."""
    return (["SchServer.RobotManager.SendMessage(%s.I_ObjectAddress," % var,
             "    c_BroadCast, SCHM_BeginModify, c_NoEventData);"]
            + statements
            + ["SchServer.RobotManager.SendMessage(%s.I_ObjectAddress," % var,
               "    c_BroadCast, SCHM_EndModify, c_NoEventData);"])


def _for_each_match(kind, match, statements):
    """Iterate the sheet once, applying `statements` to every matching object.

    Safe for in-place edits (move, rename); NOT for removal - see
    _remove_matches for that.
    """
    return (["Obj2 := Obj1.SchIterator_Create;", "try",
             "    Obj3 := Obj2.FirstSchObject;", "    while Obj3 <> nil do", "    begin",
             "        if %s then" % _match_condition(kind, match), "        begin"]
            + ["            " + st for st in statements]
            + ["        end;", "        Obj3 := Obj2.NextSchObject;", "    end;",
               "finally", "    Obj1.SchIterator_Destroy(Obj2);", "end;"])


def _remove_matches(kind, match, counter="I3"):
    """Find-one-then-restart removal: an iterator does not survive RemoveSchObject."""
    return (["for B1 := 1 to 200 do", "begin",
             "    Obj2 := Obj1.SchIterator_Create;", "    Obj5 := nil;", "    try",
             "        Obj3 := Obj2.FirstSchObject;",
             "        while (Obj3 <> nil) and (Obj5 = nil) do", "        begin",
             "            if %s then Obj5 := Obj3;" % _match_condition(kind, match),
             "            Obj3 := Obj2.NextSchObject;", "        end;", "    finally",
             "        Obj1.SchIterator_Destroy(Obj2);", "    end;",
             "    if Obj5 <> nil then", "    begin",
             "        Obj1.RemoveSchObject(Obj5);",
             "        SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,",
             "            SCHM_PrimitiveRegistration, Obj5.I_ObjectAddress);",
             "        %s := %s + 1;" % (counter, counter), "    end;", "end;"])


def _register(var):
    return ["Obj1.RegisterSchObjectInContainer(%s);" % var,
            "SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,",
            "    SCHM_PrimitiveRegistration, %s.I_ObjectAddress);" % var]


def _move_objects_script(moves):
    """One undo step moving every matching object of each move.

    A move is {"kind", "match", "to": [x, y]} or {"kind", "match", "by": [dx, dy]}.
    MoveByXY is used rather than assigning Location because it carries the
    children along - a parameter set's hidden parameters, a component's pins.
    Measured: Location assignment on a container leaves children behind.
    """
    lines = list(_SCRIPT_HEAD)
    for m in moves:
        if "to" in m:
            tx, ty = int(m["to"][0]), int(m["to"][1])
            move = "Obj3.MoveByXY(%d - Obj3.Location.X, %d - Obj3.Location.Y);" % (tx, ty)
        else:
            dx, dy = int(m["by"][0]), int(m["by"][1])
            move = "Obj3.MoveByXY(%d, %d);" % (dx, dy)
        lines += _for_each_match(m["kind"], m.get("match", {}),
                                 _modify_block("Obj3", [move]) + ["I1 := I1 + 1;"])
    lines += _script_tail(["I1"])
    return chr(10).join(lines)


def _paramset_params(entry):
    params = dict(entry.get("params") or {})
    if entry.get("class_name"):
        params.setdefault("ClassName", entry["class_name"])
    return params


def _set_paramsets_script(add, rename, delete):
    """One undo step creating / renaming / deleting parameter-set directives.

    add:    {"name", "x", "y", "orientation"=1, "color"=255,
             "class_name" | "params": {ParamName: text}}  (params are hidden)
    rename: {"match": {...}, "name"?: new visible name, "params"?: {ParamName: text}}
    delete: [match, ...]
    """
    lines = list(_SCRIPT_HEAD)
    for r in rename:
        sts = []
        if r.get("name") is not None:
            sts.append("Obj3.Name := %s;" % _pas_str(r["name"]))
        if r.get("class_name"):
            r = dict(r); r.setdefault("params", {}); r["params"]["ClassName"] = r["class_name"]
        for pname, ptext in (r.get("params") or {}).items():
            sts += ["Obj4 := Obj3.SchIterator_Create;", "try",
                    "    Obj4.AddFilter_ObjectSet(MkSet(eParameter));",
                    "    Obj5 := Obj4.FirstSchObject;", "    while Obj5 <> nil do",
                    "    begin",
                    "        if Obj5.Name = %s then" % _pas_str(pname)]
            sts += ["        begin"] + ["            " + x for x in _modify_block(
                "Obj5", ["Obj5.Text := %s;" % _pas_str(ptext)])] + ["        end;"]
            sts += ["        Obj5 := Obj4.NextSchObject;", "    end;", "finally",
                    "    Obj3.SchIterator_Destroy(Obj4);", "end;"]
        lines += _for_each_match("paramset", r.get("match", {}),
                                 _modify_block("Obj3", sts) + ["I1 := I1 + 1;"])
    for d in delete:
        lines += _remove_matches("paramset", d, "I3")
    for a in add:
        lines += ["Obj4 := SchServer.SchObjectFactory(eParameterSet, eCreate_Default);",
                  "Obj4.Name := %s;" % _pas_str(a["name"]),
                  "Obj4.Location := Point(%d, %d);" % (int(a["x"]), int(a["y"])),
                  "Obj4.Orientation := %d;" % int(a.get("orientation", 1)),
                  "Obj4.Color := %d;" % int(a.get("color", 255)),
                  "Obj4.Style := %d;" % int(a.get("style", 1))]
        for pname, ptext in _paramset_params(a).items():
            lines += ["Obj5 := SchServer.SchObjectFactory(eParameter, eCreate_Default);",
                      "Obj5.Name := %s;" % _pas_str(pname),
                      "Obj5.Text := %s;" % _pas_str(ptext),
                      "Obj5.IsHidden := True;",
                      "Obj4.AddSchObject(Obj5);"]
        lines += _register("Obj4") + ["I2 := I2 + 1;"]
    lines += _script_tail(["I1", "I2", "I3"])
    return chr(10).join(lines)


_PORT_DEFAULTS = {"height": 1000000, "io_type": 0, "style": 0, "alignment": 0,
                  "color": 128, "area_color": 8454143, "text_color": 128, "font_id": 1}


_PORT_MEMBERS = {"name": ("Name", str), "harness_type": ("HarnessType", str),
                 "width": ("Width", int), "height": ("Height", int),
                 "io_type": ("IOType", int), "style": ("Style", int),
                 "alignment": ("Alignment", int), "color": ("Color", int),
                 "area_color": ("AreaColor", int), "text_color": ("TextColor", int),
                 "font_id": ("FontID", int)}


def _port_assignments(var, props):
    out = []
    for key, (member, typ) in _PORT_MEMBERS.items():
        if props.get(key) is not None:
            val = _pas_str(props[key]) if typ is str else str(int(props[key]))
            out.append("%s.%s := %s;" % (var, member, val))
    return out


def _set_ports_script(add, rename, delete):
    """One undo step creating / editing / deleting ports.

    add:    {"name", "x", "y", "width", "harness_type"?, + any _PORT_MEMBERS key}
    rename: {"match": {...}, + any _PORT_MEMBERS key to set}
    delete: [match, ...]
    """
    lines = list(_SCRIPT_HEAD)
    for r in rename:
        props = {k: v for k, v in r.items() if k in _PORT_MEMBERS}
        lines += _for_each_match("port", r.get("match", {}),
                                 _modify_block("Obj3", _port_assignments("Obj3", props))
                                 + ["I1 := I1 + 1;"])
    for d in delete:
        lines += _remove_matches("port", d, "I3")
    for a in add:
        props = dict(_PORT_DEFAULTS)
        props.update({k: v for k, v in a.items() if k in _PORT_MEMBERS})
        lines += ["Obj4 := SchServer.SchObjectFactory(ePort, eCreate_Default);",
                  "Obj4.Location := Point(%d, %d);" % (int(a["x"]), int(a["y"]))]
        lines += _port_assignments("Obj4", props)
        lines += _register("Obj4") + ["I2 := I2 + 1;"]
    lines += _script_tail(["I1", "I2", "I3"])
    return chr(10).join(lines)


def _blocking(warnings, needles):
    """Warnings that describe an edit that is always wrong on a real sheet."""
    return [w for w in warnings if any(n in w for n in needles)]


def _in_rect(x, y, area):
    x0, y0, x1, y1 = [int(v) for v in area]
    return min(x0, x1) <= int(x) <= max(x0, x1) and min(y0, y1) <= int(y) <= max(y0, y1)


def _obj_inside(o, area):
    """An object is inside when every vertex (wires, harnesses) or its anchor is."""
    if o.get("kind") in ("wire", "signalharness"):
        vs = o.get("vertices") or []
        return bool(vs) and all(_in_rect(v["x"], v["y"], area) for v in vs)
    if o.get("kind") == "blanket":
        return _in_rect(o["x0"], o["y0"], area) and _in_rect(o["x1"], o["y1"], area)
    return _in_rect(o.get("x", 0), o.get("y", 0), area)


_AREA_KINDS = ("wire", "netlabel", "paramset", "port", "harness", "signalharness", "component",
               "power", "noerc", "junction")


_AREA_CONSTS = {"wire": "eWire", "netlabel": "eNetLabel", "paramset": "eParameterSet", "port": "ePort",
                "harness": "eHarnessConnector", "signalharness": "eSignalHarness",
                "component": "eSchComponent", "power": "ePowerObject", "noerc": "eNoERC",
                "junction": "eJunction"}


def _area_condition(area, kinds, exclude_lib_refs):
    """Pascal: is Obj3 (kind in kinds) inside `area`? Wires/harnesses need every vertex in."""
    x0, y0, x1, y1 = [int(v) for v in area]
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    kind_test = " or ".join("(Obj3.ObjectId = %s)" % _AREA_CONSTS[k] for k in kinds)
    lines = ["B2 := 0;",
             "if %s then" % kind_test, "begin",
             "    B2 := 1;",
             "    if (Obj3.ObjectId = eWire) or (Obj3.ObjectId = eSignalHarness) then",
             "    begin",
             "        for I4 := 1 to Obj3.VerticesCount do",
             "            if (Obj3.Vertex[I4].X < %d) or (Obj3.Vertex[I4].X > %d) or (Obj3.Vertex[I4].Y < %d) or (Obj3.Vertex[I4].Y > %d) then B2 := 0;" % (x0, x1, y0, y1),
             "    end",
             "    else if (Obj3.Location.X < %d) or (Obj3.Location.X > %d) or (Obj3.Location.Y < %d) or (Obj3.Location.Y > %d) then B2 := 0;" % (x0, x1, y0, y1)]
    for ref in exclude_lib_refs or []:
        lines.append("    if (Obj3.ObjectId = eSchComponent) then if Obj3.LibReference = %s then B2 := 0;" % _pas_str(ref))
    lines.append("end;")
    return lines


def _rename_copy_pas(var, renames):
    """Apply {old: new} to every text field of a fresh copy `var` (label text,
    port name, directive name + ClassName, connector type + entry names,
    component designator). List2 holds the map as name=value pairs."""
    if not renames:
        return []
    return [
        "if %s.ObjectId = eNetLabel then if List2.IndexOfName(%s.Text) >= 0 then %s.Text := List2.Values[%s.Text];" % (var, var, var, var),
        "if %s.ObjectId = ePort then if List2.IndexOfName(%s.Name) >= 0 then %s.Name := List2.Values[%s.Name];" % (var, var, var, var),
        "if %s.ObjectId = eSchComponent then if List2.IndexOfName(%s.Designator.Text) >= 0 then %s.Designator.Text := List2.Values[%s.Designator.Text];" % (var, var, var, var),
        "if %s.ObjectId = eParameterSet then" % var, "begin",
        "    if List2.IndexOfName(%s.Name) >= 0 then %s.Name := List2.Values[%s.Name];" % (var, var, var),
        "    Obj7 := %s.SchIterator_Create;" % var,
        "    Obj7.AddFilter_ObjectSet(MkSet(eParameter));",
        "    Obj8 := Obj7.FirstSchObject;",
        "    while Obj8 <> nil do", "    begin",
        "        if List2.IndexOfName(Obj8.Text) >= 0 then Obj8.Text := List2.Values[Obj8.Text];",
        "        Obj8 := Obj7.NextSchObject;", "    end;",
        "    %s.SchIterator_Destroy(Obj7);" % var, "end;",
        "if %s.ObjectId = eHarnessConnector then" % var, "begin",
        "    if List2.IndexOfName(%s.HarnessType) >= 0 then %s.HarnessType := List2.Values[%s.HarnessType];" % (var, var, var),
        "    Obj7 := %s.SchIterator_Create;" % var,
        "    Obj7.AddFilter_ObjectSet(MkSet(eHarnessEntry));",
        "    Obj8 := Obj7.FirstSchObject;",
        "    while Obj8 <> nil do", "    begin",
        "        if List2.IndexOfName(Obj8.Name) >= 0 then Obj8.Name := List2.Values[Obj8.Name];",
        "        Obj8 := Obj7.NextSchObject;", "    end;",
        "    %s.SchIterator_Destroy(Obj7);" % var, "end;"]


def _area_script(area, dx, dy, mode, kinds=None, exclude_lib_refs=None, renames=None):
    """Copy (Replicate + move + register) or move every object inside `area`.

    Three passes over the sheet, using the Selection flag as the bookkeeping
    bit: clear every selection, select what is inside NOW, then act on the
    selected objects only and deselect each as it is handled. A copy is
    registered with Selection False, so a shift that leaves a copy inside the
    area cannot chain. (I_ObjectAddress cannot be stringified through late
    binding, so an address list is not an option.)"""
    kinds = list(kinds or _AREA_KINDS)
    # Every pass filters the iterator to the kinds we act on: the unfiltered
    # iterator also yields sheet-level objects without a Selection member
    # ("Undeclared identifier: Selection" - measured on a fresh sheet).
    flt = "    Obj2.AddFilter_ObjectSet(MkSet(%s));" % ", ".join(_AREA_CONSTS[k] for k in kinds)
    lines = list(_SCRIPT_HEAD)
    for k, v in (renames or {}).items():
        lines.append("List2.Add(%s);" % _pas_str("%s=%s" % (k, v)))
    lines += ["Obj2 := Obj1.SchIterator_Create;", flt, "try", "    Obj3 := Obj2.FirstSchObject;",
              "    while Obj3 <> nil do", "    begin", "        Obj3.Selection := False;",
              "        Obj3 := Obj2.NextSchObject;", "    end;", "finally",
              "    Obj1.SchIterator_Destroy(Obj2);", "end;",
              "Obj2 := Obj1.SchIterator_Create;", flt, "try", "    Obj3 := Obj2.FirstSchObject;",
              "    while Obj3 <> nil do", "    begin"]
    lines += ["        " + l for l in _area_condition(area, kinds, exclude_lib_refs)]
    lines += ["        if B2 = 1 then", "        begin", "            Obj3.Selection := True;",
              "            I3 := I3 + 1;", "        end;",
              "        Obj3 := Obj2.NextSchObject;", "    end;", "finally",
              "    Obj1.SchIterator_Destroy(Obj2);", "end;",
              "Obj2 := Obj1.SchIterator_Create;", flt, "try", "    Obj3 := Obj2.FirstSchObject;",
              "    while Obj3 <> nil do", "    begin",
              "        if Obj3.Selection = True then", "        begin",
              "            Obj3.Selection := False;"]
    if mode == "copy":
        lines += ["            Obj5 := Obj3.Replicate;",
                  "            Obj5.Selection := False;",
                  "            Obj5.MoveByXY(%d, %d);" % (int(dx), int(dy))]
        lines += ["            " + l for l in _rename_copy_pas("Obj5", renames)]
        lines += ["            " + l for l in _register("Obj5")]
        lines += ["            I2 := I2 + 1;"]
    else:
        lines += ["            " + l for l in _modify_block("Obj3", ["Obj3.MoveByXY(%d, %d);" % (int(dx), int(dy))])]
        lines += ["            I1 := I1 + 1;"]
    lines += ["        end;", "        Obj3 := Obj2.NextSchObject;", "    end;", "finally",
              "    Obj1.SchIterator_Destroy(Obj2);", "end;"]
    lines += _script_tail(["I1", "I2", "I3"])
    return chr(10).join(lines)


def _copy_component_script(designator, lib_reference, to_x, to_y, new_designator):
    lines = list(_SCRIPT_HEAD)
    lines += ["Obj3 := SbFind(Obj1, eSchComponent, %s, -1, -1);" % _pas_str(designator)]
    if lib_reference:
        lines += ["if Obj3 <> nil then if Obj3.LibReference <> %s then Obj3 := nil;" % _pas_str(lib_reference)]
    lines += ["if Obj3 = nil then begin ResultText := 'ABORT|component %s not found'; "
              "Exit; end;" % str(designator).replace("'", ""),
              "Obj5 := Obj3.Replicate;",
              "Obj5.MoveByXY(%d - Obj3.Location.X, %d - Obj3.Location.Y);" % (int(to_x), int(to_y))]
    if new_designator:
        lines += ["Obj5.Designator.Text := %s;" % _pas_str(new_designator)]
    lines += _register("Obj5") + ["I2 := I2 + 1;"]
    lines += _script_tail(["I1", "I2", "I3"])
    return chr(10).join(lines)


def _plan_matches(items, objs, label):
    """Count what each match selects; warn on nothing and on ambiguous 'to' moves."""
    plan, warnings = [], []
    for it in items:
        kind = it.get("kind", "paramset")
        hits = [o for o in objs if _object_matches(kind, it.get("match", {}), o)]
        plan.append({label: it, "matches": len(hits)})
        if not hits:
            warnings.append("%s matches nothing on this sheet: %s" % (label, it.get("match", it)))
        elif "to" in it and len(hits) > 1:
            warnings.append("move 'to' matches %d objects - they would stack at one point: %s"
                            % (len(hits), it.get("match")))
    return plan, warnings


async def _sheet_state(kinds, timeout_seconds, document=None):
    payload, images = await _run_sandbox(_sch_dump_script(kinds, document), timeout_seconds)
    if not payload.get("success"):
        return None, None, _sandbox_return(payload, images)
    doc, objs = _parse_sch_dump(payload.get("result", ""))
    if not doc.get("is_schematic_sheet"):
        return None, None, json.dumps({
            "success": False,
            "error": "focused document is not a schematic sheet (ObjectId %s; "
                     "33 is a library)" % doc.get("object_id"),
            "document": doc}, indent=2)
    return doc, objs, None


async def _verify_added(expect, kinds, document, timeout_seconds):
    """Re-dump and confirm the objects an apply claims to have created exist.

    expect: [(kind, {field: value, ...})] - every field must match one object.
    Returns (verified: list, missing: list). The lesson behind it: a placement
    once reported success on every API signal while nothing was drawn."""
    doc, objs, err = await _sheet_state(kinds, timeout_seconds, document)
    if err:
        return [], [{"error": "read-back failed"}]
    verified, missing = [], []
    for kind, want in expect:
        hit = any(o.get("kind") == kind and all(str(o.get(k)) == str(v) for k, v in want.items()) for o in objs)
        (verified if hit else missing).append({"kind": kind, **want})
    return verified, missing


async def _apply_script(script, plan, warnings, doc, names, timeout_seconds, verify=None):
    payload, images = await _run_sandbox(script, timeout_seconds)
    if not payload.get("success"):
        payload["warning"] = ("The script FAILED while modifying the sheet. A runtime "
                              "failure leaves earlier changes applied - inspect the "
                              "sheet before retrying.")
        payload["plan"] = plan
        return _sandbox_return(payload, images)
    res = (payload.get("result") or "").strip()
    if res.startswith("ABORT"):
        return json.dumps({"success": False, "error": res}, indent=2)
    parts = res.split("|")[1:]
    counts = {n: (int(parts[i]) if i < len(parts) else None) for i, n in enumerate(names)}
    out = {"success": True, "applied": True, "document": doc, **counts,
           "plan": plan, "warnings": warnings,
           "undo": "one Ctrl+Z in Altium reverts this whole call", "raw": res}
    if verify:
        expect, kinds, document = verify
        verified, missing = await _verify_added(expect, kinds, document, timeout_seconds)
        out["verified"] = len(verified)
        if missing:
            out["success"] = False
            out["missing_after_apply"] = missing
            out["error"] = "the script reported success but %d object(s) are not on the sheet" % len(missing)
    return json.dumps(out, indent=2)


@_tool()
async def move_schematic_objects(ctx: Context, moves: list, apply: bool = False,
                                 document: str = None, force: bool = False,
                                 timeout_seconds: int = 180) -> str:
    """
    Move directives, ports, components, net labels or power ports on the
    focused schematic sheet - the things a re-route leaves behind.

    Re-routing a cluster one row up or down leaves every net-class directive,
    test pad and MFG_TP directive sitting on the NEIGHBOUR'S wire, which
    Altium accepts without a word. This moves them too.

    DEFAULTS TO A PREVIEW (apply=False). One call = one undo step.
    Coordinates are internal units (1 mil = 10000).

    Args:
        moves (list): each {"kind": paramset|port|component|netlabel|power|
            noerc|junction, "match": {...}, "to": [x, y]} or {..., "by": [dx, dy]}.
            match keys: name (paramset/port), text (netlabel/power),
            designator / lib_reference (component), plus optional exact x, y.
            Every matching object moves; a "to" move matching several objects
            stacks them and is flagged in the preview.
        apply (bool): False previews; True moves.
        document (str): full path of the sheet to edit; it must be open but
            need not be focused. Defaults to the focused sheet, which changes
            under you - pass it.

    Returns:
        str: JSON with per-move match counts, warnings (a directive or a
             component pin landing off every wire), and when applied the count.
    """
    moves = moves or []
    if not moves:
        return json.dumps({"success": False, "error": "nothing to do"})
    for m in moves:
        if ("to" not in m) == ("by" not in m):
            return json.dumps({"success": False,
                               "error": "each move needs exactly one of 'to' or 'by': %s" % (m,)})
        if m.get("kind") not in _MATCH_FIELDS:
            return json.dumps({"success": False, "error": "unknown kind in %s" % (m,)})
    logger.info("move_schematic_objects: %d moves, apply=%s" % (len(moves), apply))

    kinds = sorted({m["kind"] for m in moves} | {"wire", "pin"})
    doc, objs, err = await _sheet_state(kinds, timeout_seconds, document)
    if err:
        return err
    plan, warnings = _plan_matches(moves, objs, "move")
    segs = _wire_segments(objs)
    for m in moves:
        for o in objs:
            if not _object_matches(m["kind"], m.get("match", {}), o):
                continue
            if "to" in m:
                dx, dy = int(m["to"][0]) - o["x"], int(m["to"][1]) - o["y"]
            else:
                dx, dy = int(m["by"][0]), int(m["by"][1])
            if m["kind"] in ("paramset", "netlabel"):
                p = (o["x"] + dx, o["y"] + dy)
                if not any(_point_on_segment(p, a, b) for a, b in segs):
                    warnings.append("%s %r would land at %s, which is on no wire"
                                    % (m["kind"], o.get("name") or o.get("text"), p))
            elif m["kind"] == "component":
                for pin in objs:
                    if pin.get("kind") == "pin" and pin.get("owner_designator") == o["designator"] \
                            and pin.get("owner_lib_reference") == o["lib_reference"]:
                        p = (pin["connect_x"] + dx, pin["connect_y"] + dy)
                        if not any(_point_on_segment(p, a, b) for a, b in segs):
                            warnings.append("component %s pin %s would end at %s, on no wire"
                                            % (o["designator"], pin["designator"], p))
    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc,
                           "plan": plan, "warnings": warnings,
                           "note": "preview only - call again with apply=true to move"},
                          indent=2)
    blocked = _blocking(warnings, ("which is on no wire", "on no wire"))
    if blocked and not force:
        return json.dumps({"success": False, "applied": False, "document": doc,
                           "error": "refused: these moves would leave a directive or a pin off every wire "
                                    "(pass force=true to move anyway)", "blocked": blocked,
                           "warnings": warnings}, indent=2)
    return await _apply_script(_target_document(_move_objects_script(moves), document),
                               plan, warnings, doc, ["moved"], timeout_seconds)


@_tool()
async def set_parameter_sets(ctx: Context, add: list = None, rename: list = None,
                             delete: list = None, apply: bool = False,
                             document: str = None, force: bool = False,
                             timeout_seconds: int = 180) -> str:
    """
    Create, rename and delete parameter-set DIRECTIVES on the focused sheet:
    net-class marks, differential-pair marks, MFG_TP test-pad marks.

    A directive is a parameter set whose visible Name is the label on the
    sheet and whose HIDDEN parameters carry the meaning: a net-class directive
    has ClassName=<pcb class>; a differential pair has DifferentialPair=True
    and DifferentialPairClassName=<pair class>. House usage measured on the
    X2/X9 comms sheets: one class directive per interface wire at the pin's
    electrical point (Name MC_<interface>, orientation 1, colour 255), an
    MFG_TP directive (colour 16711680) 100 mil from each test pad's pin.

    DEFAULTS TO A PREVIEW (apply=False). One call = one undo step.
    Coordinates are internal units (1 mil = 10000). A directive names a net
    only if its location lies ON a wire; the preview flags misses.

    Args:
        add (list): [{"name": "MC_SPI6", "x":..., "y":..., "class_name": "MC_SPI6",
            "orientation": 1, "color": 255, "style": 1}] or with "params":
            {"DifferentialPair": "True", "DifferentialPairClassName": "USB"} -
            every param is hidden. style 1 (default) draws the house circle
            glyph only; style 0 also prints the Name, which collides with pin
            text at a pin end - the house uses 0 only for pair/blanket marks.
        rename (list): [{"match": {"name": "MC_I2C1", "y": 85000000},
            "name": "MC_I2C2", "class_name": "MC_I2C2"}] - sets the visible
            name and/or hidden params on every matching directive.
        delete (list): [match, ...] with name and optional x, y.
        apply (bool): False previews; True writes.
        document (str): full path of the sheet to edit (open, not necessarily
            focused). Defaults to the focused sheet - pass it.

    Returns:
        str: JSON with match counts, warnings and, when applied, counts.
    """
    add, rename, delete = add or [], rename or [], delete or []
    if not add and not rename and not delete:
        return json.dumps({"success": False, "error": "nothing to do"})
    for a in add:
        if not all(k in a for k in ("name", "x", "y")):
            return json.dumps({"success": False, "error": "add entries need name, x, y: %s" % (a,)})
        if not _paramset_params(a):
            return json.dumps({"success": False,
                               "error": "add %r has no class_name/params - a directive with no "
                                        "hidden parameter means nothing" % a["name"]})
    logger.info("set_parameter_sets: %d add, %d rename, %d delete, apply=%s"
                % (len(add), len(rename), len(delete), apply))
    doc, objs, err = await _sheet_state(["paramset", "wire"], timeout_seconds, document)
    if err:
        return err
    plan, warnings = _plan_matches([dict(r, kind="paramset") for r in rename], objs, "rename")
    p2, w2 = _plan_matches([{"kind": "paramset", "match": d} for d in delete], objs, "delete")
    plan += p2; warnings += w2
    segs = _wire_segments(objs)
    for a in add:
        p = (int(a["x"]), int(a["y"]))
        if not any(_point_on_segment(p, s, e) for s, e in segs):
            warnings.append("added directive %r at %s is on no wire - it marks nothing" % (a["name"], p))
        if any(o["kind"] == "paramset" and (o["x"], o["y"]) == p for o in objs):
            warnings.append("added directive %r lands on an existing directive at %s" % (a["name"], p))
    plan.append({"adds": len(add)})
    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc,
                           "plan": plan, "warnings": warnings,
                           "note": "preview only - call again with apply=true to write"}, indent=2)
    blocked = _blocking(warnings, ("is on no wire",))
    if blocked and not force:
        return json.dumps({"success": False, "applied": False, "document": doc,
                           "error": "refused: these directives would mark nothing (pass force=true to "
                                    "place them anyway)", "blocked": blocked, "warnings": warnings}, indent=2)
    expect = [("paramset", {"name": a["name"], "x": int(a["x"]), "y": int(a["y"])}) for a in add]
    return await _apply_script(_target_document(_set_paramsets_script(add, rename, delete), document),
                               plan, warnings, doc, ["renamed", "added", "deleted"], timeout_seconds,
                               verify=(expect, ["paramset"], document) if expect else None)


@_tool()
async def set_schematic_ports(ctx: Context, add: list = None, rename: list = None,
                              delete: list = None, apply: bool = False,
                              document: str = None, timeout_seconds: int = 180) -> str:
    """
    Create, edit and delete PORTS on the focused schematic sheet.

    In the house style a port is the harness hook for a cluster: Name is the
    harness (UART7, SPI6, ...), HarnessType one of the types in the project's
    .Harness file (UART, UART_FC, SPI, I2C, SMBUS, SDMMC, USB, CANTXRX, SWD,
    ETH_RMII, PWMx4, ...), and it stands FREE at the sheet edge level with the
    cluster's middle row - nets are global by name, the port matches a sheet
    entry on the parent. Left ports at x=1800 mil, right ports right-aligned
    to x=14000 mil, measured on the X9 comms sheet. A HarnessType missing from
    the .Harness file compiles with a warning; add the type there.

    DEFAULTS TO A PREVIEW (apply=False). One call = one undo step.
    Coordinates are internal units (1 mil = 10000).

    Args:
        add (list): [{"name": "SPI6", "harness_type": "SPI", "x":..., "y":...,
            "width": 4000000, "io_type": 0|1|2|3, "style", "alignment",
            "color", "area_color", "text_color", "font_id"}] - defaults match
            the house ports (height 100 mil, colours 128/8454143/128, font 1).
        rename (list): [{"match": {"name": "UART5"}, "name": "USART3",
            "harness_type": "UART_FC", "width": ...}] - any port member.
        delete (list): [match, ...] with name and optional x, y.
        apply (bool): False previews; True writes.
        document (str): full path of the sheet to edit (open, not necessarily
            focused). Defaults to the focused sheet - pass it.

    Returns:
        str: JSON with match counts, duplicate-name warnings and counts.
    """
    add, rename, delete = add or [], rename or [], delete or []
    if not add and not rename and not delete:
        return json.dumps({"success": False, "error": "nothing to do"})
    for a in add:
        if not all(k in a for k in ("name", "x", "y", "width")):
            return json.dumps({"success": False, "error": "add entries need name, x, y, width: %s" % (a,)})
    logger.info("set_schematic_ports: %d add, %d rename, %d delete, apply=%s"
                % (len(add), len(rename), len(delete), apply))
    doc, objs, err = await _sheet_state(["port"], timeout_seconds, document)
    if err:
        return err
    plan, warnings = _plan_matches([dict(r, kind="port") for r in rename], objs, "rename")
    p2, w2 = _plan_matches([{"kind": "port", "match": d} for d in delete], objs, "delete")
    plan += p2; warnings += w2
    names = [o["name"] for o in objs if o["kind"] == "port"]
    for a in add:
        if a["name"] in names:
            warnings.append("a port named %r already exists on this sheet" % a["name"])
    for r in rename:
        if r.get("name") in names:
            warnings.append("rename target %r already exists on this sheet" % r["name"])
    plan.append({"adds": len(add)})
    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc,
                           "plan": plan, "warnings": warnings,
                           "note": "preview only - call again with apply=true to write"}, indent=2)
    expect = [("port", {"name": a["name"], "x": int(a["x"]), "y": int(a["y"])}) for a in add]
    return await _apply_script(_target_document(_set_ports_script(add, rename, delete), document),
                               plan, warnings, doc, ["renamed", "added", "deleted"], timeout_seconds,
                               verify=(expect, ["port"], document) if expect else None)


def _find_connector_pas(harness_type, top_y, var="Obj3"):
    return ["%s := nil;" % var,
            "Obj2 := Obj1.SchIterator_Create;",
            "Obj2.AddFilter_ObjectSet(MkSet(eHarnessConnector));",
            "Obj4 := Obj2.FirstSchObject;",
            "while Obj4 <> nil do", "begin",
            "  if (Obj4.HarnessType = %s) and (Obj4.Location.Y = %d) then %s := Obj4;"
            % (_pas_str(harness_type), int(top_y), var),
            "  Obj4 := Obj2.NextSchObject;", "end;",
            "Obj1.SchIterator_Destroy(Obj2);",
            "if %s = nil then begin ResultText := 'ABORT|connector %s@%d not found'; "
            "Exit; end;"
            % (var, harness_type.replace("'", ""), int(top_y))]


def _find_signal_harness_pas(x1, y1, var="Obj3"):
    return ["%s := nil;" % var,
            "Obj2 := Obj1.SchIterator_Create;",
            "Obj2.AddFilter_ObjectSet(MkSet(eSignalHarness));",
            "Obj4 := Obj2.FirstSchObject;",
            "while Obj4 <> nil do", "begin",
            "  if (Obj4.Vertex[1].X = %d) and (Obj4.Vertex[1].Y = %d) then %s := Obj4;"
            % (int(x1), int(y1), var),
            "  Obj4 := Obj2.NextSchObject;", "end;",
            "Obj1.SchIterator_Destroy(Obj2);",
            "if %s = nil then begin ResultText := 'ABORT|signal harness @%d,%d not found'; "
            "Exit; end;" % (var, int(x1), int(y1))]


def _entry_edits_pas(var, renames=None, remove=None, add=None):
    """Edit the entries of connector `var` (renames {old: new}; remove [names];
    add [{"name", "distance_from_top", "side"}])."""
    out = []
    for old, new in (renames or {}).items():
        out += ["Obj4 := %s.SchIterator_Create;" % var,
                "Obj4.AddFilter_ObjectSet(MkSet(eHarnessEntry));",
                "Obj2 := Obj4.FirstSchObject;",
                "while Obj2 <> nil do", "begin",
                "  if Obj2.Name = %s then" % _pas_str(old), "  begin"]
        out += ["    " + x for x in _modify_block("Obj2", ["Obj2.Name := %s;" % _pas_str(new)])]
        out += ["  end;", "  Obj2 := Obj4.NextSchObject;", "end;",
                "%s.SchIterator_Destroy(Obj4);" % var]
    for name in (remove or []):
        out += ["Obj4 := %s.SchIterator_Create;" % var,
                "Obj4.AddFilter_ObjectSet(MkSet(eHarnessEntry));",
                "Obj2 := Obj4.FirstSchObject;",
                "while (Obj2 <> nil) and (Obj2.Name <> %s) do Obj2 := Obj4.NextSchObject;" % _pas_str(name),
                "%s.SchIterator_Destroy(Obj4);" % var,
                "if Obj2 <> nil then", "begin",
                "  %s.RemoveSchObject(Obj2);" % var,
                "  SchServer.RobotManager.SendMessage(%s.I_ObjectAddress, c_BroadCast," % var,
                "      SCHM_PrimitiveRegistration, Obj2.I_ObjectAddress);", "end;"]
    for e in (add or []):
        out += ["Obj2 := SchServer.SchObjectFactory(eHarnessEntry, eCreate_Default);",
                "Obj2.Name := %s;" % _pas_str(e["name"]),
                "Obj2.Side := %d;" % int(e.get("side", 1)),
                "Obj2.DistanceFromTop := %d;" % int(e["distance_from_top"]),
                "Obj2.Color := 7354880;", "Obj2.AreaColor := 8454143;",
                "Obj2.TextColor := 7354880;", "Obj2.TextFontID := 1;",
                "%s.AddSchObject(Obj2);" % var,
                # Measured: an entry added this way keeps Location 0,0 (it is not
                # drawn) until placed explicitly; re-setting DistanceFromTop does
                # not help. Side 1 = right edge, 0 = left edge of the connector.
                "Obj2.Location := Point(%s.Location.X%s, %s.Location.Y - %d);"
                % (var, " + %s.XSize" % var if int(e.get("side", 1)) == 1 else "",
                   var, int(e["distance_from_top"]))]
    return out


def _set_harness_connectors_script(add, move, delete):
    """One undo step over harness assemblies (connector + signal harness).

    add:    {"template": {"harness_type", "top_y", "harness_start": [x, y]},
             "by": [dx, dy], "harness_type"?, "y_size"?, "primary_connection_position"?,
             "entries"?: {"rename": {...}, "remove": [...], "add": [...]},
             "harness_end"?: [x, y]  (re-seats the harness's FIRST vertex)}
    move:   {"harness_type", "top_y", "harness_start": [x, y], "by": [dx, dy]}
    delete: {"harness_type", "top_y", "harness_start": [x, y]}
    """
    lines = list(_SCRIPT_HEAD)
    for a in add:
        t = a["template"]
        dx, dy = int(a["by"][0]), int(a["by"][1])
        lines += _find_connector_pas(t["harness_type"], t["top_y"])
        lines += ["Obj5 := Obj3.Replicate;", "Obj5.MoveByXY(%d, %d);" % (dx, dy)]
        if a.get("harness_type"):
            lines.append("Obj5.HarnessType := %s;" % _pas_str(a["harness_type"]))
        if a.get("y_size") is not None:
            lines.append("Obj5.YSize := %d;" % int(a["y_size"]))
        if a.get("primary_connection_position") is not None:
            lines.append("Obj5.PrimaryConnectionPosition := %d;" % int(a["primary_connection_position"]))
        ent = a.get("entries") or {}
        lines += _entry_edits_pas("Obj5", ent.get("rename"), ent.get("remove"), ent.get("add"))
        lines += _register("Obj5") + ["I2 := I2 + 1;"]
        if t.get("harness_start"):
            hx, hy = t["harness_start"]
            lines += _find_signal_harness_pas(hx, hy)
            lines += ["Obj5 := Obj3.Replicate;", "Obj5.MoveByXY(%d, %d);" % (dx, dy)]
            if a.get("harness_end"):
                lines.append("Obj5.SetState_Vertex(1, Point(%d, %d));"
                             % (int(a["harness_end"][0]), int(a["harness_end"][1])))
            lines += _register("Obj5") + ["I2 := I2 + 1;"]
    for m in move:
        dx, dy = int(m["by"][0]), int(m["by"][1])
        lines += _find_connector_pas(m["harness_type"], m["top_y"])
        lines += _modify_block("Obj3", ["Obj3.MoveByXY(%d, %d);" % (dx, dy)]) + ["I1 := I1 + 1;"]
        if m.get("harness_start"):
            lines += _find_signal_harness_pas(*m["harness_start"])
            lines += _modify_block("Obj3", ["Obj3.MoveByXY(%d, %d);" % (dx, dy)]) + ["I1 := I1 + 1;"]
    for d in delete:
        lines += _find_connector_pas(d["harness_type"], d["top_y"])
        lines += ["Obj1.RemoveSchObject(Obj3);",
                  "SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,",
                  "    SCHM_PrimitiveRegistration, Obj3.I_ObjectAddress);", "I3 := I3 + 1;"]
        if d.get("harness_start"):
            lines += _find_signal_harness_pas(*d["harness_start"])
            lines += ["Obj1.RemoveSchObject(Obj3);",
                      "SchServer.RobotManager.SendMessage(Obj1.I_ObjectAddress, c_BroadCast,",
                      "    SCHM_PrimitiveRegistration, Obj3.I_ObjectAddress);", "I3 := I3 + 1;"]
    lines += _script_tail(["I1", "I2", "I3"])
    return chr(10).join(lines)


@_tool()
async def set_harness_connectors(ctx: Context, add: list = None, move: list = None,
                                 delete: list = None, apply: bool = False,
                                 document: str = None, timeout_seconds: int = 180) -> str:
    """
    Add, move or delete harness ASSEMBLIES (a harness connector plus the
    signal harness that runs from it to a port) on the focused sheet.

    House pattern (measured on the X2/X9 comms sheets): each interface
    cluster's wires end on the entries of a harness connector; the
    connector's primary connection carries a signal harness to a harness
    port at the sheet edge. A re-route that moves the wires must move the
    connector and harness too, and a new cluster needs a new assembly.

    New assemblies are made by REPLICATING an existing one of the same type
    and side (left-side connectors have entries on their right edge, right-
    side ones on their left; the API cannot flip a connector), moving it by
    a delta, then editing it: harness_type, y_size, primary_connection_position
    (all internal units), entries {"rename": {old: new}, "remove": [names],
    "add": [{"name", "distance_from_top", "side"}]}, and harness_end to
    re-seat the harness's first vertex on the port. Geometry: a connector's
    x/y is its TOP-LEFT corner; an entry sits at y - distance_from_top; the
    harness meets the connector at y - primary_connection_position.

    DEFAULTS TO A PREVIEW (apply=False). One call = one undo step.
    Coordinates are internal units (1 mil = 10000). Identify a connector by
    harness_type + top_y (its Location.Y) and its harness by harness_start
    (the harness's first vertex) - get both from get_schematic_objects with
    kinds ["harness", "signalharness"].

    Args:
        add (list): [{"template": {"harness_type": "SPI", "top_y": 66000000,
            "harness_start": [22000000, 64000000]}, "by": [0, -4000000],
            "harness_type": "QSPI", "y_size": 7000000, "primary_connection_position":
            4000000, "entries": {...}, "harness_end": [x, y]}]
        move (list): [{"harness_type", "top_y", "harness_start", "by": [dx, dy]}]
        delete (list): [{"harness_type", "top_y", "harness_start"}]
        apply (bool): False previews; True writes.
        document (str): full path of the sheet to edit (open, not necessarily
            focused). Defaults to the focused sheet - pass it.

    Returns:
        str: JSON with what each template/move/delete matched, warnings, and
             when applied the counts (moved, added, deleted).
    """
    add, move, delete = add or [], move or [], delete or []
    if not add and not move and not delete:
        return json.dumps({"success": False, "error": "nothing to do"})
    for a in add:
        t = a.get("template") or {}
        if not all(k in t for k in ("harness_type", "top_y")) or "by" not in a:
            return json.dumps({"success": False,
                               "error": "add entries need template{harness_type, top_y} and by: %s" % (a,)})
    for m in move:
        if not all(k in m for k in ("harness_type", "top_y", "by")):
            return json.dumps({"success": False, "error": "move entries need harness_type, top_y, by: %s" % (m,)})
    logger.info("set_harness_connectors: %d add, %d move, %d delete, apply=%s"
                % (len(add), len(move), len(delete), apply))
    doc, objs, err = await _sheet_state(["harness", "signalharness", "wire"], timeout_seconds, document)
    if err:
        return err
    conns = [o for o in objs if o["kind"] == "harness"]
    harns = [o for o in objs if o["kind"] == "signalharness"]
    segs = _wire_segments(objs)

    def find_conn(ht, top):
        return [c for c in conns if c["harness_type"] == ht and c["y"] == int(top)]

    def find_harn(start):
        return [h for h in harns if start and (h.get("x"), h.get("y")) == (int(start[0]), int(start[1]))]

    plan, warnings = [], []
    for label, items in (("add", add), ("move", move), ("delete", delete)):
        for it in items:
            ref = it.get("template", it) if label == "add" else it
            c = find_conn(ref["harness_type"], ref["top_y"])
            h = find_harn(ref.get("harness_start"))
            plan.append({label: it, "connector_matches": len(c), "harness_matches": len(h)})
            if len(c) != 1:
                warnings.append("%s: connector %s@%s matches %d (need exactly 1)"
                                % (label, ref["harness_type"], ref["top_y"], len(c)))
            if ref.get("harness_start") and len(h) != 1:
                warnings.append("%s: signal harness starting at %s matches %d"
                                % (label, ref["harness_start"], len(h)))
            if label in ("add", "move") and len(c) == 1:
                dx, dy = int(it["by"][0]), int(it["by"][1])
                for e in c[0]["entries"]:
                    p = (e["x"] + dx, e["y"] + dy)
                    if not any(_point_on_segment(p, s, t) for s, t in segs):
                        warnings.append("%s: entry %r would sit at %s, on no wire end"
                                        % (label, e["name"], p))
    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc,
                           "plan": plan, "warnings": warnings,
                           "note": "preview only - call again with apply=true to write"}, indent=2)
    return await _apply_script(_target_document(_set_harness_connectors_script(add, move, delete), document),
                               plan, warnings, doc, ["moved", "added", "deleted"], timeout_seconds)


@_tool(read_only=True)
async def check_sheet_conventions(ctx: Context, document: str = None, harness_dir: str = None,
                                  label_pattern: str = None, dump_json: str = None,
                                  timeout_seconds: int = 180) -> str:
    """
    Lint a schematic sheet against the house cluster anatomy and report a
    pass/fail table instead of leaving it to eyesight.

    Rules (see server/sheetlint.py and dev/SCHEMATIC_CONVENTIONS.md s.13):
    pins with no wire, wire nets with no label, floating labels and
    directives, interface nets without a directive, class conflicts, MFG pads
    without an MFG_TP mark, harness entries off their wires, dangling signal
    harnesses, ports off their harness or wire, harness types and entry names
    not in the project's .Harness files, blanket directives and what they
    cover, duplicate port names, unannotated designators.

    Run it after every edit batch. Errors are wrong; warnings are worth a look;
    infos describe what is there.

    Args:
        document (str): full path of the sheet (open, not necessarily focused).
        harness_dir (str): folder holding the project's .Harness files; defaults
            to the sheet's folder.
        label_pattern (str): regex every net label must match, e.g.
            "^MC_(P[A-K]\\d{1,2}_\\S+|OTGFS__[NP])$".
        dump_json (str): path of a saved get_schematic_objects dump to lint
            offline instead of reading the live sheet.

    Returns:
        str: JSON with summary, findings (severity, rule, message, x, y),
             per-net rows, clusters and single nets, plus a text report.
    """
    import sheetlint
    if dump_json:
        data = json.loads(Path(dump_json).read_text(encoding="utf-8"))
        doc = data.get("document", {"name": dump_json})
        objs = data.get("objects", data)
    else:
        doc, objs, err = await _sheet_state(list(_SCH_KINDS), timeout_seconds, document)
        if err:
            return err
    folder = harness_dir or str(Path(doc.get("name", document or ".")).parent)
    types = {}
    try:
        for f in sorted(Path(folder).glob("*.Harness")):
            types.update(sheetlint.parse_harness_file(f.read_text(encoding="utf-8", errors="replace")))
    except Exception as e:  # noqa: BLE001
        logger.warning("harness files: %s" % e)
    res = sheetlint.lint(objs, types or None, label_pattern)
    res["document"] = doc
    res["harness_types_known"] = sorted(types)
    res["report"] = sheetlint.format_report(res)
    return json.dumps(res, indent=2)


@_tool()
async def copy_schematic_area(ctx: Context, area: list, by: list, renames: dict = None,
                              kinds: list = None, exclude_lib_references: list = None,
                              apply: bool = False, document: str = None,
                              timeout_seconds: int = 240) -> str:
    """
    Clone everything inside a rectangle to a new place - the way to make a new
    cluster "look like that one".

    Every object whose anchor (or every vertex, for wires and harnesses) lies
    inside `area` is replicated with its children, shifted by `by`, and
    registered; then `renames` {old: new} is applied to every text field of
    the copies: net label text, port name, directive name and ClassName,
    harness type and entry names, component designator. A cluster copied
    this way keeps wires, labels, class marks, pads, marks, connector,
    harness and port together and consistent.

    Exclude the main part with exclude_lib_references (its pins define the
    rows; you do not want a second MCU). Wires that only partly enter the
    area are NOT copied - the preview lists them.

    DEFAULTS TO A PREVIEW (apply=False). One call = one undo step.
    Coordinates are internal units (1 mil = 10000).

    Args:
        area (list): [x0, y0, x1, y1].
        by (list): [dx, dy] shift for the copies.
        renames (dict): {"MC_SPI5": "MC_SPI6", "SPI5": "SPI6", "MC_PK0_SPI5_SCK__MEI": "MC_PA5_SPI6_SCK", ...}
        kinds (list): subset of wire, netlabel, paramset, port, harness,
            signalharness, component, power, noerc, junction (default all).
        exclude_lib_references (list): components never copied (e.g. the MCU).
        apply (bool): False previews; True copies.
        document (str): sheet path (open, not necessarily focused).

    Returns:
        str: JSON with what would be copied per kind, partial wires skipped,
             rename hits, and when applied the counts and read-back verification.
    """
    kinds = [str(k) for k in (kinds or _AREA_KINDS)]
    bad = [k for k in kinds if k not in _AREA_CONSTS]
    if bad:
        return json.dumps({"success": False, "error": "unknown kinds %s" % bad})
    if not area or len(area) != 4 or not by or len(by) != 2:
        return json.dumps({"success": False, "error": "area needs [x0,y0,x1,y1] and by needs [dx,dy]"})
    renames = {str(k): str(v) for k, v in (renames or {}).items()}
    doc, objs, err = await _sheet_state(list(set(kinds) | {"wire", "pin"}), timeout_seconds, document)
    if err:
        return err
    excl = set(exclude_lib_references or [])
    inside = [o for o in objs if o.get("kind") in kinds and _obj_inside(o, area)
              and not (o.get("kind") == "component" and o.get("lib_reference") in excl)]
    partial = [o for o in objs if o.get("kind") in ("wire", "signalharness") and o.get("kind") in kinds
               and not _obj_inside(o, area) and any(_in_rect(v["x"], v["y"], area) for v in o.get("vertices", []))]
    texts = set()
    for o in inside:
        for key in ("text", "name", "harness_type", "designator"):
            if o.get(key):
                texts.add(str(o[key]))
        for p in o.get("params", []):
            texts.add(str(p.get("text")))
        for e in o.get("entries", []):
            texts.add(str(e.get("name")))
    rename_hits = {k: v for k, v in renames.items() if k in texts}
    rename_miss = sorted(set(renames) - set(rename_hits))
    per_kind = {}
    for o in inside:
        per_kind[o["kind"]] = per_kind.get(o["kind"], 0) + 1
    warnings = []
    if partial:
        warnings.append("%d wire/harness object(s) cross the area boundary and will NOT be copied" % len(partial))
    if rename_miss:
        warnings.append("rename keys matching nothing inside the area: %s" % rename_miss)
    if not inside:
        warnings.append("nothing inside the area")
    plan = {"copies": per_kind, "partial_skipped": len(partial), "renames_applied": rename_hits}
    if not apply or not inside:
        return json.dumps({"success": True, "applied": False, "document": doc, "plan": plan,
                           "warnings": warnings, "note": "preview only - call again with apply=true"}, indent=2)
    dx, dy = int(by[0]), int(by[1])
    expect = []
    for o in inside:
        if o["kind"] == "netlabel":
            expect.append(("netlabel", {"text": renames.get(o["text"], o["text"]), "x": o["x"] + dx, "y": o["y"] + dy}))
        elif o["kind"] == "port":
            expect.append(("port", {"name": renames.get(o["name"], o["name"]), "x": o["x"] + dx, "y": o["y"] + dy}))
        elif o["kind"] == "paramset":
            expect.append(("paramset", {"name": renames.get(o["name"], o["name"]), "x": o["x"] + dx, "y": o["y"] + dy}))
    script = _target_document(_area_script(area, dx, dy, "copy", kinds, exclude_lib_references, renames), document)
    return await _apply_script(script, plan, warnings, doc, ["moved", "copied", "matched"], timeout_seconds,
                               verify=(expect, ["netlabel", "port", "paramset"], document) if expect else None)


@_tool()
async def move_schematic_area(ctx: Context, area: list, by: list, kinds: list = None,
                              exclude_lib_references: list = None, apply: bool = False,
                              document: str = None, timeout_seconds: int = 240) -> str:
    """
    Shift everything inside a rectangle by a delta - a whole cluster in one
    call: wires, labels, class marks, pads and their marks, harness connector,
    signal harness and port move together, so nothing is left on the
    neighbour's row.

    Exclude the main part with exclude_lib_references (its pins define the
    rows). Wires that only partly enter the area are NOT moved and are listed
    in the preview; shrink or grow the area until it cuts nothing.

    DEFAULTS TO A PREVIEW (apply=False). One call = one undo step.
    Coordinates are internal units (1 mil = 10000).

    Args:
        area (list): [x0, y0, x1, y1].
        by (list): [dx, dy].
        kinds (list): subset of wire, netlabel, paramset, port, harness,
            signalharness, component, power, noerc, junction (default all).
        exclude_lib_references (list): components never moved (e.g. the MCU).
        apply (bool): False previews; True moves.
        document (str): sheet path (open, not necessarily focused).

    Returns:
        str: JSON with counts per kind, boundary-crossing objects, and when
             applied the moved count.
    """
    kinds = [str(k) for k in (kinds or _AREA_KINDS)]
    bad = [k for k in kinds if k not in _AREA_CONSTS]
    if bad:
        return json.dumps({"success": False, "error": "unknown kinds %s" % bad})
    if not area or len(area) != 4 or not by or len(by) != 2:
        return json.dumps({"success": False, "error": "area needs [x0,y0,x1,y1] and by needs [dx,dy]"})
    doc, objs, err = await _sheet_state(list(set(kinds) | {"wire"}), timeout_seconds, document)
    if err:
        return err
    excl = set(exclude_lib_references or [])
    inside = [o for o in objs if o.get("kind") in kinds and _obj_inside(o, area)
              and not (o.get("kind") == "component" and o.get("lib_reference") in excl)]
    partial = [o for o in objs if o.get("kind") in ("wire", "signalharness") and o.get("kind") in kinds
               and not _obj_inside(o, area) and any(_in_rect(v["x"], v["y"], area) for v in o.get("vertices", []))]
    per_kind = {}
    for o in inside:
        per_kind[o["kind"]] = per_kind.get(o["kind"], 0) + 1
    warnings = []
    if partial:
        warnings.append("%d wire/harness object(s) cross the area boundary and will NOT move: %s"
                        % (len(partial), [[(v["x"], v["y"]) for v in p["vertices"]] for p in partial[:6]]))
    if not inside:
        warnings.append("nothing inside the area")
    plan = {"moves": per_kind, "partial_skipped": len(partial)}
    if not apply or not inside:
        return json.dumps({"success": True, "applied": False, "document": doc, "plan": plan,
                           "warnings": warnings, "note": "preview only - call again with apply=true"}, indent=2)
    script = _target_document(_area_script(area, int(by[0]), int(by[1]), "move", kinds, exclude_lib_references), document)
    return await _apply_script(script, plan, warnings, doc, ["moved", "copied", "matched"], timeout_seconds)


@_tool()
async def copy_component(ctx: Context, designator: str, to: list, new_designator: str = None,
                         lib_reference: str = None, apply: bool = False, document: str = None,
                         timeout_seconds: int = 180) -> str:
    """
    Place another instance of a component that is already on the sheet
    (a test pad, a pull-up) by replicating it and moving the copy.

    Placing from a library is broken (the replica does not draw and its pins
    keep library coordinates); a copy of an on-sheet component keeps
    absolute pin coordinates and draws. Verified on the X9 comms sheet.

    DEFAULTS TO A PREVIEW (apply=False). Coordinates are internal units.

    Args:
        designator (str): the source component's designator (e.g. "BOT54"; "BOT?" also works,
            first match is used).
        to (list): [x, y] for the copy's Location - for a test pad that is its pin's
            electrical point.
        new_designator (str): designator text for the copy (e.g. "BOT?").
        lib_reference (str): disambiguate when several components share the designator.
        apply (bool): False previews; True places.
        document (str): sheet path (open, not necessarily focused).

    Returns:
        str: JSON with the source found, its pins' new electrical points and
             whether each lands on a wire, and when applied the read-back.
    """
    if not to or len(to) != 2:
        return json.dumps({"success": False, "error": "to needs [x, y]"})
    doc, objs, err = await _sheet_state(["component", "pin", "wire"], timeout_seconds, document)
    if err:
        return err
    src = [o for o in objs if o.get("kind") == "component" and o.get("designator") == designator
           and (not lib_reference or o.get("lib_reference") == lib_reference)]
    if not src:
        return json.dumps({"success": False, "error": "no component %r on this sheet" % designator})
    c = src[0]
    dx, dy = int(to[0]) - c["x"], int(to[1]) - c["y"]
    segs = _wire_segments(objs)
    pins = []
    for p in objs:
        if p.get("kind") == "pin" and p.get("owner_designator") == c["designator"] \
                and p.get("owner_lib_reference") == c["lib_reference"]:
            pt = (p["connect_x"] + dx, p["connect_y"] + dy)
            pins.append({"pin": p["designator"], "x": pt[0], "y": pt[1],
                         "on_wire": any(_point_on_segment(pt, a, b) for a, b in segs)})
    warnings = [] if all(p["on_wire"] for p in pins) else ["a copied pin would land on no wire: %s" % [p for p in pins if not p["on_wire"]]]
    plan = {"source": c, "copy_pins": pins, "new_designator": new_designator or c["designator"]}
    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc, "plan": plan,
                           "warnings": warnings, "note": "preview only - call again with apply=true"}, indent=2)
    script = _target_document(_copy_component_script(designator, c["lib_reference"], int(to[0]), int(to[1]), new_designator), document)
    expect = [("component", {"lib_reference": c["lib_reference"], "x": int(to[0]), "y": int(to[1])})]
    return await _apply_script(script, plan, warnings, doc, ["moved", "copied", "matched"], timeout_seconds,
                               verify=(expect, ["component"], document))


@_tool(read_only=True)
async def get_project_facts(ctx: Context, project: str) -> str:
    """
    Facts about an Altium project read OFFLINE from its files - no Altium
    interaction, safe at any time: net-identifier options, the document
    list, every harness type and its signals from the project's .Harness
    files, and per-sheet counts of unannotated designators.

    Args:
        project (str): full path of the .PrjPcb, or of any sheet in the
            project folder.

    Returns:
        str: JSON with options, documents, harness_types, sheets.
    """
    import sys as _sys
    root = Path(_canon(project))
    if root.suffix.lower() != ".prjpcb":
        cands = sorted(root.parent.glob("*.PrjPcb"))
        if not cands:
            return json.dumps({"success": False, "error": "no .PrjPcb next to %s" % root})
        root = cands[0]
    folder = root.parent
    text = root.read_text(encoding="utf-8", errors="replace")
    options, docs = {}, []
    for line in text.splitlines():
        if "=" in line and not line.startswith("["):
            k, _, v = line.partition("=")
            if k in ("AllowPortNetNames", "AllowSheetEntryNetNames", "NetlistSingleSheet", "AppendSheetNumberToLocalNets",
                     "NetIdentifierScope", "AutoNetIdentifierScope", "AllowPinSwapping_ByPin", "AllowPinSwapping_ByPart"):
                options[k] = v
            if k == "DocumentPath":
                docs.append(v)
    harness_types = {}
    for f in sorted(folder.glob("*.Harness")):
        try:
            import sheetlint
            for t, sigs in sheetlint.parse_harness_file(f.read_text(encoding="utf-8", errors="replace")).items():
                harness_types.setdefault(t, {"signals": sigs, "files": []})["files"].append(f.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("harness file %s: %s" % (f, e))
    sheets = []
    try:
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from pinout.schdoc import read_records
        for d in docs:
            if d.lower().endswith(".schdoc"):
                p = folder / d
                if not p.exists():
                    sheets.append({"sheet": d, "missing": True})
                    continue
                recs = read_records(p)
                comps = [r for r in recs if r.get("RECORD") == "1"]
                desigs = [r.get("Text", "") for r in recs if r.get("RECORD") == "34"]
                sheets.append({"sheet": d, "components": len(comps),
                               "unannotated": sum("?" in t for t in desigs),
                               "ports": sum(r.get("RECORD") == "18" for r in recs),
                               "harness_connectors": sum(r.get("RECORD") == "215" for r in recs),
                               "directives": sum(r.get("RECORD") == "43" for r in recs)})
    except Exception as e:  # noqa: BLE001
        sheets.append({"error": str(e)[:200]})
    return json.dumps({"success": True, "project": str(root), "options": options,
                       "nets_are_global": options.get("AllowPortNetNames") == "0",
                       "documents": docs, "harness_types": harness_types, "sheets": sheets}, indent=2)


_ERC_PAS = """Obj3 := Client.GetDocumentByPath(__DOC__);
if Obj3 = nil then begin ResultText := 'ABORT|document not open'; Exit; end;
Client.ShowDocument(Obj3);
Sleep(600);
SandboxLog('running Electrical Rules Check report');
ResetParameters;
AddStringParameter('ObjectKind', 'Validation');
AddStringParameter('Kind', 'Electrical Rules Check');
AddStringParameter('DisableDialog', 'True');
AddStringParameter('Target', 'Document');
AddStringParameter('ReturnGeneratedDocuments', 'True');
RunProcess('WorkspaceManager:GenerateReport');
I1 := -1;
GetIntegerParameter('Result', I1);
S1 := '';
GetStringParameter('File1', S1);
ResultText := 'OK|' + IntToStr(I1) + '|' + S1;"""


def _erc_watchdog(stop_event, clicked, seconds=180):
    """Press OK on the ERC setup form when it appears (it ignores DisableDialog).

    The form's buttons are TXPBitBtn controls that ignore BM_CLICK; mouse
    button messages posted at the button's centre work (measured)."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32

    def text(h):
        b = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, b, 256)
        return b.value

    def klass(h):
        b = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, b, 256)
        return b.value

    def children(h):
        out = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def cb(ch, _):
            out.append(ch)
            return True
        user32.EnumChildWindows(h, cb, 0)
        return out

    def tops():
        out = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def cb(h, _):
            out.append(h)
            return True
        user32.EnumWindows(cb, 0)
        return out

    start = time.time()
    while not stop_event.is_set() and time.time() - start < seconds:
        for h in tops():
            if klass(h) != "TERCReportSetup_Form":
                continue
            ok = [c for c in children(h) if text(c).replace("&", "") == "OK"]
            if ok:
                r = wt.RECT()
                user32.GetWindowRect(ok[0], ctypes.byref(r))
                lp = (((r.bottom - r.top) // 2) << 16) | ((r.right - r.left) // 2)
                user32.PostMessageW(ok[0], 0x0201, 1, lp)
                time.sleep(0.15)
                user32.PostMessageW(ok[0], 0x0202, 0, lp)
                clicked.append(time.time())
                time.sleep(2.0)
        time.sleep(0.5)


def _parse_erc_html(text):
    """Rows of (class, document, message) from Altium's ERC report HTML."""
    import html as _html
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", text, flags=re.S | re.I)
    cells = [_html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
             for c in re.findall(r"<td[^>]*>(.*?)</td>", body, flags=re.S | re.I)]
    rows, i = [], 0
    while i + 2 < len(cells):
        cls_ = cells[i]
        if cls_ in ("Fatal Error", "Error", "Warning", "Info"):
            rows.append({"class": cls_, "document": cells[i + 1], "message": cells[i + 2]})
            i += 3
        else:
            i += 1
    return rows


@_tool(read_only=True)
async def check_erc(ctx: Context, document: str, sheet: str = None, include_warnings: bool = True,
                    max_rows: int = 400, timeout_seconds: int = 240) -> str:
    """
    Run Altium's Electrical Rules Check on the project that owns `document`
    and return its findings - Altium's own compile messages, not geometry.

    This generates the ERC report (WorkspaceManager:GenerateReport, Validation /
    Electrical Rules Check) into the project's output folder and parses it.
    Altium insists on showing the report's setup form; this tool presses its
    OK for you. It SHOWS the sheet first (unavoidable) and leaves the report
    document open in Altium.

    Args:
        document (str): full path of any sheet in the project (open).
        sheet (str): only rows whose Document matches this file name.
        include_warnings (bool): False returns errors and fatals only.
        max_rows (int): cap on returned rows.

    Returns:
        str: JSON with the report path, counts by class, and rows
             [{class, document, message}].
    """
    import threading
    doc = _canon(document)
    stop, clicked = threading.Event(), []
    t = threading.Thread(target=_erc_watchdog, args=(stop, clicked, timeout_seconds), daemon=True)
    t.start()
    started = time.time()
    try:
        payload, images = await _run_sandbox(_ERC_PAS.replace("__DOC__", _pas_str(doc)), timeout_seconds)
    finally:
        stop.set()
    if not payload.get("success"):
        payload["note"] = "the ERC setup form was %s" % ("pressed" if clicked else "never seen")
        return _sandbox_return(payload, images)
    res = (payload.get("result") or "").split("|")
    report = res[2] if len(res) > 2 and res[2] else None
    if not report:
        # Altium names the report after the shown sheet; find the newest html in the outputs folder
        folder = Path(doc).parent
        cands = [p for p in folder.glob("Project Outputs*/*.html") if p.stat().st_mtime >= started - 5]
        report = str(max(cands, key=lambda p: p.stat().st_mtime)) if cands else None
    if not report or not Path(report).exists():
        return json.dumps({"success": False, "error": "ERC report not found after the run",
                           "setup_form_pressed": bool(clicked), "raw": payload.get("result")}, indent=2)
    rows = _parse_erc_html(Path(report).read_text(encoding="utf-8", errors="replace"))
    if sheet:
        key = Path(str(sheet)).name.lower()
        rows = [r for r in rows if r["document"].lower() == key]
    if not include_warnings:
        rows = [r for r in rows if r["class"] in ("Fatal Error", "Error")]
    counts = {}
    for r in rows:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    return json.dumps({"success": True, "report": report, "counts": counts, "rows_returned": min(len(rows), max_rows),
                       "rows_total": len(rows), "rows": rows[:max_rows],
                       "setup_form_pressed": bool(clicked)}, indent=2)


def _find_sheet_symbol_pas(name, var="Obj3"):
    """Leave the sheet symbol whose SheetName or SheetFileName matches in `var`."""
    return ["%s := nil;" % var,
            "Obj2 := Obj1.SchIterator_Create;",
            "Obj2.AddFilter_ObjectSet(MkSet(eSheetSymbol));",
            "Obj6 := Obj2.FirstSchObject;",
            "while Obj6 <> nil do", "begin",
            "  if (Obj6.SheetName.Text = %s) or (Obj6.SheetFileName.Text = %s) then %s := Obj6;"
            % (_pas_str(name), _pas_str(name), var),
            "  Obj6 := Obj2.NextSchObject;", "end;",
            "Obj1.SchIterator_Destroy(Obj2);",
            "if %s = nil then begin ResultText := 'ABORT|sheet symbol %s not found'; "
            "Exit; end;"
            % (var, str(name).replace("'", ""))]


def _set_sheet_entries_script(symbol, add, rename, delete):
    """One undo step adding / editing / removing entries on one sheet symbol.

    add:    {"name", "distance_from_top", "side"=1, "io_type"=0, "harness_type"=""}
    rename: {"match": {"name": old}, "name"?, "harness_type"?, "io_type"?,
             "distance_from_top"?, "side"?}
    delete: [name, ...]
    """
    lines = list(_SCRIPT_HEAD)
    lines += _find_sheet_symbol_pas(symbol, "Obj9")
    for r in rename:
        want = (r.get("match") or {}).get("name")
        sets = []
        for key, member, typ in (("name", "Name", str), ("harness_type", "HarnessType", str),
                                 ("io_type", "IOType", int), ("side", "Side", int),
                                 ("distance_from_top", "DistanceFromTop", int)):
            if r.get(key) is not None:
                val = _pas_str(r[key]) if typ is str else str(int(r[key]))
                sets.append("Obj7.%s := %s;" % (member, val))
        lines += ["Obj4 := Obj9.SchIterator_Create;",
                  "Obj4.AddFilter_ObjectSet(MkSet(eSheetEntry));",
                  "Obj7 := Obj4.FirstSchObject;",
                  "while Obj7 <> nil do", "begin",
                  "  if Obj7.Name = %s then" % _pas_str(want), "  begin"]
        lines += ["    " + x for x in _modify_block("Obj7", sets)]
        lines += ["    I1 := I1 + 1;", "  end;",
                  "  Obj7 := Obj4.NextSchObject;", "end;",
                  "Obj9.SchIterator_Destroy(Obj4);"]
    for name in delete:
        lines += ["Obj7 := nil;",
                  "Obj4 := Obj9.SchIterator_Create;",
                  "Obj4.AddFilter_ObjectSet(MkSet(eSheetEntry));",
                  "Obj8 := Obj4.FirstSchObject;",
                  "while (Obj8 <> nil) and (Obj7 = nil) do", "begin",
                  "  if Obj8.Name = %s then Obj7 := Obj8;" % _pas_str(name),
                  "  Obj8 := Obj4.NextSchObject;", "end;",
                  "Obj9.SchIterator_Destroy(Obj4);",
                  "if Obj7 <> nil then", "begin",
                  "  Obj9.RemoveSchObject(Obj7);",
                  "  SchServer.RobotManager.SendMessage(Obj9.I_ObjectAddress, c_BroadCast,",
                  "      SCHM_PrimitiveRegistration, Obj7.I_ObjectAddress);",
                  "  I3 := I3 + 1;", "end;"]
    for a in add:
        side = int(a.get("side", 1))
        lines += ["Obj7 := SchServer.SchObjectFactory(eSheetEntry, eCreate_Default);",
                  "Obj7.Name := %s;" % _pas_str(a["name"]),
                  "Obj7.Side := %d;" % side,
                  "Obj7.DistanceFromTop := %d;" % int(a["distance_from_top"]),
                  "Obj7.IOType := %d;" % int(a.get("io_type", 0)),
                  "Obj7.Style := %d;" % int(a.get("style", 0)),
                  "Obj7.ArrowKind := %d;" % int(a.get("arrow_kind", 0)),
                  "Obj7.Color := 7354880;", "Obj7.AreaColor := 8454143;",
                  "Obj7.TextColor := 7354880;", "Obj7.TextFontID := 1;"]
        if a.get("harness_type"):
            lines.append("Obj7.HarnessType := %s;" % _pas_str(a["harness_type"]))
        lines += ["Obj9.AddSchObject(Obj7);",
                  "SchServer.RobotManager.SendMessage(Obj9.I_ObjectAddress, c_BroadCast,",
                  "    SCHM_PrimitiveRegistration, Obj7.I_ObjectAddress);",
                  # Same trap as harness entries: a factory child reports 0,0 until placed.
                  "Obj7.Location := Point(Obj9.Location.X%s, Obj9.Location.Y - %d);"
                  % (" + Obj9.XSize" if side == 1 else "", int(a["distance_from_top"])),
                  "I2 := I2 + 1;"]
    lines += _script_tail(["I1", "I2", "I3"])
    return chr(10).join(lines)


# Emits the parent's sheet symbols with their entries, then each child sheet's
# ports. One dispatch for the whole hierarchy; a compiled project has every
# sheet in memory, so no tabs are opened.
_HIERARCHY_PAS = """Obj1 := SchServer.GetSchDocumentByPath(__DOC__);
if Obj1 = nil then begin ResultText := 'ABORT|parent sheet not in memory - compile the project first'; Exit; end;
List1 := TStringList.Create;
List1.Add('parent|' + Obj1.DocumentName);
S7 := ExtractFilePath(Obj1.DocumentName);
Obj2 := Obj1.SchIterator_Create;
Obj2.AddFilter_ObjectSet(MkSet(eSheetSymbol));
Obj3 := Obj2.FirstSchObject;
while Obj3 <> nil do
begin
  S3 := 'symbol|' + Obj3.SheetName.Text + '|' + Obj3.SheetFileName.Text;
  Obj4 := Obj3.SchIterator_Create;
  try
    Obj4.AddFilter_ObjectSet(MkSet(eSheetEntry));
    Obj5 := Obj4.FirstSchObject;
    while Obj5 <> nil do
    begin
      S3 := S3 + '|' + Obj5.Name + ',' + IntToStr(Obj5.Side) + ',' +
            IntToStr(Obj5.DistanceFromTop) + ',' + IntToStr(Obj5.IOType) + ',' + Obj5.HarnessType;
      Obj5 := Obj4.NextSchObject;
    end;
  finally
    Obj3.SchIterator_Destroy(Obj4);
  end;
  List1.Add(S3);
  List3.Add(Obj3.SheetFileName.Text);
  Obj3 := Obj2.NextSchObject;
end;
Obj1.SchIterator_Destroy(Obj2);
for I1 := 0 to List3.Count - 1 do
begin
  S1 := List3[I1];
  Obj6 := SchServer.GetSchDocumentByPath(S7 + S1);
  if Obj6 = nil then
    List1.Add('childmissing|' + S1)
  else
  begin
    S3 := 'childports|' + S1;
    Obj2 := Obj6.SchIterator_Create;
    try
      Obj2.AddFilter_ObjectSet(MkSet(ePort));
      Obj3 := Obj2.FirstSchObject;
      while Obj3 <> nil do
      begin
        S3 := S3 + '|' + Obj3.Name + ',' + Obj3.HarnessType;
        Obj3 := Obj2.NextSchObject;
      end;
    finally
      Obj6.SchIterator_Destroy(Obj2);
    end;
    List1.Add(S3);
  end;
end;
ResultText := List1.Text;"""


def _parse_hierarchy(text):
    parent, symbols, child_ports, missing = None, [], {}, []
    for line in (text or "").splitlines():
        f = line.split("|")
        if f[0] == "parent":
            parent = f[1]
        elif f[0] == "symbol":
            entries = []
            for e in f[3:]:
                if not e:
                    continue
                name, side, dft, io, htype = e.rsplit(",", 4)
                entries.append({"name": name, "side": int(side), "distance_from_top": int(dft),
                                "io_type": int(io), "harness_type": htype})
            symbols.append({"sheet_name": f[1], "file_name": f[2], "entries": entries})
        elif f[0] == "childports":
            ports = []
            for p in f[2:]:
                if not p:
                    continue
                name, _, htype = p.rpartition(",")
                ports.append({"name": name, "harness_type": htype})
            child_ports[f[1]] = ports
        elif f[0] == "childmissing":
            missing.append(f[1])
    return parent, symbols, child_ports, missing


def _compare_hierarchy(symbols, child_ports, missing):
    findings, per_symbol = [], []
    for sym in symbols:
        child = sym["file_name"]
        ports = child_ports.get(child)
        row = {"sheet_symbol": sym["sheet_name"], "child": child,
               "entries": len(sym["entries"]), "ports": None if ports is None else len(ports)}
        if ports is None:
            findings.append({"rule": "child_not_loaded", "severity": "info",
                             "message": "child sheet %s is not in memory; compile the project to check it" % child,
                             "child": child})
            per_symbol.append(row)
            continue
        by_entry = {e["name"]: e for e in sym["entries"]}
        by_port = {p["name"]: p for p in ports}
        for name, e in sorted(by_entry.items()):
            p = by_port.get(name)
            if p is None:
                findings.append({"rule": "entry_without_port", "severity": "error",
                                 "message": "sheet entry %r on %s has no port of that name on %s"
                                            % (name, sym["sheet_name"], child),
                                 "child": child, "entry": name})
            elif (p.get("harness_type") or "") != (e.get("harness_type") or ""):
                findings.append({"rule": "harness_type_mismatch", "severity": "error",
                                 "message": "%r: entry harness type %r, port harness type %r"
                                            % (name, e.get("harness_type") or "", p.get("harness_type") or ""),
                                 "child": child, "entry": name})
        for name in sorted(set(by_port) - set(by_entry)):
            findings.append({"rule": "port_without_entry", "severity": "error",
                             "message": "port %r on %s has no sheet entry on %s"
                                        % (name, child, sym["sheet_name"]),
                             "child": child, "port": name})
        row["matched"] = len(set(by_entry) & set(by_port))
        per_symbol.append(row)
    seen = {f.get("child") for f in findings if f["rule"] == "child_not_loaded"}
    for m in missing:
        if m not in seen:
            findings.append({"rule": "child_not_loaded", "severity": "info",
                             "message": "child sheet %s is not in memory" % m, "child": m})
    return findings, per_symbol


@_tool(read_only=True)
async def check_hierarchy_live(ctx: Context, document: str, timeout_seconds: int = 180) -> str:
    """
    Compare every sheet symbol on a parent sheet with the PORTS of the child
    sheet it stands for - the cross-sheet half of connectivity that a
    single-sheet check cannot see.

    Reports entries with no matching port, ports with no matching entry, and
    name matches whose harness types disagree. Altium only complains about
    these at compile time, and then indirectly ("conflicting harness
    definition", "net has only one pin").

    Read-only. A compiled project keeps every sheet in memory, so children are
    read without opening tabs; children that are not loaded are reported as
    such (compile the project, e.g. with check_erc, and run this again).

    Args:
        document (str): full path of the PARENT sheet (the one carrying the
            sheet symbols).

    Returns:
        str: JSON with per-symbol counts, findings, and the raw entry/port
             lists per sheet symbol.
    """
    doc = _canon(document)
    payload, images = await _run_sandbox(_HIERARCHY_PAS.replace("__DOC__", _pas_str(doc)), timeout_seconds)
    if not payload.get("success"):
        return _sandbox_return(payload, images)
    res = (payload.get("result") or "").strip()
    if res.startswith("ABORT"):
        return json.dumps({"success": False, "error": res}, indent=2)
    parent, symbols, child_ports, missing = _parse_hierarchy(res)
    findings, per_symbol = _compare_hierarchy(symbols, child_ports, missing)
    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return json.dumps({"success": True, "parent": parent, "counts": counts,
                       "sheet_symbols": per_symbol, "findings": findings,
                       "entries": {s["sheet_name"]: s["entries"] for s in symbols},
                       "child_ports": child_ports}, indent=2)


@_tool()
async def set_sheet_entries(ctx: Context, sheet_symbol: str, add: list = None, rename: list = None,
                            delete: list = None, apply: bool = False, document: str = None,
                            timeout_seconds: int = 180) -> str:
    """
    Add, edit and remove SHEET ENTRIES on one sheet symbol of a parent sheet -
    the parent-side counterpart of a child sheet's ports.

    An entry sits on the symbol's right edge (side 1) or left edge (side 0) at
    `distance_from_top` below the symbol's top, and carries the same
    harness_type as the child's port. Entry names must match the child's port
    names exactly; check_hierarchy_live (or check_hierarchy on the saved files) reports the disagreements.

    DEFAULTS TO A PREVIEW (apply=False). One call = one undo step.
    Distances are internal units (1 mil = 10000).

    Args:
        sheet_symbol (str): the symbol's SheetName or SheetFileName.
        add (list): [{"name": "SPI6", "distance_from_top": 15000000,
            "side": 1, "io_type": 0, "harness_type": "SPI"}]
        rename (list): [{"match": {"name": "SPI3"}, "name": "SPI6",
            "harness_type": "SPI", "distance_from_top": ..., "side": ..., "io_type": ...}]
        delete (list): entry names to remove.
        apply (bool): False previews; True writes.
        document (str): the PARENT sheet's path (open, not necessarily focused).

    Returns:
        str: JSON with the symbol found, what each change matches, warnings
             (duplicate or unknown names), and when applied the counts.
    """
    add, rename, delete = add or [], rename or [], delete or []
    if not add and not rename and not delete:
        return json.dumps({"success": False, "error": "nothing to do"})
    for a in add:
        if not a.get("name") or a.get("distance_from_top") is None:
            return json.dumps({"success": False, "error": "add entries need name and distance_from_top: %s" % (a,)})
    doc, objs, err = await _sheet_state(["sheetsymbol"], timeout_seconds, document)
    if err:
        return err
    syms = [o for o in objs if o["kind"] == "sheetsymbol"
            and sheet_symbol in (o.get("name"), o.get("file_name"))]
    if len(syms) != 1:
        return json.dumps({"success": False, "document": doc,
                           "error": "sheet symbol %r matches %d symbols on this sheet" % (sheet_symbol, len(syms)),
                           "available": [{"name": o.get("name"), "file_name": o.get("file_name")}
                                         for o in objs if o["kind"] == "sheetsymbol"]}, indent=2)
    sym = syms[0]
    names = {e["name"] for e in sym["entries"]}
    taken = {e["distance_from_top"]: e["name"] for e in sym["entries"]}
    warnings = []
    for a in add:
        if a["name"] in names:
            warnings.append("entry %r already exists on this symbol" % a["name"])
        other = taken.get(int(a["distance_from_top"]))
        if other:
            warnings.append("distance_from_top %d is already used by entry %r" % (int(a["distance_from_top"]), other))
        if int(a["distance_from_top"]) > sym["y_size"]:
            warnings.append("entry %r sits %d below the top, past the symbol's height %d"
                            % (a["name"], int(a["distance_from_top"]), sym["y_size"]))
    for r in rename:
        want = (r.get("match") or {}).get("name")
        if want not in names:
            warnings.append("rename source %r is not an entry on this symbol" % want)
        if r.get("name") and r["name"] in names and r["name"] != want:
            warnings.append("rename target %r already exists" % r["name"])
    for d in delete:
        if d not in names:
            warnings.append("delete target %r is not an entry on this symbol" % d)
    plan = {"symbol": {"name": sym.get("name"), "file_name": sym.get("file_name"),
                       "entries_now": len(sym["entries"])},
            "adds": len(add), "renames": len(rename), "deletes": len(delete)}
    if not apply:
        return json.dumps({"success": True, "applied": False, "document": doc, "plan": plan,
                           "warnings": warnings,
                           "note": "preview only - call again with apply=true to write"}, indent=2)
    script = _target_document(_set_sheet_entries_script(sheet_symbol, add, rename, delete), document)
    return await _apply_script(script, plan, warnings, doc, ["renamed", "added", "deleted"], timeout_seconds)


if __name__ == "__main__":
    logger.info("Starting Altium MCP Server...")
    logger.info(f"Using MCP directory: {MCP_DIR}")
    
    # Initialize the directory
    MCP_DIR.mkdir(exist_ok=True)
    
    # Create the AltiumScript directory if it doesn't exist
    script_dir = MCP_DIR / "AltiumScript"
    script_dir.mkdir(exist_ok=True)
    
    # Paths are verified, with a file dialog if need be, on the first command
    # that needs Altium: a dialog here would hold up the client's initialize.
    # Logged, not printed: under the stdio transport stdout is the protocol.
    logger.info(f"Altium executable: {altium_bridge.config.altium_exe_path or '(not found yet)'}")
    logger.info(f"Script path: {altium_bridge.config.script_path}")
    
    # Run the server
    mcp.run(transport='stdio')
