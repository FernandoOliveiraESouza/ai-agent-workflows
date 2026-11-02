#!/usr/bin/env python3
"""sfx_peak.py <arquivo...> [--out DIR] [--pre 0.05] [--post 0.6]

Para cada SFX baixado do Magnific (mp3/wav): acha o pico de energia (janela de 10 ms), o ataque (primeiro
ponto a 10% do pico), conta picos (>50%: 1 = golpe limpo) e estima cliques. Com --out, apara em volta do pico
(pre/post em s), aplica fade e normaliza a −1 dBFS em wav 48 kHz mono. Imprime o deslocamento do pico dentro do
arquivo aparado: posicione o cue em  t_inicio = t_acao_no_video − pico_s.
"""
import argparse, os, subprocess
import numpy as np

SR = 48000

def load(p):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', p, '-ac', '1', '-ar', str(SR), '-f', 'f32le', '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()

def analyze(x):
    w = int(0.01 * SR); e = np.sqrt(np.convolve(x ** 2, np.ones(w) / w, 'same'))
    pk = int(e.argmax()); att = int(np.argmax(e > e.max() * 0.1))
    es = np.convolve(e, np.ones(3 * w) / (3 * w), 'same')
    peaks = int(((es[1:-1] > es[:-2]) & (es[1:-1] > es[2:]) & (es[1:-1] > es.max() * 0.5)).sum())
    d = np.abs(np.diff(x)); clicks = float(d.max() / (d.mean() + 1e-9))
    return pk, att, peaks, clicks

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('files', nargs='+'); ap.add_argument('--out')
    ap.add_argument('--pre', type=float, default=0.05); ap.add_argument('--post', type=float, default=0.6)
    a = ap.parse_args()
    if a.out: os.makedirs(a.out, exist_ok=True)
    for p in a.files:
        x = load(p); pk, att, peaks, clicks = analyze(x)
        line = f"{os.path.basename(p)}: dur {len(x)/SR:.2f}s pico {pk/SR:.3f}s ataque {att/SR:.3f}s picos>50% {peaks} cliques {clicks:.0f}"
        if a.out:
            s = max(0, min(att, pk) - int(a.pre * SR)); e = min(len(x), pk + int(a.post * SR)); y = x[s:e].copy()
            f = min(len(y), int(0.08 * SR)); y[-f:] *= np.linspace(1, 0, f)
            y *= 10 ** (-1 / 20) / (np.abs(y).max() + 1e-9)
            o = os.path.join(a.out, os.path.splitext(os.path.basename(p))[0] + '.wav')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', '-', o],
                           input=y.astype(np.float32).tobytes(), check=True)
            line += f" -> {o} (pico em {(pk-s)/SR:.3f}s do arquivo aparado)"
        print(line)

if __name__ == '__main__':
    main()
