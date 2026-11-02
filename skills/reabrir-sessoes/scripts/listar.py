#!/usr/bin/env python3
"""Lista as conversas do Claude Code que pararam nas últimas horas e não estão abertas.

uso: python3 -I listar.py [--horas 12]

Mostra, para cada conversa: id, pasta, hora da última gravação, título, último pedido, fim da
última resposta e quantos subagentes gravaram nos 10 min antes do fim (sinal de trabalho em andamento).
No fim, os terminais que fecharam no mesmo período (journal), para separar queda de fechamento normal.
"""
import datetime, glob, json, os, subprocess, sys

HOME = os.path.expanduser("~")
horas = float(sys.argv[sys.argv.index("--horas") + 1]) if "--horas" in sys.argv else 12.0
agora = datetime.datetime.now().timestamp()


def vivas():
    ids = set()
    for f in glob.glob(f"{HOME}/.claude/sessions/*.json"):
        try:
            d = json.load(open(f))
            os.kill(int(d["pid"]), 0)
            ids.add(d["sessionId"])
        except (OSError, ValueError, KeyError):
            pass
    return ids


def texto(conteudo):
    if isinstance(conteudo, str):
        return conteudo
    for x in conteudo or []:
        if isinstance(x, dict) and x.get("type") == "text":
            return x["text"]
    return None


def resumo(caminho):
    r = {"titulo": None, "cwd": None, "pedido": None, "resposta": None}
    with open(caminho, errors="replace") as f:
        for linha in f:
            try:
                d = json.loads(linha)
            except ValueError:
                continue
            if d.get("aiTitle"):
                r["titulo"] = d["aiTitle"]
            if d.get("type") == "custom-title":
                r["titulo"] = d.get("customTitle")
            r["cwd"] = d.get("cwd") or r["cwd"]
            if d.get("isSidechain") or d.get("isMeta"):
                continue
            t = texto((d.get("message") or {}).get("content")) if isinstance(d.get("message"), dict) else None
            if (t and d.get("type") == "user" and not t.startswith("<")
                    and not t.startswith("This session is being continued")):
                r["pedido"] = t
            if t and d.get("type") == "assistant":
                r["resposta"] = t
    return r


abertas = vivas()
for caminho in sorted(glob.glob(f"{HOME}/.claude/projects/*/*.jsonl"), key=os.path.getmtime, reverse=True):
    fim = os.path.getmtime(caminho)
    if agora - fim > horas * 3600:
        break
    sid = os.path.basename(caminho)[:-6]
    if sid in abertas:
        continue
    r = resumo(caminho)
    subs = [s for s in glob.glob(f"{caminho[:-6]}/subagents/*.jsonl") if fim - os.path.getmtime(s) < 600]
    def linha(s, n): return (s or "").replace("\n", " ")[:n]
    print("=" * 90)
    print(f"{sid}  {datetime.datetime.fromtimestamp(fim):%m-%d %H:%M}  pasta: {r['cwd']}")
    print(f"  título: {r['titulo']}  |  subagentes ativos no fim: {len(subs)}")
    print(f"  último pedido: {linha(r['pedido'], 200)}")
    print(f"  fim da resposta: {linha((r['resposta'] or '')[-300:], 300)}")

desde = datetime.datetime.fromtimestamp(agora - horas * 3600).strftime("%Y-%m-%d %H:%M")
print("=" * 90)
print("Terminais fechados no período (vte-spawn = uma aba; server com FAILURE = queda):")
j = subprocess.run(["journalctl", "--user", "--since", desde, "--no-pager", "-o", "short-iso"],
                   capture_output=True, text=True).stdout
for l in j.splitlines():
    if ("vte-spawn" in l and "Consumed" in l) or ("gnome-terminal-server.service" in l and
                                                   ("exited" in l or "Consumed" in l)):
        print("  " + l[:150])
