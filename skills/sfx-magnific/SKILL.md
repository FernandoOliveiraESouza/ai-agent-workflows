---
name: sfx-magnific
description: Efeitos sonoros sincronizados com a ação: primeiro reaproveitar ou baixar SFX grátis (acervo local, Kenney CC0, Mixkit), e só gerar no Magnific (ElevenLabs SFX) se nada servir e o usuário liberar os créditos. Achar o quadro da ação, analisar pico e ataque, aparar e posicionar o cue pelo pico. Use sempre que um vídeo precisar de SFX (whoosh no corte, impacto, clique, snap, vácuo, ticks de UI). Nada de SFX sintetizado por código.
---

# SFX sincronizados (grátis primeiro, Magnific por último)

**Regra (2026-10-21):** sempre buscar SFX grátis antes de gerar. Créditos do Magnific só com o pedido
explícito dele naquele vídeo, depois de mostrar que nenhum som grátis serve e o custo (`simulate_cost`).

## Fluxo
1. **Quadro da ação**: extraia quadros a 30 fps em volta da ação e ache o quadro exato (impacto, encaixe, corte):
   `ffmpeg -ss T-0.5 -t 1 -i v.mp4 -vf "fps=30,scale=180:-2,drawtext=text='%{eif\:n+F0\:d}':...,tile=8x4" folha.png`
   (recorte a região do objeto com `crop=` para ver detalhes pequenos).
2. **Acervo local primeiro**: mantenha uma pasta de SFX já aparados (ex.: `$SFX_LIBRARY`, com `trim/` e pico e licença
   de cada arquivo em `SFX.md`). Copie para o projeto o que servir.
3. **Baixar grátis** (sem login, sem conta) o que faltar, cada fonte em `raw/<fonte>/` dentro do projeto, sem rodar
   nada que vier nos arquivos:
   - **Kenney.nl** (CC0): zips "Interface Sounds", "UI Audio", "Impact Sounds", "Digital Audio" etc. Ótimo para
     cliques, ticks, pops e impactos; não tem whoosh.
   - **Mixkit** (Mixkit Free License, uso comercial): whoosh, transition, swoosh, pop, riser. O mp3 fica em
     `https://assets.mixkit.co/active_storage/sfx/<id>/<id>-preview.mp3` (o id está na página da categoria).
   - Se uma fonte bloquear (Cloudflare), tente outra CC0/livre sem login (ex.: repositórios CC0 no GitHub).
   Converta os escolhidos para WAV 48 kHz estéreo e anote em `SFX.md`: arquivo, URL, licença, duração, pico.
4. **Analisar e aparar** (precisa de numpy e ffmpeg):
   `python3 ~/.claude/skills/sfx-magnific/scripts/sfx_peak.py pasta/*.wav --out pasta/trim`
   Escolha o take com `picos>50% = 1` (golpe limpo) e poucos cliques. A saída é mono: reconverta com `-ac 2` se precisar.
5. **Só se nada grátis servir**: peça ao usuário para liberar o Magnific. Com o ok: ToolSearch
   `select:mcp__magnific__audio_sfx_generate,mcp__magnific__creations_wait,mcp__magnific__simulate_cost`;
   `simulate_cost` antes; prompt descreve fonte, superfície e perspectiva ("single soft magnetic snap, phone onto
   metal mount, close mic, no music"); `durationSeconds` 1–2; `variations` 3–4; baixe os mp3 com `curl -sL` e volte ao passo 4.
   (O acervo de estoque do Magnific não tem áudio; lá só dá para gerar.)
6. **Posicionar pelo pico**: `inicio_do_cue = t_acao − pico_no_arquivo_aparado`. Whoosh: pico no corte.
7. Mixar abaixo da voz (ducking) mas audível, master −14 LUFS / pico ≤ −1 dBTP,
   e conferir com a skill `video-qa`.

O Claude não ouve áudio: diga isso e deixe o ouvido final com o usuário.
