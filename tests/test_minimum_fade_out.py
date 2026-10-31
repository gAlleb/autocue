"""Provider and native-resolver checks; run inside either supported LS image."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from mutagen.flac import FLAC

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("liquidsoap"), "Liquidsoap is not installed")
class MinimumFadeOutTests(unittest.TestCase):
    def test_policy_and_native_metadata(self):
        # name, minimum, cue_in, cue_out, start_next, requested fade,
        # extension, expected start, expected cue_out, expected fade
        cases = [
            ("default-off", None, 0, 10, 10, None, False, 10, 10, 0),
            ("explicit-off", 0, 0, 10, 10, None, False, 10, 10, 0),
            ("hard-end", 1.1, 0, 10, 10, None, False, 8.9, 10, 1.1),
            ("short-tail", 1.1, 0, 10, 9.5, None, False, 8.9, 10, 1.1),
            ("boundary", 1.1, 0, 10, 8.9, None, False, 8.9, 10, 1.1),
            ("long-enough", 1.1, 0, 10, 8, None, False, 8, 10, 2),
            ("zero-fade", 1.1, 0, 10, 10, 0, False, 10, 10, 0),
            ("short-explicit-fade", 1.1, 0, 10, 10, .2, False, 9.8, 10, .2),
            ("short-track", 1.1, 0, .8, .8, None, False, .8, .8, 0),
            ("exact-track-length", 1.1, 2, 3.1, 3.1, None, False, 3.1, 3.1, 0),
            ("nonzero-cue-in", 1.1, 2, 10, 10, None, False, 8.9, 10, 1.1),
            ("requested-cap", 5, 0, 10, 10, None, False, 7.5, 10, 2.5),
            ("long-tail-default", 1.1, 0, 10, 5, None, False, 5, 7.5, 2.5),
            ("long-tail-extension", 1.1, 0, 10, 5, None, True, 5, 10, 5),
            ("hard-end-extension", 1.1, 0, 10, 10, None, True, 8.9, 10, 1.1),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw.flac"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=12", str(raw)], check=True)
            reader = root / "reader"
            reader.write_text(f'#!/bin/sh\nexec {sys.executable} "{REPO / "cue_file"}" "$@"\n')
            reader.chmod(0o755)
            base = json.loads(subprocess.check_output(
                [sys.executable, str(REPO / "cue_file"), "-f", str(raw)],
                stderr=subprocess.PIPE, text=True))
            code = f'''%include {json.dumps(str(REPO / 'autocue.cue_file.liq'))}
settings.autocue.cue_file.path := {json.dumps(str(reader))}
settings.autocue.cue_file.nice := false
enable_autocue_metadata()
'''
            expected = {}
            hashes = {}
            for prefix in ["liq_", "ac_", "autocue_"]:
                for name, minimum, ci, co, sn, fade, ext, es, eo, ef in cases:
                    key = prefix + name
                    path = root / (key + ".flac")
                    shutil.copyfile(raw, path)
                    data = dict(base, liq_cue_in=ci, liq_cue_out=co,
                                liq_cross_start_next=sn, liq_cue_duration=co-ci)
                    if fade is not None:
                        data["liq_fade_out"] = fade
                    tags = FLAC(path)
                    tags["replaygain_track_gain"] = base["liq_amplify"]
                    tags["replaygain_reference_loudness"] = "-18.00 LUFS"
                    for k, v in data.items():
                        if k.startswith("liq_"):
                            tags[prefix + k[4:]] = str(v).lower() if isinstance(v, bool) else str(v)
                    tags.save()
                    file_meta = "[" + ",".join(
                        "(" + json.dumps(k) + "," + json.dumps(v[0]) + ")"
                        for k, v in tags.items()) + "]"
                    hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
                    # No assignment for the first case: verify the true default.
                    if minimum is not None:
                        code += f'settings.autocue.cue_file.minimum_fade_out := {float(minimum)}\n'
                    elif prefix != "liq_":
                        code += 'settings.autocue.cue_file.minimum_fade_out := 0.0\n'
                    code += f'''settings.autocue.cue_file.tag_prefix := "{prefix}"
settings.autocue.cue_file.extend_fade_out := {str(ext).lower()}
r = cue_file(request_metadata=[], file_metadata={file_meta}, {json.dumps(str(path))})
print("MIN_PROVIDER " ^ json.stringify({{name="{key}", result=r}}))
req = request.create({json.dumps(str(path))})
ok = request.resolve(req)
print("MIN_NATIVE " ^ json.stringify({{name="{key}", ok=ok, metadata=request.metadata(req)}}))
request.destroy(req)
'''
                    expected[key] = (ci, es, eo, ef)
            code += 'output.dummy(blank())\nthread.run(delay=0.1, fun () -> shutdown())\n'
            script = root / "test.liq"
            script.write_text(code)
            p = subprocess.run(["liquidsoap", str(script)], capture_output=True, text=True, timeout=180)
            out = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, out[-6000:])
            results = {}
            for m in re.finditer(r"MIN_(PROVIDER|NATIVE) ", out):
                row, _ = json.JSONDecoder().raw_decode(out[m.end():])
                results[m[1], row["name"]] = row
            self.assertEqual(len(results), len(expected) * 2, out[-6000:])
            for key, (ci, sn, co, fade) in expected.items():
                with self.subTest(case=key):
                    result = results["PROVIDER", key]["result"]
                    for field, value in [("cue_in", ci), ("cue_out", co),
                                         ("start_next", sn), ("fade_out", fade)]:
                        self.assertAlmostEqual(result[field], value, places=5)
                    native = results["NATIVE", key]
                    self.assertTrue(native["ok"])
                    meta = dict(native["metadata"])
                    self.assertEqual(meta["liq_autocue"], "cue_file")
                    for field, value in [("liq_cue_in", ci), ("liq_cue_out", co),
                                         ("liq_fade_out", fade)]:
                        self.assertAlmostEqual(float(meta[field]), value, places=5)
            for path, digest in hashes.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_invalid_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in ["-0.1", "infinity", "(0.0 / 0.0)"]:
                with self.subTest(value=value):
                    script = Path(tmp) / "invalid.liq"
                    script.write_text(f'''%include {json.dumps(str(REPO / 'autocue.cue_file.liq'))}
settings.autocue.cue_file.minimum_fade_out := {value}
ignore(cue_file(request_metadata=[], file_metadata=[], "/nonexistent"))
''')
                    p = subprocess.run(["liquidsoap", str(script)], capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(p.returncode, 0)
                    self.assertIn("Minimum fade-out must be a finite, non-negative duration", p.stdout+p.stderr)


if __name__ == "__main__":
    unittest.main()
