"""Run the optional AzuraCast router through Liquidsoap's real resolver.

The small AzuraCast provider below implements its ready-annotation contract;
its no-data path deliberately fails, so internal analysis cannot go unnoticed.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mutagen.flac import FLAC

REPO = Path(__file__).resolve().parents[1]
INTEGRATION = REPO / "autocue.azuracast.liq"
NATIVE = '''
let azuracast = ()
def azuracast.api_call(~timeout, action, data) =
  ignore(timeout)
  ignore(action)
  ignore(data)
  error.raise(error.invalid,"Unexpected cache write")
end
def azuracast_test_provider(~request_metadata, ~file_metadata, filename) =
  ignore(file_metadata)
  ignore(filename)
  if request_metadata["azuracast_autocue"] != "true" then
    error.raise(error.invalid,"Unexpected internal AzuraCast analysis")
  end
  print("NATIVE_READY")
  def optional_start =
    if list.assoc.mem("autocue_start_next",request_metadata) then
      {start_next=float_of_string(request_metadata["autocue_start_next"])}
    else {} end
  end
  def optional_gain =
    if list.assoc.mem("liq_amplify",request_metadata) then
      {amplify=request_metadata["liq_amplify"]}
    else {} end
  end
  {
    cue_in=float_of_string(request_metadata["autocue_cue_in"]),
    cue_out=float_of_string(request_metadata["autocue_cue_out"]),
    fade_in=float_of_string(request_metadata["autocue_fade_in"]),
    fade_out=float_of_string(request_metadata["autocue_fade_out"]),
    ...optional_start,...optional_gain
  }
end
autocue.register(name="azuracast",azuracast_test_provider)
settings.autocue.preferred := "azuracast"
enable_autocue_metadata()
'''
if os.environ.get("AZURACAST_NATIVE_FIXTURE"):
    NATIVE = Path(os.environ["AZURACAST_NATIVE_FIXTURE"]).read_text()


@unittest.skipUnless(shutil.which("liquidsoap"), "Liquidsoap is not installed")
class AzuraCastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.raw = cls.root / "raw.flac"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=12", "-af",
                        "afade=t=out:st=9:d=3", str(cls.raw)], check=True)
        cls.files = {}
        cls.tagged = {}
        for prefix in ["liq_", "ac_", "autocue_"]:
            path = cls.root / (prefix + ".flac")
            shutil.copyfile(cls.raw, path)
            data = subprocess.check_output([sys.executable, str(REPO / "cue_file"),
                                            "--tag-prefix", prefix, "-wfr", str(path)],
                                           stderr=subprocess.PIPE, text=True)
            cls.files[prefix] = path
            cls.tagged[prefix] = json.loads(data)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.work.cleanup)
        self.folder = Path(self.work.name)
        self.calls = self.folder / "calls.jsonl"
        self.reader = self.folder / "cue-reader"
        self.reader.write_text(f'''#!{sys.executable}
import json,subprocess,sys
from pathlib import Path
p=subprocess.run([{sys.executable!r},{str(REPO / 'cue_file')!r},*sys.argv[1:]],capture_output=True,text=True)
with Path({str(self.calls)!r}).open('a') as f:
    f.write(json.dumps({{'args':sys.argv[1:],'result':json.loads(p.stdout) if p.returncode==0 else None}})+'\\n')
sys.stdout.write(p.stdout)
sys.stderr.write(p.stderr)
sys.exit(p.returncode)
''')
        self.reader.chmod(0o755)

    @staticmethod
    def manual(**overrides):
        return dict(azuracast_autocue="true", autocue_cue_in="1.25",
                    autocue_cue_out="9.5", autocue_fade_in="0.3",
                    autocue_fade_out="1.0", autocue_start_next="8.5", **overrides)

    def resolve(self, annotations=None, *, prefix="liq_", raw=False, policy="keep",
                native=NATIVE, post_include="", path=None, extra_uris=(),
                api=None, shutdown_delay=0.1):
        path = path or (self.raw if raw else self.files[prefix])
        uri = str(path)
        if annotations:
            uri = "annotate:" + ",".join(k + "=" + json.dumps(v)
                                         for k, v in annotations.items()) + ":" + uri
        extras = "\n".join(f'''
r=request.create({json.dumps(extra)})
ok=request.resolve(r)
print("AZ_EXTRA " ^ json.stringify({{ok=ok,metadata=request.metadata(r)}}))
request.destroy(r)
''' for extra in extra_uris)
        script = self.folder / "test.liq"
        api_code = "" if api is None else '''
def azuracast.api_call(~timeout, action, data) =
  print("CACHE_CALL " ^ json.stringify({timeout=timeout,action=action,payload=data}))
''' + api + '\nend\n'
        script.write_text(native + api_code + f'''
%include {json.dumps(str(INTEGRATION))}
settings.autocue.cue_file.path := {json.dumps(str(self.reader))}
settings.autocue.cue_file.tag_prefix := {json.dumps(prefix)}
settings.autocue.cue_file.nice := false
settings.autocue.amplify_behavior := {json.dumps(policy)}
{post_include}
r=request.create({json.dumps(uri)})
ok=request.resolve(r)
print("AZ_RESULT " ^ json.stringify({{ok=ok,metadata=request.metadata(r)}}))
request.destroy(r)
{extras}
output.dummy(blank())
thread.run(delay={shutdown_delay},fun()->shutdown())
''')
        p = subprocess.run(["liquidsoap", str(script)], capture_output=True,
                           text=True, timeout=35)
        output = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, output[-5000:])
        marker = re.search(r"AZ_RESULT ", output)
        self.assertIsNotNone(marker, output[-5000:])
        result, _ = json.JSONDecoder().raw_decode(output[marker.end():])
        self.assertTrue(result["ok"], output[-5000:])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return dict(result["metadata"]), calls, output

    def test_ready_manual_values_and_gain_win_over_file(self):
        m, calls, out = self.resolve(self.manual(liq_amplify="-3.0 dB"))
        self.assertEqual(calls, [])
        self.assertEqual(out.count("NATIVE_READY"), 1)
        for key, expected in [("liq_cue_in", 1.25), ("liq_cue_out", 9.5),
                              ("liq_fade_in", .3), ("liq_fade_out", 1.)]:
            self.assertAlmostEqual(float(m[key]), expected)
        self.assertEqual(m["liq_amplify"], "-3.0 dB")
        self.assertIn("Using ready AzuraCast", out)

    def test_optional_start_and_amplify(self):
        annotations = self.manual()
        del annotations["autocue_start_next"]
        m, calls, out = self.resolve(annotations)
        self.assertEqual(calls, [])
        self.assertAlmostEqual(float(m["liq_fade_out_start_next"]), 0.)
        self.assertEqual(m["liq_amplify"], self.tagged["liq_"]["liq_amplify"])

    def test_ready_cue_duration_uses_final_points_with_each_prefix(self):
        for prefix in ["liq_", "ac_", "autocue_"]:
            for raw in [False, True]:
                with self.subTest(prefix=prefix, raw=raw):
                    m, calls, out = self.resolve(self.manual(duration="12.00"), prefix=prefix, raw=raw)
                    self.assertEqual(calls, [])
                    self.assertAlmostEqual(float(m[prefix + "cue_duration"]), 8.25)
                    self.assertAlmostEqual(float(m["duration"]), 12., places=2)
                    self.assertAlmostEqual(float(m["liq_cue_in"]), 1.25)
                    self.assertAlmostEqual(float(m["liq_cue_out"]), 9.5)

    def test_no_annotations_use_ready_file_tags(self):
        m, calls, out = self.resolve()
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["result"]["analysis"]["performed"], calls[0])
        self.assertNotIn("NATIVE_READY", out)
        self.assertIn("No ready AzuraCast", out)
        self.assertEqual(m["liq_autocue"], "cue_file")

    def test_untagged_file_is_analyzed(self):
        m, calls, out = self.resolve(raw=True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["result"]["analysis"]["performed"])
        self.assertNotIn("NATIVE_READY", out)
        self.assertGreater(float(m["liq_cue_out"]), 8.)

    def test_gain_only_uses_cue_file_and_preserves_native_gain(self):
        m, calls, out = self.resolve({"liq_amplify": "-3.0 dB"})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("NATIVE_READY", out)
        self.assertEqual(m["liq_amplify"], "-3.0 dB")

    def test_false_marker_does_not_use_azuracast_provider(self):
        a = self.manual()
        a["azuracast_autocue"] = "false"
        m, calls, out = self.resolve(a)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("NATIVE_READY", out)
        self.assertAlmostEqual(float(m["liq_cue_in"]), 0.)

    def test_invalid_bundles_fall_back_without_leaking_custom_fields(self):
        for key, value in [("autocue_fade_out", None), ("autocue_cue_in", "nan"),
                           ("autocue_cue_out", "inf"), ("autocue_fade_in", "-1"),
                           ("autocue_start_next", "99"), ("autocue_cue_out", "0.5"),
                           ("autocue_cue_in", "garbage")]:
            with self.subTest(key=key, value=value):
                if self.calls.exists(): self.calls.unlink()
                a = self.manual(liq_amplify="-4 dB")
                if value is None: del a[key]
                else: a[key] = value
                m, calls, out = self.resolve(a, prefix="autocue_")
                self.assertEqual(len(calls), 1)
                self.assertNotIn("NATIVE_READY", out)
                self.assertIn("falling back to cue_file", out)
                self.assertAlmostEqual(float(m["liq_cue_in"]), 0.)
                self.assertEqual(m["liq_amplify"], "-4 dB")

    def test_native_provider_failure_falls_back(self):
        native = NATIVE.replace('print("NATIVE_READY")',
                                'error.raise(error.invalid,"Test provider failure")')
        m, calls, out = self.resolve(self.manual(), native=native)
        self.assertEqual(len(calls), 1)
        self.assertIn("AzuraCast provider failed", out)
        self.assertAlmostEqual(float(m["liq_cue_in"]), 0.)

    def test_custom_gain_annotation_and_native_precedence(self):
        for prefix in ["ac_", "autocue_"]:
            for native in [False, True]:
                with self.subTest(prefix=prefix, native=native):
                    if self.calls.exists(): self.calls.unlink()
                    a = {prefix + "amplify": "-6 dB"}
                    if native: a["liq_amplify"] = "-3 dB"
                    m, calls, out = self.resolve(a, prefix=prefix)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(m["liq_amplify"], "-3 dB" if native else "-6 dB")

    def test_manual_branch_does_not_use_custom_file_cues(self):
        m, calls, out = self.resolve(self.manual(), prefix="autocue_")
        self.assertEqual(calls, [])
        self.assertAlmostEqual(float(m["liq_cue_in"]), 1.25)
        self.assertAlmostEqual(float(m["liq_cue_out"]), 9.5)

    def test_marker_in_file_does_not_select_azuracast(self):
        from mutagen.flac import FLAC
        path = self.folder / "file-marker.flac"
        shutil.copyfile(self.files["liq_"], path)
        tags = FLAC(path)
        for key, value in self.manual().items(): tags[key] = value
        tags.save()
        m, calls, out = self.resolve(path=path)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("NATIVE_READY", out)
        self.assertAlmostEqual(float(m["liq_cue_in"]), 0.)

    def test_missing_azuracast_runtime_fails_clearly(self):
        p = subprocess.run(["liquidsoap", str(INTEGRATION)], capture_output=True,
                           text=True, timeout=20)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("must be included after AzuraCast", p.stdout + p.stderr)

    def test_cue_file_false_still_skips_fallback(self):
        m, calls, out = self.resolve({"liq_cue_file": "false"})
        self.assertEqual(calls, [])
        self.assertNotIn("NATIVE_READY", out)
        self.assertNotIn("liq_autocue", m)

    def test_zero_fades_and_gain_are_preserved(self):
        a = self.manual(liq_amplify="0 dB")
        a.update(autocue_cue_in="0", autocue_fade_in="0",
                 autocue_fade_out="0", autocue_start_next="9.5")
        m, calls, out = self.resolve(a)
        self.assertEqual(calls, [])
        self.assertEqual(m["liq_amplify"], "0 dB")
        self.assertEqual(float(m["liq_fade_in"]), 0.)
        self.assertEqual(float(m["liq_fade_out"]), 0.)

    def test_marker_alone_falls_back(self):
        m, calls, out = self.resolve({"azuracast_autocue": "true"})
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["result"]["analysis"]["performed"])
        self.assertIn("Invalid or unavailable AzuraCast", out)

    def test_ready_data_never_runs_cue_file_even_for_untagged_file(self):
        m, calls, out = self.resolve(self.manual(liq_amplify="-2 dB"), raw=True)
        self.assertEqual(calls, [])
        self.assertEqual(m["liq_amplify"], "-2 dB")
        self.assertEqual(float(m["liq_cue_in"]), 1.25)

    def test_native_request_controls_keep_their_existing_priority(self):
        # Explicit engine annotations retain priority over provider metadata.
        a = self.manual(liq_cue_in="2.0", liq_amplify="-3 dB")
        m, calls, out = self.resolve(a)
        self.assertEqual(calls, [])
        self.assertEqual(float(m["liq_cue_in"]), 2.)

    def test_gain_policy_is_not_bypassed_in_extra_metadata(self):
        for policy in ["keep", "ignore", "override"]:
            with self.subTest(policy=policy):
                if self.calls.exists(): self.calls.unlink()
                m, calls, out = self.resolve({"liq_amplify": "-3 dB"}, policy=policy)
                self.assertEqual(len(calls), 1)
                if policy in ["keep", "ignore"]:
                    self.assertEqual(m["liq_amplify"], "-3 dB")
                else:
                    self.assertNotEqual(m["liq_amplify"], "-3 dB")

    def test_custom_prefix_skip_and_cue_points_in_fallback(self):
        for prefix in ["ac_", "autocue_"]:
            with self.subTest(prefix=prefix):
                if self.calls.exists(): self.calls.unlink()
                m, calls, out = self.resolve({prefix + "cue_file": "false"}, prefix=prefix)
                self.assertEqual(calls, [])
                self.assertNotIn("liq_autocue", m)
                m, calls, out = self.resolve({prefix + "cue_in": "1.0"}, prefix=prefix)
                self.assertEqual(len(calls), 1)
                self.assertEqual(float(m["liq_cue_in"]), 1.)

    def test_alternating_manual_and_fallback_requests_do_not_leak(self):
        def uri(a):
            return "annotate:" + ",".join(k + "=" + json.dumps(v) for k, v in a.items()) + ":" + str(self.files["liq_"])
        a = self.manual(liq_amplify="-8 dB")
        a.update(autocue_cue_in="2.0", autocue_cue_out="10.0")
        extras = [uri({"liq_amplify": "-4 dB"}), uri(a), str(self.files["liq_"])]
        m, calls, out = self.resolve(self.manual(liq_amplify="-2 dB"), extra_uris=extras)
        self.assertEqual(out.count("NATIVE_READY"), 2)
        self.assertEqual(len(calls), 2)
        rows = []
        for match in re.finditer("AZ_EXTRA ", out):
            data, _ = json.JSONDecoder().raw_decode(out[match.end():])
            self.assertTrue(data["ok"])
            rows.append(dict(data["metadata"]))
        self.assertEqual(len(rows), 3)
        self.assertEqual(m["liq_amplify"], "-2 dB")
        self.assertEqual(rows[0]["liq_amplify"], "-4 dB")
        self.assertEqual(float(rows[0]["liq_cue_in"]), 0.)
        self.assertEqual(rows[1]["liq_amplify"], "-8 dB")
        self.assertEqual(float(rows[1]["liq_cue_in"]), 2.)
        self.assertEqual(float(rows[2]["liq_cue_in"]), 0.)
        self.assertEqual(rows[2]["liq_amplify"], self.tagged["liq_"]["liq_amplify"])

    @staticmethod
    def cache_calls(output):
        result = []
        for match in re.finditer("CACHE_CALL ", output):
            call, _ = json.JSONDecoder().raw_decode(output[match.end():])
            call["payload"] = json.loads(call["payload"])
            result.append(call)
        return result

    def test_successful_fallback_saves_final_provider_values(self):
        for raw in [False, True]:
            with self.subTest(raw=raw):
                if self.calls.exists(): self.calls.unlink()
                m, calls, out = self.resolve({"azuracast_cache_key": "123_1000"}, raw=raw,
                                             api='"true"', shutdown_delay=0.3)
                saved = self.cache_calls(out)
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]["action"], "savecache")
                self.assertEqual(saved[0]["timeout"], 5.)
                payload = saved[0]["payload"]
                self.assertEqual(payload["cache_key"], "123_1000")
                data = payload["data"]
                self.assertEqual(set(data), {"cue_in", "cue_out", "fade_in", "fade_out", "start_next", "amplify"})
                for k in ["cue_in", "cue_out", "fade_in", "fade_out"]:
                    self.assertAlmostEqual(data[k], float(m["liq_" + k]))
                self.assertAlmostEqual(data["start_next"], data["cue_out"] - data["fade_out"])
                self.assertEqual(calls[0]["result"]["analysis"]["performed"], raw)
                self.assertIn("accepted cue_file cache result", out)

    def test_no_cache_key_ready_bundle_and_skipped_provider_do_not_write(self):
        cases = [{}, self.manual(azuracast_cache_key="123_1000"),
                 {"azuracast_cache_key": "123_1000", "liq_cue_file": "false"}]
        for annotations in cases:
            with self.subTest(annotations=annotations):
                m, calls, out = self.resolve(annotations, api='"true"', shutdown_delay=.3)
                self.assertEqual(self.cache_calls(out), [])

    def test_cache_api_failure_never_discards_playback_metadata(self):
        for response in ['null', '"false"', '"not-json"',
                         'error.raise(error.invalid,"Test timeout")']:
            with self.subTest(response=response):
                if self.calls.exists(): self.calls.unlink()
                m, calls, out = self.resolve({"azuracast_cache_key": "123_1000"},
                                             api=response, shutdown_delay=.3)
                self.assertEqual(len(self.cache_calls(out)), 1)
                self.assertEqual(m["liq_autocue"], "cue_file")
                self.assertIn("playback continues", out)
                self.assertNotIn("accepted cue_file cache result", out)

    def test_slow_cache_api_runs_after_resolve_without_blocking_it(self):
        m, calls, out = self.resolve({"azuracast_cache_key": "123_1000"},
            api='thread.delay(0.4)\nprint("CACHE_FINISHED")\n"true"', shutdown_delay=.8)
        self.assertEqual(len(self.cache_calls(out)), 1)
        self.assertLess(out.index("AZ_RESULT "), out.index("CACHE_FINISHED"))

    def test_gain_is_saved_as_db_and_mute_is_not_miscached(self):
        for gain, expected in [("-3 dB", -3.), ("2.0", 6.020599913), ("1", 0.)]:
            with self.subTest(gain=gain):
                m, calls, out = self.resolve({"azuracast_cache_key": "123_1000", "liq_amplify": gain},
                                             api='"true"', shutdown_delay=.3)
                self.assertEqual(m["liq_amplify"], gain)
                self.assertAlmostEqual(self.cache_calls(out)[0]["payload"]["data"]["amplify"], expected, places=6)
        m, calls, out = self.resolve({"azuracast_cache_key": "123_1000", "liq_amplify": "0"},
                                     api='"true"', shutdown_delay=.3)
        self.assertEqual(m["liq_amplify"], "0")
        self.assertEqual(self.cache_calls(out), [])
        self.assertIn("Skipping cue_file cache write", out)

    def test_cached_result_round_trip_skips_cli_and_preserves_controls(self):
        for prefix in ["liq_", "ac_", "autocue_"]:
            with self.subTest(prefix=prefix):
                if self.calls.exists(): self.calls.unlink()
                # Preserve the original file cue_out but force the provider to
                # shorten its final cue_out, reproducing the stale file tag.
                path = self.folder / (prefix + "shortened.flac")
                shutil.copyfile(self.files[prefix], path)
                tags = FLAC(path)
                tags[prefix + "cross_start_next"] = "6.0"
                tags[prefix + "fade_out"] = "1.0"
                tags.save()
                first, calls, out = self.resolve({"azuracast_cache_key": "123_1000"}, prefix=prefix, path=path,
                                                 api='"true"', shutdown_delay=.3)
                data = self.cache_calls(out)[0]["payload"]["data"]
                self.assertAlmostEqual(data["cue_out"], 7.)
                self.assertGreater(float(self.tagged[prefix][prefix + "cue_out"]), 7.)
                # AzuraCast's stored values become nextsong request annotations.
                annotations = {"azuracast_autocue": "true", "azuracast_cache_key": "123_1000",
                               "liq_amplify": str(data["amplify"]) + " dB"}
                annotations.update({"autocue_" + k: str(v) for k, v in data.items() if k != "amplify"})
                self.calls.unlink()
                second, calls, out = self.resolve(annotations, prefix=prefix, path=path, api='"true"', shutdown_delay=.3)
                self.assertEqual(calls, [])
                self.assertEqual(self.cache_calls(out), [])
                self.assertEqual(first[prefix + "cue_duration"], second[prefix + "cue_duration"])
                self.assertAlmostEqual(float(second[prefix + "cue_duration"]),
                                       data["cue_out"] - data["cue_in"], places=2)
                for key in ["liq_cue_in", "liq_cue_out", "liq_fade_in", "liq_fade_out",
                            "liq_fade_out_delay", "liq_fade_out_start_next"]:
                    self.assertAlmostEqual(float(first[key]), float(second[key]))
                for key in ["liq_cross_duration", "liq_cross_end_duration", "liq_cross_max_start_duration"]:
                    if key in first:
                        self.assertAlmostEqual(float(first[key]), float(second[key]))

    def test_ignored_gain_is_not_promoted_to_cached_request_gain(self):
        first, calls, out = self.resolve({"azuracast_cache_key": "123_1000"}, raw=True,
                                         policy="ignore", api='"true"', shutdown_delay=.3)
        data = self.cache_calls(out)[0]["payload"]["data"]
        self.assertNotIn("amplify", data)
        self.assertNotIn("liq_amplify", first)
        annotations = {"azuracast_autocue": "true", "azuracast_cache_key": "123_1000"}
        annotations.update({"autocue_" + k: str(v) for k, v in data.items()})
        self.calls.unlink()
        second, calls, out = self.resolve(annotations, raw=True, policy="ignore", api='"true"', shutdown_delay=.3)
        self.assertEqual(calls, [])
        self.assertNotIn("liq_amplify", second)
        self.assertEqual(self.cache_calls(out), [])

    @unittest.skipUnless(os.environ.get("AZURACAST_RUNTIME_FILE"), "Full AzuraCast runtime fixture is not provided")
    def test_real_http_cache_round_trip_and_http_failure(self):
        runtime = Path(os.environ["AZURACAST_RUNTIME_FILE"]).read_text()
        start = runtime.index("def azuracast.api_call(")
        end = runtime.index("\n# AzuraCast protocol implementation.", start)
        api_function = runtime[start:end]
        requests = []
        status = [200]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "key": self.headers.get("X-Liquidsoap-Api-Key"), "data": data})
                body = b"true" if status[0] == 200 else b"false"
                self.send_response(status[0])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        native = NATIVE + f'''
let settings.azuracast.http_timeout = settings.make(description="test",5.)
let settings.azuracast.api_url = settings.make(description="test","http://127.0.0.1:{server.server_port}")
let settings.azuracast.api_key = settings.make(description="test","local-test-key")
''' + api_function
        first, calls, out = self.resolve({"azuracast_cache_key": "123_1000", "liq_amplify": "2.0"},
                                         native=native, raw=True, shutdown_delay=.5)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/savecache")
        self.assertEqual(requests[0]["key"], "local-test-key")
        data = requests[0]["data"]["data"]
        self.assertAlmostEqual(data["amplify"], 6.020599913, places=6)
        self.assertEqual(first["liq_amplify"], "2.0")
        annotations = {"azuracast_autocue": "true", "azuracast_cache_key": "123_1000",
                       "liq_amplify": str(data["amplify"]) + " dB"}
        annotations.update({"autocue_" + k: str(v) for k, v in data.items() if k != "amplify"})
        self.calls.unlink()
        second, calls, out = self.resolve(annotations, native=native, raw=True, shutdown_delay=.5)
        self.assertEqual(calls, [])
        self.assertEqual(len(requests), 1)
        self.assertAlmostEqual(10 ** (float(second["liq_amplify"].split()[0]) / 20), 2.)
        for key in ["liq_cue_in", "liq_cue_out", "liq_fade_in", "liq_fade_out"]:
            self.assertEqual(float(first[key]), float(second[key]))
        status[0] = 500
        m, calls, out = self.resolve({"azuracast_cache_key": "123_1001"}, native=native, shutdown_delay=.5)
        self.assertEqual(len(requests), 2)
        self.assertEqual(m["liq_autocue"], "cue_file")
        self.assertIn("did not accept", out)


if __name__ == "__main__":
    unittest.main()
