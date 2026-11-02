#!/usr/bin/env python3
"""Testes das funções puras do auditoria.py (sem tocar no sistema).

    python3 -I ~/.claude/skills/latencia-audio/scripts/test_auditoria.py
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("auditoria", os.path.join(HERE, "auditoria.py"))
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

PROC_042 = """firmware:        1.0.4.0
owner:           ffc0000100000000
clock source:    internal
rate:            48000
measured rate:   48000
locked:          yes
extended status: 00000040
enabled:         1
substreams:      1
period/buffer:   256/512
iso irq/queue:   12/96 frames
tx stream 0:     16 pcm + 1 midi, iso channel 0
tx stream 1:     2 pcm + 0 midi, iso channel 1
rx stream 0:     8 pcm + 1 midi, iso channel 2
starts:          13
start retries:   0
bus resets:      0
stalls:          1
max gap:         0 us (0 over 1 ms)
rt thread:       flush max 0 us, wake late max 0 us
packet errors:   tx 0, rx 0
last error:      0
"""

PROC_043_EXTRA = """period range:    32-2048 (x2 above 48k), buffer max 4096
output latency:  168 frames (unit 72 + queue 96)
session:         STALE (restarts at the next prepare)
"""

STATUS_RUN = """state: RUNNING
owner_pid   : 906438
trigger_time: 39791.647556608
tstamp      : 40192.263450660
delay       : 424
avail       : 192
avail_max   : 272
-----
hw_ptr      : 19229120
appl_ptr    : 19229440
"""

HW = """access: MMAP_INTERLEAVED
format: S32_LE
subformat: STD
channels: 8
rate: 48000 (48000/1)
period_size: 256
buffer_size: 512
"""

PW_TOP = """S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
C   53      0      0    ---     ---   ---   ---     0                  alsa_output.firewire-0x000a92040310caa0.pro-output-0
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
S   30      0      0    ---     ---   ---   ---     0                  Dummy-Driver
R   53    256  48000  24.4us   4.6us  0.00  0.00    3    S32LE 8 48000 alsa_output.firewire-0x000a92040310caa0.pro-output-0
R   39    900  48000   8.7us   5.1us  0.00  0.00    0    F32LE 2 48000  + Firefox
S   54      0      0    ---     ---   ---   ---     0                  alsa_input.firewire-0x000a92040310caa0.pro-input-0
"""


class Formulas(unittest.TestCase):
    def test_us_to_frames(self):
        self.assertEqual(A.us_to_frames(2000, 48000), 96)
        self.assertEqual(A.us_to_frames(1500, 48000), 72)
        self.assertEqual(A.us_to_frames(2000, 44100), 89)
        self.assertEqual(A.us_to_frames(2000, 96000), 192)

    def test_queue_048k(self):
        self.assertEqual(A.expected_queue(48000, 32, 64), (8, 64))
        self.assertEqual(A.expected_queue(48000, 64, 128), (12, 96))
        for q in (128, 256, 512, 1024, 2048):
            self.assertEqual(A.expected_queue(48000, q, 2 * q), (12, 96), q)

    def test_queue_96k(self):
        # README: 96 kHz 64x2 mediu 242 = 114 + fila 128
        self.assertEqual(A.expected_queue(96000, 64, 128), (16, 128))
        self.assertEqual(A.expected_queue(96000, 256, 512)[1], 192)

    def test_queue_sem_cap_era_0_4_1(self):
        # investigação de 2026-10-21: período 1024/4096 do modo timer -> 256/2048
        p = {"max_queue_us": 0}
        self.assertEqual(A.expected_queue(48000, 1024, 4096, p), (256, 2048))

    def test_queue_mod6_ajuste(self):
        # fila precisa de >= 2 intervalos de IRQ em ciclos: 256 (43) < 2 x 128 (22) -> +1 pacote
        p = {"max_queue_us": 0, "irq_per_queue": 2}
        self.assertEqual(A.expected_queue(48000, 128, 256, p), (128, 262))

    def test_queue_us_piso(self):
        self.assertEqual(A.expected_queue(48000, 256, 512, {"queue_us": 1000}), (9, 72))
        self.assertEqual(A.expected_queue(48000, 256, 512, {"queue_us": 1000}, min_queue_us=0), (6, 48))

    def test_midi_periodo_zero(self):
        self.assertEqual(A.expected_queue(48000, 0, 0), (0, 0))

    def test_tabela_plano(self):
        got = {r["q"]: r["round_trip"] for r in A.latency_table(48000)}
        self.assertEqual([got[q] for q in (32, 64, 128, 256, 512)], [200, 296, 424, 680, 1192])
        r96 = {r["q"]: r for r in A.latency_table(96000)}
        self.assertEqual(r96[64]["round_trip"], 370)
        self.assertAlmostEqual(r96[64]["ms"], 3.85, places=2)

    def test_tamanhos_validos(self):
        self.assertEqual(A.valid_buffer_sizes(48000), [32, 64, 128, 256, 512, 1024, 2048])
        self.assertEqual(A.valid_buffer_sizes(96000), [64, 128, 256, 512, 1024, 2048])
        self.assertNotIn(4096, A.valid_buffer_sizes(44100))

    def test_placa(self):
        self.assertEqual(A.card_latency(48000), (72, True))
        self.assertEqual(A.card_latency(44100), (72, False))
        self.assertEqual(A.card_latency(96000), (114, True))
        self.assertEqual(A.expected_internal(48000, 96), 168)

    def test_orcamento_estimado(self):
        b = A.budget(48000, 256, 96, 72, 0, informed=648)
        self.assertEqual(b["soma"], 680)
        self.assertEqual(b["base"], "ESTIMADO")
        self.assertAlmostEqual(sum(r["pct"] for r in b["rows"]), 100.0, delta=0.3)
        self.assertEqual(b["erro_alinhamento"], 32)

    def test_orcamento_medido_residuo(self):
        b = A.budget(48000, 32, 64, 72, 0, measured=250, informed=200)
        self.assertEqual(b["residuo"], 50)
        self.assertTrue(b["residuo_aviso"])
        self.assertEqual(b["base"], "MEDIDO")
        self.assertEqual(b["rows"][0]["pct"], round(100 * 64 / 250, 1))
        b = A.budget(48000, 32, 64, 72, 0, measured=204)
        self.assertFalse(b["residuo_aviso"])


class Parsers(unittest.TestCase):
    def test_tube_042(self):
        d = A.parse_tube_proc(PROC_042)
        self.assertEqual((d["period"], d["buffer"], d["iso_irq"], d["iso_queue"]), (256, 512, 12, 96))
        self.assertEqual((d["starts"], d["stalls"], d["bus_resets"], d["pkt_tx"], d["pkt_rx"]), (13, 1, 0, 0, 0))
        self.assertTrue(d["locked"])
        self.assertEqual(len(d["streams"]), 3)
        self.assertIsNone(d["period_range"])
        self.assertFalse(d["session_stale"])

    def test_tube_043(self):
        d = A.parse_tube_proc(PROC_042 + PROC_043_EXTRA)
        self.assertEqual(d["period_range"], (32, 2048))
        self.assertEqual(d["buffer_max"], 4096)
        self.assertEqual(d["output_latency"], {"total": 168, "unit": 72, "queue": 96})
        self.assertTrue(d["session_stale"])

    def test_sem_resposta(self):
        self.assertFalse(A.parse_tube_proc("unit not responding\n")["responding"])

    def test_status(self):
        s = A.parse_pcm_status(STATUS_RUN)
        self.assertEqual(s["state"], "RUNNING")
        self.assertEqual(s["queued_beyond_buffer"], 424 - 320)
        self.assertEqual(A.parse_pcm_status("closed\n"), {"state": "CLOSED"})

    def test_hw(self):
        h = A.parse_hw_params(HW)
        self.assertEqual((h["period_size"], h["buffer_size"], h["rate"], h["channels"]), (256, 512, 48000, 8))
        self.assertIsNone(A.parse_hw_params("closed"))

    def test_pw_top(self):
        rows = A.parse_pw_top(PW_TOP)
        self.assertEqual(len(rows), 4)          # só a última iteração
        out = [r for r in rows if r["id"] == 53][0]
        self.assertEqual((out["quant"], out["err"], out["format"]), (256, 3, "S32LE 8 48000"))
        ff = [r for r in rows if r["id"] == 39][0]
        self.assertTrue(ff["follower"])
        self.assertEqual(ff["name"], "Firefox")
        self.assertEqual(ff["driver"], A.OUT_NODE)

    def test_metadata(self):
        t = "update: id:0 key:'clock.quantum' value:'256' type:''\nupdate: id:0 key:'clock.force-quantum' value:'0' type:''"
        self.assertEqual(A.parse_pw_metadata(t), {"clock.quantum": "256", "clock.force-quantum": "0"})

    def test_interrupts(self):
        t = "           CPU0       CPU1\n  92:          0   16106251  IR-IO-APIC    7-fasteoi   firewire_ohci\n"
        self.assertEqual(A.parse_interrupts(t), {"irq": 92, "per_cpu": [0, 16106251], "total": 16106251})

    def test_ps(self):
        t = "    PID     TID CLS RTPRIO PSR COMMAND\n    374     374  FF     50   2 irq/92-firewire_ohci\n 1 1 TS - 3 systemd\n"
        th = A.parse_ps_threads(t)
        self.assertEqual(th[0]["rtprio"], 50)
        self.assertEqual(th[0]["comm"], "irq/92-firewire_ohci")
        self.assertEqual(th[1]["rtprio"], 0)

    def test_misc(self):
        self.assertEqual(A.parse_cpu_list("0-2,14"), {0, 1, 2, 14})
        psi = A.parse_psi("some avg10=0.14 avg60=0.13 avg300=0.06 total=1\nfull avg10=0.00 avg60=0 avg300=0 total=0")
        self.assertEqual(psi["some"]["avg10"], 0.14)
        self.assertEqual(A.parse_preempt("x CPU: 13 ... PREEMPT(lazy) \n", ""), ("lazy", "journal (rastro)"))
        self.assertEqual(A.parse_preempt("Dynamic Preempt: voluntary", "quiet preempt=full"), ("full", "cmdline"))
        c = A.parse_clock_conf("default.clock.rate = 48000\ndefault.clock.quantum = 256\ndefault.clock.min-quantum = 256\n")
        self.assertEqual((c["rate"], c["quantum"], c["max-quantum"]), (48000, 256, None))
        d = A.parse_latencia_direto("cliques: 38 de 38  xruns: 0\nlatência do hardware+driver (ida e volta): mediana 136 amostras"
                                    " = 2.833 ms (mín 135, máx 137)\n")
        self.assertEqual((d["median"], d["found"], d["xruns"], d["min"]), (136, 38, 0, 135))

    def test_pcm_alive(self):
        r = {"state": "RUNNING", "hw_ptr": 0}
        self.assertFalse(A.pcm_alive([r, r, r]))
        self.assertTrue(A.pcm_alive([r, {"state": "RUNNING", "hw_ptr": 256}]))
        self.assertIsNone(A.pcm_alive([{"state": "CLOSED"}]))


class Avaliacao(unittest.TestCase):
    def estado_parado(self):
        frozen = {"state": "RUNNING", "hw_ptr": 0, "appl_ptr": 0, "delay": 0}
        snap = {"pcm0c": {"status": frozen, "hw_params": A.parse_hw_params(HW), "sw_params": None}}
        return {"tube_present": True, "tube_proc_text": PROC_042, "cards": "PreSonus FIRESTUDIO_TUBE ... S400",
                "params": {}, "pcm_quick": [snap, snap, snap], "pcm_end": snap,
                "samples": [{"t": 0, "tube": A.parse_tube_proc(PROC_042), "pcm": {"pcm0c": frozen}},
                            {"t": 5, "tube": A.parse_tube_proc(PROC_042), "pcm": {"pcm0c": frozen}}],
                "metadata": {"clock.quantum": "256", "clock.force-quantum": "0", "clock.rate": "48000"},
                "nodes": {"firestudio_tube.out": {"props": {"node.group": "loopback-1-29", "node.passive": True}},
                          A.OUT_NODE: {"props": {"api.alsa.disable-tsched": True, "api.alsa.period-num": 2,
                                                 "api.alsa.headroom": 0}}},
                "jack_info": {"ok": False}, "pipewire_pid": 1}

    def test_pcm_parado_e_falha(self):
        checks, d = A.evaluate(self.estado_parado())
        by = {c["id"]: c for c in checks}
        self.assertEqual(by["A2"]["status"], A.FALHA)
        self.assertIn("PCM parado", by["A2"]["medido"])
        self.assertEqual(by["P6"]["status"], A.OK)       # node.group do loopback não conta
        self.assertEqual(by["D3"]["status"], A.OK)
        self.assertEqual(d["t_expected"], 680)
        ids = [c["id"] for c in checks]
        self.assertEqual(len(ids), len(set(ids)))
        mel = A.improvements(checks, d)
        self.assertTrue(any("recover" in m["item"] for m in mel))

    def test_force_quantum_orfao(self):
        st = self.estado_parado()
        st["metadata"]["clock.force-quantum"] = "32"
        by = {c["id"]: c for c in A.evaluate(st)[0]}
        self.assertEqual(by["P2"]["status"], A.FALHA)    # grafo 32, placa 256
        self.assertEqual(by["A1"]["status"], A.FALHA)

    def test_pipewire_parado(self):
        st = self.estado_parado()
        st["pipewire_pid"] = None
        by = {c["id"]: c for c in A.evaluate(st)[0]}
        self.assertEqual(by["P0"]["status"], A.FALHA)
        self.assertEqual(by["P6"]["status"], A.NA)

    def test_compare(self):
        prev = {"quando": "x", "checks": [{"id": "A2", "status": "FALHA"}], "resumo": {"q": 256}}
        cur = {"checks": [{"id": "A2", "status": "OK", "medido": "vivo"}], "resumo": {"q": 32}}
        c = A.compare(cur, prev)
        self.assertEqual(c["mudancas"][0]["agora"], "OK")
        self.assertEqual(c["numeros"]["q"], {"antes": 256, "agora": 32})


if __name__ == "__main__":
    unittest.main(verbosity=1)
