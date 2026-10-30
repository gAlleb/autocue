"""Black-box executable selection and opt-in dependency diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CUE_FILE = Path(os.environ.get("CUE_FILE", REPO / "cue_file"))
ADAPTER = Path(os.environ.get("AUTOCUE_LIQ", REPO / "autocue.cue_file.liq"))


class ToolPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.real = {name: shutil.which(name) for name in ["ffmpeg", "ffprobe"]}
        cls.files = {}
        for ext, codec in [("mp3", "libmp3lame"), ("flac", "flac"), ("m4a", "alac")]:
            path = cls.root / ("tagged." + ext)
            subprocess.run([cls.real["ffmpeg"], "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=3", "-c:a", codec, str(path)], check=True)
            subprocess.run([sys.executable, str(CUE_FILE), "-kwnrf", str(path)],
                           check=True, capture_output=True)
            cls.files[ext] = path

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        self.log = self.case / "calls.jsonl"
        self.tools = {}
        for name in self.real:
            path = self.case / (name + " tool's wrapper")
            path.write_text(f'''#!{sys.executable}
import os, sys, json
with open({str(self.log)!r}, 'a') as f:
    f.write(json.dumps({{'name':{name!r},'args':sys.argv[1:]}})+'\\n')
os.execv({self.real[name]!r}, [{self.real[name]!r}, *sys.argv[1:]])
''')
            path.chmod(0o755)
            self.tools[name] = str(path)
        guard = self.case / "guard"
        guard.mkdir()
        self.fallback = self.case / "unexpected-default"
        for name in self.real:
            p = guard / name
            p.write_text(f'''#!{sys.executable}
from pathlib import Path
Path({str(self.fallback)!r}).write_text('unexpected PATH lookup')
raise SystemExit(97)
''')
            p.chmod(0o755)
        self.env = {**os.environ, "PATH": str(guard) + os.pathsep + os.environ["PATH"]}

    def run_cli(self, *args, code=0):
        p = subprocess.run([sys.executable, str(CUE_FILE), *map(str, args)],
                           capture_output=True, text=True, env=self.env, timeout=25)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertFalse(self.fallback.exists())
        return json.loads(p.stdout)

    def options(self):
        return ["--ffmpeg", self.tools["ffmpeg"], "--ffprobe", self.tools["ffprobe"]]

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_cached_read_uses_selected_probe_without_ffmpeg(self):
        missing = str(self.case / "missing ffmpeg")
        data = self.run_cli("--ffmpeg", missing, "--ffprobe", self.tools["ffprobe"], self.files["flac"])
        self.assertFalse(data["analysis"]["performed"])
        self.assertEqual([x["name"] for x in self.events()], ["ffprobe"])

    def test_selected_tools_analyze_and_write_all_formats(self):
        for ext, original in self.files.items():
            with self.subTest(ext=ext):
                self.log.unlink(missing_ok=True)
                target = self.case / ("write." + ext)
                shutil.copyfile(original, target)
                before = subprocess.check_output([self.real["ffmpeg"], "-v", "error", "-i", str(target),
                                                  "-map", "0:a:0", "-f", "hash", "-hash", "sha256", "-"])
                data = self.run_cli(*self.options(), "-kwnrf", target)
                self.assertTrue(data["analysis"]["performed"])
                events = self.events()
                self.assertEqual(sum(e["name"] == "ffprobe" for e in events), 1)
                copies = [e for e in events if e["name"] == "ffmpeg" and "copy" in e["args"]]
                self.assertEqual(len(copies), 0 if ext == "m4a" else 1)
                self.assertEqual(sum(e["name"] == "ffmpeg" and "copy" not in e["args"] for e in events), 1)
                self.assertFalse(any("-version" in e["args"] for e in events))
                after = subprocess.check_output([self.real["ffmpeg"], "-v", "error", "-i", str(target),
                                                 "-map", "0:a:0", "-f", "hash", "-hash", "sha256", "-"])
                self.assertEqual(before, after)

    def test_manual_cli_diagnostics_and_missing_tool(self):
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.files.values()}
        report = self.run_cli("--diagnose", *self.options())
        self.assertTrue(report["ok"])
        for name in self.tools:
            self.assertEqual(report[name]["path"], self.tools[name])
            self.assertTrue(report[name]["ok"])
            self.assertTrue(report[name]["version"].startswith(name + " version "))
        self.assertEqual(sorted((e["name"], e["args"]) for e in self.events()),
                         [("ffmpeg", ["-version"]), ("ffprobe", ["-version"])])
        bad = self.run_cli("--diagnose", "--ffmpeg", str(self.case / "missing"),
                           "--ffprobe", self.tools["ffprobe"], code=1)
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["ffmpeg"]["ok"])
        self.assertTrue(bad["ffmpeg"]["error"])
        self.assertEqual(hashes, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.files.values()})

    def test_diagnostics_reject_bad_exit_wrong_binary_and_timeout(self):
        bad = self.case / "bad tool"
        for body in ["print('ffmpeg version fake')\nraise SystemExit(3)",
                     "print('not ffmpeg')", "import time\ntime.sleep(10)"]:
            bad.write_text(f"#!{sys.executable}\n" + body + "\n")
            bad.chmod(0o755)
            data = self.run_cli("--diagnose", "--ffmpeg", bad, "--ffprobe", self.tools["ffprobe"], code=1)
            self.assertFalse(data["ffmpeg"]["ok"])
            self.assertTrue(data["ffmpeg"]["error"])
            if "time.sleep" in body:
                self.assertIn("timed out", data["ffmpeg"]["error"])

    @unittest.skipUnless(shutil.which("liquidsoap"), "Liquidsoap is not installed")
    def test_liquidsoap_settings_and_manual_diagnostics(self):
        reader = self.case / "cue reader"
        reader.write_text(f'''#!{sys.executable}
import os,sys
os.execv({sys.executable!r}, [{sys.executable!r},{str(CUE_FILE)!r},*sys.argv[1:]])
''')
        reader.chmod(0o755)
        target = self.case / "adapter.flac"
        shutil.copyfile(self.files["flac"], target)
        # Reject an incompatible/invalid CLI report, even if its tools look healthy.
        tool = {"path": "fake", "ok": True, "version": "fake", "error": ""}
        invalid_calls = []
        for i, version in enumerate(["99.0.0", "unknown"]):
            fake = self.case / f"incompatible CLI {i}"
            payload = json.dumps({"version": version, "ok": True, "ffmpeg": tool, "ffprobe": tool})
            fake.write_text(f"#!{sys.executable}\nprint({payload!r})\n")
            fake.chmod(0o755)
            invalid_calls.append(f'''settings.autocue.cue_file.path := {json.dumps(str(fake))}
if diagnose_cue_file() then error.raise(error.invalid,"Incompatible diagnostic report accepted") end''')
        broken = self.case / "broken report CLI"
        broken.write_text(f"#!{sys.executable}\nprint('not JSON')\n")
        broken.chmod(0o755)
        invalid_calls.append(f'''settings.autocue.cue_file.path := {json.dumps(str(broken))}
if diagnose_cue_file() then error.raise(error.invalid,"Malformed diagnostic report accepted") end''')
        script = self.case / "adapter.liq"
        script.write_text(f'''%include {json.dumps(str(ADAPTER))}
settings.autocue.cue_file.path := {json.dumps(str(reader))}
settings.autocue.cue_file.ffmpeg := {json.dumps(self.tools['ffmpeg'])}
settings.autocue.cue_file.ffprobe := {json.dumps(self.tools['ffprobe'])}
list.iter(fun (use_json) -> begin
  settings.autocue.cue_file.use_json_metadata := use_json
  settings.autocue.cue_file.force_analysis := true
  settings.autocue.cue_file.write_tags := true
  data=cue_file(request_metadata=[],file_metadata=[],{json.dumps(str(target))})
  if not null.defined(data) then error.raise(error.invalid,"Selected adapter tools failed") end
end,[true,false])
print("NORMAL_PROCESSING_DONE")
settings.autocue.preferred := "internal"
settings.autocue.amplify_behavior := "override"
settings.autocue.metadata.priority := 42
if not diagnose_cue_file(print=true) then error.raise(error.invalid,"Manual diagnostics failed") end
settings.autocue.cue_file.ffprobe := {json.dumps(str(self.case / 'missing probe'))}
if diagnose_cue_file() then error.raise(error.invalid,"Missing probe accepted") end
{chr(10).join(invalid_calls)}
settings.autocue.cue_file.path := {json.dumps(str(self.case / 'missing CLI'))}
if diagnose_cue_file() then error.raise(error.invalid,"Missing CLI accepted") end
if settings.autocue.preferred() != "internal" or settings.autocue.amplify_behavior() != "override" or settings.autocue.metadata.priority() != 42 then
  error.raise(error.invalid,"Diagnostic changed provider setup")
end
print("TOOL_SETTINGS_PASSED")
output.dummy(blank())
thread.run(delay=0.1,fun () -> shutdown())
''')
        p = subprocess.run(["liquidsoap", str(script)], capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("TOOL_SETTINGS_PASSED", p.stdout)
        self.assertFalse(self.fallback.exists())
        events = self.events()
        first_probe = next(i for i,e in enumerate(events) if e["args"] == ["-version"])
        normal = events[:first_probe]
        self.assertEqual(sum(e["name"] == "ffprobe" for e in normal), 2)
        self.assertEqual(sum(e["name"] == "ffmpeg" and "copy" in e["args"] for e in normal), 2)
        self.assertEqual(sum(e["name"] == "ffmpeg" and "copy" not in e["args"] for e in normal), 2)
