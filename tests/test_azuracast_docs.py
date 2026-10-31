"""Execute the published AzuraCast transition, including version selection."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import wave

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "azuracast.md"


@unittest.skipUnless(shutil.which("liquidsoap"), "Liquidsoap is not installed")
class AzuraCastDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        text = DOC.read_text().split("<!-- azuracast-crossfade-example -->", 1)[1]
        self.example = re.search(r"```liquidsoap\n(.*?)\n```", text, re.S)[1]
        version = subprocess.check_output(["liquidsoap", "--version"], text=True)
        match = re.search(r"Liquidsoap (\d+)\.(\d+)", version)
        self.modern = tuple(map(int, match.groups())) >= (2, 5)

    def run_script(self, name, code):
        path = self.root / (name + ".liq")
        path.write_text(code)
        p = subprocess.run(["liquidsoap", str(path)], capture_output=True,
                           text=True, timeout=20)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-5000:])
        return p.stdout + p.stderr

    def test_version_branch_and_explicit_delay_priority(self):
        # Execute the exact metadata-selection block from the documented
        # function. Assert its contract independently of audio buffering.
        block = self.example.split("%ifversion", 1)[1].split("%endif", 1)[0]
        function = "def select_delay(old, new) =\n%ifversion" + block + """%endif
fade_in_metadata
end
"""
        cases = [([], [("liq_fade_out_start_next", "1.2")], "1.2" if self.modern else ""),
                 ([("liq_fade_in_delay", "0.4")], [("liq_fade_out_start_next", "1.2")], "0.4"),
                 ([], [], "")]
        code = function
        for incoming, outgoing, expected in cases:
            def liq_pairs(pairs):
                return "[" + ",".join("(" + json.dumps(k) + "," + json.dumps(v) + ")"
                                       for k, v in pairs) + "]"
            code += f'''
m=select_delay({{metadata={liq_pairs(outgoing)}}}, {{metadata={liq_pairs(incoming)}}})
if m["liq_fade_in_delay"] != {json.dumps(expected)} then
  error.raise(error.failure, "Unexpected delay branch")
end
'''
        code += 'output.dummy(blank())\nthread.run(delay=0.1,fun()->shutdown())\n'
        self.run_script("branch", code)

    def render(self, name, *, reference, live, explicit):
        # Reference uses fade.in's direct delay argument on 2.5; the published
        # example transfers metadata instead. 2.4 must retain the old add.
        delay = 'delay=float_of_string(default=0., old.metadata["liq_fade_out_start_next"]),' if self.modern else ''
        transition = '''def live_aware_crossfade(old,new) =
if azuracast.to_live() then
sequence([fade.out(duration=2.5,old.source),fade.in(duration=0.1,new.source)])
else
add(normalize=false,[
fade.in(type="lin",''' + delay + '''initial_metadata=new.metadata,duration=0.1,new.source),
fade.out(type="lin",initial_metadata=old.metadata,duration=2.5,old.source)])
end
end
radio=cross(duration=2.5,live_aware_crossfade,radio)
''' if reference else self.example
        wav = self.root / (name + ".wav")
        code = f'''
settings.frame.duration := 0.02
%include {json.dumps(str(REPO / "autocue.cue_file.liq"))}
let azuracast = {{to_live=fun()->{str(live).lower()}}}
def track(freq, start, incoming) =
  m=autocue.metadata(implementation="test",{{cue_in=0.,cue_out=4.,fade_in=0.7,fade_out=2.,start_next=start}})
  metadata.map(insert_missing=true,update=false,fun(_)->[...incoming,...m],
    sine(duration=4.,amplitude=0.1,freq))
end
radio=sequence([
  track(440.,3.2,[]),
  track(880.,3.2,{ '[("liq_fade_in_delay","0.4")]' if explicit else '[]'}),
  track(660.,4.,[])])
''' + transition + f'''
clock.assign_new(sync="none",[radio])
output.file(%wav,fallible=true,{json.dumps(str(wav))},radio)
thread.run(delay=2.,fun()->shutdown())
'''
        self.run_script(name, code)
        with wave.open(str(wav)) as f:
            pcm = f.readframes(f.getnframes())
        self.assertGreater(len(pcm), 100000)
        self.assertTrue(any(pcm))
        return hashlib.sha256(pcm).hexdigest()

    def test_documented_add_matches_reference_for_delayed_entry(self):
        self.assertEqual(self.render("doc", reference=False, live=False, explicit=False),
                         self.render("reference", reference=True, live=False, explicit=False))

    def test_documented_add_preserves_explicit_incoming_delay(self):
        self.assertEqual(self.render("doc", reference=False, live=False, explicit=True),
                         self.render("reference", reference=True, live=False, explicit=True))

    def test_documented_live_branch_remains_sequential(self):
        self.assertEqual(self.render("doc", reference=False, live=True, explicit=False),
                         self.render("reference", reference=True, live=True, explicit=False))
