---
name: reabrir-sessoes
allowed-tools: Bash(python3 -I ~/.claude/skills/reabrir-sessoes/scripts/listar.py:*), Bash(~/.claude/skills/reabrir-sessoes/scripts/abrir.sh:*)
description: Reabre os terminais com as conversas do Claude que estavam abertas antes de uma queda (área de trabalho reiniciou, terminal fechou, PC travou), cada uma na sua conversa e retomando o trabalho. Use quando o usuário pedir "reabra os terminais", "volte a fazer tudo que estava fazendo", "o pc travou", "fechou tudo", ou quando ele disser que os terminais do Claude sumiram.
---

# Reabrir as sessões do Claude depois de uma queda

Fale em português do Brasil, curto. Criada em 2026-10-24 (segunda vez que o pedido apareceu).

## 1. Descobrir o que caiu
```
python3 -I ~/.claude/skills/reabrir-sessoes/scripts/listar.py --horas 12
```
Lista as conversas paradas que não estão abertas (as vivas, inclusive esta, ficam de fora) e, no fim, os
terminais que fecharam no período, pelo journal.
- **Caiu junto:** a última gravação bate com um `gnome-terminal-server ... FAILURE` (ou com o fim da sessão
  gráfica, ou com o boot depois de um travamento). Essas são reabertas.
- **Fechada antes pelo usuário:** um `vte-spawn ... Consumed` isolado, sem falha, em outra hora. Não reabra
  sozinho: diga ao usuário quais são e o que estava em andamento, e pergunte.
- Se o PC travou e reiniciou (`uptime`), o journal do boot anterior sai com `journalctl --user -b -1`.

## 2. Reabrir, uma janela por conversa
```
~/.claude/skills/reabrir-sessoes/scripts/abrir.sh <session-id> "<mensagem inicial>"   # estava trabalhando
~/.claude/skills/reabrir-sessoes/scripts/abrir.sh <session-id>                        # estava parada
```
- **Nunca** chame `gnome-terminal ... claude --resume` direto daqui: a conversa herda `CLAUDE_CODE_CHILD_SESSION`
  e roda com "Transcript saving is off" (não grava nada). O `abrir.sh` limpa as variáveis `CLAUDE*`.
- Mensagem para quem estava no meio do trabalho ou com subagentes ativos no fim: "Os terminais fecharam sozinhos
  às HH:MM de AAAA-MM-DD (motivo), e os subagentes e processos em segundo plano desta conversa morreram junto.
  Confira o estado real e retome de onde parou." Quem tinha terminado ou esperava o usuário abre sem mensagem.
- Abre em modo `auto`; se a conversa usava outro, confira com
  `grep -o '"permissionMode":"[a-zA-Z]*"' <transcrito> | tail -1` e avise.
- Antes de reabrir várias pesadas (builds, 50 agentes), confira a memória livre (`free -g`) e os tetos dos
  projetos: o PC já travou por RAM em 2026-10-23.

## 3. Provar que voltou
- `ls ~/.claude/sessions/*.json`: cada conversa reaberta aparece com o `sessionId` dela.
- `grep -c 'fecharam sozinhos' ~/.claude/projects/*/<id>.jsonl` maior que 0 nas que receberam a mensagem (o
  histórico está gravando).
- Se precisar ver a tela sem tirar o foco do usuário:
  `xwd -silent -id <janela> -out x.xwd && ffmpeg -loglevel error -i x.xwd x.png` (janelas em
  `xdotool search --class gnome-terminal`). Não pode aparecer "Transcript saving is off".

## 4. Registrar
Linha no diário de hoje do vault (`$VAULT/Pessoal/Diario/AAAA-MM-DD.md`): hora e causa da queda, o que foi reaberto e
o que ficou de fora.
