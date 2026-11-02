# Orçamento da latência: fórmulas e de onde vem cada número

Cadeia: MixDaw (JUCE/Tracktion) → pipewire-jack → grafo PipeWire 1.0.5 → nó ALSA `pro-output-0` → driver
`snd-firestudio-tube` (snd-firewire-lib) → controlador OHCI → FireWire S400 → DICE TCD2210 → DAC → cabo
Out 3 → Line 9 → ADC → DICE → OHCI → driver → `pro-input-0` → grafo → MixDaw. 48 kHz salvo indicação.
`Q` = quantum do grafo = período ALSA no modo IRQ.

## 1. Fila isócrona de saída (driver)

Porta exata em `scripts/auditoria.py: expected_queue()` de `set_period()` (`tube-stream.c:112`, 0.4.3):

```
packet  = ceil(rate / 8000)                      # 6 frames a 48k (1 ciclo de 125 µs)
us→fr   = ceil(us · (rate/100) / 10000)          # us_to_frames (:87)
fila    = período · clamp(queue_periods, 2, 16)  # padrão 2 períodos
se queue_us > 0:      fila = max(us→fr(max(queue_us, 1500)), 4 · packet)    # piso 1,5 ms só no 0.4.3
senão max_queue_us>0: fila = min(fila, us→fr(max(max_queue_us, 1500)))      # 2000 µs = 96 frames
fila    = min(fila, max(buffer, 2 · período))
irq     = min(período, max(fila / clamp(irq_per_queue, 2, 16), packet))     # 8 IRQs por volta
enquanto ciclos(fila) < 2 · ciclos(irq): fila += packet     # ciclos = ceil(frames · 8000 / rate) (:107)
```

- No modo IRQ a 48k: **fila = min(2Q, 96)**. 32 → 64; ≥ 64 → 96. A 96k: min(2Q, 192); a 44,1k: min(2Q, 89).
- Cada frame na fila é atraso de saída: a lib copia os frames do buffer ALSA **quando o pacote é enfileirado**
  e o `hw_ptr` anda nessa hora (`amdtp-stream.c:587-600`, `update_pcm_pointers`). O `delay` do status ALSA
  inclui a fila (`compute_pcm_extra_delay`, `amdtp-stream.c:1075-1129`), por isso
  **fila medida = delay − (appl_ptr − hw_ptr)** (± 1 pacote).
- Fila < período **não** quebra: o aviso de período vem de `update_pcm_pointers`/thread `tube-rt`; a fila só limita
  quanto a thread RT pode atrasar (≈ fila − 1 IRQ). Filas < ~1,3 ms travam (README do driver).
- Período 0 (sessão aberta só pelo MIDI, bug 2b): `set_period` passa fila = buffer e a lib usa o padrão dela
  (`iso irq/queue 480/1440` visto antes do 0.4.3: 10 ms de IRQ, 30 ms de fila).
- Antes do `max_queue_us` (0.4.1, modo timer, período 1024/4096): fila 2048 = 42,7 ms (92 % da ida e volta).

## 2. Placa

`card = 72` frames a 48 kHz (1,50 ms) e `114` a 96 kHz (1,19 ms), medidos com cabo e `tools/latencia`
(README do driver; `tube-proc.c:12 unit_latency_frames`). Inclui conversores, DICE, pacote FireWire, atraso SYT da lib
(`TRANSFER_DELAY_TICKS 0x2e00` − 1 ciclo + 8 frames = 0,52 ms = 25 frames, `amdtp-stream.c:29,288`) e a varredura da
thread RT (250 µs, ~0,13 ms médio na captura). **44,1 kHz e 32 kHz nunca foram medidos** (o driver supõe 72).

## 3. PipeWire 1.0.5

- Modo IRQ (`api.alsa.disable-tsched = true`, regra `53-firestudio-tube-latencia.lua`): `period_size =
  clock.target_duration` (= quantum) **só em `set_format`, na abertura** (spa `alsa-pcm.c`). Mudar o quantum ao vivo
  (`force-quantum`) não muda o período ALSA: o grafo vai a 256 com a placa a 32 → trava (2026-10-21 10:46).
  Por isso a troca de buffer precisa **reabrir a placa** (suspend/resume dos nós ou ciclo de perfil).
- `headroom` = 0 no modo IRQ. Modo timer: período do perfil (1024 × 4) e headroom mínimo de 32 frames na captura
  (aparece como `Latency minRate 32` no `pro-input-0`).
- Buffer de saída: 1 quantum à frente do `hw_ptr` (`appl − hw = Q`). Captura: 1 quantum.
- `latency.internal.rate` (prop do nó) → param `ProcessLatency` → somado à latência das portas. Deve valer
  **placa + fila efetiva**: 136 em 32, 168 de 64 a 2048 (48k). O painel novo grava por tamanho
  (`layout.py: internal_latency/set_internal_latency/write_latency_rule` → regra `54-…-atual.lua`).

## 4. JACK (pipewire-jack) e MixDaw

- Latência informada nas portas físicas: captura = `Q + headroom`; playback = `Q + interna`.
  **Informado = 2Q + headroom + interna.** Lido por `jack_rt info` (igual ao `jack_port_get_latency_range`).
- MixDaw: `AudioDevices.cpp:190-262 jackPortLatency` (maior `range.max` das portas físicas do cliente) e
  `:633-659 currentLatency` (JACK → usa as portas); `Recording.cpp:65-110 compensationMs/syncLatencyCompensation`
  (compensação = extra do usuário + caminho − o que o Tracktion já soma); caches por chave em
  `Monitoring.cpp:207-231` e `Recording.cpp:100-106` (sem a latência das portas na chave antes da Etapa 3).
  O MixDaw processa no mesmo ciclo do JACK: **0 frames próprios** (decisão de arquitetura do DAW), fora a latência de plugins (PDC).

## 5. Fórmula da ida e volta

```
T = Q (saída ALSA) + fila + placa + Q (captura) + headroom + MixDaw
modo IRQ 48k:  T = 2Q + 72 + min(2Q, 96)
erro de alinhamento = T − informado = interna_correta − interna_declarada   (se o resto bate)
```

Direto no ALSA (`tools/latencia`, sem PipeWire): `mediana = placa + fila`; DAW vê `mediana + n·período`.
32×2 → 136 (+64 = 200 = 4,2 ms); 64×2 → 198 medido com a fila antiga de 128 (hoje, fila 96: 168 previsto).

## 6. Orçamento e percentuais (como a auditoria calcula)

| componente | frames | fonte |
|---|---|---|
| fila isócrona | medida no status ALSA, senão `iso queue` do /proc | MEDIDO / CÓDIGO |
| buffer ALSA de saída | Q | CÓDIGO |
| placa | 72 / 114 | MEDIDO ANTES |
| captura PipeWire | Q + headroom | CÓDIGO |
| MixDaw | 0 | decisão de arquitetura do DAW |

- % sobre o **T medido** (`--measure jack`); sem medição, sobre a soma e o relatório diz **ESTIMADO**.
- Resíduo = T medido − soma: **AVISO se |resíduo| > 10 %** (atraso que nenhuma camada explica: verificar fila
  medida, headroom escondido, plugin com latência, resampler).
- Erro de alinhamento = T − informado (MEDIDO com `--measure jack`; senão ESTIMADO). Tolerância ±8 frames (1 pacote + SYT).

## 7. Latência por tamanho (modo IRQ, driver 0.4.3, `max_queue_us 2000`)

Gerada por `latency_table()`; "interna" = o que o `pro-output-0` deve declarar.

**48 kHz** (placa 72 medida)

| Q | iso irq | fila | interna | ida e volta | ms |
|---|---|---|---|---|---|
| 32 | 8 | 64 | 136 | 200 | 4,17 |
| 64 | 12 | 96 | 168 | 296 | 6,17 |
| 128 | 12 | 96 | 168 | 424 | 8,83 |
| 256 | 12 | 96 | 168 | 680 | 14,17 |
| 512 | 12 | 96 | 168 | 1192 | 24,83 |
| 1024 | 12 | 96 | 168 | 2216 | 46,17 |
| 2048 | 12 | 96 | 168 | 4264 | 88,83 |

**96 kHz** (placa 114 medida; Q mínimo 64, máximo 2048: buffer ≤ 4096 em qualquer taxa, `amdtp-stream.c:210-211`)

| Q | iso irq | fila | interna | ida e volta | ms |
|---|---|---|---|---|---|
| 64 | 16 | 128 | 242 | 370 | 3,85 |
| 128 | 24 | 192 | 306 | 562 | 5,85 |
| 256 | 24 | 192 | 306 | 818 | 8,52 |
| 512 | 24 | 192 | 306 | 1330 | 13,85 |
| 1024 | 24 | 192 | 306 | 2354 | 24,52 |
| 2048 | 24 | 192 | 306 | 4402 | 45,85 |

**44,1 kHz** (placa 72 **suposta**, não medida)

| Q | iso irq | fila | interna | ida e volta | ms |
|---|---|---|---|---|---|
| 32 | 8 | 64 | 136 | 200 | 4,54 |
| 64 | 11 | 89 | 161 | 289 | 6,55 |
| 128 | 11 | 89 | 161 | 417 | 9,46 |
| 256 | 11 | 89 | 161 | 673 | 15,26 |
| 512 | 11 | 89 | 161 | 1185 | 26,87 |

88,2 kHz: Q 64 → 370 (4,20 ms), 128 → 547, 256 → 803 (placa 114 suposta).

## 8. Comparação

Windows/ASIO (PaeFireStudio 4.2.1): buffer 64 → 117 + 86 = 203 amostras **informadas** (4,2 ms); o Linux em 32
(200 reais) empata com o ASIO 64. `snd_dice` do kernel: fila = buffer ALSA inteiro (85 ms com 1024 × 4).
