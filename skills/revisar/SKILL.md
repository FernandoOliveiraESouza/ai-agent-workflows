---
name: revisar
description: Revisor de código local (tudo na máquina): coleta o diff, roda analisadores só nas linhas mudadas (clippy, ruff, shellcheck, semgrep, segredos), revisores especialistas em paralelo que leem o código inteiro e não só o diff, verificação adversarial de cada achado, re-revisão do que já foi apontado e aprendizados por repositório. Use quando o usuário pedir revisão, "procura falhas", "revisa antes de mandar", antes de abrir ou atualizar um PR, e antes de commitar mudança grande ou crítica (FFI, concorrência, exportação, áudio, dinheiro, segurança). Qualquer linguagem e projeto.
---

# Revisar (revisor de código local)

Nada sai da máquina: o código não é enviado a serviço nenhum (o semgrep só baixa regras públicas, `--metrics off`).
Não é derivado do CodeRabbit (os termos dele proíbem engenharia reversa e uso para criar concorrente, 4.2).

```bash
R=~/.claude/skills/revisar/scripts/revisar.py
python3 $R coletar --repo <repo> --out <scratchpad>/rev [--base main | --commit SHA | --range A..B | --pr N | --tudo CAMINHO...] [--sem-clippy] [--sem-semgrep]
```

Sem alvo: o que não foi commitado (arquivos novos incluídos, sem mexer no índice). `--pr N` busca a cabeça do PR numa
ref local (`refs/revisor/pr-N`) e lê uma árvore separada: rode `python3 $R limpar --out <dir>` no fim. Saídas:
`resumo.md` (leia primeiro), `alvo.json`, `diff.patch` (12 linhas de contexto), `estaticos.json`.
Ferramentas em `~/.local/opt/revisor-venv/bin` (ruff, shellcheck, semgrep); clippy roda só nos pacotes tocados, com
memória limitada (pode levar minutos; `--sem-clippy` quando só se quer a leitura).

## Fluxo

1. **Coletar** (comando acima) e ler `resumo.md`, as regras listadas (AGENTS.md, CONTRIBUTING… do projeto), os
   `aprendizados` e o `historico` se houver.
2. **Revisores em paralelo** (Agent, `model: opus`, numa mensagem só). Quantos:
   - até ~150 linhas mudadas: **A** e **B** abaixo; até ~800: **A, B, C**; acima: **A, B, C, D** e divida A por área.
   - **A. Correção:** lógica, caminhos de erro (o que fica no estado depois de um `?`/`return`/exceção no meio),
     recursos e tempo de vida (drop/close/free, ordem, FFI, buffers em uso), concorrência (locks, deadlock, ordem
     de threads), limites (vazio, zero, overflow, unicode, arquivo enorme), regressões em quem chama o código mudado.
     **Rastreio:** para cada recurso que sai de uma lista/fila/contador (pendentes, livres, abertos), ele sai antes
     ou depois de a operação confirmar? Se ela falhar, quem ainda sabe que ele existe? Siga também as funções que a
     correção chama (ex.: um `flush` reaproveitado) e os caminhos que já existiam e chegam no mesmo estado.
   - **B. Testes e prova:** o teste exercita mesmo o caminho mudado? Pode passar sem testar (pulo silencioso,
     asserção fraca, mock que esconde)? Falta teste de regressão para a correção? Teste que depende de hardware ou
     tempo e vira instável?
   - **C. Segurança e dados:** entrada hostil, injeção (shell, SQL, caminho), segredos, permissões, `unsafe`/FFI sem
     `SAFETY`, dados do usuário perdidos ou corrompidos, gravação não atômica.
   - **D. Regras do projeto e simplicidade:** o que AGENTS/CONTRIBUTING e o padrão de engenharia do principal
     exigem (ex.: nada de `unwrap` em produção, formato de commit, teste enfraquecido, fallback silencioso), código
     duplicado de algo que já existe, abstração desnecessária.
   Prompt de cada revisor (o brief é tudo o que ele sabe): caminhos de `resumo.md`, `diff.patch`, `estaticos.json`,
   das regras e dos aprendizados; a árvore a ler (`alvo.json:arvore`); a dimensão dele; e:
   > Leia os arquivos inteiros e siga quem chama e quem é chamado: o diff sozinho esconde a maioria das falhas.
   > Critério comum, além da sua dimensão: o padrão de engenharia do principal (`~/.claude/kernel/KERNEL.md`, seção 8)
   > e o checklist `~/.claude/kernel/codigo-checklist.md`. Violação deles com cenário concreto é achado.
   > Só aponte o que você consegue descrever como cenário concreto (entrada/estado → resultado errado). Não mexa
   > em nenhum arquivo. Responda só JSON: `[{"arquivo","linha","severidade":"critica|alta|media|baixa",
   > "resumo","cenario","evidencia":"trecho do código","confianca":0-1}]`, `[]` se nada.
   Os achados estáticos "na_mudanca" entram na conta de quem os entende (clippy/ruff → A ou D, semgrep → C).
3. **Juntar**: tirar repetidos (mesmo arquivo e mesma causa), descartar o que os aprendizados já dizem ser falso.
4. **Verificar** (Agent `opus`, um por arquivo ou por ≤5 achados, em paralelo), sem contar quem achou nem dar a
   opinião do revisor:
   > Tente REFUTAR cada achado lendo o código (e rodando um teste ou comando barato, se ajudar, sem alterar a
   > árvore). Veredito por achado: CONFIRMED (cenário acontece), PLAUSIBLE (depende de algo não verificável aqui)
   > ou REFUTED (com o motivo). JSON.
   Fica só CONFIRMED e PLAUSIBLE.
5. **Re-revisão** (há `historico`): para cada achado anterior, diga se foi corrigido, se persiste ou se mudou de
   lugar; os novos aparecem à parte.
6. **Relatar**: `ReportFindings` com os achados, do mais grave ao menos, e depois um resumo curto em português
   (o que encontrou, o que foi descartado e por quê, analisadores que falharam). Grave `<out>/achados.json`
   (`{"historico_destino": ..., "achados": [...]}`) e rode `python3 $R guardar --achados <out>/achados.json`.
7. **Corrigir** só se o usuário pedir (ou se a tarefa já era "revisa e corrige"): cada correção com teste e o
   portão do projeto; depois, re-revisão do mesmo alvo.
8. **Aprender**: quando o usuário disser que um achado está errado ou não importa, ou confirmar um tipo de
   achado, `python3 $R aprender --repo <repo> "<lição em uma linha>"`.

## Regras
- Comentário em PR (GitHub) só quando o usuário pedir; padrão é relatar aqui.
- Achado sem cenário concreto não é achado. Estilo não é achado (o formatador do projeto cuida).
- Diga em uma linha quantos revisores e verificadores rodaram e em que modelo.
- Validação (2026-10-22): num PR real de um projeto Rust de código aberto, achou os 3 problemas que o revisor
  automático do GitHub tinha achado em 3 rodadas (drenagem para no 1º erro do `flush`; teste pula sem testar;
  `read_oldest` devolve o slot antes de o driver confirmar).
