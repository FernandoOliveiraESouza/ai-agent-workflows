#!/usr/bin/env python3
"""PreToolUse do portão de qualidade (seção 8 do principal, ~/.claude/kernel/KERNEL.md). Pede o ok do USUÁRIO antes de:
  - comando git que apaga trabalho, mistura arquivos de outros agentes ou reescreve histórico
    (add -A/./-u, commit -a, reset --hard, checkout/restore que descarta, clean, stash, push --force, rebase,
    branch -D, --no-verify, filter-branch, worktree remove --force...), também dentro de bash -c, eval, $( ), if/for
    e afins, e seguindo o cd do próprio comando;
  - edição que enfraquece teste (marca de pular ou isolar teste a mais, verificações ou testes a menos).
E NEGA, sem perguntar:
  - qualquer escrita numa pasta que só se executa (repositório de ferramenta baixado só para usar; lista em so-executar.txt);
  - a entrega do subagente pelo SubagentHandback quando ele mudou código sem verificar (a mesma checagem do
    final.py; ali o Claude Code descarta o bloqueio do SubagentStop).
Falha aberta: um erro inesperado deixa a ferramenta seguir."""
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))   # append: a pasta não pode sombrear a biblioteca padrão
import comum  # noqa: E402

DECISAO = 'ask'   # o Claude Code mostra a pergunta ao usuário, com o motivo abaixo
OPCOES_GLOBAIS_COM_VALOR = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix',
                            '--config-env'}
GIT_QUE_MEXE = {'add', 'commit', 'reset', 'checkout', 'restore', 'clean', 'stash', 'pull', 'merge', 'rebase', 'apply',
                'am', 'rm', 'mv', 'switch', 'cherry-pick', 'revert', 'submodule', 'config', 'tag', 'branch', 'worktree',
                'gc', 'prune', 'update-ref', 'filter-branch', 'filter-repo', 'reflog', 'init', 'fetch'}
LIMITE_LEITURA = 2_000_000   # Write em arquivo maior que isso não é analisado


def amplo(alvo):
    """O alvo pega a pasta inteira ou mais: ".", "./", "..", ":/", "*"."""
    return alvo in (':/', ':', '*', ':(top)') or re.fullmatch(r'\.\.?(/\.\.)*', os.path.normpath(alvo)) is not None


def git_partes(palavras, pasta=None):
    """(subcomando, argumentos, pasta do repositório, desliga ganchos?) de um git, ou None se não for git."""
    k = comum.indice_comando(palavras)
    if k is None or os.path.basename(palavras[k]) != 'git':
        return None
    resto, j, sem_ganchos = palavras[k + 1:], 0, False
    while j < len(resto) and resto[j].startswith('-'):   # opções globais: git -C pasta -c k=v ...
        op = resto[j]
        valor = resto[j + 1] if j + 1 < len(resto) else ''
        if op == '-C':
            alvo = os.path.expanduser(valor)
            pasta = alvo if os.path.isabs(alvo) else (os.path.join(pasta, alvo) if pasta else None)
        if (op == '-c' and valor.lower().startswith('core.hookspath')) or op.lower().startswith('-ccore.hookspath'):
            sem_ganchos = True
        j += 2 if op in OPCOES_GLOBAIS_COM_VALOR else 1
    if j >= len(resto):
        return None
    return resto[j], resto[j + 1:], pasta, sem_ganchos


def git_perigoso(palavras, cwd=None):
    """Se o comando simples for um git perigoso, devolve o motivo em linguagem simples; senão, None."""
    partes = git_partes(palavras, cwd)
    if not partes:
        return None
    sub, args, pasta, sem_ganchos = partes
    curtas = ''.join(a[1:] for a in args if re.fullmatch(r'-[A-Za-z]+', a))
    alvos = [a for a in args if not a.startswith('-')]
    forca = bool({'-f', '--force'} & set(args)) or 'f' in curtas

    def existe(a):
        return bool(pasta) and os.path.lexists(os.path.join(pasta, os.path.expanduser(a)))

    if sub == 'add' and ({'-A', '--all', '-u', '--update'} & set(args) or 'A' in curtas or 'u' in curtas
                         or any(amplo(a) for a in alvos)):
        return 'adiciona tudo de uma vez e pode levar arquivos de outros agentes; o padrão manda adicionar pelo nome'
    if sub == 'commit' and ('--all' in args or 'a' in curtas):
        return 'faz commit de todas as mudanças da pasta, inclusive as de outros agentes'
    if sub in ('commit', 'push', 'merge', 'rebase', 'am', 'cherry-pick', 'revert', 'pull') and (
            '--no-verify' in args or (sub == 'commit' and 'n' in curtas) or sem_ganchos):
        return 'pula as verificações automáticas do projeto (--no-verify ou core.hooksPath)'
    if sub == 'config' and any(a.lower() == 'core.hookspath' for a in args) \
            and not {'--get', '--get-all', '--list', '-l', '--unset', '--unset-all'} & set(args):
        return 'desliga de vez as verificações automáticas do projeto (core.hooksPath)'
    if sub == 'reset' and {'--hard', '--merge', '--keep'} & set(args):
        return 'apaga mudanças que ainda não foram salvas no git, sem volta'
    if sub == 'checkout':
        if '-B' in args or 'B' in curtas:
            return 'recria a branch do zero e pode perder commits que só estavam nela'
        cria = bool({'-b', '--orphan'} & set(args)) or 'b' in curtas
        if not cria and ('--' in args or forca or any(amplo(a) or existe(a) for a in alvos)):
            return 'descarta mudanças não salvas de arquivos (podem ser de outro agente ou suas)'
    if sub == 'restore':
        so_tira_do_stage = ('--staged' in args or 'S' in curtas) and '--worktree' not in args and 'W' not in curtas
        if not so_tira_do_stage:
            return 'descarta mudanças não salvas de arquivos (podem ser de outro agente ou suas)'
    if sub == 'switch' and ({'--discard-changes', '-C', '--force-create'} & set(args) or forca or 'C' in curtas):
        return 'troca de branch descartando mudanças não salvas, ou recria a branch'
    if sub == 'clean' and not ({'-n', '--dry-run'} & set(args) or 'n' in curtas):
        return 'apaga arquivos que não estão no git, sem volta (inclusive anotações e trabalho de outros agentes)'
    if sub == 'stash' and not (args and args[0] in ('list', 'show')):
        return 'esconde mudanças de todos, inclusive de outros agentes, e é fácil perdê-las'
    if sub == 'push' and ({'--force', '-f', '--force-with-lease', '--force-if-includes', '--mirror', '--delete', '-d',
                           '--prune'} & set(args) or any(a.startswith(('+', ':')) or a.startswith('--force')
                                                         for a in args) or 'f' in curtas):
        return 'reescreve ou apaga histórico (ou branches) no servidor'
    if sub == 'rebase' and not ({'--abort', '--continue', '--skip', '--quit', '--show-current-patch'} & set(args)):
        return 'reescreve histórico; em branch compartilhada atrapalha os outros'
    if sub == 'branch' and ('-D' in args or 'D' in curtas
                            or (({'-d', '--delete'} & set(args) or 'd' in curtas) and forca) or (forca and alvos)):
        return 'apaga ou move uma branch à força, mesmo com trabalho que não foi juntado'
    if sub in ('filter-branch', 'filter-repo'):
        return 'reescreve todo o histórico do repositório'
    if sub == 'reflog' and alvos[:1] in (['expire'], ['delete']):
        return 'apaga o registro que permite recuperar trabalho perdido'
    if sub == 'update-ref' and {'-d', '--delete'} & set(args):
        return 'apaga uma referência do git direto'
    if sub == 'worktree' and alvos[:1] == ['remove'] and forca:   # sem --force o próprio git recusa se houver mudança
        return 'apaga à força uma cópia de trabalho, com o que estiver nela'
    if sub == 'rm' and ('-r' in args or 'r' in curtas) and any(amplo(a) for a in alvos):
        return ('tira do git todos os arquivos da pasta' if '--cached' in args
                else 'tira do git e apaga do disco todos os arquivos da pasta')
    return None


def escreve_em_so_executar(palavras, redir, pasta, raizes, variaveis=None):
    """A pasta só de executar que o comando simples altera (gravando arquivo ou mexendo no git dela), ou None.
    Caminho com variável que não dá para resolver não conta: o portão nega só o que tem certeza."""
    variaveis = variaveis or {}
    for alvo in comum.alvos_de_escrita(palavras, redir):
        caminho = comum.expandir(alvo, variaveis)
        r = comum.dentro_de(caminho, raizes, pasta) if caminho is not None else None
        if r:
            return r
    # No git, basta resolver a pasta do repositório (-C ou a pasta atual): uma mensagem "$MSG" não esconde o resto.
    expandidas = [e if (e := comum.expandir(p, variaveis)) is not None else p for p in palavras]
    partes = git_partes(expandidas, pasta)
    if partes and partes[0] in GIT_QUE_MEXE and partes[2] and not git_so_consulta(partes[0], partes[1]):
        return comum.dentro_de(partes[2], raizes)
    return None


def git_so_consulta(sub, args):
    """O git só lista ou mostra (branch -a, tag, stash list, worktree list, config --get...), sem mudar nada."""
    soltos = [a for a in args if not a.startswith('-')]
    if sub in ('branch', 'tag'):
        return not soltos and not {'-d', '-D', '-m', '-M', '-c', '-C', '-f', '--delete', '--move', '--copy',
                                   '--force', '-u', '--set-upstream-to', '--unset-upstream'} & set(args)
    if sub in ('stash', 'worktree'):
        return soltos[:1] in (['list'], ['show'])
    if sub == 'reflog':
        return not soltos or soltos[0] in ('show', 'exists')
    if sub == 'config':
        return len(soltos) <= 1 and not {'--unset', '--unset-all', '--add', '--replace-all', '--rename-section',
                                         '--remove-section', '-e', '--edit'} & set(args)
    if sub == 'submodule':
        return not soltos or soltos[0] in ('status', 'summary', 'foreach')
    return False


def trechos_editados(nome, entrada):
    """[(antes, depois)] da edição; num Write, o antes é o arquivo atual (o hook roda antes de gravar)."""
    if nome == 'Edit':
        return [(str(entrada.get('old_string') or ''), str(entrada.get('new_string') or ''))]
    if nome == 'MultiEdit':
        return [(str(e.get('old_string') or ''), str(e.get('new_string') or ''))
                for e in entrada.get('edits') or [] if isinstance(e, dict)]
    if nome == 'Write':
        caminho, antes = str(entrada.get('file_path') or ''), ''
        if os.path.lexists(caminho):
            if not os.path.isfile(caminho) or os.path.getsize(caminho) > LIMITE_LEITURA:
                return []   # FIFO, aparelho ou arquivo enorme: não abre (abrir um FIFO travaria o hook)
            try:
                with open(caminho, encoding='utf-8', errors='replace') as f:
                    antes = f.read()
            except OSError:
                pass
        return [(antes, str(entrada.get('content') or ''))]
    return []


def enfraquece_teste(nome, entrada):
    caminho = entrada.get('file_path') or ''
    if not comum.eh_codigo(caminho):
        return None
    pula = verifica = testes = 0
    eh_teste = bool(comum.TESTE_CAMINHO.search(caminho))
    for antes, depois in trechos_editados(nome, entrada):
        a = comum.sem_strings(comum.sem_comentarios(antes, caminho))
        d = comum.sem_strings(comum.sem_comentarios(depois, caminho))
        pula += len(comum.PULA_TESTE.findall(d)) - len(comum.PULA_TESTE.findall(a))
        if eh_teste or comum.TESTE_TRECHO.search(antes + depois):
            verifica += len(comum.VERIFICA_TESTE.findall(d)) - len(comum.VERIFICA_TESTE.findall(a))
            testes += len(comum.TESTE_DEF.findall(d)) - len(comum.TESTE_DEF.findall(a))
    partes = []
    if pula > 0:
        partes.append(f'desliga, pula ou isola {pula} teste(s)')
    if testes < 0:
        partes.append(f'tira {-testes} teste(s)')
    if verifica < 0:
        partes.append(f'tira {-verifica} verificação(ões) de teste')
    if not partes:
        return None
    return (f'Esta edição em {os.path.basename(caminho)} {" e ".join(partes)}. O padrão proíbe enfraquecer teste para '
            'ficar verde. Aprove só se o agente explicou por que o teste estava errado.')


def negar(motivo):
    comum.responder({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                            'permissionDecisionReason': motivo}})


def so_executar(raiz):
    return (f'{raiz} é um repositório que o usuário só executa: nada se escreve nem se '
            'modifica nele. Rode-o normalmente e ponha a saída na pasta do projeto ou do agente.')


def main():
    d = comum.ler_entrada()
    nome, entrada = d.get('tool_name') or '', d.get('tool_input') or {}
    if not isinstance(entrada, dict):
        return
    if nome == 'SubagentHandback':
        comum.checar_entrega(d, str(entrada.get('message') or ''), 'PreToolUse')
        return
    raizes, cwd = comum.raizes_so_executar(), d.get('cwd')
    motivo = None
    if nome == 'Bash':
        for palavras, redir, pasta, variaveis in comum.comandos_com_pasta(str(entrada.get('command') or ''), cwd):
            raiz = escreve_em_so_executar(palavras, redir, pasta, raizes, variaveis) if raizes else None
            if raiz:
                negar(so_executar(raiz))
            m = git_perigoso(palavras, pasta)
            if m and not motivo:
                motivo = (f'Comando git que pede o seu ok pelo padrão (seção 8 do principal): ele {m}. '
                          'Aprove só se você pediu isso ou entende o efeito.')
    elif nome in comum.EDICAO:
        raiz = comum.dentro_de(entrada.get('file_path') or entrada.get('notebook_path'), raizes, cwd)
        if raiz:
            negar(so_executar(raiz))
        if nome != 'NotebookEdit':
            motivo = enfraquece_teste(nome, entrada)
    if motivo:
        comum.responder({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': DECISAO,
                                                'permissionDecisionReason': motivo}})


if __name__ == '__main__':
    try:
        main()
    except Exception as erro:   # falha aberta; o erro fica no modo detalhado do Claude Code
        print(f'portao/guarda: {erro!r}', file=sys.stderr)
    sys.exit(0)
