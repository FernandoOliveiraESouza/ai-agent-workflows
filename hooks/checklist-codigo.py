#!/usr/bin/env python3
"""PostToolUse (Read|Edit|Write|MultiEdit|NotebookEdit) e SessionStart (compact|clear): leva o checklist de engenharia
($AGENT_CHECKLIST, padrão ~/.claude/kernel/codigo-checklist.md) a quem abre ou edita código FORA da pasta onde a
sessão começou.
Dentro dela, quem injeta é a regra ~/.claude/rules/engenharia-checklist.md (link para o checklist); mas o Claude Code
(2.1.309) só aplica regra com `paths:` a arquivo dentro da pasta inicial, e o código dos projetos costuma ficar fora
da pasta onde as sessões começam.
Injeta uma vez por contexto (sessão principal ou cada subagente) e de novo depois de compactar ou limpar.
Os padrões de arquivo de código vêm do `paths:` do próprio checklist, para não existirem duas listas.
Falha aberta: qualquer erro só deixa de injetar."""
import fnmatch
import json
import os
import pathlib
import re
import stat
import sys
import tempfile

CHECKLIST = pathlib.Path(os.path.expanduser(os.environ.get('AGENT_CHECKLIST', '~/.claude/kernel/codigo-checklist.md')))
KERNEL = os.environ.get('AGENT_KERNEL', '~/.claude/kernel/KERNEL.md')
LIMITE = 9500  # o Claude Code troca por uma prévia a saída de hook com mais de 10.000 caracteres
CAMPO_ARQUIVO = {'Read': 'file_path', 'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path',
                 'NotebookEdit': 'notebook_path'}


def pasta_marcas():
    """Pasta privada das marcas de "já injetado neste contexto", ou None se não for nossa."""
    base = os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir()
    pasta = os.path.join(base, f'claude-checklist-{os.getuid()}')
    try:
        os.mkdir(pasta, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(pasta)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return None
    return pasta


def limpo(texto):
    """Id seguro para nome de arquivo (o id vem do Claude Code, mas nunca vira caminho sem passar por aqui)."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', texto)[:100] if isinstance(texto, str) else ''


def padroes(texto):
    """Nomes de arquivo de código pelo `paths:` do checklist: "**/*.{c,h}" vira "*.c" e "*.h"."""
    fm = re.match(r'---\n(.*?)\n---\n', texto, re.S)
    saida, dentro = [], False
    for linha in (fm.group(1).splitlines() if fm else []):
        if re.match(r'paths\s*:\s*$', linha):
            dentro = True
            continue
        item = re.match(r'\s+-\s*["\']?(.+?)["\']?\s*$', linha) if dentro else None
        if not item:
            dentro = False
            continue
        nome = item.group(1).split('/')[-1]
        chaves = re.search(r'\{([^{}]*)\}', nome)
        if chaves:
            saida += [nome[:chaves.start()] + op + nome[chaves.end():] for op in chaves.group(1).split(',')]
        else:
            saida.append(nome)
    return saida


def main():
    d = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace'))   # UTF-8 em qualquer locale
    if not isinstance(d, dict):
        return
    sessao = limpo(d.get('session_id'))
    evento = d.get('hook_event_name')
    if not sessao or evento not in ('SessionStart', 'PostToolUse'):
        return
    pasta = pasta_marcas()
    if pasta is None:
        return
    if evento == 'SessionStart':   # compactou ou limpou: o checklist saiu do contexto
        for nome in os.listdir(pasta):
            if nome.startswith(sessao + '--'):
                try:
                    os.unlink(os.path.join(pasta, nome))
                except FileNotFoundError:
                    pass
        return

    entrada = d.get('tool_input')
    campo = CAMPO_ARQUIVO.get(d.get('tool_name'))
    arquivo = entrada.get(campo) if campo and isinstance(entrada, dict) else None
    cwd = d.get('cwd')
    if not isinstance(arquivo, str) or not arquivo or not isinstance(cwd, str) or not cwd:
        return
    caminho = os.path.normpath(os.path.join(cwd, os.path.expanduser(arquivo)))
    relativo = os.path.relpath(caminho, cwd)
    if not (relativo == '..' or relativo.startswith('..' + os.sep) or os.path.isabs(relativo)):
        return   # dentro da pasta da sessão: a regra do Claude Code já injeta

    texto = CHECKLIST.read_text(encoding='utf-8')
    nome = os.path.basename(caminho)
    if not any(fnmatch.fnmatchcase(nome, p) for p in padroes(texto)):
        return
    marca = os.path.join(pasta, f'{sessao}--{limpo(d.get("agent_id")) or "principal"}')
    try:
        os.close(os.open(marca, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return   # já injetado neste contexto
    corpo = re.sub(r'^---\n.*?\n---\n', '', texto, count=1, flags=re.S).strip()
    aviso = (f'Você abriu ou editou código fora da pasta da sessão ({caminho}). Siga este checklist do principal do '
             f'usuário ({CHECKLIST}), junto com a seção 8 do {KERNEL}.\n\n')
    saida = {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': (aviso + corpo)[:LIMITE]}}
    sys.stdout.buffer.write(json.dumps(saida, ensure_ascii=False).encode('utf-8'))


try:
    main()
except Exception as erro:   # falha aberta: o hook nunca atrapalha a ferramenta; o erro fica no modo detalhado
    print(f'checklist-codigo: {erro!r}', file=sys.stderr)
