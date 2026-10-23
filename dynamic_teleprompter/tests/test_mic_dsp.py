import unittest
from pathlib import Path
import numpy as np
from mic_dsp import DEFAULT_BANDS, NoiseSuppressor, make_eq, process_builtin
from audio_effects import normalize_chain, process_chain, plugin_errors


class MicrophoneDSPTests(unittest.TestCase):
    def test_eq_peak_gain_matches_frequency_response_and_preserves_stream_state(self):
        bands = [dict(kind='Peak', frequency=1000, gain=6, q=2)]
        samples = (.03 * np.sin(2*np.pi*1000*np.arange(48000)/48000)).astype(np.float32)
        whole = make_eq(bands)(samples, 48000)
        board = make_eq(bands)
        split = np.concatenate([board(samples[i:i+512],48000,reset=False) for i in range(0,len(samples),512)])
        np.testing.assert_allclose(whole,split,atol=1e-6)
        gain = 20*np.log10(np.linalg.norm(whole[5000:])/np.linalg.norm(samples[5000:]))
        self.assertAlmostEqual(gain,6,delta=.1)

    def test_gate_preserves_waveform_in_open_sections(self):
        samples=(.2*np.sin(2*np.pi*200*np.arange(48000)/48000)).astype(np.float32)
        state={}
        output=np.concatenate([process_builtin(samples[i:i+512],dict(type='gate'),state,-45) for i in range(0,len(samples),512)])
        # The former sample-wise gate chopped every sine-wave zero crossing.
        np.testing.assert_allclose(output[10000:],samples[10000:],atol=1e-4)

    @unittest.skipUnless((Path(__file__).parents[1]/'native/rnnoise.dll').exists(),'RNNoise native build required')
    def test_suppression_chunk_boundaries_and_noise_reduction(self):
        samples=np.random.default_rng(10).normal(0,.01,48000).astype(np.float32)
        def run(size):
            processor=NoiseSuppressor()
            return np.concatenate([processor.process(samples[i:i+size],1) for i in range(0,len(samples),size)])
        whole=run(480); split=run(512)
        self.assertEqual(len(split),len(samples))
        np.testing.assert_allclose(whole,split,atol=1e-6)
        self.assertLess(np.mean(split[20000:]**2),np.mean(samples[20000:]**2)*.9)

    def test_effect_parameters_survive_project_chain_roundtrip(self):
        chain=normalize_chain([dict(type='eq',enabled=True,bands=[dict(kind='Peak',frequency=1234,gain=-3,q=2)]),
            dict(type='suppress',enabled=True,strength=72),dict(type='gate',enabled=True,attack=3,release=250)])
        self.assertEqual(normalize_chain(chain),chain)
        self.assertEqual(chain[0]['bands'][0]['frequency'],1234)

    def test_processing_keeps_pcm_length_with_all_builtin_effects(self):
        samples=np.random.default_rng(1).integers(-1000,1000,512,dtype=np.int16).tobytes()
        for _ in range(10):
            result,_=process_chain(samples,[dict(type='eq',enabled=True),dict(type='gate',enabled=True)])
            self.assertEqual(len(result),len(samples))
        self.assertNotIn('eq',plugin_errors()); self.assertNotIn('gate',plugin_errors())


if __name__=='__main__':
    unittest.main()
