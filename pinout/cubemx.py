"""Ask STM32CubeMX itself whether an .ioc is what it would have written.

`IocFile.check()` knows the rules we have already learned. This module catches
the ones we have not: it loads a COPY of the file in CubeMX headless, saves it,
and diffs. The GUI reports only some of its repairs as load warnings and the
headless mode logs none of them, but the SAVED FILE shows every one - on the
X9 Mission Computer (2026-09-22) EXTI pins without their SH binding came back
with `SH.GPXTIn.ConfNb=0` and every NVIC vector gone.

Keys we wrote that CubeMX REMOVED are the failures that matter. Added and
changed keys are reported too; some of those are CubeMX normalising the file
on any save (`ProjectManager.functionlistsort`, `Mcu.PinN` order), so pass the
untouched original as `baseline` and only the changes our edit caused remain.

    python -m pinout.cubemx edited.ioc --baseline original.ioc

Never point this at the only copy of a file: it works on a temporary copy,
but CubeMX is still CubeMX.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .ioc import IocFile


class CubeMXError(RuntimeError):
    pass


def find_cubemx() -> List[Path]:
    """Installed CubeMX homes, STM32CUBEMX_HOME first, then newest first.

    Machines accumulate installs: an old one under Program Files refuses a
    newer .ioc ("The version of the current IOC is too high"), so every
    candidate is returned and roundtrip() moves on when one refuses.
    """
    seen, found = set(), []
    env = os.environ.get("STM32CUBEMX_HOME")
    candidates = [Path(env)] if env else []
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"),
                 os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates += [Path(base, "Programs", "STM32CubeMX"),
                           Path(base, "STMicroelectronics", "STM32Cube", "STM32CubeMX")]
    for home in candidates:
        exe = home / "STM32CubeMX.exe"
        if exe.is_file() and home.resolve() not in seen:
            seen.add(home.resolve())
            found.append(home)
    pinned = found[:1] if env else []
    rest = sorted(found[len(pinned):], key=lambda h: (h / "STM32CubeMX.exe").stat().st_mtime,
                  reverse=True)
    return pinned + rest


def _keys(text: str) -> Dict[str, str]:
    ioc = IocFile(text)
    return {k: ioc.get(k) for k in ioc.keys()}


def _is_pin_list(key: str) -> bool:
    return key == "Mcu.PinsNb" or (key.startswith("Mcu.Pin") and key[len("Mcu.Pin"):].isdigit())


@dataclass
class RoundTrip:
    added: Dict[str, str] = field(default_factory=dict)
    removed: Dict[str, str] = field(default_factory=dict)
    changed: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    pins_added: List[str] = field(default_factory=list)
    pins_removed: List[str] = field(default_factory=list)
    cubemx: Optional[Path] = None
    saved_text: str = ""
    log: str = ""

    @property
    def ok(self) -> bool:
        """Nothing we wrote was thrown away. Added/changed keys still deserve a look."""
        return not self.removed and not self.pins_removed

    @property
    def clean(self) -> bool:
        """CubeMX would have written exactly this file (pin order aside)."""
        return self.ok and not self.added and not self.changed and not self.pins_added

    def summary(self) -> str:
        if self.clean:
            return "CubeMX round trip: no changes (pin-list order aside)"
        out = ["CubeMX round trip:"]
        out += [f"  REMOVED {k}={v}" for k, v in sorted(self.removed.items())]
        out += [f"  REMOVED from Mcu.PinN: {p}" for p in self.pins_removed]
        out += [f"  added   {k}={v}" for k, v in sorted(self.added.items())]
        out += [f"  added to Mcu.PinN: {p}" for p in self.pins_added]
        out += [f"  changed {k}: {a} -> {b}" for k, (a, b) in sorted(self.changed.items())]
        return "\n".join(out)


def diff(before: str, after: str) -> RoundTrip:
    """What changed between the file we handed CubeMX and the file it saved.

    Mcu.PinN is compared as a set: CubeMX re-sorts it by ball position on
    every save, and only membership carries meaning.
    """
    b, a = _keys(before), _keys(after)
    r = RoundTrip()
    for k in b.keys() | a.keys():
        if _is_pin_list(k):
            continue
        if k not in a:
            r.removed[k] = b[k]
        elif k not in b:
            r.added[k] = a[k]
        elif a[k] != b[k]:
            r.changed[k] = (b[k], a[k])
    pb = {v for k, v in b.items() if _is_pin_list(k) and k != "Mcu.PinsNb"}
    pa = {v for k, v in a.items() if _is_pin_list(k) and k != "Mcu.PinsNb"}
    r.pins_added, r.pins_removed = sorted(pa - pb), sorted(pb - pa)
    return r


def subtract_baseline(result: RoundTrip, baseline: RoundTrip) -> RoundTrip:
    """Drop the changes CubeMX also makes to the untouched original: those are
    CubeMX normalising any file it saves, not consequences of our edit."""
    r = RoundTrip(cubemx=result.cubemx, saved_text=result.saved_text, log=result.log,
                  pins_added=result.pins_added, pins_removed=result.pins_removed)
    r.added = {k: v for k, v in result.added.items() if baseline.added.get(k) != v}
    r.removed = {k: v for k, v in result.removed.items() if k not in baseline.removed}
    r.changed = {k: ab for k, ab in result.changed.items()
                 if baseline.changed.get(k, (None, None))[1] != ab[1]}
    return r


def copy_name(text: str, fallback: str) -> str:
    """File name for the temporary copy. CubeMX renames the project after the
    file it saves, so a copy called `x.fixed.ioc` would report ProjectName and
    ProjectFileName as changed; the file's own ProjectFileName avoids that."""
    name = IocFile(text).get("ProjectManager.ProjectFileName") or ""
    return name if name.endswith(".ioc") and "/" not in name and "\\" not in name else fallback


def _run(home: Path, files: List[Path], timeout: int) -> str:
    script = files[0].parent.parent / "roundtrip.txt"
    lines = []
    for f in files:
        lines += [f'config load "{f}"', f'config save "{f}"']
    script.write_bytes(("\r\n".join(lines + ["exit"]) + "\r\n").encode("utf-8"))
    java = home / "jre" / "bin" / ("java.exe" if os.name == "nt" else "java")
    proc = subprocess.run([str(java), "-jar", "STM32CubeMX.exe", "-q", str(script)],
                          cwd=home, capture_output=True, timeout=timeout)
    return (proc.stdout + proc.stderr).decode("utf-8", "replace")


def roundtrip(ioc_path, baseline=None, cubemx: Optional[Path] = None,
              timeout: int = 900) -> RoundTrip:
    """Load and save a temporary copy of `ioc_path` with CubeMX; report the diff.

    With `baseline` (the untouched original), it is round-tripped in the same
    CubeMX session and its normalisation is subtracted from the result.
    """
    homes = [Path(cubemx)] if cubemx else find_cubemx()
    if not homes:
        raise CubeMXError("no STM32CubeMX install found; set STM32CUBEMX_HOME")
    sources = [Path(ioc_path)] + ([Path(baseline)] if baseline else [])
    befores = [p.read_bytes().decode("utf-8") for p in sources]
    last_log = ""
    for home in homes:
        tmp = Path(tempfile.mkdtemp(prefix="cubemx_rt_"))
        try:
            copies = []
            for i, (src, text) in enumerate(zip(sources, befores)):
                d = tmp / f"f{i}"
                d.mkdir()
                copies.append(d / copy_name(text, src.name))
                shutil.copyfile(src, copies[-1])
            log = _run(home, copies, timeout)
            if "version of the current IOC is too high" in log:
                last_log = log
                continue                            # older install; try the next one
            if any(line.strip() == "KO" for line in log.splitlines()):
                raise CubeMXError(f"CubeMX at {home} failed a command:\n" + log[-3000:])
            afters = [c.read_bytes().decode("utf-8") for c in copies]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        result = diff(befores[0], afters[0])
        if baseline:
            result = subtract_baseline(result, diff(befores[1], afters[1]))
        result.cubemx, result.saved_text, result.log = home, afters[0], log
        return result
    raise CubeMXError("every CubeMX install refused the file as too new for it:\n" + last_log[-2000:])


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ioc")
    ap.add_argument("--baseline", help="the untouched original, to subtract CubeMX's own normalisation")
    ap.add_argument("--cubemx", help="CubeMX install folder (default: newest found)")
    args = ap.parse_args(argv)
    problems = IocFile.load(args.ioc).check()
    for p in problems:
        print(f"check: {p}")
    r = roundtrip(args.ioc, baseline=args.baseline, cubemx=args.cubemx)
    print(f"CubeMX: {r.cubemx}")
    print(r.summary())
    return 0 if (r.ok and not problems) else 1


if __name__ == "__main__":
    sys.exit(main())
