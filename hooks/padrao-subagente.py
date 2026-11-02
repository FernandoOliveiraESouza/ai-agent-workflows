#!/usr/bin/env python3
"""SubagentStart (matcher Explore|Plan): os subagentes Explore e Plan não recebem o CLAUDE.md, então não veem o
kernel. Este hook injeta neles a seção 8 do kernel ($AGENT_KERNEL, padrão ~/.claude/kernel/KERNEL.md: padrão de
engenharia), lida na hora para nunca ficar desatualizada. Se o principal sumir, não injeta nada (o hook de início de sessão já avisa)."""
import json
import os
import pathlib
import sys

KERNEL = pathlib.Path(os.path.expanduser(os.environ.get('AGENT_KERNEL', '~/.claude/kernel/KERNEL.md')))
CHECKLIST = os.environ.get('AGENT_CHECKLIST', '~/.claude/kernel/codigo-checklist.md')
LIMITE = 9500  # o Claude Code aceita até 10.000 caracteres de additionalContext

try:
    tipo = json.load(sys.stdin).get('agent_type')
except (ValueError, AttributeError):
    tipo = None
if tipo is not None and tipo not in ('Explore', 'Plan'):   # o matcher já filtra; segunda barreira
    sys.exit(0)

try:
    texto = KERNEL.read_text(encoding='utf-8')
except OSError:
    sys.exit(0)
inicio = texto.find('## 8. Código: padrão de engenharia')
if inicio >= 0:
    secao = texto[inicio:]
else:   # o título mudou: não injetar nada calaria o padrão sem ninguém saber
    print('padrao-subagente: título da seção 8 não encontrado no principal', file=sys.stderr)
    secao = ('(Este hook não achou a seção 8 pelo título. Leia a seção "Código: padrão de engenharia" de '
             f'{KERNEL} antes de buscar, analisar ou planejar código.)')

aviso = ('Você não recebe o CLAUDE.md do usuário. Ao buscar, analisar ou planejar código, siga este trecho do '
         f'arquivo principal dele ({KERNEL}); o checklist completo está em {CHECKLIST}.\n\n')
saida = {'hookSpecificOutput': {'hookEventName': 'SubagentStart', 'additionalContext': (aviso + secao)[:LIMITE]}}
sys.stdout.buffer.write(json.dumps(saida, ensure_ascii=False).encode('utf-8'))   # UTF-8 em qualquer locale
