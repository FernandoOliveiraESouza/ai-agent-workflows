#!/usr/bin/env python3
"""Auditoria de latência da cadeia FireStudio Tube -> driver -> ALSA -> PipeWire -> JACK -> MixDaw.

Rodar sempre com `python3 -I` (arquivo único, só stdlib). Por padrão SÓ LÊ o sistema
(/proc, /sys, pw-dump, pw-top -b, pw-metadata sem chave, journalctl, ps, cliente JACK passivo).

    python3 -I auditoria.py --out DIR [--dur 30] [--history [DIR]] [--compare auditoria.json]
    python3 -I auditoria.py --out DIR --measure jack|direto --confirmo-cabo-out3-line9
    python3 -I auditoria.py --out DIR --sizes 32,64,128,256,512 --altera-sistema [--modo-troca painel|reabrir]
    python3 -I auditoria.py --out DIR --recover

Saídas em DIR: relatorio.md, auditoria.json (schema 1), contadores.tsv, bruto/.
Códigos: 0 ok · 1 FALHA · 2 uso · 3 ambiente (sem placa/driver/PipeWire) · 4 sem clique na medição
· 5 não restaurou o estado (rode --recover).

Regras (2026-10-21): nunca Out 5/6; não grava microfone nem guarda áudio; sem sudo; só altera o
sistema com --altera-sistema/--recover/--measure (e restaura em finally).
"""

import argparse
import datetime as _dt
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

SCHEMA = 1
HOME = os.path.expanduser("~")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(HOME, ".cache", "latencia-audio")
PROC_TUBE = "/proc/asound/Tube"
SYS_PARAMS = "/sys/module/snd_firestudio_tube/parameters"
CARD_ID = "firewire-0x000a92"
OUT_NODE = "alsa_output.firewire-0x000a92040310caa0.pro-output-0"
IN_NODE = "alsa_input.firewire-0x000a92040310caa0.pro-input-0"
DRIVER_REPO = os.path.expanduser(os.environ.get("LATENCIA_DRIVER_REPO", "~/snd-firestudio-tube"))
PANEL_REPO = os.path.expanduser(os.environ.get("LATENCIA_PANEL_REPO", "~/firestudio-tube"))
MIXDAW_REPO = os.path.expanduser(os.environ.get("LATENCIA_DAW_REPO", "~/MixDaw"))
WP_DIR = os.path.join(HOME, ".config", "wireplumber", "main.lua.d")
PW_CONF_DIR = os.path.join(HOME, ".config", "pipewire", "pipewire.conf.d")
CLOCK_CONF = os.path.join(PW_CONF_DIR, "firestudio-tube-clock.conf")
LATENCY_RULE = os.path.join(WP_DIR, "54-firestudio-tube-latencia-atual.lua")
HISTORY_DEFAULT = os.path.expanduser(os.environ.get("LATENCIA_HISTORY", "~/.local/share/latencia-audio/auditorias"))
JACK_OUT = "FireStudio Tube Pro:playback_AUX2"   # Out 3 (nunca AUX4/AUX5 = Out 5/6)
JACK_IN = "FireStudio Tube Pro:capture_AUX8"     # Line 9

OK, AVISO, FALHA, INFO, NA = "OK", "AVISO", "FALHA", "INFO", "NA"
LAYERS = [("H", "Hardware / FireWire"), ("K", "Kernel / sistema"), ("D", "Driver snd-firestudio-tube"),
          ("A", "ALSA"), ("P", "PipeWire"), ("W", "WirePlumber"), ("J", "JACK (pipewire-jack)"),
          ("M", "MixDaw"), ("C", "Painel / serviços")]

# Driver defaults (tube-stream.c) used when /sys is unreadable.
DRIVER_DEFAULTS = {"queue_periods": 2, "irq_per_queue": 8, "queue_us": 0, "max_queue_us": 2000,
                   "rt_interval_us": 250, "rt_priority": 70, "gap_probe": "N"}
MIN_QUEUE_US = 1500   # tube-stream.c 0.4.3 (MIN_QUEUE_US); o 0.4.2 não tinha piso no queue_us


# ============================================================================ funções puras
def us_to_frames(us, rate):
    """tube-stream.c us_to_frames: DIV_ROUND_UP(us * (rate / 100), 10000)."""
    return -(-(us * (rate // 100)) // 10000)


def frames_to_cycles(frames, rate):
    """tube-stream.c frames_to_cycles: DIV_ROUND_UP(frames * 8000, rate)."""
    return -(-(frames * 8000) // rate)


def packet_frames(rate):
    return -(-rate // 8000)


def expected_queue(rate, period, buffer, params=None, min_queue_us=MIN_QUEUE_US):
    """Porta de set_period() (tube-stream.c, 0.4.2 :105-145 / 0.4.3): devolve (irq, fila) em
    frames, os valores de `iso irq/queue` do /proc. period 0 = sessão aberta pelo MIDI
    (fila = buffer, IRQ = 0 no set_period; a lib usa o padrão dela). min_queue_us=0 = 0.4.2."""
    p = dict(DRIVER_DEFAULTS)
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    qp = min(max(int(p["queue_periods"]), 2), 16)
    ipq = min(max(int(p["irq_per_queue"]), 2), 16)
    queue_us, max_queue_us = int(p["queue_us"]), int(p["max_queue_us"])
    irq, queue = period, buffer
    if period > 0:
        queue = period * qp
        if queue_us > 0:
            queue = max(us_to_frames(max(queue_us, min_queue_us), rate), 4 * packet_frames(rate))
        elif max_queue_us > 0:
            queue = min(queue, us_to_frames(max(max_queue_us, 1500), rate))
        queue = min(queue, max(buffer, 2 * irq))
        irq = min(irq, max(queue // ipq, packet_frames(rate)))
        while frames_to_cycles(queue, rate) < 2 * frames_to_cycles(irq, rate):
            queue += packet_frames(rate)
    return irq, queue


def rate_multiplier(rate):
    return 2 if rate > 48000 else 1


def valid_buffer_sizes(rate, period_max=None, buffer_max=4096):
    """Períodos aceitos: mínimo 32 (x2 acima de 48k, tube-pcm.c period_size_constraint) e, com
    2 períodos, buffer <= 4096 quadros em QUALQUER taxa (amdtp-stream.c:210-211:
    period_bytes_max = 2048 quadros, buffer_bytes_max = 2 x isso). Só potências de 2."""
    m = rate_multiplier(rate)
    hi = min(period_max or 2048 * m, buffer_max // 2)
    return [q for q in (16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192)
            if 32 * m <= q <= hi]


def card_latency(rate):
    """Latência própria da placa (ADC/DAC + DICE + FireWire + SYT), medida com cabo (README do
    driver): 72 a 48 kHz, 114 a 96 kHz. Outras taxas: suposição (não medida)."""
    if rate > 48000:
        return 114, rate == 96000
    return 72, rate == 48000


def expected_internal(rate, queue):
    """O que `latency.internal.rate` do pro-output-0 deve declarar: placa + fila efetiva."""
    return card_latency(rate)[0] + queue


def round_trip(q_out, queue, card, q_in, headroom=0, mixdaw=0):
    """Ida e volta (frames): buffer de saída (1 quantum) + fila + placa + captura (1 quantum +
    headroom) + MixDaw. Em modo IRQ a 48k: 2Q + 72 + min(2Q, 96)."""
    return q_out + queue + card + q_in + headroom + mixdaw


def latency_table(rate, params=None, sizes=None):
    rows = []
    for q in sizes or valid_buffer_sizes(rate):
        irq, queue = expected_queue(rate, q, 2 * q, params)
        card = card_latency(rate)[0]
        t = round_trip(q, queue, card, q)
        rows.append({"q": q, "irq": irq, "queue": queue, "internal": card + queue, "round_trip": t,
                     "ms": round(1000.0 * t / rate, 2)})
    return rows


def budget(rate, q, queue, card, headroom=0, mixdaw=0, measured=None, informed=None,
           queue_source="ESTIMADO"):
    """Orçamento da ida e volta. % sobre o total MEDIDO quando houver; senão sobre a soma
    (marcado ESTIMADO). Resíduo = medido - soma (aviso se > 10 %)."""
    comps = [("Fila isócrona do driver (saída)", queue, queue_source),
             ("Buffer ALSA de saída (1 quantum)", q, "CÓDIGO"),
             ("Placa (conversores, DICE, FireWire, SYT)", card, "MEDIDO ANTES"),
             ("Captura PipeWire (1 quantum + headroom)", q + headroom, "CÓDIGO"),
             ("MixDaw (mesmo ciclo do JACK)", mixdaw, "decisão de arquitetura do DAW")]
    soma = sum(c[1] for c in comps)
    base = measured if measured else soma
    rows = []
    for name, frames, kind in comps:
        rows.append({"componente": name, "frames": frames, "ms": round(1000.0 * frames / rate, 3),
                     "pct": round(100.0 * frames / base, 1) if base else 0.0, "tipo": kind})
    out = {"rows": rows, "soma": soma, "soma_ms": round(1000.0 * soma / rate, 3),
           "base": "MEDIDO" if measured else "ESTIMADO", "total": base,
           "total_ms": round(1000.0 * base / rate, 3)}
    if measured:
        res = measured - soma
        out["residuo"] = res
        out["residuo_pct"] = round(100.0 * res / measured, 1)
        out["residuo_aviso"] = abs(res) > 0.10 * measured
    if informed is not None:
        out["informado"] = informed
        out["erro_alinhamento"] = base - informed
        out["erro_alinhamento_ms"] = round(1000.0 * (base - informed) / rate, 3)
        out["erro_tipo"] = "MEDIDO" if measured else "ESTIMADO"
    return out


def parse_kv(text):
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _int(v, default=None):
    try:
        return int(str(v).strip().split()[0])
    except (ValueError, IndexError, AttributeError):
        return default


def parse_tube_proc(text):
    """/proc/asound/Tube/firestudio (tube-proc.c)."""
    kv = parse_kv(text)
    d = {"raw": kv, "responding": "unit not responding" not in (text or "") and bool(kv)}
    d["firmware"] = kv.get("firmware")
    d["owner"] = kv.get("owner")
    d["clock_source"] = kv.get("clock source")
    d["rate"] = _int(kv.get("rate"))
    d["measured_rate"] = _int(kv.get("measured rate"))
    d["locked"] = kv.get("locked") == "yes"
    d["enabled"] = _int(kv.get("enabled"))
    d["substreams"] = _int(kv.get("substreams"))
    m = re.match(r"(\d+)/(\d+)", kv.get("period/buffer", ""))
    d["period"], d["buffer"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    m = re.match(r"(\d+)/(\d+)", kv.get("iso irq/queue", ""))
    d["iso_irq"], d["iso_queue"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    for key in ("starts", "start retries", "bus resets", "stalls", "last error"):
        d[key.replace(" ", "_")] = _int(kv.get(key))
    m = re.match(r"(\d+) us \((\d+) over 1 ms\)", kv.get("max gap", ""))
    d["gap_max_us"], d["gaps_over_1ms"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    m = re.match(r"flush max (\d+) us, wake late max (\d+) us", kv.get("rt thread", ""))
    d["rt_flush_max_us"], d["rt_wake_max_us"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    m = re.match(r"tx (\d+), rx (\d+)", kv.get("packet errors", ""))
    d["pkt_tx"], d["pkt_rx"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    d["formation_changed"] = "CHANGED" in kv.get("formation", "")
    d["streams"] = []
    for k, v in kv.items():
        m, n = re.fullmatch(r"(tx|rx) stream (\d+)", k), re.match(r"(\d+) pcm \+ (\d+) midi", v)
        if m and n:
            d["streams"].append({"dir": m.group(1), "index": int(m.group(2)),
                                 "pcm": int(n.group(1)), "midi": int(n.group(2))})
    d["streams"].sort(key=lambda s: (s["dir"], s["index"]))
    # Linhas novas do driver 0.4.3 (opcionais).
    m = re.search(r"(\d+)\s*-\s*(\d+)", kv.get("period range", ""))
    d["period_range"] = (int(m.group(1)), int(m.group(2))) if m else None
    m = re.search(r"buffer max (\d+)", kv.get("period range", ""))
    d["buffer_max"] = int(m.group(1)) if m else None
    m = re.match(r"(\d+) frames \(unit (\d+) \+ queue (\d+)\)", kv.get("output latency", ""))
    d["output_latency"] = ({"total": int(m.group(1)), "unit": int(m.group(2)), "queue": int(m.group(3))}
                           if m else None)
    d["session_stale"] = "STALE" in kv.get("session", "")
    return d


def parse_pcm_status(text):
    t = (text or "").strip()
    if not t or t == "closed":
        return {"state": "CLOSED"}
    kv = parse_kv(t)
    d = {"state": kv.get("state", "?")}
    for k in ("owner_pid", "delay", "avail", "avail_max", "hw_ptr", "appl_ptr"):
        d[k] = _int(kv.get(k))
    if d.get("delay") is not None and d.get("hw_ptr") is not None and d.get("appl_ptr") is not None:
        d["queued_beyond_buffer"] = d["delay"] - (d["appl_ptr"] - d["hw_ptr"])
    return d


def parse_hw_params(text):
    t = (text or "").strip()
    if not t or t == "closed":
        return None
    kv = parse_kv(t)
    m = re.match(r"(\d+)", kv.get("rate", ""))
    return {"access": kv.get("access"), "format": kv.get("format"), "channels": _int(kv.get("channels")),
            "rate": int(m.group(1)) if m else None, "period_size": _int(kv.get("period_size")),
            "buffer_size": _int(kv.get("buffer_size"))}


def parse_sw_params(text):
    t = (text or "").strip()
    if not t or t == "closed":
        return None
    kv = parse_kv(t)
    return {k: _int(kv.get(k)) for k in ("avail_min", "start_threshold", "stop_threshold",
                                         "silence_threshold", "silence_size", "period_step")}


PW_TOP_ROW = re.compile(r"^(\S)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(.*)$")


def parse_pw_top(text):
    """`LC_ALL=C pw-top -b -n N`: linhas da última iteração."""
    blocks, cur = [], None
    for line in (text or "").splitlines():
        if re.match(r"^S\s+ID\s+QUANT", line):
            cur = []
            blocks.append(cur)
            continue
        if cur is None:
            continue
        m = PW_TOP_ROW.match(line.rstrip())
        if not m:
            continue
        rest = m.group(10).strip()
        fm = re.match(r"^(\S+ \d+ \d+)\s+(.*)$", rest)
        fmt, name = (fm.group(1), fm.group(2)) if fm else ("", rest)
        follower = name.startswith("+ ")
        cur.append({"status": m.group(1), "id": int(m.group(2)), "quant": int(m.group(3)),
                    "rate": int(m.group(4)), "wait": m.group(5), "busy": m.group(6),
                    "wq": m.group(7), "bq": m.group(8), "err": int(m.group(9)), "format": fmt,
                    "name": name[2:].strip() if follower else name.strip(), "follower": follower})
    rows = blocks[-1] if blocks else []
    driver = None
    for r in rows:
        if not r["follower"]:
            driver = r["name"]
        r["driver"] = driver if r["follower"] else r["name"]
    return rows


def parse_pw_metadata(text):
    return dict(re.findall(r"key:'([^']+)' value:'([^']*)'", text or ""))


def parse_interrupts(text, match="firewire_ohci"):
    for line in (text or "").splitlines():
        if match in line:
            parts = line.split()
            irq = parts[0].rstrip(":")
            counts = []
            for p in parts[1:]:
                if p.isdigit():
                    counts.append(int(p))
                else:
                    break
            return {"irq": int(irq) if irq.isdigit() else irq, "per_cpu": counts, "total": sum(counts)}
    return None


def parse_ps_threads(text):
    """`ps -eLo pid,tid,cls,rtprio,psr,comm`"""
    out = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, tid, cls, prio, psr, comm = parts
        out.append({"pid": _int(pid), "tid": _int(tid), "cls": cls, "rtprio": _int(prio, 0) if prio != "-" else 0,
                    "psr": _int(psr), "comm": comm.strip()})
    return out


def parse_cpu_list(s):
    cpus = set()
    for part in (s or "").strip().split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                cpus.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            cpus.add(int(part))
    return cpus


def parse_psi(text):
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"(some|full) avg10=([\d.]+) avg60=([\d.]+) avg300=([\d.]+)", line)
        if m:
            out[m.group(1)] = {"avg10": float(m.group(2)), "avg60": float(m.group(3)), "avg300": float(m.group(4))}
    return out


def parse_preempt(journal_text, cmdline=""):
    m = re.search(r"\bpreempt=(\w+)", cmdline or "")
    if m:
        return m.group(1), "cmdline"
    m = re.search(r"Dynamic Preempt: (\w+)", journal_text or "")
    if m:
        return m.group(1), "journal"
    m = re.search(r"PREEMPT\((\w+)\)", journal_text or "")
    if m:
        return m.group(1), "journal (rastro)"
    return None, None


def parse_clock_conf(text):
    d = {}
    for k in ("rate", "quantum", "min-quantum", "max-quantum"):
        m = re.search(r"default\.clock\.%s\s*=\s*(\d+)" % re.escape(k), text or "")
        d[k] = int(m.group(1)) if m else None
    return d


def parse_latencia_direto(text):
    """Saída de tools/latencia (driver): mediana em amostras."""
    m = re.search(r"mediana (\d+) amostras", text or "")
    d = {"median": int(m.group(1)) if m else None}
    m = re.search(r"cliques: (\d+) de (\d+)\s+xruns: (\d+)", text or "")
    if m:
        d.update(found=int(m.group(1)), pulses=int(m.group(2)), xruns=int(m.group(3)))
    m = re.search(r"\(mín (\d+), máx (\d+)\)", text or "")
    if m:
        d.update(min=int(m.group(1)), max=int(m.group(2)))
    return d


def pcm_alive(readings):
    """readings: lista de parse_pcm_status. True vivo, False parado (RUNNING com hw_ptr igual),
    None fechado/indeterminado."""
    run = [r for r in readings if r.get("state") == "RUNNING" and r.get("hw_ptr") is not None]
    if len(run) < 2:
        return None
    return len({r["hw_ptr"] for r in run}) > 1


def delta(a, b):
    if a is None or b is None:
        return None
    return b - a


def fmt_ms(frames, rate):
    if frames is None or not rate:
        return "?"
    return "%.2f ms" % (1000.0 * frames / rate)


# ============================================================================ coleta (só leitura)
class Raw:
    """Guarda a saída bruta de cada comando/arquivo em bruto/."""

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.n = 0

    def save(self, name, text):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80]
        with open(os.path.join(self.folder, safe + ".txt"), "w", encoding="utf-8") as f:
            f.write(text if isinstance(text, str) else json.dumps(text, indent=1, ensure_ascii=False))


def run(cmd, timeout=10, env=None, raw=None, name=None):
    try:
        e = dict(os.environ)
        if env:
            e.update(env)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=e)
        out = p.stdout + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
        rc = p.returncode
    except FileNotFoundError:
        out, rc = "[comando não encontrado: %s]" % cmd[0], 127
    except subprocess.TimeoutExpired as ex:
        out = (ex.stdout or b"").decode("utf-8", "replace") if isinstance(ex.stdout, bytes) else (ex.stdout or "")
        out += "\n[timeout %ss]" % timeout
        rc = 124
    if raw and name:
        raw.save(name, "$ %s\n[rc=%s]\n%s" % (" ".join(cmd), rc, out))
    return rc, out


def stdout_only(cmd, timeout=10, env=None):
    try:
        e = dict(os.environ)
        if env:
            e.update(env)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=e).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def pcm_dirs():
    return sorted(glob.glob(os.path.join(PROC_TUBE, "pcm*")))


def snapshot_pcm():
    out = {}
    for d in pcm_dirs():
        name = os.path.basename(d)
        sub = os.path.join(d, "sub0")
        out[name] = {"status": parse_pcm_status(read(os.path.join(sub, "status"))),
                     "hw_params": parse_hw_params(read(os.path.join(sub, "hw_params"))),
                     "sw_params": parse_sw_params(read(os.path.join(sub, "sw_params")))}
    return out


def sample_counters(t0):
    tube = parse_tube_proc(read(os.path.join(PROC_TUBE, "firestudio")))
    irq = parse_interrupts(read("/proc/interrupts"))
    pcm = {os.path.basename(d): parse_pcm_status(read(os.path.join(d, "sub0", "status"))) for d in pcm_dirs()}
    return {"t": round(time.monotonic() - t0, 2), "tube": tube, "irq": irq, "pcm": pcm}


def tube_nodes_from_dump(dump):
    nodes, device, clients, mixdaw_clients = {}, None, {}, []
    for o in dump or []:
        info = o.get("info") or {}
        props = info.get("props") or {}
        if o.get("type") == "PipeWire:Interface:Node" and CARD_ID in props.get("node.name", ""):
            params = info.get("params") or {}
            nodes[props["node.name"]] = {"id": o.get("id"), "state": info.get("state"), "props": props,
                                         "Latency": params.get("Latency"),
                                         "ProcessLatency": params.get("ProcessLatency")}
        elif o.get("type") == "PipeWire:Interface:Node" and props.get("node.name", "").startswith("firestudio_tube"):
            nodes[props["node.name"]] = {"id": o.get("id"), "state": info.get("state"), "props": props}
        elif o.get("type") == "PipeWire:Interface:Device" and CARD_ID in props.get("device.name", ""):
            prof = (info.get("params") or {}).get("Profile") or [{}]
            device = {"id": o.get("id"), "name": props.get("device.name"),
                      "profile": (prof[0] or {}).get("name"), "props_profile": props.get("device.profile")}
        elif o.get("type") == "PipeWire:Interface:Client":
            app = props.get("application.name", "")
            clients[o.get("id")] = app
            if re.search(r"mixdaw", app + props.get("application.process.binary", ""), re.I):
                mixdaw_clients.append({"id": o.get("id"), "app": app,
                                       "pid": props.get("application.process.id")})
    return nodes, device, mixdaw_clients


def jack_rt_binary(raw=None):
    src = os.path.join(SKILL_DIR, "scripts", "jack_rt.c")
    out = os.path.join(CACHE, "jack_rt")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out, None
    os.makedirs(CACHE, exist_ok=True)
    inc, lib = os.path.join(HOME, ".local", "include"), os.path.join(HOME, ".local", "lib", "pipewire-jack")
    cmd = ["gcc", "-O2", "-Wall", "-I", inc, src, "-o", out, "-L", lib, "-Wl,-rpath," + lib, "-ljack", "-lm"]
    rc, txt = run(cmd, timeout=60, raw=raw, name="gcc_jack_rt")
    return (out, None) if rc == 0 else (None, txt.strip()[-400:])


def jack_rt(args, timeout=20, raw=None, name=None):
    binary, err = jack_rt_binary(raw)
    if not binary:
        return {"ok": False, "error": "não compilou: " + (err or "?")}
    rc, txt = run([binary] + args, timeout=timeout, raw=raw, name=name)
    for line in reversed(txt.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
                d["rc"] = rc
                return d
            except ValueError:
                break
    try:
        d = json.loads(txt[:txt.rfind("}") + 1]) if "{" in txt else {}
        if isinstance(d, dict) and d:
            d["rc"] = rc
            return d
    except ValueError:
        pass
    return {"ok": False, "rc": rc, "error": txt.strip()[-300:]}


def find_processes(pattern):
    out = []
    for comm_path in glob.glob("/proc/[0-9]*/comm"):
        pid = int(comm_path.split("/")[2])
        comm = read(comm_path).strip()
        if re.search(pattern, comm, re.I):
            out.append({"pid": pid, "comm": comm})
    return out


def symlinks_info():
    out = {}
    for path in sorted(glob.glob(os.path.join(WP_DIR, "5*-firestudio*.lua"))):
        real = os.path.realpath(path)
        out[os.path.basename(path)] = {"link": os.path.islink(path), "target": real,
                                       "exists": os.path.exists(real),
                                       "in_repo": real.startswith(os.path.join(PANEL_REPO, "config"))}
    return out


def collect(args, raw):
    """Coleta tudo que for possível SEM alterar nada. Devolve o dicionário de estado."""
    st = {"when": _dt.datetime.now().isoformat(timespec="seconds"), "host": socket.gethostname()}
    t0 = time.monotonic()
    epoch0 = time.time()

    # --- driver e ALSA, leituras iniciais rápidas (PCM vivo em 3 leituras)
    st["tube_present"] = os.path.isdir(PROC_TUBE)
    st["tube_proc_text"] = read(os.path.join(PROC_TUBE, "firestudio"))
    raw.save("proc_tube_firestudio", st["tube_proc_text"])
    st["cards"] = read("/proc/asound/cards")
    raw.save("proc_asound_cards", st["cards"])
    st["params"] = {}
    for p in glob.glob(os.path.join(SYS_PARAMS, "*")):
        v = read(p).strip()
        st["params"][os.path.basename(p)] = int(v) if v.isdigit() else v
    raw.save("sys_params", st["params"])
    quick = []
    for _ in range(3):
        quick.append(snapshot_pcm())
        time.sleep(0.25)
    st["pcm_quick"] = quick
    for name, d in quick[-1].items():
        sub = os.path.join(PROC_TUBE, name, "sub0")
        raw.save("pcm_%s" % name, "\n".join("== %s\n%s" % (f, read(os.path.join(sub, f)))
                                           for f in ("hw_params", "sw_params", "status")))

    # --- PipeWire (início da janela)
    rc, txt = run(["pw-metadata", "-n", "settings", "0"], timeout=5, raw=raw, name="pw_metadata_settings")
    st["metadata"] = parse_pw_metadata(txt)
    rc, txt = run(["pw-top", "-b", "-n", "2"], timeout=8, env={"LC_ALL": "C"}, raw=raw, name="pw_top_inicio")
    st["pw_top_start"] = parse_pw_top(txt)
    st["pw_available"] = rc != 127

    # --- janela de --dur: contadores
    samples = [sample_counters(t0)]
    watch = None
    watch_proc = None
    if args.dur > 0 and not args.sem_watch:
        binary, err = jack_rt_binary(raw)
        if binary:
            try:
                watch_proc = subprocess.Popen([binary, "watch", str(args.dur)], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True)
            except OSError:
                watch_proc = None
    step = max(0.5, args.dur / 30.0) if args.dur > 0 else 0
    end = t0 + args.dur
    while args.dur > 0 and time.monotonic() < end:
        time.sleep(min(step, max(0.05, end - time.monotonic())))
        samples.append(sample_counters(t0))
    st["samples"] = samples
    if watch_proc:
        try:
            out, errt = watch_proc.communicate(timeout=args.dur + 10)
            raw.save("jack_rt_watch", out + errt)
            line = [l for l in out.splitlines() if l.strip().startswith("{")]
            watch = json.loads(line[-1]) if line else {"ok": False, "error": errt.strip()[-200:]}
        except (subprocess.TimeoutExpired, ValueError) as e:
            watch_proc.kill()
            watch = {"ok": False, "error": str(e)}
    st["jack_watch"] = watch

    rc, txt = run(["pw-top", "-b", "-n", "2"], timeout=8, env={"LC_ALL": "C"}, raw=raw, name="pw_top_fim")
    st["pw_top_end"] = parse_pw_top(txt)
    st["tube_end_text"] = read(os.path.join(PROC_TUBE, "firestudio"))
    st["pcm_end"] = snapshot_pcm()

    rc, txt = run(["pw-dump"], timeout=10, raw=None)
    try:
        dump = json.loads(txt) if rc == 0 else []
    except ValueError:
        dump = []
    raw.save("pw_dump_tube", [o for o in dump if CARD_ID in json.dumps((o.get("info") or {}).get("props") or {})
                              or "firestudio_tube" in json.dumps((o.get("info") or {}).get("props") or {})])
    st["nodes"], st["device"], st["mixdaw_pw_clients"] = tube_nodes_from_dump(dump)
    st["pw_version"] = stdout_only(["pipewire", "--version"]).strip().splitlines()[-1:] or [""]
    st["wp_version"] = stdout_only(["wireplumber", "--version"]).strip().splitlines()[-1:] or [""]

    # --- configs
    st["clock_conf_text"] = read(CLOCK_CONF)
    st["clock_conf"] = parse_clock_conf(st["clock_conf_text"])
    raw.save("clock_conf", st["clock_conf_text"] or "(ausente)")
    st["pw_conf_files"] = sorted(os.listdir(PW_CONF_DIR)) if os.path.isdir(PW_CONF_DIR) else []
    st["jack_conf_files"] = sorted(glob.glob(os.path.join(HOME, ".config", "pipewire", "jack.conf.d", "*")))
    st["wp_links"] = symlinks_info()
    st["latency_rule_text"] = read(LATENCY_RULE)
    m = re.search(r'latency\.internal\.rate"?\]?\s*=\s*(\d+)', st["latency_rule_text"])
    st["latency_rule_value"] = int(m.group(1)) if m else None

    # --- JACK
    st["jack_info"] = jack_rt(["info"], timeout=10, raw=raw, name="jack_rt_info")

    # --- kernel / sistema
    st["uname"] = stdout_only(["uname", "-rv"]).strip()
    st["cmdline"] = read("/proc/cmdline").strip()
    cfg = read("/boot/config-%s" % os.uname().release)
    st["kconfig"] = {k: v for k, v in re.findall(r"^(CONFIG_(?:PREEMPT\w*|HZ|HZ_\d+|NO_HZ\w*|HIGH_RES_TIMERS|"
                                                 r"IRQ_FORCED_THREADING))=(\S+)", cfg, re.M)}
    rc, kj = run(["journalctl", "-k", "-b", "--no-pager", "-o", "short-iso"], timeout=15)
    st["preempt"] = parse_preempt(kj, st["cmdline"])
    pat = re.compile(r"firewire|firestudio|tube|ohci|oom-kill|Out of memory|Dynamic Preempt|xrun", re.I)
    kl = [l for l in kj.splitlines() if pat.search(l)]
    raw.save("journal_kernel_filtrado", "\n".join(kl[-400:]))
    st["journal_k"] = {"stalled": sum("stalled" in l for l in kl),
                       "stalled_last": next((l for l in reversed(kl) if "stalled" in l), None),
                       "oom": sum("Out of memory" in l for l in kl),
                       "oom_last": next((l for l in reversed(kl) if "Out of memory" in l), None),
                       "fw_errors": [l for l in kl if re.search(r"firewire.*(error|fail|timeout|reset)", l, re.I)][-10:],
                       "in_window": [l for l in kl if "stalled" in l or "bus reset" in l.lower()][-5:]}
    since = "@%d" % int(epoch0)
    rc, kw = run(["journalctl", "-k", "--since", since, "--no-pager", "-o", "short-iso"], timeout=10)
    st["journal_k"]["window"] = [l for l in kw.splitlines() if pat.search(l)][-20:]
    rc, uj = run(["journalctl", "--user", "-b", "--no-pager", "-o", "short-iso", "-u", "pipewire",
                  "-u", "wireplumber", "-u", "pipewire-pulse", "-n", "400"], timeout=15)
    raw.save("journal_user_pipewire", uj)
    ul = uj.splitlines()
    last = max([i for i, l in enumerate(ul) if "Started pipewire.service" in l] or [0])
    st["journal_pw"] = {"restarts": [l for l in ul if "Started pipewire.service" in l][-3:],
                        "problems": [l for l in ul[last:]
                                     if re.search(r"xrun|timeout|error|fail|underrun|overrun", l, re.I)
                                     and not re.search(r"libcamera|jackdbus|portal|camera", l)][-15:]}
    st["governors"] = sorted(set(read(p).strip() for p in
                                 glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")))
    states = []
    for sd in sorted(glob.glob("/sys/devices/system/cpu/cpu0/cpuidle/state*")):
        states.append({"name": read(os.path.join(sd, "name")).strip(),
                       "latency_us": _int(read(os.path.join(sd, "latency"))),
                       "disabled": read(os.path.join(sd, "disable")).strip() == "1"})
    st["cstates"] = states
    st["cpuidle_driver"] = read("/sys/devices/system/cpu/cpuidle/current_driver").strip()
    st["pm_qos"] = sorted(set(read(p).strip() for p in
                              glob.glob("/sys/devices/system/cpu/cpu*/power/pm_qos_resume_latency_us")))
    st["clocksource"] = read("/sys/devices/system/clocksource/clocksource0/current_clocksource").strip()
    st["sched_rt"] = {"runtime_us": _int(read("/proc/sys/kernel/sched_rt_runtime_us")),
                      "period_us": _int(read("/proc/sys/kernel/sched_rt_period_us"))}
    st["perf_paranoid"] = _int(read("/proc/sys/kernel/perf_event_paranoid"))
    try:
        import resource
        rt = resource.getrlimit(resource.RLIMIT_RTPRIO)[0]
        ml = resource.getrlimit(resource.RLIMIT_MEMLOCK)[0]
        st["limits"] = {"rtprio": -1 if rt == resource.RLIM_INFINITY else rt,
                        "memlock": -1 if ml == resource.RLIM_INFINITY else ml}
    except (ImportError, ValueError, OSError):
        st["limits"] = {}
    try:
        import grp
        st["groups"] = sorted({grp.getgrgid(g).gr_name for g in os.getgroups()})
    except (ImportError, KeyError):
        st["groups"] = []
    st["psi"] = {k: parse_psi(read("/proc/pressure/%s" % k)) for k in ("cpu", "memory", "io")}
    mem = parse_kv(read("/proc/meminfo"))
    st["swap"] = {"total_kb": _int(mem.get("SwapTotal")), "free_kb": _int(mem.get("SwapFree")),
                  "avail_kb": _int(mem.get("MemAvailable")), "total_mem_kb": _int(mem.get("MemTotal"))}
    rc, pst = run(["ps", "-eLo", "pid,tid,cls,rtprio,psr,comm"], timeout=10)
    st["threads"] = parse_ps_threads(pst)
    raw.save("ps_threads_rt", "\n".join("%(pid)s %(tid)s %(cls)s %(rtprio)s cpu%(psr)s %(comm)s" % t
                                        for t in st["threads"] if t["cls"] in ("FF", "RR")))
    irq = parse_interrupts(read("/proc/interrupts"))
    st["irq"] = irq
    if irq:
        st["irq_affinity"] = read("/proc/irq/%s/smp_affinity_list" % irq["irq"]).strip()
        st["irq_effective"] = read("/proc/irq/%s/effective_affinity_list" % irq["irq"]).strip()
    st["rtkit"] = bool(find_processes(r"^rtkit-daemon$"))
    st["irqbalance"] = bool(find_processes(r"^irqbalance$"))
    st["thp"] = {"enabled": read("/sys/kernel/mm/transparent_hugepage/enabled").strip(),
                 "defrag": read("/sys/kernel/mm/transparent_hugepage/defrag").strip(),
                 "swappiness": _int(read("/proc/sys/vm/swappiness"))}
    st["nvidia"] = os.path.isdir("/sys/module/nvidia")
    pw = find_processes(r"^pipewire$")
    st["pipewire_pid"] = pw[0]["pid"] if pw else None
    if st["pipewire_pid"]:
        for t in st["threads"]:
            if t["pid"] == st["pipewire_pid"] and t["comm"] == "pw-data-loop":
                t["allowed"] = read("/proc/%d/task/%d/status" % (t["pid"], t["tid"]))
                m = re.search(r"Cpus_allowed_list:\s*(\S+)", t["allowed"])
                st["dataloop"] = {"tid": t["tid"], "cls": t["cls"], "rtprio": t["rtprio"], "psr": t["psr"],
                                  "allowed": m.group(1) if m else ""}
                break

    # --- driver: versão, código
    st["driver_loaded_srcversion"] = read("/sys/module/snd_firestudio_tube/srcversion").strip()
    ko = os.path.join(DRIVER_REPO, "snd-firestudio-tube.ko")
    st["driver_repo_srcversion"] = stdout_only(["modinfo", "-F", "srcversion", ko]).strip() if os.path.exists(ko) else ""
    st["driver_installed_version"] = stdout_only(["modinfo", "-F", "version", "snd_firestudio_tube"]).strip()
    m = re.search(r'PACKAGE_VERSION="?([\w.]+)', read(os.path.join(DRIVER_REPO, "dkms.conf")))
    st["driver_repo_version"] = m.group(1) if m else None
    src = read(os.path.join(DRIVER_REPO, "tube-stream.c"))
    st["driver_code"] = {"session_stale": "session_stale" in src, "has_source": bool(src)}

    # --- MixDaw
    procs = [p for p in find_processes(r"^mixdaw") if p["comm"].lower().startswith("mixdaw")]
    st["mixdaw"] = {"procs": procs}
    if procs:
        pid = procs[0]["pid"]
        th = [t for t in st["threads"] if t["pid"] == pid]
        st["mixdaw"]["threads_rt"] = [t for t in th if t["cls"] in ("FF", "RR")]
        st["mixdaw"]["threads_total"] = len(th)
    settings = read(os.path.join(HOME, ".config", "MixDaw", "Settings.xml"))
    st["mixdaw"]["settings"] = dict(re.findall(r'<VALUE name="(mixdaw(?:Record|Monitor)\w*)" val="([^"]*)"', settings))
    jf = os.path.join(MIXDAW_REPO, "external", "tracktion_engine", "modules", "juce", "modules",
                      "juce_audio_devices", "native", "juce_JackAudio.cpp")
    st["mixdaw"]["code"] = {
        "juce_buffer_callback": "jack_set_buffer_size_callback" in read(jf),
        "juce_patch": bool(glob.glob(os.path.join(MIXDAW_REPO, "patches", "juce", "*buffer*"))),
        "latency_watcher": "latencyGeneration" in read(os.path.join(MIXDAW_REPO, "src", "audio", "AudioDevices.cpp")),
        "has_source": os.path.isdir(MIXDAW_REPO)}

    # --- painel
    layout = read(os.path.join(PANEL_REPO, "firestudio_control", "layout.py"))
    m = re.search(r"^BUFFER_SIZES\s*=\s*\[([^\]]*)\]", layout, re.M)
    st["panel"] = {"buffer_sizes": [int(x) for x in re.findall(r"\d+", m.group(1))] if m else None,
                   "apply_buffer": "def apply_buffer(" in layout,
                   "reopen": "def reopen_tube_nodes(" in layout,
                   "install_53": "53-firestudio-tube-latencia" in read(os.path.join(PANEL_REPO, "install.sh")),
                   "running": bool(find_processes(r"firestudio-con|firestudio_con"))}
    for unit in ("firestudio-tube-ctl", "firestudio-tube-unmute", "pipewire", "wireplumber", "pipewire-pulse"):
        st["panel"]["svc_" + unit] = stdout_only(["systemctl", "--user", "is-active", unit]).strip()
    st["elapsed_s"] = round(time.monotonic() - t0, 1)
    return st


# ============================================================================ avaliação
def mk(cid, titulo, status, medido, esperado, fora="", fix=None, ganho="", risco="", esforco=""):
    c = {"id": cid, "camada": cid[0], "titulo": titulo, "status": status, "medido": medido,
         "esperado": esperado, "fora": fora}
    if fix:
        c["melhoria"] = {"acao": fix, "ganho": ganho, "risco": risco, "esforco": esforco}
    return c


def derive(st):
    """Números centrais da cadeia, a partir do estado coletado."""
    tube = parse_tube_proc(st.get("tube_proc_text", ""))
    tube_end = parse_tube_proc(st.get("tube_end_text", "") or st.get("tube_proc_text", ""))
    md = st.get("metadata") or {}
    rate = tube.get("rate") or _int(md.get("clock.rate")) or 48000
    force_q = _int(md.get("clock.force-quantum"), 0) or 0
    q_meta = force_q or _int(md.get("clock.quantum"))
    out = (st.get("nodes") or {}).get(OUT_NODE, {})
    inn = (st.get("nodes") or {}).get(IN_NODE, {})
    props = out.get("props") or {}
    tsched_off = bool(props.get("api.alsa.disable-tsched"))
    headroom = _int(props.get("api.alsa.headroom"), None)
    if headroom is None:
        headroom = 0 if tsched_off else 32
    top = st.get("pw_top_end") or st.get("pw_top_start") or []
    top_q = None
    for r in top:
        if r["name"] in (OUT_NODE, IN_NODE) and r["status"] == "R" and r["quant"]:
            top_q = r["quant"]
            break
    pcm_last = (st.get("pcm_end") or (st.get("pcm_quick") or [{}])[-1]) or {}
    hwp = {k: (v.get("hw_params") or None) for k, v in pcm_last.items()}
    play_hw = hwp.get("pcm0p")
    jack = st.get("jack_info") or {}
    q = q_meta or top_q or (jack.get("buffer") if jack.get("ok") else None) or (play_hw or {}).get("period_size") or 256
    period = (play_hw or {}).get("period_size") or tube.get("period") or q
    buffer = (play_hw or {}).get("buffer_size") or tube.get("buffer") or 2 * period
    irq_e, queue_e = expected_queue(rate, tube.get("period") if tube.get("period") is not None else period,
                                    tube.get("buffer") if tube.get("buffer") is not None else buffer,
                                    st.get("params"))
    card, card_measured = card_latency(rate)
    # fila medida: delay - (appl - hw) do playback rodando
    measured_queue = None
    play_reads = [((snap.get("pcm0p") or {}).get("status") or {}) for snap in (st.get("pcm_quick") or []) + [pcm_last]]
    if pcm_alive(play_reads):          # PCM parado: delay/hw_ptr congelados não medem fila nenhuma
        for s in play_reads:
            if s.get("state") == "RUNNING" and s.get("queued_beyond_buffer") is not None:
                measured_queue = s["queued_beyond_buffer"]
    queue_now = tube.get("iso_queue") if tube.get("iso_queue") is not None else queue_e
    # latência informada
    informed_in = informed_out = None
    if jack.get("ok"):
        for p in jack.get("ports", []):
            if p["name"].startswith("FireStudio Tube Pro:") and p.get("physical"):
                if p["dir"] == "capture":
                    informed_in = max(informed_in or 0, p["latency"][1])
                else:
                    informed_out = max(informed_out or 0, p["latency"][1])
    informed = (informed_in + informed_out) if None not in (informed_in, informed_out) else None
    pl = out.get("ProcessLatency") or [{}]
    declared = _int((pl[0] or {}).get("rate")) if pl else None
    if declared is None:
        declared = _int(props.get("latency.internal.rate"))
    t_expected = round_trip(q, queue_now, card, q, headroom)
    return {"rate": rate, "q": q, "q_meta": q_meta, "force_q": force_q, "top_q": top_q, "period": period,
            "buffer": buffer, "tsched_off": tsched_off, "headroom": headroom, "tube": tube, "tube_end": tube_end,
            "irq_expected": irq_e, "queue_expected": queue_e, "queue_now": queue_now,
            "queue_measured": measured_queue, "card": card, "card_measured": card_measured,
            "internal_expected": card + queue_now, "internal_declared": declared,
            "informed_in": informed_in, "informed_out": informed_out, "informed": informed,
            "t_expected": t_expected, "hw_params": hwp}


PW_DEPENDENT = {"P2", "P3", "P4", "P5", "P6", "P7", "P8", "P10", "P11", "P12", "W2", "W3", "J1", "J2", "J3", "J4",
                "M1", "K3", "K4"}


def evaluate(st, measurement=None):
    d = derive(st)
    rate, q = d["rate"], d["q"]
    tube, tube_end = d["tube"], d["tube_end"]
    C = []
    add = C.append
    samples = st.get("samples") or []
    s0, s1 = (samples[0], samples[-1]) if samples else ({}, {})
    t0, t1 = s0.get("tube") or tube, s1.get("tube") or tube_end
    dur = (s1.get("t") or 0) - (s0.get("t") or 0)

    # ---------------- H
    present = st.get("tube_present") and tube.get("responding")
    add(mk("H1", "Placa presente no driver snd-firestudio-tube", OK if present else FALHA,
           "/proc/asound/Tube ok" if present else "ausente/sem resposta", "Tube com /proc/asound/Tube/firestudio",
           "placa desligada, cabo FireWire, ou snd_dice no lugar do driver próprio",
           fix="ligar a placa / `sudo scripts/usar-driver-novo.sh`", risco="baixo", esforco="baixo"))
    s400 = "S400" in st.get("cards", "")
    add(mk("H2", "Link S400 e firmware", OK if s400 else AVISO,
           "S400" if s400 else "sem S400 no nome da placa", "S400 (firmware %s < 1.0.11 exige S400)" % tube.get("firmware"),
           "velocidade menor que S400 = pacotes maiores no barramento, risco de travar"))
    lock_ok = tube.get("locked") and tube.get("measured_rate") == tube.get("rate")
    add(mk("H3", "Relógio travado e taxa medida = nominal", OK if lock_ok else FALHA,
           "locked=%s, medida %s, nominal %s, fonte %s" % (tube.get("locked"), tube.get("measured_rate"),
                                                            tube.get("rate"), tube.get("clock_source")),
           "locked yes, medida = nominal", "sem lock = estalos/deriva; conferir fonte de clock no painel"))
    br = delta(t0.get("bus_resets"), t1.get("bus_resets"))
    add(mk("H4", "Bus resets na janela", OK if not br else FALHA, "%s em %.0f s (total %s)" % (br, dur, t1.get("bus_resets")),
           "0", "reset no barramento reinicia o isócrono (estalo/silêncio); cabo, hub ou outro aparelho FireWire"))
    pe = (delta(t0.get("pkt_tx"), t1.get("pkt_tx")) or 0) + (delta(t0.get("pkt_rx"), t1.get("pkt_rx")) or 0)
    add(mk("H5", "Erros de pacote isócrono (tx/rx) na janela", OK if pe == 0 else FALHA,
           "+%s (total tx %s rx %s, last error %s)" % (pe, t1.get("pkt_tx"), t1.get("pkt_rx"), t1.get("last_error")),
           "0", "pacote malformado/descontínuo: a lib cancela o stream (CIP/SYT/DBC); cabo, clock ou fila curta"))
    i0, i1 = s0.get("irq"), s1.get("irq")
    irq_rate = round(delta(i0["total"], i1["total"]) / dur, 1) if i0 and i1 and dur > 0 else None
    iso = tube.get("iso_irq")
    exp_irq_hz = round(rate / iso, 0) if iso else None
    if irq_rate is None:
        stt, med = NA, "sem janela (--dur 0) ou sem IRQ firewire_ohci"
    elif any(v.get("state") == "RUNNING" for sn in samples for v in (sn.get("pcm") or {}).values()) and irq_rate < 1:
        stt, med = FALHA, "%.1f IRQ/s com PCM RUNNING" % irq_rate
    elif exp_irq_hz and any(v.get("state") == "RUNNING" for sn in samples for v in (sn.get("pcm") or {}).values()) \
            and irq_rate < 0.25 * exp_irq_hz:
        stt, med = AVISO, "%.0f IRQ/s (bem abaixo do esperado: isócrono parado ou IRQs agrupados; calibrar com a placa sã)" % irq_rate
    else:
        stt, med = (AVISO if exp_irq_hz and irq_rate > 4 * exp_irq_hz else OK), "%.0f IRQ/s" % irq_rate
    add(mk("H6", "Taxa de IRQ do OHCI (isócrono andando)", stt, med,
           "~%s/s (taxa / iso irq %s)" % (exp_irq_hz, iso) if exp_irq_hz else "> 0 com stream rodando",
           "0 com PCM RUNNING = isócrono parado; muito acima = tempestade de IRQ"))
    add(mk("H7", "Afinidade do IRQ OHCI", INFO, "IRQ %s: permitido %s, efetivo CPU %s" % (
        (st.get("irq") or {}).get("irq"), st.get("irq_affinity"), st.get("irq_effective")),
        "CPU dedicado, diferente do pw-data-loop", "ver K4"))
    form = ", ".join("%s%d %d+%d" % (s["dir"], s["index"], s["pcm"], s["midi"]) for s in tube.get("streams", []))
    add(mk("H8", "Formação dos streams", FALHA if tube.get("formation_changed") else OK, form or "?",
           "tx0 16+1, tx1 2+0 (Loop), rx0 8+1; sem 'formation CHANGED'",
           "a placa mudou de formação: recarregar o driver"))
    add(mk("H9", "Atraso SYT da lib (transfer delay)", INFO, "0x2e00 ticks = 25 frames a 48k (amdtp-stream.c:288-292)",
           "dentro dos 72 da placa", "mexer é na lib do kernel (afeta todos os drivers FireWire): não recomendado"))
    add(mk("H10", "Latência própria da placa nesta taxa", OK if d["card_measured"] else AVISO,
           "%d frames (%s)" % (d["card"], "medida" if d["card_measured"] else "NÃO medida nesta taxa, suposta"),
           "72 @48k, 114 @96k (cabo Out 3 -> Line 9)",
           "sem medida nesta taxa a compensação pode errar alguns frames",
           fix="medir com `--measure jack` nesta taxa (44,1k nunca foi medida)", ganho="exatidão do alinhamento",
           risco="baixo", esforco="baixo"))

    # ---------------- K
    mode, src = st.get("preempt") or (None, None)
    full = mode == "full" or st.get("kconfig", {}).get("CONFIG_PREEMPT_RT") == "y"
    add(mk("K1", "Modelo de preempção", OK if full else AVISO, "%s (%s); PREEMPT_DYNAMIC=%s" % (
        mode or "?", src or "não achado no journal", st.get("kconfig", {}).get("CONFIG_PREEMPT_DYNAMIC")),
        "full (preempt=full) ou PREEMPT_RT",
        "voluntary/lazy: código de kernel não preemptível segura a CPU (ex.: thread RT acordou 2,6 ms atrasada com NVENC)",
        fix="kernel cmdline `preempt=full` (GRUB, sudo, reiniciar)", ganho="menos picos de atraso da thread RT (margem p/ 32-64)",
        risco="baixo (throughput um pouco menor)", esforco="baixo"))
    ohci = [t for t in st.get("threads", []) if re.match(r"irq/\d+-firewire", t["comm"])]
    ti = "threadirqs" in st.get("cmdline", "")
    add(mk("K2", "IRQs em threads", OK if ti else (INFO if ohci else AVISO),
           "threadirqs %s; thread do OHCI: %s" % ("sim" if ti else "não", ohci[0]["comm"] if ohci else "não"),
           "threadirqs na cmdline (prioridade de IRQ ajustável)",
           "sem threadirqs só alguns IRQs viram thread; prioridade relativa fica fora de controle",
           fix="cmdline `threadirqs` (+ rtirq ou udev para prioridades)", risco="baixo", esforco="baixo"))
    others = [t for t in st.get("threads", []) if re.match(r"irq/\d+-", t["comm"]) and not re.match(r"irq/\d+-firewire", t["comm"])]
    trt = [t for t in st.get("threads", []) if t["comm"] == "tube-rt"]
    dl = st.get("dataloop") or {}
    op = ohci[0]["rtprio"] if ohci else None
    max_other = max([t["rtprio"] for t in others] or [0])
    good = op is not None and op >= 70 and op > max_other
    add(mk("K3", "Ordem de prioridades RT", OK if good else AVISO,
           "IRQ OHCI %s, outros IRQs máx %s, tube-rt %s, pw-data-loop %s" % (
               op, max_other, trt[0]["rtprio"] if trt else "ausente", dl.get("rtprio")),
           "pw-data-loop (88) > IRQ OHCI (>= 70, acima de nvidia/xhci) >= tube-rt (70) > MixDaw/jack",
           "IRQ do OHCI empatado com o da NVIDIA (50) pode esperar atrás dele",
           fix="subir `irq/N-firewire_ohci` para FIFO 75-80 (chrt via udev/rtirq, sudo)", ganho="menos stalls sob carga gráfica",
           risco="baixo", esforco="baixo"))
    eff = parse_cpu_list(st.get("irq_effective", ""))
    same = dl.get("psr") in eff if dl else False
    allowed_all = dl and len(parse_cpu_list(dl.get("allowed", ""))) > 1
    add(mk("K4", "IRQ OHCI e pw-data-loop no mesmo CPU", AVISO if same else (INFO if allowed_all else OK),
           "IRQ efetivo %s; pw-data-loop agora no CPU %s (permitidos %s)" % (st.get("irq_effective"), dl.get("psr"), dl.get("allowed")),
           "CPUs diferentes e fixos", "no mesmo CPU o IRQ preempta ou espera o grafo; o pw-data-loop migra livremente",
           fix="fixar o IRQ OHCI num CPU (smp_affinity) e o pw-data-loop fora dele", risco="baixo", esforco="médio"))
    gov = st.get("governors") or []
    add(mk("K5", "Governador de CPU", OK if gov == ["performance"] else AVISO, ", ".join(gov) or "?", "performance em todos",
           "powersave/schedutil sobem a frequência tarde: callbacks mais lentos em buffers pequenos",
           fix="`powerprofilesctl set performance` ou cpupower", risco="baixo", esforco="baixo"))
    enabled = [s for s in st.get("cstates", []) if not s["disabled"]]
    worst = max([s["latency_us"] or 0 for s in enabled] or [0])
    add(mk("K6", "C-states / PM QoS", OK if worst <= 100 else AVISO,
           "estado mais fundo habilitado %s µs (%s); pm_qos %s" % (worst, ", ".join("%s:%s" % (s["name"], s["latency_us"]) for s in enabled),
                                                                 ",".join(st.get("pm_qos") or [])),
           "saída <= 100 µs, ou PM QoS travando", "sair de C-state fundo atrasa o IRQ/thread RT",
           fix="PM QoS (/dev/cpu_dma_latency segurado por um processo) se aparecerem atrasos", risco="baixo", esforco="baixo"))
    kc = st.get("kconfig", {})
    add(mk("K7", "Timer: HZ, hrtimers, clocksource", OK if kc.get("CONFIG_HIGH_RES_TIMERS") == "y" and st.get("clocksource") == "tsc" else AVISO,
           "HZ=%s, HIGH_RES=%s, NO_HZ_FULL=%s, clocksource %s" % (kc.get("CONFIG_HZ"), kc.get("CONFIG_HIGH_RES_TIMERS"),
                                                               kc.get("CONFIG_NO_HZ_FULL"), st.get("clocksource")),
           "HIGH_RES_TIMERS=y, clocksource tsc", "hpet/acpi_pm = leitura de tempo lenta; sem hrtimer a thread de 250 µs vira 1 ms"))
    lim = st.get("limits") or {}
    lim_ok = (lim.get("rtprio") in (-1,) or (lim.get("rtprio") or 0) >= 88) and lim.get("memlock") == -1
    add(mk("K8", "Limites do usuário (rtprio, memlock, grupo audio)", OK if lim_ok else AVISO,
           "rtprio %s, memlock %s, grupos %s" % (lim.get("rtprio"), lim.get("memlock"),
                                                "audio" if "audio" in st.get("groups", []) else "sem audio"),
           "rtprio >= 88 (ou rtkit), memlock ilimitado", "sem isso JACK/MixDaw rodam sem tempo real",
           fix="`sudo scripts/configurar-tempo-real.sh` (driver) e novo login", risco="baixo", esforco="baixo"))
    add(mk("K9", "rtkit-daemon", OK if st.get("rtkit") else AVISO, "rodando" if st.get("rtkit") else "ausente",
           "rodando", "o PipeWire pede RT ao rtkit"))
    srt = st.get("sched_rt") or {}
    add(mk("K10", "sched_rt_runtime_us", INFO if srt.get("runtime_us") in (950000, -1) else AVISO,
           "%s / %s" % (srt.get("runtime_us"), srt.get("period_us")), "950000/1000000 (padrão) ou -1",
           "valor menor estrangula todas as threads RT"))
    psi = st.get("psi") or {}
    worst_psi = max([(psi.get(k, {}).get("some", {}) or {}).get("avg10", 0) for k in ("cpu", "memory", "io")] or [0])
    memfull = ((psi.get("memory") or {}).get("full") or {}).get("avg10", 0)
    add(mk("K11", "Pressão (PSI) cpu/mem/io avg10", OK if worst_psi < 5 and memfull < 1 else AVISO,
           "cpu %s, mem %s (full %s), io %s" % tuple([(psi.get(k, {}).get("some", {}) or {}).get("avg10") for k in ("cpu",)] +
                                                   [(psi.get("memory", {}).get("some", {}) or {}).get("avg10"), memfull,
                                                    (psi.get("io", {}).get("some", {}) or {}).get("avg10")]),
           "< 5 % some, mem full ~0", "tarefas esperando CPU/memória/disco: callbacks atrasam"))
    sw = st.get("swap") or {}
    used_pct = round(100.0 * (sw["total_kb"] - sw["free_kb"]) / sw["total_kb"], 0) if sw.get("total_kb") else 0
    jk = st.get("journal_k") or {}
    add(mk("K12", "OOM e swap desde o boot", AVISO if jk.get("oom") or used_pct > 50 else OK,
           "%s OOM-kill(s)%s; swap %d%% usado" % (jk.get("oom"), (" (último: %s)" % jk["oom_last"][:19]) if jk.get("oom_last") else "",
                                                 used_pct),
           "0 OOM; swap pouco usado", "OOM/swap = páginas do áudio saem da RAM (memlock ajuda só a quem trava)",
           fix="limitar builds paralelos (-j 3) e abas; zram/swap maior", ganho="evita travadas sob compilação",
           risco="baixo", esforco="baixo"))
    pp = st.get("perf_paranoid")
    add(mk("K13", "perf_event_paranoid", INFO, str(pp), "<= 2 para usar perf sem sudo",
           "4 = perf bloqueado (só diagnóstico; não afeta o áudio)"))
    win = jk.get("window") or []
    add(mk("K14", "Kernel: firewire/tube na janela", FALHA if any("stalled" in l for l in win) else (AVISO if win else OK),
           ("; ".join(l[20:110] for l in win[-3:]) or "nada") + ("; no boot: %s stall(s), último %s" % (
               jk.get("stalled"), (jk.get("stalled_last") or "")[:19]) if jk.get("stalled") else ""),
           "nada na janela", "'stalled, reporting XRUN' = isócrono parou (fila vazia)"))

    add(mk("K15", "irqbalance", AVISO if st.get("irqbalance") else OK, "rodando" if st.get("irqbalance") else "parado",
           "parado, ou com o IRQ do OHCI banido (IRQBALANCE_BANNED_CPULIST/--banirq)",
           "irqbalance move o IRQ do OHCI entre CPUs (pode cair no CPU do pw-data-loop)",
           fix="banir o IRQ do OHCI no irqbalance e fixar a afinidade (sudo)", risco="baixo", esforco="baixo"))
    thp = st.get("thp") or {}
    sel = lambda v: (re.search(r"\[(\w+)\]", v or "") or [None, v])[1]
    add(mk("K16", "Transparent Huge Pages / swappiness / NVIDIA", AVISO if sel(thp.get("defrag")) == "always" else INFO,
           "THP %s, defrag %s, swappiness %s, nvidia %s" % (sel(thp.get("enabled")), sel(thp.get("defrag")),
                                                           thp.get("swappiness"), "carregado" if st.get("nvidia") else "não"),
           "defrag madvise/never", "defrag=always compacta memória de forma síncrona (picos de ms); NVIDIA já segurou CPU 2,6 ms"))

    # ---------------- D
    prm = st.get("params") or {}
    add(mk("D1", "Versão e parâmetros do driver", INFO,
           "repo %s, instalado (DKMS) %s, carregado = .ko do repo: %s; %s" % (
               st.get("driver_repo_version"), st.get("driver_installed_version") or "não",
               "sim" if st.get("driver_loaded_srcversion") and st.get("driver_loaded_srcversion") == st.get("driver_repo_srcversion") else "não",
               ", ".join("%s=%s" % (k, prm[k]) for k in sorted(prm))),
           "max_queue_us 2000, queue_us 0, rt_interval_us 250, rt_priority 70", ""))
    pp_ = (d["hw_params"].get("pcm0p") or {}).get("period_size")
    match = tube.get("period") is not None and (pp_ is None or pp_ == tube.get("period"))
    add(mk("D2", "period/buffer do driver = hw_params", OK if match else AVISO,
           "/proc %s/%s, hw_params play %s" % (tube.get("period"), tube.get("buffer"), pp_), "iguais",
           "o driver guarda um período só para os dois sentidos (pcm_period_frames)"))
    iso_ok = (tube.get("iso_irq"), tube.get("iso_queue")) == (d["irq_expected"], d["queue_expected"])
    midi_bug = tube.get("period") == 0 and tube.get("substreams")
    add(mk("D3", "Fila/IRQ do /proc = set_period recalculado", OK if iso_ok else FALHA,
           "iso irq/queue %s/%s" % (tube.get("iso_irq"), tube.get("iso_queue")),
           "%s/%s (set_period com período %s, buffer %s)" % (d["irq_expected"], d["queue_expected"], tube.get("period"), tube.get("buffer")),
           "bug 2/2b: sessão aberta antes (ex.: MIDI com período 0 = IRQ 10 ms, fila 30 ms) e o período novo não foi aplicado" if midi_bug or not iso_ok else "",
           fix="driver 0.4.3 (reserve aplica o período novo quando nada roda)", ganho="até dezenas de ms", risco="médio", esforco="médio"))
    mq = d["queue_measured"]
    tol = max(8, 2 * packet_frames(rate))
    if mq is None:
        stt, med = NA, "playback fechado ou parado"
    else:
        # a fila é reabastecida a cada IRQ: ocupação entre fila e fila + 1 IRQ (+ pacotes em voo)
        lo_q = (tube.get("iso_queue") or 0) - packet_frames(rate)
        hi_q = (tube.get("iso_queue") or 0) + (tube.get("iso_irq") or 0) + tol
        stt = OK if lo_q <= mq <= hi_q else AVISO
        med = "%d frames (delay - (appl - hw))" % mq
    add(mk("D4", "Fila medida no status ALSA", stt, med,
           "fila %s .. fila + 1 IRQ + %d (%s..%s)" % (tube.get("iso_queue"), tol, (tube.get("iso_queue") or 0) - packet_frames(rate),
                                                       (tube.get("iso_queue") or 0) + (tube.get("iso_irq") or 0) + tol),
           "fila real maior que a calculada = atraso escondido (o PipeWire não vê)"))
    qn = tube.get("iso_queue") or 0
    floor = us_to_frames(1300, rate)
    add(mk("D5", "Margem da fila contra travamento", FALHA if qn and qn < floor else (AVISO if qn and qn < us_to_frames(1500, rate) else OK),
           "%d frames = %s" % (qn, fmt_ms(qn, rate)), ">= 1,5 ms (filas < ~1,3 ms travam)",
           "fila curta = thread RT atrasada > fila - 1 IRQ esvazia o isócrono (stall)"))
    sd = delta(t0.get("stalls"), t1.get("stalls"))
    sr = delta(t0.get("start_retries"), t1.get("start_retries"))
    add(mk("D6", "Contadores A/B na janela (stalls, retries, starts)",
           FALHA if sd else (AVISO if sr else OK),
           "stalls +%s (total %s), start retries +%s, starts +%s (total %s)" % (
               sd, t1.get("stalls"), sr, delta(t0.get("starts"), t1.get("starts")), t1.get("starts")),
           "stalls +0, retries +0", "stall = isócrono parou e o watchdog avisou XRUN",
           fix="ver K3/K1/D5; se for sob carga, buffer maior ou queue_period_pct", risco="—", esforco="—"))
    rt_ok = trt and trt[0]["cls"] == "FF" and trt[0]["rtprio"] == prm.get("rt_priority", 70) and prm.get("rt_interval_us", 250) > 0
    gap = tube.get("gap_max_us")
    add(mk("D7", "Thread tube-rt e gap_probe", OK if rt_ok else AVISO,
           "tube-rt %s; rt_interval %s µs; gap_probe %s (max gap %s µs, >1 ms %s, wake late %s µs)" % (
               ("%s %s CPU %s" % (trt[0]["cls"], trt[0]["rtprio"], trt[0]["psr"])) if trt else "ausente",
               prm.get("rt_interval_us"), prm.get("gap_probe"), gap, tube.get("gaps_over_1ms"), tube.get("rt_wake_max_us")),
           "FIFO 70, 250 µs; com gap_probe=Y: max gap < fila - 1 IRQ",
           "sem a thread o processamento fica no workqueue sem RT (ms de atraso sob carga)",
           fix="deixar gap_probe=Y por um dia (`echo Y | sudo tee .../gap_probe`) e olhar max gap", ganho="dado real de margem",
           risco="baixo", esforco="baixo"))
    m = rate_multiplier(rate)
    p = tube.get("period") or 0
    lim_ok = p == 0 or (32 * m <= p <= 2048 and (tube.get("buffer") or 0) <= 4096)
    add(mk("D8", "Limites de período e caso ≡ 2 (mod 6)", OK if lim_ok else FALHA,
           "período %s, buffer %s%s" % (p, tube.get("buffer"), " (≡ 2 mod 6: coberto pelo ajuste da fila)" if p and p % 6 == 2 else ""),
           "32 (64 >48k)..2048, buffer <= 4096 em qualquer taxa", "fora disso o driver recusa ou o start não termina"))
    ol = tube.get("output_latency")
    if ol:
        add(mk("D9", "/proc 'output latency' (0.4.3)", OK if ol["total"] == d["card"] + qn else AVISO,
               "%(total)s (unit %(unit)s + queue %(queue)s)" % ol, "%d + %d" % (d["card"], qn), ""))
    else:
        add(mk("D9", "/proc 'period range'/'output latency' (0.4.3)", INFO, "linhas ausentes (driver < 0.4.3)", "presentes no 0.4.3", ""))
    has_fix = st.get("driver_code", {}).get("session_stale")
    loaded_is_repo = bool(st.get("driver_loaded_srcversion")) and \
        st.get("driver_loaded_srcversion") == st.get("driver_repo_srcversion")
    proc_has = tube.get("period_range") is not None
    fixed_loaded = has_fix and (loaded_is_repo or proc_has)
    add(mk("D10", "Reinício após stall (bug 3) carregado", OK if fixed_loaded else AVISO,
           "session_stale %s no código; módulo carregado %s o compilado no repo%s" % (
               "presente" if has_fix else "ausente", "=" if loaded_is_repo else "≠",
               "; sessão STALE agora" if tube.get("session_stale") else ""),
           "presente (driver 0.4.3) e carregado",
           "sem isso, após um stall o prepare seguinte não reinicia a sessão: PCM RUNNING com hw_ptr parado",
           fix="driver 0.4.3: `sudo scripts/instalar.sh && sudo scripts/recarregar.sh`", ganho="recupera sozinho de stalls",
           risco="médio", esforco="baixo"))

    # ---------------- A
    pcm_all = st.get("pcm_quick") or []
    names = sorted(set(k for snap in pcm_all for k in snap) | set((st.get("pcm_end") or {}).keys()))
    hw = d["hw_params"]
    open_ = {k: v for k, v in hw.items() if v}
    per = {k: (v["period_size"], v["buffer_size"]) for k, v in open_.items()}
    exp_period = q if d["tsched_off"] else None
    if not open_:
        stt = INFO
    elif exp_period and any(v[0] != exp_period for v in per.values()):
        stt = FALHA
    elif len(set(per.values())) > 1:
        stt = AVISO
    else:
        stt = OK
    add(mk("A1", "hw_params dos PCMs abertos", stt,
           ", ".join("%s %s/%s %s %sch %s" % (k, v["period_size"], v["buffer_size"], v["format"], v["channels"], v["rate"])
                     for k, v in sorted(open_.items())) or "todos fechados",
           ("período = quantum %d, buffer %d nos dois sentidos" % (q, 2 * q)) if d["tsched_off"] else "período do perfil (modo timer)",
           "período ≠ quantum em modo IRQ = o grafo roda num tamanho e a placa em outro (troca sem reabrir) -> trava",
           fix="trocar buffer reabrindo a placa (painel novo / --recover)", ganho="sem travar na troca", risco="baixo", esforco="—"))
    alive = {}
    for n in names:
        rd = [snap.get(n, {}).get("status", {}) for snap in pcm_all] + [(st.get("pcm_end") or {}).get(n, {}).get("status", {})]
        rd += [(sn.get("pcm") or {}).get(n, {}) for sn in samples]
        alive[n] = pcm_alive(rd)
    dead = [n for n, v in alive.items() if v is False]
    xrun = [n for n in names if ((st.get("pcm_end") or {}).get(n, {}).get("status") or {}).get("state") == "XRUN"]
    add(mk("A2", "PCM vivo (hw_ptr andando em RUNNING)", FALHA if dead or xrun else (OK if any(alive.values()) else INFO),
           ("PCM parado (RUNNING, hw_ptr congelado): %s" % ", ".join(dead) if dead else "") +
           ("; em XRUN: %s" % ", ".join(xrun) if xrun else "") +
           ("" if dead or xrun else ", ".join("%s %s" % (k, "vivo" if v else "fechado") for k, v in sorted(alive.items()))),
           "hw_ptr avança entre leituras", "PCM parado: placa muda; o PipeWire continua RUNNING sem som (stall + bug 3)",
           fix="perguntar ao usuário e rodar `--recover` (suspende/retoma a placa)", ganho="som de volta", risco="baixo", esforco="baixo"))
    swp = ((st.get("pcm_end") or {}).get("pcm0p") or {}).get("sw_params") or {}
    hwp = hw.get("pcm0p") or {}
    sw_ok = not swp or not d["tsched_off"] or swp.get("avail_min") == hwp.get("period_size")
    add(mk("A3", "sw_params (avail_min, thresholds)", OK if sw_ok else AVISO,
           "avail_min %s, start %s, stop %s" % (swp.get("avail_min"), swp.get("start_threshold"), swp.get("stop_threshold")) if swp else "fechado",
           "modo IRQ: avail_min = período; stop_threshold = buffer", "avail_min diferente = acorda fora do período"))
    owners = {n: ((st.get("pcm_end") or {}).get(n, {}).get("status") or {}).get("owner_pid") for n in names}
    owners = {k: v for k, v in owners.items() if v}
    ow_names = {k: read("/proc/%d/comm" % v).strip() for k, v in owners.items()}
    add(mk("A4", "Dono dos PCMs", INFO, ", ".join("%s pid %s (%s)" % (k, owners[k], ow_names[k]) for k in owners) or "nenhum aberto",
           "pipewire (ou o programa no modo direto)", ""))

    # ---------------- P
    md = st.get("metadata") or {}
    cc = st.get("clock_conf") or {}
    vals = {"conf": cc.get("quantum"), "conf_min": cc.get("min-quantum"), "conf_max": cc.get("max-quantum"),
            "meta": _int(md.get("clock.quantum")), "meta_min": _int(md.get("clock.min-quantum")),
            "meta_max": _int(md.get("clock.max-quantum")), "force": d["force_q"], "pw_top": d["top_q"],
            "hw_play": (hw.get("pcm0p") or {}).get("period_size"), "hw_cap": (hw.get("pcm0c") or {}).get("period_size"),
            "jack": (st.get("jack_info") or {}).get("buffer")}
    # force-quantum ≠ 0 vale no grafo: entra na comparação (é o caso da regressão: grafo 256, placa 32)
    present_vals = {k: v for k, v in vals.items() if v and k not in ("force", "conf_min", "conf_max",
                                                                      "meta_min", "meta_max") or (k == "force" and v)}
    present_vals.update({k: v for k, v in vals.items() if k in ("conf_min", "conf_max", "meta_min", "meta_max")
                         and v and not d["force_q"]})
    agree = len(set(present_vals.values())) <= 1
    add(mk("P1", "Versões PipeWire / WirePlumber", INFO, "%s; %s" % (" ".join(st.get("pw_version") or []),
                                                                 " ".join(st.get("wp_version") or [])),
           "PipeWire 1.0.5 / WP 0.4.17 (comportamento lido do código dessas versões)", ""))
    add(mk("P2", "Buffer igual em conf × metadata × pw-top × hw_params × JACK", OK if agree and not d["force_q"] else
           (FALHA if not agree else AVISO), ", ".join("%s=%s" % (k, v) for k, v in vals.items() if v is not None),
           "todos = Q, force-quantum 0",
           "force-quantum órfão ou valores diferentes: troca aplicada só ao vivo, sem reabrir a placa (regressão de 2026-10-21)",
           fix="painel novo (apply_buffer: reabre a placa, force 0)", ganho="troca de buffer sem travar", risco="baixo", esforco="—"))
    rates = {"proc": tube.get("rate"), "meta_rate": _int(md.get("clock.rate")),
             "force_rate": _int(md.get("clock.force-rate")) or None, "jack": (st.get("jack_info") or {}).get("rate"),
             "conf": cc.get("rate")}
    clock_names = {(n.get("props") or {}).get("clock.name") for k, n in (st.get("nodes") or {}).items() if k.startswith("alsa_")}
    rv = {v for v in rates.values() if v}
    add(mk("P3", "Taxa única, sem resampler, mesmo clock.name", OK if len(rv) <= 1 and len(clock_names) <= 1 else AVISO,
           "%s; clock.name %s" % (", ".join("%s=%s" % kv for kv in rates.items() if kv[1]), ", ".join(sorted(str(c) for c in clock_names))),
           "uma taxa só; clock.name igual nos nós da Tube", "taxas diferentes = resampler (latência + CPU) ou drift"))
    props = (st.get("nodes") or {}).get(OUT_NODE, {}).get("props") or {}
    want = {"api.alsa.disable-tsched": True, "api.alsa.period-num": 2, "api.alsa.headroom": 0}
    got = {k: props.get(k) for k in want}
    add(mk("P4", "Modo IRQ: tsched/period-num/headroom no pro-output-0", OK if got == want else AVISO,
           ", ".join("%s=%s" % (k.split(".")[-1], v) for k, v in got.items()) + ", period-size %s" % props.get("api.alsa.period-size"),
           "disable-tsched true, period-num 2, headroom 0",
           "modo timer: período 1024 do perfil + headroom 32 na captura (era a fila de 43 ms)"))
    exp_int, dec = d["internal_expected"], d["internal_declared"]
    add(mk("P5", "Latência interna declarada (ProcessLatency) = placa + fila", OK if dec == exp_int else AVISO,
           "%s frames" % dec, "%d (placa %d + fila %d)" % (exp_int, d["card"], d["queue_now"]),
           "erro de alinhamento dos takes = diferença (%s)" % (fmt_ms((exp_int - dec) if dec is not None else None, rate)),
           fix="declarar por tamanho (painel novo: set_internal_latency + regra 54)", ganho="takes alinhados", risco="baixo", esforco="—"))
    top = st.get("pw_top_end") or []
    drivers = sorted({r["driver"] for r in top if r["status"] == "R" and r["quant"] > 0
                      and r["name"] in (OUT_NODE, IN_NODE) and not r["follower"]})
    # o module-loopback põe node.group nos próprios nós (normal); só os nós ALSA da Tube contam
    groups = [k for k, n in (st.get("nodes") or {}).items()
              if k.startswith("alsa_") and (n.get("props") or {}).get("node.group")]
    stt = FALHA if groups else (AVISO if len(drivers) > 1 else OK)
    add(mk("P6", "Driver do grafo e node.group", stt,
           "drivers da Tube rodando: %s; node.group em: %s" % (", ".join(d_.split(".")[-1] for d_ in drivers) or "nenhum",
                                                             ", ".join(groups) or "nenhum"),
           "um driver (captura, priority 2500, quando aberta); sem node.group",
           "node.group travou o grafo no WP 0.4/PW 1.0.5; dois drivers = entrada e saída em ciclos diferentes"))
    e0 = {r["name"]: r["err"] for r in st.get("pw_top_start") or []}
    e1 = {r["name"]: r["err"] for r in top}
    errd = {n: e1[n] - e0[n] for n in e1 if n in e0 and CARD_ID in n and e1[n] - e0[n] > 0}
    add(mk("P7", "ERR dos nós da Tube no pw-top (janela)", OK if not errd else AVISO,
           (", ".join("%s +%d" % (n.split(".")[-1], v) for n, v in errd.items()) or "+0") +
           "; total %s" % ", ".join("%s %s" % (n.split(".")[-1], v) for n, v in e1.items() if CARD_ID in n),
           "+0", "xruns/atrasos do driver do grafo (o pro-output-0 já subia devagar com Firefox)",
           fix="investigar ERR do pro-output-0 (clientes atrasados? Firefox no grafo de 32)", risco="baixo", esforco="médio"))
    add(mk("P8", "pw-data-loop em tempo real", OK if dl.get("cls") == "FF" and (dl.get("rtprio") or 0) >= 80 else FALHA,
           "%s %s" % (dl.get("cls"), dl.get("rtprio")), "FIFO >= 80 (88)", "sem RT o grafo inteiro atrasa"))
    jp = st.get("journal_pw") or {}
    add(mk("P9", "Journal do PipeWire/WirePlumber", AVISO if jp.get("problems") else OK,
           ("; ".join(l[11:19] + " " + l.split(": ", 1)[-1][:90] for l in jp.get("problems", [])[-3:]) or "limpo desde o último início") +
           ("; reiniciado: %s" % jp["restarts"][-1][:19] if jp.get("restarts") else ""), "sem xrun/erro", ""))
    lb = (st.get("nodes") or {}).get("firestudio_tube.out", {}).get("props") or {}
    add(mk("P10", "Loopback do sistema passivo", OK if lb.get("node.passive") in (True, "true") or not lb else AVISO,
           "node.passive=%s" % lb.get("node.passive") if lb else "ausente", "true",
           "loopback ativo segura a placa aberta e entra no grafo de baixa latência"))
    states = {k.split(".")[-1]: n.get("state") for k, n in (st.get("nodes") or {}).items() if k.startswith("alsa_")}
    add(mk("P11", "Estado dos nós da Tube", FALHA if "error" in states.values() else OK,
           ", ".join("%s %s" % kv for kv in sorted(states.items())) or "nenhum nó", "running/suspended/idle (nunca error)", ""))
    dev = st.get("device") or {}
    add(mk("P12", "Perfil do cartão", OK if dev.get("profile") == "pro-audio" else AVISO, str(dev.get("profile")),
           "pro-audio", "outro perfil abre playback/captura separados (DICE exige juntos)"))

    # ---------------- W
    links = st.get("wp_links") or {}
    need = ["51-firestudio-tube.lua", "52-firestudio-tube-loop.lua", "53-firestudio-tube-latencia.lua"]
    miss = [n for n in need if n not in links]
    broken = [n for n, v in links.items() if not v["exists"]]
    add(mk("W1", "Regras WirePlumber instaladas (symlinks para o repo)", FALHA if broken else (AVISO if miss else OK),
           ", ".join("%s%s" % (n, "" if v["in_repo"] else " (cópia/fora do repo)") for n, v in links.items()) +
           ("; faltando %s" % ", ".join(miss) if miss else ""), "51, 52, 53 (+54 gerado pelo painel)",
           "regra faltando = PipeWire volta ao modo timer (fila de 43 ms)"))
    add(mk("W2", "Regras aplicadas no nó (pw-dump)", OK if got == want else AVISO, "ver P4", "props da 53 no pro-output-0",
           "symlink presente mas WirePlumber não reiniciado depois da mudança"))
    rv54 = st.get("latency_rule_value")
    add(mk("W3", "Regra 54 gerada (latência por tamanho)", INFO if rv54 is None else (OK if rv54 == exp_int else AVISO),
           "ausente" if rv54 is None else "%d" % rv54, "%d (= P5)" % exp_int,
           "regra 54 desatualizada: ao reiniciar o PipeWire volta a declarar a latência de outro tamanho"))
    add(mk("W4", "install.sh do painel linka a 53", OK if (st.get("panel") or {}).get("install_53") else AVISO,
           "sim" if (st.get("panel") or {}).get("install_53") else "não (link feito à mão)", "sim",
           "reinstalar o painel perderia o modo IRQ", fix="painel: linkar 53 no install.sh", risco="baixo", esforco="baixo"))

    # ---------------- J
    ji = st.get("jack_info") or {}
    lib = ji.get("lib", "")
    add(mk("J1", "libjack é a do PipeWire", OK if "pipewire" in lib else (FALHA if ji.get("ok") else AVISO),
           lib or ji.get("error", "?"), "~/.local/lib/pipewire-jack/libjack.so.0",
           "jackd de verdade ou libjack errada: MixDaw fora do grafo do PipeWire"))
    add(mk("J2", "Buffer/taxa do JACK = grafo", OK if ji.get("ok") and ji.get("buffer") == q and ji.get("rate") == rate else (NA if not ji.get("ok") else FALHA),
           "%s @ %s" % (ji.get("buffer"), ji.get("rate")), "%s @ %s" % (q, rate), "JACK em outro tamanho = troca não propagada"))
    exp_inf = q + d["headroom"] + q + (dec or 0)
    add(mk("J3", "Latência informada nas portas físicas", OK if d["informed"] == exp_inf else (NA if d["informed"] is None else AVISO),
           "captura %s + playback %s = %s" % (d["informed_in"], d["informed_out"], d["informed"]),
           "Q+headroom (%d) + Q+interna (%d) = %d" % (q + d["headroom"], q + (dec or 0), exp_inf),
           "o MixDaw alinha a gravação por esse número"))
    w = st.get("jack_watch")
    if not w:
        add(mk("J4", "xruns e maior intervalo do JACK (watch)", NA, "não rodou (--sem-watch ou --dur 0)", "0 xrun, gap < 2 períodos", ""))
    else:
        bad = not w.get("ok") or w.get("xruns") or (w.get("max_gap_us") or 0) > 2.5 * (w.get("period_us") or 1e9) or \
            (w.get("cycles", 0) < 0.5 * (w.get("expected_cycles") or 0))
        add(mk("J4", "xruns e maior intervalo do JACK (watch)", AVISO if bad else OK,
               "ciclos %s/%s, xruns %s, maior intervalo %s µs (período %s µs), trocas de buffer %s" % (
                   w.get("cycles"), w.get("expected_cycles"), w.get("xruns"), w.get("max_gap_us"), w.get("period_us"),
                   w.get("buffer_changes")) if w.get("ok") else w.get("error"),
               "0 xrun, maior intervalo < 2,5 períodos, ciclos ≈ esperados", "ciclos faltando = grafo parado"))
    add(mk("J5", "Overrides jack.conf.d", INFO, ", ".join(os.path.basename(f) for f in st.get("jack_conf_files", [])) or "nenhum",
           "nenhum (o buffer vem do grafo)", "node.latency/jack.* fixos brigam com o painel"))

    # ---------------- M
    mx = st.get("mixdaw") or {}
    running = bool(mx.get("procs"))
    add(mk("M1", "MixDaw rodando e no grafo", INFO if not running else (OK if st.get("mixdaw_pw_clients") else AVISO),
           ("pid %s; cliente PW %s" % (mx["procs"][0]["pid"], ", ".join(c["app"] for c in st.get("mixdaw_pw_clients", [])) or "nenhum"))
           if running else "não está aberto", "aberto e conectado via JACK", ""))
    rt_n = len(mx.get("threads_rt") or [])
    add(mk("M2", "Threads RT do MixDaw", NA if not running else (OK if rt_n else FALHA),
           "%d de %s threads em RT" % (rt_n, mx.get("threads_total")) if running else "—",
           ">= 1 (callback de áudio)", "callback sem RT = xruns em buffer pequeno; workers sem RT = picos com muitos plugins",
           fix="workers do grafo do Tracktion em RT (lista do motor-5)", risco="médio", esforco="médio"))
    sets = mx.get("settings") or {}
    add(mk("M3", "Compensação e monitoração do MixDaw", INFO,
           "compensação extra %s ms; reduzir latência ao monitorar %s; limite plugin %s ms" % (
               sets.get("mixdawRecordCompensationMs"), sets.get("mixdawMonitorReduceLatency"),
               sets.get("mixdawMonitorPluginLatencyLimitMs")),
           "compensação extra 0 (a latência vem do JACK + interna correta)", "compensação manual mascara erro de P5"))
    code = mx.get("code") or {}
    add(mk("M4", "JUCE: callback de troca de buffer do JACK", OK if code.get("juce_buffer_callback") or code.get("juce_patch") else AVISO,
           "jack_set_buffer_size_callback %s; patch %s" % ("sim" if code.get("juce_buffer_callback") else "não",
                                                          "sim" if code.get("juce_patch") else "não"),
           "registrado (patch patches/juce/0001)", "sem ele, buffer novo = Tracktion fatia blocos (8×32 com plugins preparados p/ 32) e latência errada",
           fix="Etapa 3 do plano (patch JUCE + reinício do device)", ganho="troca ao vivo correta", risco="médio", esforco="médio"))
    add(mk("M5", "Cache de latência com geração (JackLatencyWatcher)", OK if code.get("latency_watcher") else AVISO,
           "latencyGeneration %s" % ("presente" if code.get("latency_watcher") else "ausente"), "presente",
           "caches de Monitoring/Recording sem a latência das portas na chave: compensação velha após troca",
           fix="Etapa 3 (cachedLatency único + watcher)", risco="baixo", esforco="médio"))
    meas = (measurement or {}).get("jack") if measurement else None
    if meas and meas.get("ok"):
        err = round(meas["median"] - meas["informed"], 1)
        add(mk("M6", "Alinhamento: medido × informado", OK if abs(err) <= 8 else AVISO, "erro %+.1f frames (%s)" % (err, fmt_ms(err, rate)),
               "|erro| <= 8 (1 pacote)", "takes gravados fora do tempo por esse valor"))
    else:
        add(mk("M6", "Alinhamento: medido × informado", NA, "sem medição (use --measure jack)", "|erro| <= 8", ""))

    # ---------------- C
    pn = st.get("panel") or {}
    pw_up = bool(st.get("pipewire_pid"))
    add(mk("P0", "PipeWire rodando", OK if pw_up else FALHA, "pid %s" % st.get("pipewire_pid") if pw_up else "parado",
           "pipewire, wireplumber e pipewire-pulse ativos",
           "sem PipeWire não há grafo: MixDaw/JACK sem placa (reinício em andamento?)",
           fix="`systemctl --user start pipewire pipewire-pulse wireplumber` (perguntar antes)", risco="baixo", esforco="baixo"))
    if not pw_up:
        for c in C:
            if c["id"] in PW_DEPENDENT and c["status"] in (AVISO, FALHA, OK):
                c["status"], c["medido"] = NA, "PipeWire parado (" + str(c["medido"])[:60] + ")"
    sizes = pn.get("buffer_sizes")
    pr = tube.get("period_range")
    valid = set(valid_buffer_sizes(rate, pr[1] * rate_multiplier(rate) if pr else None, tube.get("buffer_max") or 4096))
    invalid = [s for s in (sizes or []) if s not in valid]
    add(mk("C1", "Tamanhos oferecidos pelo painel × driver", OK if sizes and not invalid else AVISO,
           "%s%s" % (sizes, (" (inválidos a %d Hz: %s)" % (rate, invalid)) if invalid else ""),
           "só %s" % sorted(valid), "tamanho inválido = abre e falha/trava",
           fix="painel: valid_buffer_sizes(rate)", risco="baixo", esforco="baixo"))
    add(mk("C2", "Painel troca buffer reabrindo a placa", OK if pn.get("apply_buffer") and pn.get("reopen") else FALHA,
           "apply_buffer %s, reopen_tube_nodes %s" % (pn.get("apply_buffer"), pn.get("reopen")),
           "presentes (Etapa 2)", "só force-quantum ao vivo: em modo IRQ o grafo muda e a placa não -> trava (10:46 de 2026-10-21)",
           fix="Etapa 2 do plano (painel)", ganho="troca de buffer sem travar", risco="baixo", esforco="—"))
    svc = {u: pn.get("svc_" + u) for u in ("firestudio-tube-ctl", "firestudio-tube-unmute", "pipewire", "wireplumber", "pipewire-pulse")}
    add(mk("C3", "Serviços de usuário", OK if all(v == "active" for v in svc.values()) else AVISO,
           ", ".join("%s %s" % kv for kv in svc.items()), "todos active", "ctl parado = painel sem DSP; unmute parado = Loop mudo"))
    add(mk("C4", "Painel aberto", INFO, "sim" if pn.get("running") else "não", "—", "ele segura o controle ALSA (recarregar driver fecha antes)"))
    return C, d


FIXED_IMPROVEMENTS = [
    {"item": "Prioridade do IRQ OHCI 50 -> >= 70 e CPU separado do pw-data-loop", "camada": "K", "ref": ["K3", "K4"],
     "ganho": "menos stalls/xruns sob carga (NVIDIA no mesmo nível)", "risco": "baixo", "esforco": "baixo (udev/rtirq, sudo)"},
    {"item": "preempt=full + threadirqs (+ PM QoS se C-state aparecer)", "camada": "K", "ref": ["K1", "K2", "K6"],
     "ganho": "margem para 32/64 com o PC ocupado", "risco": "baixo", "esforco": "baixo (GRUB, reiniciar)"},
    {"item": "gap_probe=Y por um dia de uso real", "camada": "D", "ref": ["D7"],
     "ganho": "mede a margem real da fila; decide queue_period_pct", "risco": "baixo", "esforco": "baixo"},
    {"item": "ERR do pro-output-0 subindo devagar", "camada": "P", "ref": ["P7"],
     "ganho": "menos estalos com navegador tocando", "risco": "baixo", "esforco": "médio"},
    {"item": "OOM/pressão de memória durante builds", "camada": "K", "ref": ["K11", "K12"],
     "ganho": "sem travadas de áudio durante compilação", "risco": "baixo", "esforco": "baixo (-j 3, zram)"},
    {"item": "Placa a 44,1 kHz nunca medida (72 suposto)", "camada": "H", "ref": ["H10"],
     "ganho": "alinhamento exato a 44,1k", "risco": "baixo", "esforco": "baixo (--measure jack a 44,1k)"},
    {"item": "MixDaw: workers do grafo sem RT, gravação com locks, serial < 50 µs (refazer sem plugins)",
     "camada": "M", "ref": ["M2"], "ganho": "menos xruns com muitas faixas", "risco": "médio", "esforco": "médio"},
]


def improvements(checks, d):
    by_id = {c["id"]: c for c in checks}
    out = []
    order = {FALHA: 0, AVISO: 1}
    for c in sorted(checks, key=lambda c: (order.get(c["status"], 9), c["id"])):
        if c["status"] in (FALHA, AVISO) and c.get("melhoria"):
            m = c["melhoria"]
            out.append({"item": m["acao"], "camada": c["camada"], "evidencia": "%s %s: %s" % (c["id"], c["status"], c["medido"]),
                        "ganho": m.get("ganho") or "—", "risco": m.get("risco") or "—", "esforco": m.get("esforco") or "—",
                        "origem": "verificação"})
    seen = {o["item"] for o in out}
    for f in FIXED_IMPROVEMENTS:
        if f["item"] in seen:
            continue
        ev = "; ".join("%s %s" % (r, by_id[r]["status"]) for r in f["ref"] if r in by_id)
        out.append({"item": f["item"], "camada": f["camada"], "evidencia": ev or "—", "ganho": f["ganho"],
                    "risco": f["risco"], "esforco": f["esforco"], "origem": "regra fixa"})
    return out


# ============================================================================ comparação
def compare(cur, prev):
    pc = {c["id"]: c for c in prev.get("checks", [])}
    changes = []
    for c in cur.get("checks", []):
        p = pc.get(c["id"])
        if p and p["status"] != c["status"]:
            changes.append({"id": c["id"], "antes": p["status"], "agora": c["status"], "medido": c["medido"]})
    nums = {}
    for k in ("q", "rate", "queue_now", "internal_declared", "internal_expected", "informed", "t_expected"):
        a, b = (prev.get("resumo") or {}).get(k), (cur.get("resumo") or {}).get(k)
        if a != b:
            nums[k] = {"antes": a, "agora": b}
    ma = ((prev.get("medicao") or {}).get("jack") or {}).get("median")
    mb = ((cur.get("medicao") or {}).get("jack") or {}).get("median")
    if ma is not None or mb is not None:
        nums["medido_jack"] = {"antes": ma, "agora": mb}
    return {"anterior": prev.get("quando"), "mudancas": changes, "numeros": nums}


# ============================================================================ ações que alteram (opt-in)
def pactl_suspend(on, raw=None):
    v = "1" if on else "0"
    rc1, _ = run(["pactl", "suspend-sink", OUT_NODE, v], timeout=5, raw=raw, name="pactl_suspend_sink_%s" % v)
    rc2, _ = run(["pactl", "suspend-source", IN_NODE, v], timeout=5, raw=raw, name="pactl_suspend_source_%s" % v)
    return rc1 == 0


def card_name():
    out = stdout_only(["pactl", "list", "cards", "short"])
    for line in out.splitlines():
        if CARD_ID in line:
            return line.split()[1]
    return None


def silence_wav(rate, seconds):
    import wave
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "silencio-%d.wav" % rate)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0" * 4 * int(rate * seconds))
    return path


def wake(rate=48000, seconds=3.0):
    """Abre a placa com silêncio pelo sink do sistema (DAW 1/2), como o wake_stream do painel."""
    try:
        return subprocess.Popen(["pw-play", "--target", "firestudio_tube", silence_wav(rate, seconds)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def wait_alive(period=None, timeout=8.0, require_running=True, rate=48000):
    """Espera o playback RUNNING com hw_ptr andando (e período = period, se dado). Sem ninguém
    usando a placa, acorda com silêncio. require_running=False aceita PCM fechado (nada travado)."""
    end = time.monotonic() + timeout
    last = None
    waker = None
    try:
        while time.monotonic() < end:
            sub = os.path.join(PROC_TUBE, "pcm0p", "sub0")
            s = parse_pcm_status(read(os.path.join(sub, "status")))
            h = parse_hw_params(read(os.path.join(sub, "hw_params")))
            if s.get("state") == "CLOSED":
                if not require_running:
                    return True
                if waker is None:
                    waker = wake(rate, min(timeout, 5.0))
            elif s.get("state") == "RUNNING" and h and (period is None or h["period_size"] == period):
                if last is not None and s["hw_ptr"] != last:
                    return True
                last = s["hw_ptr"]
            time.sleep(0.2)
        return False
    finally:
        if waker and waker.poll() is None:
            waker.terminate()


def recover(raw):
    """Destrava a placa: suspende/retoma os nós; se continuar parada, ciclo de perfil."""
    steps = []
    pactl_suspend(True, raw)
    time.sleep(0.6)
    pactl_suspend(False, raw)
    ok = wait_alive(timeout=6, require_running=False)
    steps.append({"metodo": "suspend", "ok": ok})
    if not ok:
        card = card_name()
        if card:
            run(["pactl", "set-card-profile", card, "off"], timeout=5, raw=raw, name="perfil_off")
            time.sleep(1.0)
            run(["pactl", "set-card-profile", card, "pro-audio"], timeout=5, raw=raw, name="perfil_pro")
            ok = wait_alive(timeout=8, require_running=False)
            steps.append({"metodo": "perfil", "ok": ok})
    return ok, steps


def backup_state():
    md = parse_pw_metadata(stdout_only(["pw-metadata", "-n", "settings", "0"]))
    files = {}
    for p in (CLOCK_CONF, LATENCY_RULE):
        files[p] = read(p, None) if os.path.exists(p) else None
    sub = os.path.join(PROC_TUBE, "pcm0p", "sub0")
    h = parse_hw_params(read(os.path.join(sub, "hw_params")))
    tube = parse_tube_proc(read(os.path.join(PROC_TUBE, "firestudio")))
    return {"metadata": {k: md.get(k) for k in ("clock.quantum", "clock.min-quantum", "clock.max-quantum",
                                                "clock.force-quantum", "clock.force-rate", "clock.rate")},
            "files": files, "period": (h or {}).get("period_size"), "rate": tube.get("rate") or 48000,
            "q": _int(md.get("clock.force-quantum"), 0) or _int(md.get("clock.quantum"))}


def set_meta(key, value, raw=None):
    run(["pw-metadata", "-n", "settings", "0", key, str(value)], timeout=5, raw=raw, name="meta_%s_%s" % (key, value))


def set_quantum_reopen(q, raw=None):
    """Modo 'reabrir' próprio: min/max/quantum na ordem segura, force 0, e reabre a placa."""
    md = parse_pw_metadata(stdout_only(["pw-metadata", "-n", "settings", "0"]))
    cur_min = _int(md.get("clock.min-quantum"), q)
    # descendo: min primeiro (min <= max sempre); subindo: max primeiro
    order = [("clock.min-quantum", q), ("clock.max-quantum", q)] if q < (cur_min or q) else \
        [("clock.max-quantum", q), ("clock.min-quantum", q)]
    pactl_suspend(True, raw)
    time.sleep(0.5)
    for k, v in order + [("clock.quantum", q), ("clock.force-quantum", 0)]:
        set_meta(k, v, raw)
    pactl_suspend(False, raw)


def set_quantum_panel(rate, q, raw=None):
    code = ("import sys, json; sys.path.insert(0, %r)\n"
            "from firestudio_control import layout as L\n"
            "if hasattr(L, 'apply_buffer'):\n"
            "    r = L.apply_buffer(%d, %d)\n"
            "    print(json.dumps({'modo': 'apply_buffer', 'r': r}, default=str))\n"
            "else:\n"
            "    L.set_clock(%d, %d)\n"
            "    print(json.dumps({'modo': 'set_clock (legado: só force-quantum)'}))\n") % (PANEL_REPO, rate, q, rate, q)
    rc, out = run([sys.executable, "-I", "-c", code], timeout=40, raw=raw, name="painel_%d" % q)
    return rc, out


def restore_state(bk, mode, raw=None):
    for path, text in bk["files"].items():
        try:
            if text is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                tmp = path + ".tmp-latencia"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
        except OSError:
            pass
    q = bk.get("q")
    if mode == "painel" and q:
        set_quantum_panel(bk["rate"], q, raw)
        # o painel reescreve os arquivos: volta o conteúdo exato
        for path, text in bk["files"].items():
            if text is not None:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
    elif q:
        set_quantum_reopen(q, raw)
    for k, v in bk["metadata"].items():
        if v is not None:
            set_meta(k, v, raw)
    if bk.get("period") is None:      # placa estava fechada: basta não ficar travada
        return wait_alive(timeout=10, require_running=False, rate=bk["rate"])
    return wait_alive(period=bk.get("period"), timeout=10, rate=bk["rate"])


def measure_jack(raw, secs=6, level=-20):
    return jack_rt(["rt", "--out", JACK_OUT, "--in", JACK_IN, "--secs", str(secs), "--level-db", str(level)],
                   timeout=secs + 20, raw=raw, name="jack_rt_rt")


def measure_direto(rate, q, raw):
    tool = os.path.join(DRIVER_REPO, "tools", "latencia")
    script = os.path.join(DRIVER_REPO, "scripts", "tube-direto.sh")
    if not (os.path.exists(tool) and os.path.exists(script)):
        return {"ok": False, "error": "tools/latencia ou tube-direto.sh ausente"}
    rc, out = run(["sh", script, tool, "-r", str(rate), "-p", str(q), "-n", "2", "-o", "3", "-i", "9", "-s", "10"],
                  timeout=40, raw=raw, name="latencia_direto_%d" % q)
    d = parse_latencia_direto(out)
    d["ok"] = d.get("median") is not None
    d["rc"] = rc
    if d["ok"]:
        d["daw_round_trip"] = d["median"] + 2 * q
    return d


def sizes_run(args, raw, rate):
    rows, restored = [], True
    bk = backup_state()
    raw.save("backup_estado", bk)
    try:
        for q in args.sizes:
            row = {"q": q}
            if q not in valid_buffer_sizes(rate):
                row.update(status=FALHA, nota="tamanho inválido para o driver a %d Hz" % rate)
                rows.append(row)
                continue
            tb0 = parse_tube_proc(read(os.path.join(PROC_TUBE, "firestudio")))
            if args.modo_troca == "painel":
                rc, out = set_quantum_panel(rate, q, raw)
                row["painel"] = out.strip()[-300:]
            else:
                set_quantum_reopen(q, raw)
            alive = wait_alive(period=q, timeout=8, rate=rate)
            time.sleep(1.5)
            tb1 = parse_tube_proc(read(os.path.join(PROC_TUBE, "firestudio")))
            sub = lambda n: os.path.join(PROC_TUBE, n, "sub0")
            hp = parse_hw_params(read(os.path.join(sub("pcm0p"), "hw_params")))
            hc = parse_hw_params(read(os.path.join(sub("pcm0c"), "hw_params")))
            md = parse_pw_metadata(stdout_only(["pw-metadata", "-n", "settings", "0"]))
            ji = jack_rt(["info"], timeout=10)
            irq, queue = expected_queue(rate, q, 2 * q, None)
            row.update(vivo=alive, hw_play=(hp or {}).get("period_size"), hw_cap=(hc or {}).get("period_size"),
                       meta=_int(md.get("clock.quantum")), force=_int(md.get("clock.force-quantum")),
                       jack=ji.get("buffer"), iso=(tb1.get("iso_irq"), tb1.get("iso_queue")), iso_esperado=(irq, queue),
                       starts=delta(tb0.get("starts"), tb1.get("starts")), stalls=delta(tb0.get("stalls"), tb1.get("stalls")),
                       t_esperado=round_trip(q, queue, card_latency(rate)[0], q))
            if args.measure == "jack":
                row["medido"] = measure_jack(raw, secs=4)
            ok = alive and row["hw_play"] == q and row["meta"] == q and not row["force"] and row["jack"] == q \
                and not row["stalls"] and row["iso"] == (irq, queue)
            row["status"] = OK if ok else FALHA
            rows.append(row)
    finally:
        restored = restore_state(bk, args.modo_troca, raw)
    return rows, restored


# ============================================================================ relatório
def md_escape(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def write_report(path, res):
    d = res["resumo"]
    rate = d["rate"]
    L = []
    a = L.append
    n_f = sum(c["status"] == FALHA for c in res["checks"])
    n_a = sum(c["status"] == AVISO for c in res["checks"])
    a("# Auditoria de latência: FireStudio Tube -> MixDaw")
    a("")
    a("%s · %s · janela %s s · modo %s · skill latencia-audio" % (res["quando"], res["host"], res["janela_s"], res["modo"]))
    a("")
    a("**Resultado: %d FALHA, %d AVISO** (código de saída %d)." % (n_f, n_a, res["codigo"]))
    a("")
    a("## Estado agora")
    a("")
    a("| | valor |")
    a("|---|---|")
    a("| Taxa / buffer (quantum) | %d Hz / %s (%s) |" % (rate, d["q"], fmt_ms(d["q"], rate)))
    a("| Modo do PipeWire | %s, headroom %s |" % ("IRQ (tsched off)" if d["tsched_off"] else "timer", d["headroom"]))
    a("| Driver: período/buffer, iso irq/fila | %s/%s, %s/%s (esperado %s/%s) |" % (
        d["tube"].get("period"), d["tube"].get("buffer"), d["tube"].get("iso_irq"), d["tube"].get("iso_queue"),
        d["irq_expected"], d["queue_expected"]))
    a("| Fila medida (delay − (appl − hw)) | %s |" % ("—" if d["queue_measured"] is None else d["queue_measured"]))
    a("| Latência interna declarada × esperada | %s × %s |" % (d["internal_declared"], d["internal_expected"]))
    a("| Ida e volta esperada | %d frames = %s |" % (d["t_expected"], fmt_ms(d["t_expected"], rate)))
    a("| Informada ao JACK/MixDaw (captura + playback) | %s = %s |" % (d["informed"], fmt_ms(d["informed"], rate)))
    mj = (res.get("medicao") or {}).get("jack")
    if mj:
        a("| Medida (JACK, cabo Out 3 -> Line 9) | %s |" % (
            "%.1f frames = %s (%s/%s pulsos, jitter %.1f)" % (mj["median"], fmt_ms(mj["median"], rate), mj["found"], mj["pulses"], mj["jitter"])
            if mj.get("ok") else "sem clique: %s" % mj.get("error", "nenhum pulso voltou")))
    mdr = (res.get("medicao") or {}).get("direto")
    if mdr:
        a("| Medida direto no ALSA (tools/latencia) | %s |" % (
            "placa+driver %s, DAW %s frames" % (mdr["median"], mdr.get("daw_round_trip")) if mdr.get("ok") else mdr.get("error", "falhou")))
    a("")
    bad = [c for c in res["checks"] if c["status"] in (FALHA, AVISO)]
    a("## Falhas e avisos")
    a("")
    if bad:
        a("| ID | | verificação | medido | esperado | o que significa |")
        a("|---|---|---|---|---|---|")
        for c in sorted(bad, key=lambda c: (c["status"] != FALHA, c["id"])):
            a("| %s | %s | %s | %s | %s | %s |" % (c["id"], c["status"], md_escape(c["titulo"]), md_escape(c["medido"]),
                                                  md_escape(c["esperado"]), md_escape(c["fora"])))
    else:
        a("Nenhuma.")
    a("")
    b = res["orcamento"]
    a("## Orçamento da ida e volta (%s)" % b["base"])
    a("")
    a("| componente | frames | ms | % do total | tipo |")
    a("|---|---|---|---|---|")
    for r in b["rows"]:
        a("| %s | %d | %.2f | %.1f %% | %s |" % (r["componente"], r["frames"], r["ms"], r["pct"], r["tipo"]))
    a("| **soma** | **%d** | **%.2f** | | |" % (b["soma"], b["soma_ms"]))
    if "residuo" in b:
        a("| resíduo (medido − soma) | %d | %s | %.1f %% | %s |" % (b["residuo"], fmt_ms(b["residuo"], rate), b["residuo_pct"],
                                                                "**AVISO > 10 %**" if b["residuo_aviso"] else "ok"))
    if "erro_alinhamento" in b:
        a("")
        a("Erro de alinhamento da gravação (total − informado): **%+d frames = %s** (%s)." % (
            b["erro_alinhamento"], fmt_ms(b["erro_alinhamento"], rate), b["erro_tipo"]))
    a("")
    a("## Latência por tamanho a %d Hz (calculada do driver: 2Q + placa + fila)" % rate)
    a("")
    a("| buffer | iso irq | fila | interna (placa+fila) | ida e volta | ms |")
    a("|---|---|---|---|---|---|")
    for r in res["tabela"]:
        a("| %d | %d | %d | %d | %d | %.2f |" % (r["q"], r["irq"], r["queue"], r["internal"], r["round_trip"], r["ms"]))
    a("")
    a("## Onde melhorar (priorizado)")
    a("")
    a("| # | item | camada | evidência | ganho | risco | esforço |")
    a("|---|---|---|---|---|---|---|")
    for i, m in enumerate(res["melhorias"], 1):
        a("| %d | %s | %s | %s | %s | %s | %s |" % (i, md_escape(m["item"]), m["camada"], md_escape(m["evidencia"]),
                                                   md_escape(m["ganho"]), m["risco"], m["esforco"]))
    if res.get("tamanhos"):
        a("")
        a("## Troca de buffer (--sizes, modo %s)" % res["modo_troca"])
        a("")
        a("| Q | status | vivo | hw play/cap | metadata/force | JACK | iso (esperado) | starts | stalls | medido |")
        a("|---|---|---|---|---|---|---|---|---|---|")
        for r in res["tamanhos"]:
            med = r.get("medido") or {}
            a("| %s | %s | %s | %s/%s | %s/%s | %s | %s (%s) | %s | %s | %s |" % (
                r["q"], r["status"], r.get("vivo"), r.get("hw_play"), r.get("hw_cap"), r.get("meta"), r.get("force"),
                r.get("jack"), r.get("iso"), r.get("iso_esperado"), r.get("starts"), r.get("stalls"),
                "%.1f" % med["median"] if med.get("ok") else (r.get("nota") or "—")))
        a("")
        a("Estado restaurado: **%s**." % ("sim" if res.get("restaurado") else "NÃO (rode --recover)"))
    if res.get("comparacao"):
        cp = res["comparacao"]
        a("")
        a("## Comparação com %s" % cp.get("anterior"))
        a("")
        for ch in cp["mudancas"]:
            a("- %s: %s -> %s (%s)" % (ch["id"], ch["antes"], ch["agora"], md_escape(ch["medido"])))
        for k, v in cp["numeros"].items():
            a("- %s: %s -> %s" % (k, v["antes"], v["agora"]))
        if not cp["mudancas"] and not cp["numeros"]:
            a("Sem mudanças.")
    a("")
    a("## Todas as verificações")
    for code, name in LAYERS:
        a("")
        a("### %s · %s" % (code, name))
        a("")
        for c in [c for c in res["checks"] if c["camada"] == code]:
            a("- **%s %s** %s: %s (esperado: %s)" % (c["id"], c["status"], c["titulo"], md_escape(c["medido"]), md_escape(c["esperado"])))
    a("")
    a("Checklist completo (inclui itens manuais): `~/.claude/skills/latencia-audio/references/checklist.md`. "
      "Fórmulas: `references/orcamento.md`. Saída bruta: `bruto/`.")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def write_counters(path, samples):
    cols = ["t_s", "starts", "stalls", "start_retries", "bus_resets", "pkt_tx", "pkt_rx", "iso_irq", "iso_queue",
            "irq_ohci", "play_state", "play_hw_ptr", "play_delay", "play_fila", "cap_state", "cap_hw_ptr"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(cols) + "\n")
        for s in samples:
            t = s.get("tube") or {}
            p = (s.get("pcm") or {}).get("pcm0p") or {}
            c = (s.get("pcm") or {}).get("pcm0c") or {}
            row = [s.get("t"), t.get("starts"), t.get("stalls"), t.get("start_retries"), t.get("bus_resets"),
                   t.get("pkt_tx"), t.get("pkt_rx"), t.get("iso_irq"), t.get("iso_queue"), (s.get("irq") or {}).get("total"),
                   p.get("state"), p.get("hw_ptr"), p.get("delay"), p.get("queued_beyond_buffer"), c.get("state"), c.get("hw_ptr")]
            f.write("\t".join("" if v is None else str(v) for v in row) + "\n")


# ============================================================================ main
def parse_args(argv):
    ap = argparse.ArgumentParser(description="Auditoria de latência FireStudio Tube -> MixDaw (só leitura por padrão).")
    ap.add_argument("--out", required=True, help="pasta de saída (relatorio.md, auditoria.json, contadores.tsv, bruto/)")
    ap.add_argument("--dur", type=float, default=30, help="janela de observação em segundos (padrão 30)")
    ap.add_argument("--sem-watch", action="store_true", help="não abre o cliente JACK 'watch' durante a janela")
    ap.add_argument("--history", nargs="?", const=HISTORY_DEFAULT, help="guarda esta rodada e compara com a anterior")
    ap.add_argument("--compare", help="auditoria.json anterior para comparar")
    ap.add_argument("--measure", choices=["jack", "direto"], help="mede a ida e volta pelo cabo Out 3 -> Line 9")
    ap.add_argument("--confirmo-cabo-out3-line9", dest="confirm_cable", action="store_true",
                    help="obrigatório com --measure: cabo Out 3 -> Line 9 ligado, volumes baixos, Line 9 fora das mixes")
    ap.add_argument("--sizes", help="lista de buffers para testar a troca, ex. 32,64,128,256,512")
    ap.add_argument("--altera-sistema", dest="alter", action="store_true", help="obrigatório com --sizes")
    ap.add_argument("--modo-troca", choices=["painel", "reabrir"], default="painel")
    ap.add_argument("--recover", action="store_true", help="destrava a placa (suspende/retoma; senão ciclo de perfil)")
    args = ap.parse_args(argv)
    if args.measure and not args.confirm_cable:
        ap.error("--measure exige --confirmo-cabo-out3-line9 (cabo Out 3 -> Line 9, nunca Out 5/6)")
    if args.sizes:
        if not args.alter:
            ap.error("--sizes altera o buffer do sistema: exige --altera-sistema")
        try:
            args.sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
        except ValueError:
            ap.error("--sizes: lista de inteiros, ex. 32,64,256")
    if args.dur < 0 or args.dur > 3600:
        ap.error("--dur 0..3600")
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out, exist_ok=True)
    raw = Raw(os.path.join(out, "bruto"))
    if not os.path.isdir("/proc/asound") or shutil.which("pw-dump") is None:
        print("ambiente sem ALSA ou sem PipeWire (pw-dump)", file=sys.stderr)
        return 3
    modo = "recover" if args.recover else ("sizes" if args.sizes else ("measure-" + args.measure if args.measure else "coleta"))
    recovered = None
    if args.recover:
        ok, steps = recover(raw)
        recovered = {"ok": ok, "passos": steps}
        print("recover: %s %s" % ("ok" if ok else "FALHOU", steps))
    st = collect(args, raw)
    if not st["tube_present"]:
        print("placa ausente: /proc/asound/Tube não existe (driver snd-firestudio-tube carregado?)", file=sys.stderr)
    measurement = {}
    d0 = derive(st)
    if args.measure == "jack" and not args.sizes:
        measurement["jack"] = measure_jack(raw)
    elif args.measure == "direto":
        measurement["direto"] = measure_direto(d0["rate"], d0["q"], raw)
        if not wait_alive(timeout=10, require_running=False, rate=d0["rate"]):
            print("AVISO: a placa não voltou ao PipeWire depois do modo direto; rode --recover", file=sys.stderr)
    checks, d = evaluate(st, measurement)
    mt = None
    if measurement.get("jack", {}).get("ok"):
        mt = measurement["jack"]["median"]
    b = budget(d["rate"], d["q"], d["queue_measured"] if d["queue_measured"] is not None else d["queue_now"], d["card"],
               d["headroom"], 0, measured=mt, informed=d["informed"],
               queue_source="MEDIDO" if d["queue_measured"] is not None else "CÓDIGO")
    if b.get("residuo_aviso"):
        checks.append(mk("M7", "Resíduo do orçamento", AVISO, "%+d frames (%s %%)" % (b["residuo"], b["residuo_pct"]),
                         "|resíduo| <= 10 %", "atraso que nenhuma camada explica"))
    sizes_rows, restored = None, True
    if args.sizes:
        sizes_rows, restored = sizes_run(args, raw, d["rate"])
    resumo = {k: v for k, v in d.items() if k not in ("tube", "tube_end", "hw_params")}
    resumo["tube"] = {k: v for k, v in d["tube"].items() if k != "raw"}
    code = 0
    if any(c["status"] == FALHA for c in checks) or (sizes_rows and any(r["status"] == FALHA for r in sizes_rows)):
        code = 1
    if args.measure and not any(m.get("ok") for m in measurement.values()) and measurement:
        code = 4
    if recovered is not None and not recovered["ok"]:
        code = 1
    if not st["tube_present"]:
        code = 3
    if not restored:
        code = 5
    res = {"schema": SCHEMA, "quando": st["when"], "host": st["host"], "modo": modo, "janela_s": args.dur,
           "modo_troca": args.modo_troca if args.sizes else None, "codigo": code, "resumo": resumo, "checks": checks,
           "orcamento": b, "tabela": latency_table(d["rate"], st.get("params")),
           "melhorias": improvements(checks, d), "medicao": measurement or None, "tamanhos": sizes_rows,
           "restaurado": restored if args.sizes else None, "recover": recovered,
           "estado": {k: st.get(k) for k in ("metadata", "clock_conf", "params", "wp_links", "jack_watch", "preempt",
                                             "governors", "cstates", "limits", "psi", "swap", "panel", "mixdaw",
                                             "driver_repo_version", "driver_installed_version", "irq", "dataloop")}}
    prev = None
    if args.compare:
        try:
            with open(os.path.expanduser(args.compare), encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, ValueError) as e:
            print("--compare: não li %s (%s)" % (args.compare, e), file=sys.stderr)
    hist_dir = None
    if args.history:
        hroot = os.path.expanduser(args.history)
        os.makedirs(hroot, exist_ok=True)
        if prev is None:
            olds = sorted(glob.glob(os.path.join(hroot, "*", "auditoria.json")))
            if olds:
                try:
                    with open(olds[-1], encoding="utf-8") as f:
                        prev = json.load(f)
                except (OSError, ValueError):
                    prev = None
        hist_dir = os.path.join(hroot, _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S"))
    if prev:
        res["comparacao"] = compare(res, prev)
    with open(os.path.join(out, "auditoria.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False, default=str)
    write_report(os.path.join(out, "relatorio.md"), res)
    write_counters(os.path.join(out, "contadores.tsv"), st.get("samples") or [])
    if hist_dir:
        os.makedirs(hist_dir, exist_ok=True)
        for n in ("auditoria.json", "relatorio.md", "contadores.tsv"):
            shutil.copy2(os.path.join(out, n), os.path.join(hist_dir, n))
    n_f = sum(c["status"] == FALHA for c in checks)
    n_a = sum(c["status"] == AVISO for c in checks)
    print("%d FALHA, %d AVISO · ida e volta esperada %d frames (%s) · informada %s · relatório %s" % (
        n_f, n_a, d["t_expected"], fmt_ms(d["t_expected"], d["rate"]), d["informed"], os.path.join(out, "relatorio.md")))
    for c in checks:
        if c["status"] == FALHA:
            print("  FALHA %s %s: %s" % (c["id"], c["titulo"], c["medido"]))
    if hist_dir:
        print("histórico: %s" % hist_dir)
    return code


if __name__ == "__main__":
    sys.exit(main())
