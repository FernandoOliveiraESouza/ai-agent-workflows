---
name: projetos
allowed-tools: Bash(python3 -I ~/.claude/skills/projetos/scripts/listar.py:*)
description: Menu de projetos do usuário. Mostra a lista (registro em ~/vault/Projetos/00-PROJETOS.md) para escolher qual retomar, ou criar um projeto novo, e já carrega onde o escolhido parou. Use quando o usuário digitar /projetos, "projetos", "meus projetos", "voltar a um projeto", "em que projeto eu estava", "retomar", ou pedir para começar um projeto novo.
---

# Projetos: escolher, retomar ou criar

Registro único: `~/vault/Projetos/00-PROJETOS.md` (tabela: Projeto | Pasta | Retomar | Status | Última vez).
Fale em português do Brasil, curto.

## Lista atual (gerada na hora pelo script; já está pronta, não leia o registro de novo)
!`python3 -I ~/.claude/skills/projetos/scripts/listar.py`

## 1. Sem argumento: mostrar a lista (tem que ser instantâneo)
Responda **só** com a lista acima, igual, e a linha "Responda com o número ou o nome." **Sem ferramentas, sem resumo, sem
mais nada.** Não use AskUserQuestion: a caixa só tem 4 opções e o usuário quer ver todos de uma vez.
Quando ele responder, vá para a seção 2 (ou para a 3, se for "Novo projeto").

## 0. Com argumento (vindo do comando `projetos` do terminal)
- `retomar <nome>`: vá direto para a seção 2 com o projeto desse nome.
- `novo`: vá direto para a seção 3.

## 2. Retomar um projeto
1. Leia os arquivos da coluna **Retomar**, na ordem. Se um caminho não existir mais, avise e procure no vault
   (`grep -ril --include='*.md' "<nome>" ~/vault/`) antes de supor qualquer coisa.
2. Se a pasta tiver `CLAUDE.md` ou `AGENTS.md`, leia e siga durante o trabalho: elas só acrescentam o específico
   do projeto; em conflito, vale o principal (`~/.claude/kernel/KERNEL.md`).
3. Resuma em até 8 linhas:
   - onde parou (com a data);
   - o que está pronto;
   - o que está pendente;
   - decisões que esperam pelo usuário;
   - riscos de segurança abertos, se houver (segurança é prioridade nº 1 no kernel);
   - o próximo passo sugerido.
4. Atualize no registro a **Última vez** para a data de hoje e o status para `ativo`, se estava `pausado`.
   Atualize também a seção "Agora" de `~/vault/Pessoal/00-PESSOAL.md` (curto) e anote no diário do dia.
5. Pergunte se segue pelo próximo passo sugerido ou por outra coisa. Não comece a mexer no código antes da resposta.

## 3. Novo projeto
1. Pergunte, numa mensagem só: nome do projeto e objetivo em uma frase. Se ele já tiver dito, não pergunte de novo.
2. Crie a pasta `~/vault/Agentes/<slug>/` (minúsculas, com hífens) e o `README.md` dela, com frontmatter
   (`tipo: projeto`, `data`, `tags`, `status: ativo`) e as seções **Objetivo**, **Andamento (com data)** e **Onde está cada coisa**.
3. Registre o projeto:
   - linha no registro `~/vault/Projetos/00-PROJETOS.md` (Retomar = o README novo; Última vez = hoje);
   - `- [[Agentes/<slug>/README|<Nome>]]` em `~/vault/Agentes/00-Agentes.md`;
   - seção do projeto na área certa de `~/vault/Projetos/` (código, notas, agentes, o que não pode mudar de lugar);
   - nota no diário do dia.
4. Antes de construir, avalie se compensa dividir entre subagentes, conforme o principal (`~/.claude/kernel/KERNEL.md`).

## Manter o registro vivo
- Ao **pausar** um projeto ("pausa", "volto amanhã"): status `pausado`, e escreva no arquivo principal de Retomar onde parou e
  o próximo passo.
- Ao **concluir**: status `concluido`.
- Projeto novo criado fora desta skill também entra no registro.
