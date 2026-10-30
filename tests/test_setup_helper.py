"""Exercise the optional setup helper in a real Liquidsoap process."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
ADAPTER = Path(os.environ.get("AUTOCUE_LIQ", REPO / "autocue.cue_file.liq"))
CUE_FILE = Path(os.environ.get("CUE_FILE", REPO / "cue_file"))


@unittest.skipUnless(shutil.which("liquidsoap"), "Liquidsoap is not installed")
class SetupHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = self.root / "calls"
        self.counter = 0

    def cli(self, output="cue_file 5.0.0\n", *, code=0, real=False):
        self.counter += 1
        # Spaces and an apostrophe exercise executable-path quoting too.
        path = self.root / f"cli {self.counter}'s helper"
        path.write_text(f'''#!{sys.executable}
import os, sys
from pathlib import Path
if sys.argv[1:] != ['--version']:
    sys.exit(9)
with Path({str(self.calls)!r}).open('a') as f:
    f.write('--version\\n')
if {real!r}:
    os.execv({sys.executable!r}, [{sys.executable!r}, {str(CUE_FILE)!r}, *sys.argv[1:]])
sys.stdout.write({output!r})
sys.stdout.flush()
sys.exit({code!r})
''')
        path.chmod(0o755)
        return str(path)

    def run_liquidsoap(self, body, *, code=0, timeout=20):
        script = self.root / "check.liq"
        script.write_text(f'''%include {json.dumps(str(ADAPTER))}
{body}
output.dummy(blank())
thread.run(delay=0.1, fun () -> shutdown())
''')
        p = subprocess.run(["liquidsoap", str(script)], capture_output=True,
                           text=True, timeout=timeout)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p

    def test_numeric_release_parser_and_invalid_input(self):
        cases = [
            ("5.0.0", 5, 0, 0, "5.0.0"),
            ("cue_file 5.10.20", 5, 10, 20, "5.10.20"),
            ("  cue_file\t10.2.30  ", 10, 2, 30, "10.2.30"),
            ("cue_file 5.1.0-rc.1+build.7", 5, 1, 0, "5.1.0-rc.1+build.7"),
            ("0.2.3", 0, 2, 3, "0.2.3"),
        ]
        invalid = ["", "unknown", "5", "5.0", "5.0.0.1", "v5.0.0",
                   "ffmpeg 5.0.0", "cue_file garbage 5.0.0", "5.0.0garbage",
                   "5.-1.0", "05.0.0", "5.0.0-", "5.0.0+", "5.0.0-rc..1",
                   "999999999999999999999999999999.0.0",
                   "5.999999999999999999999999999999.0"]
        rows = ",\n".join("(" + ",".join(json.dumps(v) for v in row) + ")"
                          for row in cases)
        self.run_liquidsoap(f'''
list.iter(fun (entry) -> begin
  let (text, major, minor, patch, normalized) = entry
  v=semver(text)
  if null.get(v?.major) != major or null.get(v?.minor) != minor or null.get(v?.patch) != patch or v.version != normalized then
    error.raise(error.invalid,"Wrong numeric version parse: " ^ string(text))
  end
end,[{rows}])
list.iter(fun (text) -> begin
  if null.defined(semver(text)?.major) then
    error.raise(error.invalid,"Malformed version accepted: " ^ string(text))
  end
end,{json.dumps(invalid)})
if null.defined(semver(null)?.major) then error.raise(error.invalid,"Null version accepted") end
''')

    def test_real_cli_and_substituted_version_matrix(self):
        # expected adapter version, CLI output, compatible, normalized version
        matrix = [
            ("5.0.0", "cue_file 5.0.0\nmutagen test\n", True, "5.0.0"),
            ("5.0.0", "cue_file 5.0.1\n", True, "5.0.1"),
            ("5.0.0", "cue_file 5.99.10\n", True, "5.99.10"),
            ("5.99.10", "cue_file 5.0.0\n", True, "5.0.0"),
            ("5.0.0", "cue_file 4.99.99\n", False, "4.99.99"),
            ("5.0.0", "cue_file 6.0.0\n", False, "6.0.0"),
            ("5.0.0", "cue_file 10.0.0\n", False, "10.0.0"),
            ("10.2.3", "cue_file 10.10.0\n", True, "10.10.0"),
            ("10.2.3", "cue_file 2.10.0\n", False, "2.10.0"),
            ("5.0.0", "cue_file 5.1.0-rc.1+build.7\r\n", True, "5.1.0-rc.1+build.7"),
            ("5.0.0", "5.2.3\n", True, "5.2.3"),
            ("5.0.0", "garbage\n", False, "(unknown)"),
            ("5.0.0", "", False, "(unknown)"),
            ("5.0.0", "ffmpeg 5.0.0\n", False, "(unknown)"),
            ("invalid", "garbage\n", False, "(unknown)"),
            ("invalid", "cue_file 5.0.0\n", False, "5.0.0"),
        ]
        rows = [(adapter, self.cli(output), compatible, normalized)
                for adapter, output, compatible, normalized in matrix]
        # A failing process must be rejected even if it prints a valid version.
        rows.append(("5.0.0", self.cli(code=3), False, "(unknown)"))
        real_version = subprocess.check_output(
            [sys.executable, str(CUE_FILE), "--version"], text=True
        ).splitlines()[0].split()[-1]
        rows.append(("5.0.0", self.cli(real=True), True, real_version))
        entries = ",\n".join("(" + ",".join(json.dumps(v) for v in row) + ")"
                             for row in rows)
        p = self.run_liquidsoap(f'''
list.iter(fun (entry) -> begin
  let (expected, executable, compatible, normalized) = entry
  settings.autocue.cue_file.version := expected
  settings.autocue.cue_file.path := executable
  settings.autocue.metadata.priority := 42
  settings.autocue.preferred := "internal"
  settings.autocue.amplify_behavior := "override"
  if check_autocue_setup(print=true) != compatible then
    error.raise(error.invalid,"Wrong compatibility: " ^ string(entry))
  end
  if settings.autocue.cue_file.version_external() != normalized then
    error.raise(error.invalid,"Wrong reported external version")
  end
  if compatible then
    if settings.autocue.metadata.priority() != 10 or settings.autocue.preferred() != "cue_file" or settings.autocue.amplify_behavior() != "keep" then
      error.raise(error.invalid,"Successful setup behavior changed")
    end
  else
    if settings.autocue.metadata.priority() != 42 or settings.autocue.preferred() != "internal" or settings.autocue.amplify_behavior() != "override" then
      error.raise(error.invalid,"Failed probe changed existing setup")
    end
  end
end,[{entries}])
print("SETUP_VERSION_MATRIX_PASSED")
''')
        self.assertIn("SETUP_VERSION_MATRIX_PASSED", p.stdout)
        self.assertEqual(self.calls.read_text().splitlines(), ["--version"] * len(rows))

    def test_missing_and_non_executable_cli(self):
        missing = str(self.root / "missing cue file")
        not_executable = self.root / "not executable"
        not_executable.write_text("cue_file 5.0.0\n")
        self.run_liquidsoap(f'''
list.iter(fun (path) -> begin
  settings.autocue.cue_file.path := path
  if check_autocue_setup() then error.raise(error.invalid,"Unavailable CLI accepted") end
  if settings.autocue.cue_file.version_external() != "(unknown)" then
    error.raise(error.invalid,"Unavailable CLI left a usable version")
  end
end,{json.dumps([missing, str(not_executable)])})
''')

    def test_explicit_shutdown_on_incompatible_major(self):
        cli = self.cli("cue_file 6.0.0\n")
        self.run_liquidsoap(f'''
settings.autocue.cue_file.path := {json.dumps(cli)}
ignore(check_autocue_setup(shutdown=true,print=true))
''', code=2)

    def test_helper_remains_opt_in(self):
        cli = self.cli()
        self.run_liquidsoap(f'''
settings.autocue.cue_file.path := {json.dumps(cli)}
# No explicit helper call: including the adapter must not probe the CLI.
''')
        self.assertFalse(self.calls.exists())
