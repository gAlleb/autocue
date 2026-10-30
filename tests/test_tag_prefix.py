"""Black-box tests: python3 -m unittest discover -s tests.

Requires ffmpeg, ffprobe and Mutagen. CUE_FILE may select another executable.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from mutagen.flac import FLAC

CUE_FILE = Path(os.environ.get("CUE_FILE", Path(__file__).resolve().parents[1] / "cue_file"))


class TagPrefixTests(unittest.TestCase):
    @staticmethod
    def cli(path, *args):
        return json.loads(subprocess.check_output(
            [sys.executable, str(CUE_FILE), *args, str(path)], stderr=subprocess.PIPE))

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.files = {}
        for ext, codec in [("mp3", "libmp3lame"), ("flac", "flac"), ("m4a", "alac")]:
            raw = cls.root / ("raw." + ext)
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=12", "-af", "afade=t=out:st=9:d=3",
                "-metadata", "artist=Prefix test", "-c:a", codec, str(raw)
            ], check=True)
            for prefix in ["liq_", "ac_"]:
                path = cls.root / (prefix + "." + ext)
                shutil.copyfile(raw, path)
                cls.cli(path, "--tag-prefix", prefix, "-kwnrf")
                cls.files[ext, prefix] = path

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def tags(path):
        data = json.loads(subprocess.check_output([
            "ffprobe", "-v", "quiet", "-show_entries", "format_tags:stream_tags",
            "-of", "json", str(path)]))
        tags = {}
        for stream in data.get("streams", []):
            tags.update(stream.get("tags", {}))
        tags.update(data.get("format", {}).get("tags", {}))
        return {k.lower(): v for k, v in tags.items()}

    def test_formats_and_legacy_default(self):
        for ext in ["mp3", "flac", "m4a"]:
            with self.subTest(ext=ext):
                legacy, custom = self.files[ext, "liq_"], self.files[ext, "ac_"]
                before = hashlib.sha256(custom.read_bytes()).hexdigest()
                old = self.cli(legacy)
                self.assertEqual(old, self.cli(legacy, "--tag-prefix", "liq_"))
                new = self.cli(custom, "--tag-prefix", "AC_")
                self.assertFalse(new["analysis"]["performed"])
                self.assertEqual(old, new)
                self.assertEqual(before, hashlib.sha256(custom.read_bytes()).hexdigest())
                tags = self.tags(custom)
                self.assertIn("ac_cue_in", tags)
                self.assertIn("replaygain_track_gain", tags)
                self.assertEqual(tags["artist"], "Prefix test")
                self.assertFalse(any(k.startswith("liq_") for k in tags))
                self.assertEqual(len(tags["ac_true_peak"].split(".")[1]), 6)
                self.assertFalse(any(k.startswith("ac_") for k in new))

    def mixed_file(self, name):
        path = self.root / name
        shutil.copyfile(self.files["flac", "ac_"], path)
        tags = FLAC(path)
        for key, value in self.tags(self.files["flac", "liq_"]).items():
            if key.startswith("liq_"):
                tags[key] = value
        tags["liq_cue_in"] = "3.00"
        tags["ac_unrelated"] = "keep me"
        tags.save()
        return path

    def test_selected_namespace_and_canonical_json(self):
        path = self.mixed_file("mixed.flac")
        self.assertEqual(self.cli(path)["liq_cue_in"], 3.)
        self.assertEqual(self.cli(path, "--tag-prefix", "ac_")["liq_cue_in"], 0.)
        override = self.root / "override.json"
        override.write_text(json.dumps({"liq_cue_in": "1.25"}))
        result = self.cli(path, "--tag-prefix", "ac_", "-j", str(override))
        self.assertEqual(result["liq_cue_in"], 1.25)
        self.assertFalse(result["analysis"]["performed"])

    def test_partial_namespace_does_not_use_legacy(self):
        path = self.mixed_file("partial.flac")
        tags = FLAC(path)
        del tags["ac_cue_out"]
        tags.save()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.cli(path, "--tag-prefix", "ac_")
        self.assertTrue(result["analysis"]["performed"])
        self.assertIn("liq_cue_out", result["analysis"]["reason"])
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_write_preserves_other_tags_and_audio(self):
        path = self.mixed_file("rewrite.flac")
        def audio_hash():
            return subprocess.check_output([
                "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                "-f", "hash", "-hash", "sha256", "-"])
        before = audio_hash()
        result = self.cli(path, "--tag-prefix", "ac_", "-w")
        tags = self.tags(path)
        self.assertFalse(result["analysis"]["performed"])
        self.assertEqual(tags["liq_cue_in"], "3.00")
        self.assertEqual(tags["ac_unrelated"], "keep me")
        self.assertEqual(before, audio_hash())

    def test_invalid_prefix_does_not_touch_file(self):
        path = self.files["flac", "liq_"]
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        for prefix in ["", "bad prefix", "ac_=", "ac_\n"]:
            p = subprocess.run([sys.executable, str(CUE_FILE), "--tag-prefix", prefix,
                                "-w", str(path)], capture_output=True)
            self.assertEqual(p.returncode, 2)
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_other_prefixes(self):
        for prefix in ["cue_file_", "radio.v1-", "liq_custom_"]:
            path = self.root / (prefix + ".flac")
            shutil.copyfile(self.root / "raw.flac", path)
            self.cli(path, "--tag-prefix", prefix, "-kwnrf")
            result = self.cli(path, "--tag-prefix", prefix)
            self.assertFalse(result["analysis"]["performed"])
            self.assertIn(prefix + "cue_in", self.tags(path))

    @unittest.skipUnless(shutil.which("liquidsoap"), "Liquidsoap is not installed")
    def test_liquidsoap_amplify_policy(self):
        adapter = Path(os.environ.get(
            "AUTOCUE_LIQ", Path(__file__).resolve().parents[1] / "autocue.cue_file.liq"))
        wrapper = self.root / "gain-reader"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                           + shlex.quote(str(CUE_FILE)) + ' "$@"\n')
        wrapper.chmod(0o755)
        cases = []
        for prefix in ["liq_", "ac_"]:
            normal = self.files["flac", prefix]
            calculated = self.cli(normal, "--tag-prefix", prefix)["liq_amplify"]
            for mode in ["keep", "override", "ignore"]:
                # Explicit request gain must survive keep/ignore even when
                # the provider derives a different gain from ReplayGain.
                expected = calculated if mode == "override" else "1.00 dB"
                uri = 'annotate:liq_amplify="1.00 dB":' + str(normal)
                cases.append((prefix, mode, uri, expected))
            cases.append((prefix, "keep", str(normal), calculated))
        raw = self.root / "raw.flac"
        calculated = self.cli(raw)["liq_amplify"]
        for mode in ["keep", "override", "ignore"]:
            cases.append(("liq_", mode, str(raw), "" if mode == "ignore" else calculated))
        script = self.root / "gain.liq"
        entries = ",\n".join("(" + ",".join(json.dumps(v) for v in case) + ")" for case in cases)
        script.write_text(f'''settings.log.stdout := true
settings.log.level := 3
%include {json.dumps(str(adapter))}
settings.autocue.cue_file.path := {json.dumps(str(wrapper))}
enable_autocue_metadata()
list.iter(fun (entry) -> begin
  let (prefix, mode, uri, expected) = entry
  settings.autocue.cue_file.tag_prefix := prefix
  settings.autocue.amplify_behavior := mode
  r=request.create(uri)
  if not request.resolve(r) then error.raise(error.invalid,"Gain test resolve failed") end
  m=request.metadata(r)
  if m["liq_autocue"] != "cue_file" or m["liq_amplify"] != expected then
    error.raise(error.invalid,"Wrong gain: " ^ prefix ^ " " ^ mode ^ " " ^ uri ^ " expected=" ^ expected ^ " actual=" ^ m["liq_amplify"])
  end
  request.destroy(r)
end, [{entries}])
print("GAIN_POLICY_TEST_PASSED")
output.dummy(blank())
thread.run(delay=0.1, fun () -> shutdown())
''')
        p = subprocess.run(["liquidsoap", str(script)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("GAIN_POLICY_TEST_PASSED", p.stdout)

    @unittest.skipUnless(shutil.which("liquidsoap"), "Liquidsoap is not installed")
    def test_liquidsoap_reads_selected_namespace(self):
        adapter = Path(os.environ.get(
            "AUTOCUE_LIQ", Path(__file__).resolve().parents[1] / "autocue.cue_file.liq"))
        path = self.mixed_file("liquidsoap-mixed.flac")
        tags = FLAC(path)
        tags["liq_fade_in"] = "2.0"
        tags["ac_fade_in"] = "0.5"
        tags.save()
        wrapper = self.root / "reader"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                           + shlex.quote(str(CUE_FILE)) + ' "$@"\n')
        wrapper.chmod(0o755)
        script = self.root / "prefix.liq"
        script.write_text(f'''settings.log.stdout := true
settings.log.level := 3
%include {json.dumps(str(adapter))}
settings.autocue.cue_file.path := {json.dumps(str(wrapper))}
settings.autocue.cue_file.tag_prefix := "AC_"
enable_autocue_metadata()
r=request.create({json.dumps(str(path))})
if not request.resolve(r) then error.raise(error.invalid,"Resolve failed") end
m=request.metadata(r)
if m["liq_autocue"] != "cue_file" or m["liq_cue_in"] != "0.0" or m["liq_fade_in"] != "0.5" then
  error.raise(error.invalid,"Wrong file namespace")
end
request.destroy(r)
r=request.create(excluded_metadata_resolvers=["autocue"], {json.dumps(str(path))})
ignore(request.resolve(r))
data=null.get(cue_file(request_metadata=[("liq_cue_in","1.25"),("liq_fade_in","0.3")],file_metadata=request.metadata(r),{json.dumps(str(path))}))
if data.cue_in != 1.25 or data.fade_in != 0.3 then error.raise(error.invalid,"Request priority changed") end
request.destroy(r)
print("PREFIX_TEST_PASSED")
output.dummy(blank())
thread.run(delay=0.1, fun () -> shutdown())
''')
        p = subprocess.run(["liquidsoap", str(script)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFIX_TEST_PASSED", p.stdout)
        self.assertIn("Using existing tags; audio analysis skipped", p.stdout)


if __name__ == "__main__":
    unittest.main()
