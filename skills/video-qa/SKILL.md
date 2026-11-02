---
name: video-qa
description: Checagem de entrega de vídeo num comando só: formato/fps/duração, loudness (−14 LUFS, pico ≤ −1 dBTP), quadros pretos, congelados, trechos que voltam e parecem travada (rewind) e folha de contato com linha guia das legendas. Use antes de entregar ou mostrar QUALQUER render ao usuário (anúncios, brand films, Reels, TikTok).
---

# Video QA

Roda tudo o que antes era feito à mão, em vários comandos, a cada render.

```bash
python3 ~/.claude/skills/video-qa/scripts/qa.py <video.mp4> --guide 1248 --out <scratchpad> [--max-dur 60] [--spec 1080x1920@30] [--fps-sheet 2]
```

- Precisa de numpy e ffmpeg: use um venv que tenha numpy (ex.: `python3 -m venv ~/.venvs/media && ~/.venvs/media/bin/pip install numpy`).
- `--guide Y` desenha uma linha vermelha em y=Y na folha (ex.: 1248 = base das legendas num vídeo 1080x1920).
- Sai com código 1 se algo falhar. As linhas `CONGELA` com só a cartela final são esperadas.
- **Sempre olhe a folha** (`*_sheet.png`) com a ferramenta de imagem: o script não vê se a legenda casa com a
  imagem, se cobre o produto ou se o clipe está errado.

## O que conferir na folha
1. Todas as legendas na mesma linha de base e cartões em bloco único (sem palavras desalinhadas).
2. Selos/letreiros num estilo, tamanho e altura só.
3. Texto legível (contraste) e nunca sobre o produto.
4. Legenda casa com a imagem; sem o mesmo plano voltando (parece travada).
5. Claims e footage conforme o guia do projeto.
