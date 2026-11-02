#!/usr/bin/env python3
"""Stop e SubagentStop do portão de qualidade (seção 8 do principal, ~/.claude/kernel/KERNEL.md).
Se o agente mudou código neste pedido (ou, num subagente, na conversa dele), inclusive pelo Bash (sed -i, cat >,
tee, cp, patch), ele só termina depois de:
  1. rodar build, testes, lint ou o próprio programa DEPOIS da última mudança, ou escrever na resposta final
     "Sem verificação automática: <como verificou de verdade>";
  2. em mudança grande (mais de 300 linhas) ou sensível (login, senha, token, pagamento, cripto, segurança),
     passar por revisão independente (/revisar, /code-review, /security-review ou agente revisor), ou escrever
     "Sem revisão independente: <motivo>".
Depois de 3 bloqueios sem mudança nova no código, solta o agente e avisa o usuário (o Claude Code só soltaria no 9º,
e qualquer ferramenta zera a conta dele). O subagente que entrega pelo SubagentHandback é checado no guarda.py.
Falha aberta: qualquer erro deixa o agente terminar."""
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))   # append: a pasta não pode sombrear a biblioteca padrão
import comum  # noqa: E402


def main():
    d = comum.ler_entrada()
    evento = d.get('hook_event_name')
    if evento == 'Stop' and d.get('prompt_id'):   # sem prompt_id, não dá para saber o que é deste pedido
        comum.checar_entrega(d, str(d.get('last_assistant_message') or ''), 'Stop')
    elif evento == 'SubagentStop':
        comum.checar_entrega(d, str(d.get('last_assistant_message') or ''), 'SubagentStop')


if __name__ == '__main__':
    try:
        main()
    except Exception as erro:   # falha aberta: um erro do portão nunca prende o agente
        print(f'portao/final: {erro!r}', file=sys.stderr)
    sys.exit(0)
