---
name: latencia-audio
description: Auditoria da cadeia FireStudio Tube → driver snd-firestudio-tube → ALSA → PipeWire/WirePlumber → JACK → MixDaw num comando: placa parada (PCM com hw_ptr congelado), buffer igual em todas as camadas, fila isócrona recalculada do código, latência declarada × real, orçamento em ms e %, kernel/RT, medição pelo cabo Out 3 → Line 9, teste de troca de buffer e lista priorizada de onde melhorar. Use sempre que mexer em driver, PipeWire, buffer ou taxa, quando o usuário falar de latência, estalos, xrun ou som parado, e antes de declarar a cadeia ok.
---

# Latência de áudio (FireStudio Tube → MixDaw)

```bash
python3 -I ~/.claude/skills/latencia-audio/scripts/auditoria.py --out <scratchpad>/lat [--dur 30] [--history]
```

Só lê o sistema por padrão. Saídas em `--out`: `relatorio.md` (leia primeiro), `auditoria.json` (schema 1),
`contadores.tsv` (série dos contadores), `bruto/`. Códigos: 0 ok · 1 FALHA · 2 uso · 3 ambiente · 4 sem clique ·
5 não restaurou (rode `--recover`). O `jack_rt` é compilado sozinho em `~/.cache/latencia-audio/`.

## Fluxo
1. **Coletar** (só leitura): o comando acima. `--history` guarda em
   `$LATENCIA_HISTORY` (padrão `~/.local/share/latencia-audio/auditorias/`) e compara com a rodada anterior; `--compare <json>` compara
   com uma específica. `--sem-watch` não abre o cliente JACK de observação.
2. **Placa parada?** (A2 FALHA "PCM parado", D6/K14 stall): **pergunte ao usuário** antes de
   `--recover` (suspende/retoma os nós; se não voltar, ciclo de perfil off → pro-audio).
3. **Medir** (só com o ok dele): confirme cabo Out 3 → Line 9, monitores/fones baixos e Line 9 fora das mixes
   Main/Fones; então `--measure jack --confirmo-cabo-out3-line9` (pulsos Hann −20 dBFS pelo JACK) ou `--measure direto`
   (tira a placa do PipeWire e roda `tools/latencia` do driver). Dá o T medido, o resíduo e o erro de alinhamento.
4. **Troca de buffer** (altera o sistema, só com ok): `--sizes 32,64,128,256,512 --altera-sistema
   [--modo-troca painel|reabrir]`. Faz backup do metadata, da conf de clock e da regra 54 e restaura em `finally`.
5. **Priorizar**: a seção "Onde melhorar" vem das FALHA/AVISO + regras fixas (item · camada · evidência · ganho ·
   risco · esforço). Para ir além do script, siga `references/checklist.md` (itens `manual`); fórmulas, citações de
   código e tabela por tamanho em `references/orcamento.md`.
6. **Registrar**: resumo no vault (Sessões/Erros-e-Soluções/ADR), `--history` ligado, números com MEDIDO/ESTIMADO.

## Regras (2026-10-21)
- **Nunca Out 5/6** (AUX4/AUX5). O `jack_rt rt` recusa tudo que não seja `playback_AUX2` → `capture_AUX8`, pelo nome.
- **Não gravar microfone nem guardar áudio**: a medição fica só na memória do `jack_rt`.
- **Sem sudo**: o que precisar (instalar driver, cyclictest, chrt de IRQ, gap_probe) vira linha com `!` para o usuário.
- **Não reiniciar PipeWire/WirePlumber nem recarregar o driver sem perguntar.** `--recover`, `--measure` e `--sizes`
  só com o ok dele naquele momento.
- **O Claude não ouve**: estalos e craquelado quem confirma é o usuário.

## Subagentes
Quem mexer em driver, painel, WirePlumber ou MixDaw roda a coleta **antes e depois** de cada mudança e anexa os dois
`auditoria.json` (ou o `--compare` entre eles) ao relatório. Subagente não usa `--recover/--measure/--sizes` sem o
coordenador liberar. Testes das funções puras: `python3 -I scripts/test_auditoria.py`.
- **Nunca medir níveis de captura com `parec`/PulseAudio (2026-10-21):** ele remapeia canais (o mic da entrada 15 apareceu no "canal 5" e os canais 7-16 em zero). Para ler canais, só portas JACK (`scripts/jack_rt.c` ou um cliente JACK por porta).
