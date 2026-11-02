#!/usr/bin/env python3
"""Coletor do revisor de código: o que mudou, o que os analisadores dizem das linhas mudadas,
as regras do projeto e o que já foi aprendido. Só lê o repositório; nada sai da máquina.

  revisar.py coletar [--base REF | --commit SHA | --range A..B | --pr N | --tudo CAMINHO...]
                     [--repo DIR] --out DIR [--sem-clippy] [--sem-semgrep]
  revisar.py aprender --repo DIR "texto da lição"
  revisar.py guardar --achados DIR/achados.json     (histórico para a re-revisão)
  revisar.py limpar --out DIR                               (remove a árvore temporária de --pr/--commit)

Saídas em --out: resumo.md (leia primeiro), alvo.json, diff.patch, estaticos.json.
Códigos: 0 ok · 2 uso · 3 ambiente (não é repositório git, alvo vazio).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
VENV = Path.home() / ".local/opt/revisor-venv/bin"
DADOS = Path.home() / ".local/share/revisor"
REGRAS = ["AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "docs/contributing.md", ".revisor.md",
          ".github/copilot-instructions.md", "docs/testing.md"]
LINGUAS = {
    ".rs": "rust", ".py": "python", ".sh": "shell", ".bash": "shell", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".go": "go", ".java": "java", ".kt": "kotlin", ".swift": "swift",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".lua": "lua", ".wgsl": "wgsl", ".glsl": "glsl",
    ".html": "html", ".css": "css", ".sql": "sql", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".md": "markdown",
}
# segredos em linhas adicionadas (padrões públicos e conservadores)
SEGREDOS = [
    ("chave AWS", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("token GitHub", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("chave Anthropic/OpenAI", re.compile(r"sk-(ant-)?[A-Za-z0-9_-]{20,}")),
    ("chave Google", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("token Slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("chave privada", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("senha literal", re.compile(r"(?i)(password|passwd|senha|secret|api[_-]?key)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")),
]


def sh(cmd, cwd, check=True, timeout=None, env=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.strip()[:400]}")
    return r


def slug(repo):
    url = sh(["git", "remote", "get-url", "origin"], repo, check=False).stdout.strip()
    base = url.rstrip("/").removesuffix(".git").split("/")[-1] if url else Path(repo).name
    return re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "repo"


# ---------- alvo ----------

def diff_do_alvo(a, repo):
    """(descrição, base, cabeça, texto do diff) do que vai ser revisado."""
    ctx = ["-U12", "--no-color", "--no-ext-diff", "-M"]
    if a.pr:
        num = str(a.pr)
        info = json.loads(sh(["gh", "pr", "view", num, "--json", "baseRefName,headRefOid,title,url"], repo).stdout)
        # a cabeça do PR fica numa ref local, sem mexer na árvore de trabalho
        ref = f"refs/revisor/pr-{num}"
        r = sh(["git", "fetch", "-q", "origin", f"pull/{num}/head:{ref}"], repo, check=False)
        if r.returncode != 0:
            up = "upstream" if "upstream" in sh(["git", "remote"], repo).stdout.split() else "origin"
            sh(["git", "fetch", "-q", up, f"pull/{num}/head:{ref}"], repo)
        base = sh(["git", "merge-base", ref, f"origin/{info['baseRefName']}"], repo, check=False).stdout.strip()
        if not base:
            base = sh(["git", "merge-base", ref, f"upstream/{info['baseRefName']}"], repo).stdout.strip()
        return f"PR #{num}: {info['title']} ({info['url']})", base, ref, sh(["git", "diff", *ctx, base, ref], repo).stdout
    if a.range:
        b, _, h = a.range.partition("..")
        h = h.lstrip(".") or "HEAD"
        return f"intervalo {a.range}", b, h, sh(["git", "diff", *ctx, b, h], repo).stdout
    if a.commit:
        return f"commit {a.commit}", f"{a.commit}~1", a.commit, sh(["git", "diff", *ctx, f"{a.commit}~1", a.commit], repo).stdout
    if a.base:
        base = sh(["git", "merge-base", a.base, "HEAD"], repo).stdout.strip()
        return f"branch atual contra {a.base}", base, "HEAD", sh(["git", "diff", *ctx, base, "HEAD"], repo).stdout
    if a.tudo:
        return f"arquivos inteiros: {' '.join(a.tudo)}", None, None, ""
    # padrão: o que ainda não foi commitado (inclusive arquivos novos)
    texto = sh(["git", "diff", *ctx, "HEAD"], repo).stdout
    # arquivos novos ainda fora do git entram como criados, sem mexer no índice
    for p in sh(["git", "ls-files", "--others", "--exclude-standard", "-z"], repo).stdout.split("\0"):
        if p and (Path(repo) / p).is_file() and (Path(repo) / p).stat().st_size < 2_000_000:
            novo = sh(["git", "diff", *ctx, "--no-index", "--", "/dev/null", p], repo, check=False).stdout
            texto += novo
    return "mudanças não commitadas", "HEAD", None, texto


def arquivos_do_diff(texto):
    """[{caminho, status, linhas: [[ini, fim], ...] das linhas novas/alteradas}]"""
    arqs, atual, nova = [], None, 0
    for ln in texto.splitlines():
        if ln.startswith("diff --git "):
            atual = {"caminho": None, "status": "modificado", "linhas": [], "adicionadas": 0, "removidas": 0}
            arqs.append(atual)
        elif atual is None:
            continue
        elif ln.startswith("new file"):
            atual["status"] = "novo"
        elif ln.startswith("deleted file"):
            atual["status"] = "apagado"
        elif ln.startswith("rename to "):
            atual["status"] = "renomeado"
        elif ln.startswith("+++ "):
            p = ln[4:]
            atual["caminho"] = p[2:] if p.startswith("b/") else (None if p == "/dev/null" else p)
        elif ln.startswith("--- ") and atual["caminho"] is None and ln[4:] != "/dev/null":
            atual["caminho_antigo"] = ln[6:]
        elif ln.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
            nova = int(m.group(1)) if m else 0
        elif ln.startswith("+"):
            atual["adicionadas"] += 1
            r = atual["linhas"]
            if r and r[-1][1] == nova - 1:
                r[-1][1] = nova
            else:
                r.append([nova, nova])
            nova += 1
        elif ln.startswith("-"):
            atual["removidas"] += 1
        else:
            nova += 1
    for f in arqs:
        if f["caminho"] is None:
            f["caminho"] = f.get("caminho_antigo")
        f["lingua"] = LINGUAS.get(Path(f["caminho"] or "").suffix.lower(), "outro")
    return [f for f in arqs if f["caminho"]]


def na_mudanca(arq, linha):
    return any(a - 3 <= linha <= b + 3 for a, b in arq["linhas"])


# ---------- analisadores ----------

def ferramenta(nome):
    p = VENV / nome
    return str(p) if p.exists() else shutil.which(nome)


def rodar_clippy(repo, rust, out):
    """clippy só nos pacotes tocados; achados mapeados para arquivo:linha."""
    pacotes = set()
    for f in rust:
        d = (Path(repo) / f["caminho"]).parent
        while d != Path(repo).parent:
            c = d / "Cargo.toml"
            if c.exists() and re.search(r"(?m)^\[package\]", c.read_text(errors="replace")):
                m = re.search(r'(?m)^name\s*=\s*"([^"]+)"', c.read_text(errors="replace"))
                if m:
                    pacotes.add(m.group(1))
                break
            d = d.parent
    if not pacotes:
        return [], "nenhum pacote"
    cmd = ["nice", "cargo", "clippy", "-j", "8", "--message-format=json", "--all-targets"]
    for p in sorted(pacotes):
        cmd += ["-p", p]
    if shutil.which("systemd-run"):
        cmd = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=26G", "-p", "MemorySwapMax=0", *cmd]
    r = sh(cmd, repo, check=False, timeout=3600)
    achados = []
    for ln in r.stdout.splitlines():
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        msg = m.get("message") if m.get("reason") == "compiler-message" else None
        if not msg or msg.get("level") not in ("warning", "error"):
            continue
        sp = next((s for s in msg.get("spans", []) if s.get("is_primary")), None)
        if not sp:
            continue
        achados.append({"ferramenta": "clippy", "arquivo": sp["file_name"], "linha": sp["line_start"],
                        "regra": (msg.get("code") or {}).get("code", ""), "nivel": msg["level"],
                        "mensagem": msg["message"]})
    return achados, f"pacotes {', '.join(sorted(pacotes))}"


def rodar_ruff(repo, py):
    exe = ferramenta("ruff")
    if not exe or not py:
        return [], "sem ruff" if py else "sem arquivos"
    # erros reais (F), bugs prováveis (B), sintaxe (E9) e segurança (S); estilo fica de fora
    r = sh([exe, "check", "--no-cache", "--output-format", "json", "--select", "F,B,E9,S,PLE",
            *[f["caminho"] for f in py]], repo, check=False)
    try:
        dados = json.loads(r.stdout or "[]")
    except ValueError:
        return [], "saída ilegível"
    return [{"ferramenta": "ruff", "arquivo": os.path.relpath(d["filename"], repo), "linha": d["location"]["row"],
             "regra": d.get("code") or "", "nivel": "warning", "mensagem": d["message"]} for d in dados], "ok"


def rodar_shellcheck(repo, shs):
    exe = ferramenta("shellcheck")
    if not exe or not shs:
        return [], "sem shellcheck" if shs else "sem arquivos"
    r = sh([exe, "-f", "json", "-S", "warning", *[f["caminho"] for f in shs]], repo, check=False)
    try:
        dados = json.loads(r.stdout or "[]")
    except ValueError:
        return [], "saída ilegível"
    return [{"ferramenta": "shellcheck", "arquivo": d["file"], "linha": d["line"], "regra": f"SC{d['code']}",
             "nivel": d["level"], "mensagem": d["message"]} for d in dados], "ok"


def rodar_semgrep(repo, arqs):
    exe = ferramenta("semgrep")
    alvos = [f["caminho"] for f in arqs if f["lingua"] not in ("markdown", "outro", "json", "toml")]
    if not exe or not alvos:
        return [], "sem semgrep" if alvos else "sem arquivos"
    # regras baixadas do registro público; o código não sai da máquina (--metrics off)
    r = sh([exe, "scan", "--metrics", "off", "--quiet", "--json", "--config", "p/default", "--config", "p/secrets",
            *alvos], repo, check=False, timeout=1800)
    try:
        dados = json.loads(r.stdout or "{}")
    except ValueError:
        return [], "saída ilegível"
    return [{"ferramenta": "semgrep", "arquivo": d["path"], "linha": d["start"]["line"],
             "regra": d["check_id"].split(".")[-1], "nivel": d["extra"].get("severity", "").lower(),
             "mensagem": d["extra"].get("message", "")[:400]} for d in dados.get("results", [])], "ok"


def procurar_segredos(texto):
    achados, arq, nova = [], None, 0
    for ln in texto.splitlines():
        if ln.startswith("+++ "):
            arq = ln[6:]
        elif ln.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
            nova = int(m.group(1)) if m else 0
        elif ln.startswith("+"):
            for nome, rx in SEGREDOS:
                if rx.search(ln):
                    achados.append({"ferramenta": "segredos", "arquivo": arq, "linha": nova, "regra": nome,
                                    "nivel": "error", "mensagem": f"{nome} numa linha adicionada"})
            nova += 1
        elif not ln.startswith("-"):
            nova += 1
    return achados


# ---------- comandos ----------

def coletar(a):
    repo = sh(["git", "rev-parse", "--show-toplevel"], a.repo or os.getcwd(), check=False).stdout.strip()
    if not repo:
        print("não é um repositório git", file=sys.stderr)
        return 3
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    desc, base, cabeca, texto = diff_do_alvo(a, repo)
    if a.tudo:
        arqs = []
        for alvo in a.tudo:
            for p in sh(["git", "ls-files", "--", alvo], repo).stdout.split():
                n = len((Path(repo) / p).read_text(errors="replace").splitlines()) if (Path(repo) / p).is_file() else 0
                arqs.append({"caminho": p, "status": "inteiro", "linhas": [[1, max(n, 1)]], "adicionadas": n,
                             "removidas": 0, "lingua": LINGUAS.get(Path(p).suffix.lower(), "outro")})
    else:
        arqs = arquivos_do_diff(texto)
    if not arqs:
        print("nada para revisar nesse alvo", file=sys.stderr)
        return 3
    (out / "diff.patch").write_text(texto)
    # com --pr/--commit/--range a árvore pode não estar na cabeça: os analisadores leem a cabeça certa
    trabalho = repo
    if cabeca and cabeca != "HEAD" and sh(["git", "rev-parse", cabeca], repo).stdout.strip() != \
            sh(["git", "rev-parse", "HEAD"], repo).stdout.strip():
        trabalho = str(out / "arvore")
        if Path(trabalho).exists():
            sh(["git", "worktree", "remove", "--force", trabalho], repo, check=False)
            shutil.rmtree(trabalho, ignore_errors=True)
        sh(["git", "worktree", "add", "-q", "--detach", trabalho, cabeca], repo)
    vivos = [f for f in arqs if f["status"] != "apagado"]
    por = lambda *ls: [f for f in vivos if f["lingua"] in ls]
    est, notas = [], {}
    for nome, fn in [("ruff", lambda: rodar_ruff(trabalho, por("python"))),
                     ("shellcheck", lambda: rodar_shellcheck(trabalho, por("shell"))),
                     ("semgrep", lambda: ([], "desligado") if a.sem_semgrep else rodar_semgrep(trabalho, vivos)),
                     ("clippy", lambda: ([], "desligado") if a.sem_clippy or not por("rust") else rodar_clippy(trabalho, por("rust"), out))]:
        try:
            achados, nota = fn()
        except Exception as e:  # um analisador quebrado não impede a revisão
            achados, nota = [], f"falhou: {e}"[:300]
        est += achados
        notas[nome] = f"{nota} ({len(achados)} achados)"
    est += procurar_segredos(texto)
    mapa = {f["caminho"]: f for f in arqs}
    for e in est:
        f = mapa.get(e["arquivo"])
        e["na_mudanca"] = bool(f and na_mudanca(f, e["linha"]))
    est = [e for e in est if e["arquivo"] in mapa]
    (out / "estaticos.json").write_text(json.dumps(est, ensure_ascii=False, indent=1))
    s = slug(repo)
    regras = [r for r in REGRAS if (Path(trabalho) / r).is_file()]
    licoes = DADOS / s / "aprendizados.md"
    branch = sh(["git", "branch", "--show-current"], repo, check=False).stdout.strip() or "detached"
    hist = DADOS / s / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', a.pr and f'pr-{a.pr}' or branch)}.json"
    alvo = {"repo": repo, "arvore": trabalho, "slug": s, "descricao": desc, "base": base, "cabeca": cabeca,
            "arquivos": arqs, "regras": regras, "aprendizados": str(licoes) if licoes.exists() else None,
            "historico": str(hist) if hist.exists() else None, "historico_destino": str(hist),
            "analisadores": notas, "coletado": datetime.datetime.now().isoformat(timespec="seconds")}
    (out / "alvo.json").write_text(json.dumps(alvo, ensure_ascii=False, indent=1))
    add = sum(f["adicionadas"] for f in arqs)
    rem = sum(f["removidas"] for f in arqs)
    l = [f"# Revisão: {desc}", "", f"- Repositório: `{repo}` (árvore lida: `{trabalho}`)",
         f"- {len(arqs)} arquivos, +{add} −{rem} linhas. Diff com 12 linhas de contexto: `{out / 'diff.patch'}`",
         f"- Regras do projeto: {', '.join(regras) or 'nenhuma'}",
         f"- Aprendizados: {licoes if licoes.exists() else 'nenhum ainda'}",
         f"- Revisão anterior: {hist if hist.exists() else 'nenhuma'}", "", "## Arquivos", ""]
    for f in arqs:
        faixas = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in f["linhas"][:12])
        l.append(f"- `{f['caminho']}` ({f['lingua']}, {f['status']}, +{f['adicionadas']} −{f['removidas']}): linhas {faixas or '-'}")
    l += ["", "## Analisadores", ""] + [f"- {k}: {v}" for k, v in notas.items()]
    na = [e for e in est if e["na_mudanca"]]
    l += ["", f"## Achados estáticos nas linhas mudadas ({len(na)} de {len(est)})", ""]
    for e in na[:80]:
        l.append(f"- `{e['arquivo']}:{e['linha']}` [{e['ferramenta']} {e['regra']}] {e['mensagem'][:200]}")
    (out / "resumo.md").write_text("\n".join(l) + "\n")
    print(out / "resumo.md")
    return 0


def aprender(a):
    repo = sh(["git", "rev-parse", "--show-toplevel"], a.repo or os.getcwd()).stdout.strip()
    p = DADOS / slug(repo) / "aprendizados.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    novo = not p.exists()
    with p.open("a") as f:
        if novo:
            f.write(f"# Aprendizados do revisor: {slug(repo)}\n\nO que o usuário confirmou ou descartou; vale nas próximas revisões.\n\n")
        f.write(f"- {datetime.date.today().isoformat()}: {a.texto}\n")
    print(p)
    return 0


def limpar(a):
    alvo = json.loads((Path(a.out) / "alvo.json").read_text())
    if alvo["arvore"] != alvo["repo"]:
        sh(["git", "worktree", "remove", "--force", alvo["arvore"]], alvo["repo"], check=False)
    print("limpo")
    return 0


def guardar(a):
    dados = json.loads(Path(a.achados).read_text())
    destino = Path(dados.get("historico_destino") or json.loads((Path(a.achados).parent / "alvo.json").read_text())["historico_destino"])
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(dados, ensure_ascii=False, indent=1))
    print(destino)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coletar")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--base")
    g.add_argument("--commit")
    g.add_argument("--range")
    g.add_argument("--pr", type=int)
    g.add_argument("--tudo", nargs="+", metavar="CAMINHO")
    c.add_argument("--repo")
    c.add_argument("--out", required=True)
    c.add_argument("--sem-clippy", action="store_true")
    c.add_argument("--sem-semgrep", action="store_true")
    ap = sub.add_parser("aprender")
    ap.add_argument("--repo")
    ap.add_argument("texto")
    gu = sub.add_parser("guardar")
    gu.add_argument("--achados", required=True)
    li = sub.add_parser("limpar")
    li.add_argument("--out", required=True)
    a = p.parse_args()
    try:
        return {"coletar": coletar, "aprender": aprender, "guardar": guardar, "limpar": limpar}[a.cmd](a)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
