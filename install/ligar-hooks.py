"""Liga (ou desliga) no ~/.claude/settings.json os hooks deste repositório. Rode você mesmo, no terminal (o modo
automático do Claude Code não deixa o agente editar a própria configuração):
    python3 -I install/ligar-hooks.py                      (liga o checklist e o hook dos subagentes Explore/Plan)
    python3 -I install/ligar-hooks.py --portao             (liga também o portão de qualidade)
    python3 -I install/ligar-hooks.py --desligar           (desliga tudo daqui, menos o hook dos subagentes)
    python3 -I install/ligar-hooks.py --desligar --portao  (desliga só o portão)
Os hooks precisam já estar copiados em ~/.claude/hooks/. Faz backup em ~/.claude/backups/, não duplica o que já está
ligado, não mexe em nenhum outro hook e grava de forma atômica (temporário + fsync + rename).
Todo comando termina em `|| true`: se o arquivo do hook sumir, o python sai com 2, e no PreToolUse isso bloquearia
todas as ferramentas de todas as sessões."""
import json
import os
import shutil
import sys
import tempfile
import time

SETTINGS = os.path.expanduser('~/.claude/settings.json')
SUBAGENTE = '.claude/hooks/padrao-subagente.py'
CHECKLIST = '.claude/hooks/checklist-codigo.py'
GUARDA = '.claude/hooks/portao/guarda.py'
FINAL = '.claude/hooks/portao/final.py'
# (evento, matcher, script, timeout em segundos)
HOOKS_PADRAO = [
    ('SubagentStart', 'Explore|Plan', SUBAGENTE, 10),
    ('PostToolUse', 'Read|Edit|Write|MultiEdit|NotebookEdit', CHECKLIST, 10),
    ('SessionStart', 'compact|clear', CHECKLIST, 10),
]
HOOKS_PORTAO = [
    ('PreToolUse', 'Bash|Edit|Write|MultiEdit|NotebookEdit|SubagentHandback', GUARDA, 10),
    ('Stop', None, FINAL, 30),
    ('SubagentStop', None, FINAL, 30),
]


def comando(script):
    return f'python3 -I "$HOME/{script}" || true'


def do_script(h, script):
    return f'$HOME/{script}"' in (h.get('command') or '')


def main():
    args = [a.replace('--portão', '--portao') for a in sys.argv[1:]]   # aceita com ou sem til
    desconhecidos = set(args) - {'--portao', '--desligar'}
    if desconhecidos:
        sys.exit(f'Opção desconhecida: {" ".join(sorted(desconhecidos))}; nada foi mudado.')
    desligar, portao = '--desligar' in args, '--portao' in args
    if desligar:
        alvo = HOOKS_PORTAO if portao else HOOKS_PADRAO + HOOKS_PORTAO
    else:
        alvo = HOOKS_PADRAO + (HOOKS_PORTAO if portao else [])
    for _, _, script, _ in alvo:   # não liga hook cujo arquivo não existe
        if not desligar and not os.path.isfile(os.path.expanduser('~/' + script)):
            sys.exit(f'Não encontrei ~/{script}; nada foi mudado.')
    with open(SETTINGS, encoding='utf-8') as f:
        d = json.load(f)
    hooks = d.setdefault('hooks', {})
    mudou = []
    if desligar:
        scripts = {s for _, _, s, _ in alvo if s != SUBAGENTE}
        for evento in list(hooks):
            grupos = []
            for g in hooks[evento]:
                resto = [h for h in g.get('hooks', []) if not any(do_script(h, s) for s in scripts)]
                if len(resto) != len(g.get('hooks', [])):
                    mudou.append(f'desligado: {evento} {g.get("matcher", "")}'.strip())
                if resto:
                    grupos.append(dict(g, hooks=resto))
            if grupos:
                hooks[evento] = grupos
            else:
                del hooks[evento]
    else:
        for evento, matcher, script, timeout in alvo:
            # já ligado (com qualquer matcher): acerta matcher, comando (o `|| true`) e timeout, sem duplicar
            grupos = [g for g in hooks.get(evento, []) if any(do_script(h, script) for h in g.get('hooks', []))]
            for g in grupos:
                mexeu = False
                if g.get('matcher') != matcher:
                    if matcher:
                        g['matcher'] = matcher
                    else:
                        g.pop('matcher', None)
                    mexeu = True
                for h in g.get('hooks', []):
                    if do_script(h, script) and (h.get('command') != comando(script) or h.get('timeout') != timeout):
                        h.update(type='command', command=comando(script), timeout=timeout)
                        mexeu = True
                if mexeu:
                    mudou.append(f'ajustado: {evento} {matcher or ""}'.strip())
            if grupos:
                continue
            grupo = {'hooks': [{'type': 'command', 'command': comando(script), 'timeout': timeout}]}
            if matcher:
                grupo = {'matcher': matcher, **grupo}
            hooks.setdefault(evento, []).append(grupo)
            mudou.append(f'ligado: {evento} {matcher or ""}'.strip())
    if not mudou:
        print('Nada a fazer: já estava assim.')
        return
    copia = os.path.expanduser(f'~/.claude/backups/settings.json.{time.strftime("%Y-%m-%d-%H%M%S")}-{os.getpid()}'
                               f'.antes-{"desligar" if desligar else "ligar"}-hooks')
    os.makedirs(os.path.dirname(copia), exist_ok=True)
    shutil.copy2(SETTINGS, copia)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SETTINGS), prefix='.settings.', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(d, indent=2, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, os.stat(SETTINGS).st_mode & 0o777)
        with open(tmp, encoding='utf-8') as f:
            json.load(f)   # só troca o arquivo se o novo for JSON válido
        os.replace(tmp, SETTINGS)
    except BaseException:
        os.unlink(tmp)
        raise
    print('\n'.join(mudou))
    print(f'Backup do settings.json anterior: {copia}')
    print('Vale para as próximas sessões (e para esta, se o Claude Code recarregar a configuração).')


main()
