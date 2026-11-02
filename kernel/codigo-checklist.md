---
paths:
  - "**/*.{c,cc,cpp,cxx,h,hh,hpp,hxx,m,mm}"
  - "**/*.{rs,go,java,kt,swift,cs,zig}"
  - "**/*.{py,rb,php,lua,pl}"
  - "**/*.{js,jsx,mjs,cjs,ts,tsx,vue,svelte}"
  - "**/*.{sh,bash,zsh,ps1}"
  - "**/*.{sql,html,css,scss}"
  - "**/{CMakeLists.txt,Makefile,Cargo.toml,package.json,pyproject.toml,meson.build}"
  - "**/*.cmake"
---

# Checklist de engenharia (níveis 2 e 3)
Use para listar os modos de falha antes de codar e para reler o diff. Pule o que não tem a ver com a mudança.

## Desenho
- Dados primeiro: modelo, invariantes, onde o estado vive e quem é o dono. Uma única fonte da verdade; prefira
  tipos que tornem estado inválido impossível de representar.
- Fronteiras: regra de negócio não depende de interface, banco ou rede; contrato explícito nas bordas (tipos, erros
  possíveis, unidades).
- Evolução: o que acontece com 10× mais dados ou usuários, se o processo cair no meio e como se desfaz a mudança.
  Decisão difícil de desfazer (formato de arquivo, schema, API pública) leva versão.
- Reaproveite o que o projeto já tem antes de criar; nada de abstração para futuro hipotético nem recurso não pedido.

## Entrada e limites
- Tudo que vem de arquivo, rede, plugin, dispositivo, CLI ou usuário é hostil: vazio, enorme, truncado, duplicado
  ou malicioso. Índice checado, aritmética sem overflow, nada de divisão por zero, alocação e recursão com limite.
- 0, 1 e máximo; Unicode e caminhos com espaço; fuso e horário de verão; relógio monotônico para medir duração;
  dinheiro nunca em ponto flutuante.

## Erros e dados
- Toda falha de I/O, rede, disco cheio, permissão ou timeout tem tratamento decidido e mensagem que o usuário
  entende e consegue resolver.
- Estado consistente depois de erro, cancelamento ou queda: sem arquivo pela metade, transação aberta ou item
  perdido de lista ou fila.
- Compatibilidade: arquivos, configurações, banco e APIs antigos continuam abrindo ou ganham migração testada;
  formato novo tem versão; o que vai para produção tem caminho de volta.

## Segurança (a política está na seção 3 do kernel, `~/.claude/kernel/KERNEL.md`)
- Valide na borda; nada de SQL, shell ou caminho montado com string (injeção, path traversal); autorização
  conferida no servidor em toda rota; segredos fora do código, do log e do git.
- Serviço novo escuta em 127.0.0.1, a menos que precise de mais; menor privilégio; confira as portas com `ss -lntup`.
- Dados pessoais: só o necessário, e nunca no log.

## Concorrência e tempo de vida
- Corrida, deadlock, ordem de inicialização e desligamento, cancelamento; recurso liberado em todo caminho,
  inclusive no erro.
- Quem ainda aponta para o objeto quando ele é destruído? Callbacks, timers e threads param antes.
- Espera por outra thread ou processo externo sempre tem timeout: o desligamento não pode travar porque um plugin,
  host ou serviço não responde.

## Tempo real e baixo nível
- Caminho de tempo real (áudio, vídeo, jogo, controle): nada de alocar ou liberar memória (inclusive o último
  ponteiro compartilhado que solta um objeto), lock, I/O, log ou chamada que bloqueia; comunicação por fila sem lock.
- Kernel e drivers: nada que durma em contexto atômico; todo caminho de erro desfaz o que fez; dado do dispositivo
  é hostil.
- FFI e `unsafe`: invariantes escritas num comentário de segurança; quem é dono de cada ponteiro e por quanto tempo.

## Desempenho e recursos
- Algoritmo adequado ao volume real; nada de I/O, alocação ou consulta dentro de laço quente (N+1).
- Toda fila, cache, buffer, retry e log tem limite; cache só com chave certa e invalidação definida.
- Rede sempre com timeout; retry só em operação idempotente, com backoff.
- Meça CPU e memória com o programa parado e em uso, antes e depois. Otimize medindo, não por palpite.

## Interface
- Estados de carregando, vazio, erro e desabilitado; ação destrutiva com confirmação ou desfazer; nada que
  congele a tela.
- O controle faz o que promete, de ponta a ponta (ouvir, ver, salvar e reabrir).

## Testes
- Teste o comportamento, não a implementação: caminho feliz, limites e erros.
- Determinísticos: sem rede, relógio, ordem ou `sleep`; semente fixa no aleatório.
- Teste de regressão que falha sem a correção.

## Dependências e configuração
- Dependência nova só com motivo: prefira a biblioteca padrão ou o que o projeto já usa; confira manutenção,
  licença e tamanho; versão fixa no lockfile.
- Nada que muda por ambiente (caminho, URL, porta, chave) fixo no código.

## Operação
- Logs úteis nos pontos de falha, sem dados sensíveis; o processo sobe e desce limpo.
