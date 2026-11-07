"""Small native execution boundary for the macOS coding fixture, not a platform."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run_fixture_tests(fixture: Path, timeout: float = 10):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        raise RuntimeError(
            "This fixture requires the verified macOS execution boundary"
        )
    fixture = fixture.resolve(strict=True)
    interpreter = Path(sys._base_executable).resolve(strict=True)
    python_root = Path(sys.base_prefix).resolve(strict=True)

    def quoted(path):
        return json.dumps(str(path))

    with tempfile.TemporaryDirectory(prefix="fixture-scratch-") as directory:
        scratch = Path(directory).resolve()
        profile = f"""(version 1)
(deny default)
(deny network* process-info* sysctl-read signal process-fork)
(import "dyld-support.sb")
(allow syscall* mach-bootstrap process-exec)
(allow file-read-metadata)
(allow sysctl-read (sysctl-name "hw.pagesize" "hw.pagesize32" "hw.pagesize64" "hw.ncpu" "hw.activecpu" "hw.physicalcpu" "hw.logicalcpu" "hw.memsize" "kern.osrelease" "kern.ostype" "kern.osversion" "kern.argmax" "kern.boottime" "hw.machine" "kern.version" "kern.usrstack64"))
(allow file-read* file-map-executable
  (subpath "/System/Library") (subpath "/usr/lib") (subpath "/usr/share")
  (literal "/dev/random") (literal "/dev/urandom")
  (subpath {quoted(python_root)}) (subpath {quoted(fixture)}))
(allow file-read* file-write* (subpath {quoted(scratch)}))
(allow file-read* file-write-data (literal "/dev/null") (literal "/dev/zero"))
"""
        # A regular file plus the child's OS file-size limit bounds output while
        # it is produced. A post-hoc slice of PIPE output would not bound memory.
        runner = (
            "import resource,sys,unittest; "
            "resource.setrlimit(resource.RLIMIT_FSIZE,(65536,65536)); "
            "unittest.main(module=None,argv=['unittest','discover','-s',sys.argv[1],"
            "'-p','test_solution.py','-v'])"
        )
        with (scratch / "output.log").open("w+b") as output:
            process = subprocess.run(
                [
                    "/usr/bin/sandbox-exec",
                    "-p",
                    profile,
                    str(interpreter),
                    "-I",
                    "-B",
                    "-c",
                    runner,
                    str(fixture),
                ],
                cwd=fixture,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(scratch),
                    "TMPDIR": str(scratch),
                    "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
            )
            output.seek(0)
            diagnostic = output.read(65536).decode(errors="replace")
        return subprocess.CompletedProcess(
            process.args, process.returncode, "", diagnostic
        )
