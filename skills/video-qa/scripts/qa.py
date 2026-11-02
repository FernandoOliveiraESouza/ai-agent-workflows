#!/usr/bin/env python3
"""qa.py <video.mp4> [--guide Y] [--fps-sheet 1] [--out DIR] [--spec WxH@FPS] [--max-dur S]

Checagem de entrega de vídeo em um comando (economiza as checagens manuais repetidas):
  1. formato (codec, resolução, fps, duração) contra --spec / --max-dur
  2. loudness integrado e pico (ebur128 true peak) — alvo −14 LUFS, pico ≤ −1 dBTP
  3. quadros pretos (blackdetect)
  4. quadros congelados (freezedetect) — cartela final estática é esperada
  5. "travada/rewind": quadro com movimento que reaparece quase idêntico 0,3–5 s depois
  6. folha de contato (1 quadro/s por padrão) com linha guia opcional em y=--guide (ex.: 1248 = base das legendas)
Saída: relatório em texto + caminho da folha. Olhe a folha com a ferramenta de imagem.
"""
import argparse, json, os, re, subprocess, sys
import numpy as np

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def probe(f):
    r = run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,codec_name,width,height,r_frame_rate:format=duration',
             '-of', 'json', f])
    return json.loads(r.stdout)

def loudness(f):
    e = run(['ffmpeg', '-hide_banner', '-i', f, '-af', 'ebur128=peak=true', '-f', 'null', '-']).stderr
    i = re.findall(r'I:\s+(-?[\d.]+) LUFS', e); p = re.findall(r'Peak:\s+(-?[\d.]+) dBFS', e)
    return (float(i[-1]) if i else None), (float(p[-1]) if p else None)

def detect(f, filt, key):
    e = run(['ffmpeg', '-hide_banner', '-i', f, '-vf', filt, '-map', '0:v', '-f', 'null', '-']).stderr
    return re.findall(key, e)

def repeats(f, fps):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', f, '-vf', 'scale=32:56,format=gray', '-f', 'rawvideo', '-'],
                         capture_output=True).stdout
    x = np.frombuffer(raw, np.uint8).reshape(-1, 56, 32).astype(np.float32)
    n = len(x); motion = np.r_[0, np.abs(np.diff(x, axis=0)).mean(axis=(1, 2))]
    lo, hi = int(0.3 * fps), int(5 * fps); hits = []
    for i in range(n):
        if motion[i] < 1.0: continue
        for j in range(i + lo, min(n, i + hi)):
            if motion[j] >= 1.0 and np.abs(x[i] - x[j]).mean() < 2.0:
                # rewind de verdade: a imagem mudou entre i e j (não é plano parado contínuo)
                if np.abs(x[i:j] - x[i]).mean(axis=(1, 2)).max() > 6.0:
                    hits.append((i, j))
                break
    out, last = [], None
    for i, j in hits:
        if last and i - last[0] <= 3 and abs((j - i) - (last[1] - last[0])) <= 3:
            last = (i, j); continue
        out.append((round(i / fps, 2), round(j / fps, 2))); last = (i, j)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video'); ap.add_argument('--guide', type=int); ap.add_argument('--fps-sheet', type=float, default=1)
    ap.add_argument('--out', default='/tmp'); ap.add_argument('--spec', default='1080x1920@30'); ap.add_argument('--max-dur', type=float)
    a = ap.parse_args(); f = a.video; ok = True
    pr = probe(f); v = next(s for s in pr['streams'] if s['codec_type'] == 'video')
    au = [s for s in pr['streams'] if s['codec_type'] == 'audio']
    num, den = map(int, v['r_frame_rate'].split('/')); fps = num / den; dur = float(pr['format']['duration'])
    w, h, sf = re.match(r'(\d+)x(\d+)@([\d.]+)', a.spec).groups()
    fmt_ok = v['width'] == int(w) and v['height'] == int(h) and abs(fps - float(sf)) < 0.01 and v['codec_name'] == 'h264'
    if a.max_dur and dur > a.max_dur: fmt_ok = False
    ok &= fmt_ok
    print(f"FORMATO  {'OK ' if fmt_ok else 'FALHA'} {v['codec_name']} {v['width']}x{v['height']} {fps:g} fps, {dur:.2f} s, áudio: {au[0]['codec_name'] if au else 'NENHUM'}")
    if au:
        I, P = loudness(f); l_ok = I is not None and abs(I + 14) <= 0.6 and P is not None and P <= -1.0
        ok &= l_ok; print(f"LOUDNESS {'OK ' if l_ok else 'ATENÇÃO'} {I} LUFS, pico {P} dBTP (alvo −14 / ≤ −1)")
    blk = detect(f, 'blackdetect=d=0.05:pix_th=0.10', r'black_start:([\d.]+)')
    ok &= not blk; print(f"PRETO    {'OK ' if not blk else 'FALHA'} {blk[:10]}")
    frz = detect(f, 'freezedetect=n=0.003:d=0.25', r'freeze_start: ([\d.]+)')
    print(f"CONGELA  {'OK ' if not frz else 'VER'} {[round(float(t),2) for t in frz[:10]]} (cartela final parada é normal)")
    rep = repeats(f, fps); ok &= not rep
    print(f"REPETE   {'OK ' if not rep else 'FALHA'} {rep[:10]} (pares tempo → mesmo quadro de novo; parece travada)")
    os.makedirs(a.out, exist_ok=True)
    sheet = os.path.join(a.out, os.path.splitext(os.path.basename(f))[0] + '_sheet.png')
    n = int(dur * a.fps_sheet) + 1; cols = 10; rows = (n + cols - 1) // cols
    vf = f"fps={a.fps_sheet}"
    if a.guide: vf += f",drawbox=y={a.guide}:w=iw:h=3:color=red"
    vf += f",scale=216:-2,drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='%{{pts\\:flt}}':x=4:y=4:fontsize=16:fontcolor=yellow:box=1:boxcolor=black,tile={cols}x{rows}:padding=3"
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', f, '-vf', vf, '-frames:v', '1', sheet])
    print(f"FOLHA    {sheet}  (olhe: legendas na mesma linha, nada sobre o produto, clipe certo)")
    print('RESULTADO', 'PASSOU' if ok else 'TEM PROBLEMA')
    sys.exit(0 if ok else 1)

if __name__ == '__main__':
    main()
