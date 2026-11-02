# Checklist de latência: FireStudio Tube → MixDaw (prompt interno)

Você é o engenheiro de áudio em tempo real desta máquina (Linux 7.0 PREEMPT_DYNAMIC, OHCI + FireStudio Tube DICE
TCD2210, driver próprio `snd-firestudio-tube`, PipeWire 1.0.5 + WirePlumber 0.4.17, pipewire-jack, MixDaw
JUCE/Tracktion, painel `firestudio-control`). Objetivo: **a menor ida e volta estável em todos os buffers, com a
latência informada igual à real** (takes alinhados) e **troca de buffer sem travar**. Percorra as camadas na ordem
H → K → D → A → P → W → J → M → C; cada número vem de um comando, nunca de memória. Marque **MEDIDO**, **CÓDIGO** ou
**ESTIMADO**. Antes de propor mudança, diga ganho (ms e % do total), risco e esforço.

**Regras duras (2026-10-21):** nunca Out 5/6 (AUX4/AUX5); medir só com cabo Out 3 → Line 9 (`playback_AUX2` →
`capture_AUX8`) e volumes baixos; não gravar microfone nem guardar áudio; sem sudo (passe a linha com `!` ao
usuário); não reiniciar PipeWire/WirePlumber nem recarregar o driver sem perguntar; o Claude não ouve.

Formato: **ID · o que medir · comando · esperado · se estiver fora**. `(auto)` = a `auditoria.py` verifica;
`(manual)` = rodar à mão quando a investigação pedir. `T` = `/proc/asound/Tube`.

## H · Hardware, OHCI e FireWire

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| H1 (auto) | placa no driver próprio | `cat T/firestudio`; `cat /proc/asound/cards` | `/proc/asound/Tube/firestudio` responde | snd_dice no lugar, placa desligada, cabo |
| H2 (auto) | velocidade do link, firmware | longname em `/proc/asound/cards` | `S400`, firmware 1.0.4.0 | < S400: pacotes não cabem, start falha |
| H3 (auto) | relógio | `locked`, `measured rate`, `clock source` | locked yes, medida = nominal, internal | estalos/deriva; fonte de clock errada no painel |
| H4 (auto) | bus resets na janela | Δ `bus resets` | 0 | reset reinicia o isócrono: cabo, hub, outro aparelho |
| H5 (auto) | erros de pacote (CIP/DBC/SYT) | Δ `packet errors: tx, rx`, `last error` | 0 | lib cancela o stream; cabo, clock ou fila curta |
| H6 (auto) | IRQ do OHCI andando | Δ `/proc/interrupts` firewire_ohci / s | ~ taxa / `iso irq` com stream rodando (calibrar) | ~0 com PCM RUNNING = isócrono parado |
| H7 (auto) | afinidade do IRQ | `/proc/irq/N/{smp,effective}_affinity_list` | CPU fixo, longe do pw-data-loop | ver K4/K15 |
| H8 (auto) | formação dos streams | `tx/rx stream` no /proc | tx0 16+1, tx1 2+0, rx0 8+1; sem `formation CHANGED` | recarregar o driver |
| H9 (auto) | atraso SYT da lib | código `amdtp-stream.c:29,288` | 25 frames (dentro dos 72) | só na lib do kernel: não mexer |
| H10 (auto) | latência própria da placa na taxa | tabela do README | 72 @48k, 114 @96k medidos | 44,1/32/88,2k não medidos: medir com `--measure jack` |
| H11 (manual) | controlador OHCI | `lspci -nnk \| grep -iA3 1394` | TI/LSI/Agere com `firewire_ohci` | VIA/Ricoh têm quirks de isócrono |
| H12 (manual) | ASPM da placa PCIe FireWire | `lspci -vv -s <slot> \| grep -i aspm` | ASPM Disabled | L1 acorda tarde: IRQ atrasado |
| H13 (manual) | topologia do barramento | `ls /sys/bus/firewire/devices/`; `cat …/fw*/vendor_name model_name` | só o PC e a Tube | outro nó = IRM/cycle master trocado, resets |
| H14 (manual) | canais isócronos | `iso channel` no /proc | 0, 1 (tx) e 2 (rx) alocados | -1 = sem banda/canal; outro app FireWire |
| H15 (manual) | mixer da placa no teste | painel: Line 9 fora de Main/Fones, Out 3 baixo | sem realimentação | microfonia/clique alto no loopback |

## K · Kernel e sistema

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| K1 (auto) | modelo de preempção | `journalctl -k -b \| grep -E 'Dynamic Preempt\|PREEMPT\('`; `/proc/cmdline` | `full` (ou PREEMPT_RT) | lazy/voluntary: código de kernel segura a CPU (2,6 ms com NVENC) |
| K2 (auto) | IRQs em threads | `threadirqs` em `/proc/cmdline`; `ps -eLo comm \| grep irq/` | threadirqs | prioridade de IRQ fora de controle |
| K3 (auto) | ordem de prioridades RT | `ps -eLo pid,tid,cls,rtprio,psr,comm` | pw-data-loop 88 > IRQ OHCI ≥ 70 (> nvidia/xhci) ≥ tube-rt 70 > jack/MixDaw | OHCI empatado com a NVIDIA (50) espera atrás dela |
| K4 (auto) | CPU em comum | psr do pw-data-loop × IRQ efetivo | CPUs diferentes e fixos | IRQ e grafo disputando o mesmo núcleo |
| K5 (auto) | governador | `cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` | performance em todos | frequência sobe tarde em buffers pequenos |
| K6 (auto) | C-states / PM QoS | `…/cpu0/cpuidle/state*/{name,latency,disable}`; `pm_qos_resume_latency_us` | saída ≤ 100 µs | C6 fundo atrasa IRQ e thread RT |
| K7 (auto) | timers | `CONFIG_HZ`, `HIGH_RES_TIMERS`, `current_clocksource` | 1000, y, tsc | hpet/acpi_pm lentos; thread de 250 µs vira 1 ms |
| K8 (auto) | limites | `ulimit -r -l`; grupo `audio` | rtprio ≥ 88 (95), memlock ilimitado | JACK/MixDaw sem RT: `sudo scripts/configurar-tempo-real.sh` |
| K9 (auto) | rtkit | processo `rtkit-daemon` | rodando | PipeWire sem RT |
| K10 (auto) | estrangulamento RT | `/proc/sys/kernel/sched_rt_{runtime,period}_us` | 950000/1000000 ou -1 | menor = threads RT pausadas |
| K11 (auto) | pressão | `/proc/pressure/{cpu,memory,io}` | some avg10 < 5 %, mem full ~0 | tarefas esperando: callbacks atrasam |
| K12 (auto) | OOM e swap | `journalctl -k -b \| grep 'Out of memory'`; `/proc/meminfo` | 0 OOM, swap baixo | builds -j alto derrubam o áudio (OOM 08:15/08:18 de 2026-10-21) |
| K13 (auto) | perf | `/proc/sys/kernel/perf_event_paranoid` | ≤ 2 p/ diagnosticar | 4 = perf bloqueado (só diagnóstico) |
| K14 (auto) | kernel na janela | `journalctl -k --since @t0` (firewire/tube) | nada | `isochronous streams stalled, reporting XRUN` = stall |
| K15 (auto) | irqbalance | processo `irqbalance` | parado ou com o IRQ do OHCI banido | move o IRQ do OHCI entre CPUs |
| K16 (auto) | THP, swappiness, NVIDIA | `/sys/kernel/mm/transparent_hugepage/{enabled,defrag}`; `/proc/sys/vm/swappiness`; `/sys/module/nvidia` | defrag madvise/never | compactação síncrona = picos de ms |
| K17 (manual) | latência de agendamento | `sudo cyclictest -m -Sp90 -i200 -h400 -D60 -q` (rt-tests) | máx < 100 µs; < 50 µs ideal | picos > fila − 1 IRQ derrubam 32/64 |
| K18 (manual) | quem segura a CPU | `sudo perf sched latency` / `sudo trace-cmd record -e sched_switch -e irq` sob carga | nada > 500 µs | identificar driver (NVIDIA) ou softirq longo |
| K19 (manual) | isolamento | `isolcpus`/`nohz_full`/`rcu_nocbs` em `/proc/cmdline` | opcional: 1 núcleo para pw-data-loop + IRQ | só se K17 mostrar ruído |
| K20 (manual) | timer_migration, SMT, boost | `/proc/sys/kernel/timer_migration`; `/sys/devices/system/cpu/smt/control` | padrão | só em último caso |

## D · Driver snd-firestudio-tube

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| D1 (auto) | versão e parâmetros | `/sys/module/snd_firestudio_tube/parameters/*`; `srcversion` × `modinfo -F srcversion <repo>.ko` | max_queue_us 2000, queue_us 0, queue_periods 2, irq_per_queue 8, rt_interval_us 250, rt_priority 70; carregado = repo | módulo velho carregado (instalar/recarregar com `!`) |
| D2 (auto) | período do driver = hw_params | `period/buffer` × `T/pcm0p/sub0/hw_params` | iguais | período único para os 2 sentidos divergiu |
| D3 (auto) | fila/IRQ = `set_period` recalculado | `iso irq/queue` × `expected_queue()` | 48k IRQ: 8/64 (32), 12/96 (≥ 64) | bug 2/2b: sessão aberta antes (MIDI período 0 → 480/1440) |
| D4 (auto) | fila real | `delay − (appl_ptr − hw_ptr)` do playback | = `iso queue` ± 8 | atraso escondido que o PipeWire não vê |
| D5 (auto) | margem anti-travamento | `iso queue` em ms | ≥ 1,5 ms | < 1,3 ms trava |
| D6 (auto) | contadores na janela | Δ `stalls`, `start retries`, `starts` | +0, +0 (starts +1 por troca de buffer) | stall = isócrono parou; ver K1/K3 |
| D7 (auto) | thread `tube-rt` | `ps`: FIFO 70; `gap_probe` | presente; max gap < fila − 1 IRQ | sem a thread: workqueue sem RT, ms de atraso |
| D8 (auto) | limites de período | período 32..2048 (64.. a 96k), buffer ≤ 4096; ≡ 2 mod 6 | dentro; fila ajustada em ciclos | fora = recusa ou start sem fim |
| D9 (auto) | linhas 0.4.3 | `period range`, `output latency` no /proc | `output latency` = 72/114 + fila | driver antigo carregado |
| D10 (auto) | reinício após stall (bug 3) | `session_stale` no código + módulo carregado = repo; `session: STALE` | corrigido e carregado | PCM RUNNING com hw_ptr parado até reiniciar o PipeWire |
| D11 (manual) | bug 2 (período misto) | `tools/periodo-misto` (driver) | 2.º sentido recusado (-EBUSY) ou reaplicado | um sentido em 32 e o outro em 256 |
| D12 (manual) | bug 2b (MIDI antes do áudio) | `scripts/tube-direto.sh sh -c 'amidi …& aplay -D hw:Tube -c8 -f S32_LE -r48000 --period-size=32 --buffer-size=64 /dev/zero'` e ler /proc | `iso irq/queue 8/64` | 480/1440: áudio herdou a sessão do MIDI |
| D13 (manual, sudo) | recuperação de stall | `echo 0 \| sudo tee …/rt_interval_us` (provoca) e `250` de volta | aplay avisa XRUN e continua; `starts` sobe | bug 3 presente |
| D14 (manual, sudo) | estresse | `tools/bancada.sh "" 32x2 64x2 128x2 256x2 512x2` com render pesado | 0 xrun ≥ 64 | decide `queue_period_pct` |
| D15 (manual) | margem real em uso | `gap_probe=Y` um dia; `max gap`, `wake late max` | max gap < 1 ms | quem atrasa a thread RT (K17/K18) |
| D16 (manual) | parâmetros de partida | `tx_init_skip_cycles 800`, `ready_timeout_ms 400` | padrão (como o Windows) | start lento/falho |

## A · ALSA

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| A1 (auto) | hw_params dos 2 sentidos | `cat T/pcm0p/sub0/hw_params T/pcm0c/sub0/hw_params` | período = Q, buffer = 2Q (modo IRQ), S32_LE, 8/16 canais, taxa do grafo | período ≠ Q = troca sem reabrir → trava |
| A2 (auto) | PCM vivo | `status` 3+ vezes: `hw_ptr` andando em RUNNING | anda | **PCM parado**: RUNNING com hw_ptr congelado (ou XRUN): perguntar e `--recover` |
| A3 (auto) | sw_params | `T/pcm0p/sub0/sw_params` | avail_min = período, stop_threshold = buffer | acorda fora do período |
| A4 (auto) | dono | `owner_pid` → `/proc/<pid>/comm` | pipewire (ou o programa no modo direto) | outro app segurando a placa |
| A5 (manual) | `NO_PERIOD_WAKEUP` / modo | `T/pcm0p/sub0/hw_params` + props do nó | modo IRQ sem `no_period_wakeup` | modo timer: a thread RT não avisa período |
| A6 (manual) | dispositivos | `aplay -l; arecord -l` | `hw:Tube,0` (8 out / 16 in) e `hw:Tube,1` (Loop 2 in) | formação/carregamento errado |
| A7 (manual) | timestamps | `tstamp`, `trigger_time` do status | avançam | relógio do PCM parado |

## P · PipeWire 1.0.5

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| P0 (auto) | PipeWire rodando | processo `pipewire`; `systemctl --user is-active pipewire` | ativo | grafo inexistente |
| P1 (auto) | versões | `pipewire --version; wireplumber --version` | 1.0.5 / 0.4.17 | comportamento muda (WP 0.5 ignora .lua!) |
| P2 (auto) | buffer igual em tudo | `~/.config/pipewire/pipewire.conf.d/firestudio-tube-clock.conf` × `pw-metadata -n settings 0` × `LC_ALL=C pw-top -b -n 2` × hw_params × `jack_rt info` | todos = Q; min = max = Q; `force-quantum 0` | force-quantum órfão / grafo ≠ placa = regressão de 2026-10-21 |
| P3 (auto) | taxa e resampler | `clock.rate`, `force-rate`, /proc `rate`, JACK, `clock.name` dos nós | uma taxa, mesmo `clock.name` (api.alsa.0) | resampler = latência + CPU; drift |
| P4 (auto) | modo IRQ | props do `pro-output-0` em `pw-dump` | disable-tsched true, period-num 2, headroom 0 | modo timer: período 1024 + headroom 32 (fila de 43 ms) |
| P5 (auto) | latência interna | `ProcessLatency` / `latency.internal.rate` do `pro-output-0` | = placa + fila (136 em 32; 168 em ≥ 64 a 48k) | takes desalinhados pela diferença |
| P6 (auto) | driver do grafo, node.group | linhas sem `+` no pw-top; `node.group` nos nós ALSA | 1 driver (captura, priority 2500, quando aberta); sem node.group nos nós da Tube | node.group travou o grafo no WP 0.4; 2 drivers = ciclos diferentes |
| P7 (auto) | ERR por nó | Δ coluna ERR do pw-top (A/B) | +0 | xruns do driver do grafo; clientes atrasados |
| P8 (auto) | pw-data-loop RT | `ps`: thread do pid do pipewire | FIFO 88 | grafo sem RT |
| P9 (auto) | journal | `journalctl --user -u pipewire -u wireplumber` desde o último início | sem xrun/erro | — |
| P10 (auto) | loopback do sistema | `firestudio_tube.out` `node.passive` | true | segura a placa e entra no grafo de 32 |
| P11 (auto) | estado dos nós | `pw-dump` `state` | running/suspended/idle | error = abrir falhou |
| P12 (auto) | perfil | `Profile` do device | pro-audio | outros perfis abrem os sentidos separados (DICE quer juntos) |
| P13 (manual) | período real aberto | `api.alsa.period-size` do nó (0 = segue o quantum) | segue o quantum ao abrir | valor fixo brigaria com o painel |
| P14 (manual) | suspensão por ociosidade | `session.suspend-timeout-seconds` (WP) | padrão 5 s ou 0 | reabrir custa 100 ms+ de início |
| P15 (manual) | clientes que pedem quantum | coluna QUANT dos seguidores (Firefox 900) | min = max = Q prende o grafo | sem min/max o grafo cresce sozinho |
| P16 (manual) | `PIPEWIRE_LATENCY`/`node.latency` em clientes | `pw-dump` props dos clientes | nada fixo | cliente forçando outro quantum |
| P17 (manual) | latência interna ao vivo | `pw-cli e <id> ProcessLatency` | igual à regra 54 | painel não aplicou |

## W · WirePlumber 0.4

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| W1 (auto) | regras instaladas | `ls -l ~/.config/wireplumber/main.lua.d/` | 51, 52, 53 → `~/firestudio-tube/config/wireplumber/` (+54 gerada) | regra faltando = volta ao modo timer |
| W2 (auto) | regras aplicadas | props em `pw-dump` | as da 53 | WP não reiniciado depois da mudança |
| W3 (auto) | regra 54 por tamanho | `54-firestudio-tube-latencia-atual.lua` | = P5 do tamanho atual | ao reiniciar volta a latência de outro tamanho |
| W4 (auto) | install.sh linka a 53 | `grep 53- ~/firestudio-tube/install.sh` | sim | reinstalar perde o modo IRQ |
| W5 (manual) | status geral | `wpctl status; wpctl inspect <id>` | Tube Pro como padrão do MixDaw via JACK | — |

## J · JACK (pipewire-jack)

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| J1 (auto) | libjack | `jack_rt info` → `lib` | `~/.local/lib/pipewire-jack/libjack.so.0` | jackd real/libjack errada |
| J2 (auto) | buffer/taxa | `jack_rt info` | = grafo | troca não propagada |
| J3 (auto) | latência das portas físicas | `jack_rt info` (`FireStudio Tube Pro:*`) | captura Q+headroom; playback Q+interna | MixDaw alinha por esse número |
| J4 (auto) | xruns e maior intervalo | `jack_rt watch <s>` (sem portas) | 0 xrun; maior intervalo < 2,5 períodos; ciclos ≈ esperados | grafo parado ou atrasado |
| J5 (auto) | overrides | `~/.config/pipewire/jack.conf.d/` | nenhum | node.latency fixo briga com o painel |
| J6 (manual) | ida e volta pelo cabo | `auditoria.py --measure jack --confirmo-cabo-out3-line9` (`jack_rt rt`: pulsos Hann −20 dBFS, limiar 12 dB sobre o ruído) | = tabela ± 8 | resíduo > 10 % = componente escondido |
| J7 (manual) | troca de buffer com cliente aberto | `jack_rt watch 60` durante troca no painel | `buffer_changes` ≥ 1, sem xrun além da troca | clientes não recebem o callback |

## M · MixDaw

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| M1 (auto) | rodando e no grafo | `/proc/*/comm` MixDaw; cliente PW `application.name` | conectado via JACK | ALSA direto (sem PipeWire) ou sem dispositivo |
| M2 (auto) | threads RT | `ps -L -o cls,rtprio,comm -p <pid>` | callback em RT; workers do grafo em RT (meta) | xruns em buffer pequeno; picos com muitos plugins |
| M3 (auto) | compensação e monitoração | `~/.config/MixDaw/Settings.xml`: `mixdawRecordCompensationMs`, `mixdawMonitorReduceLatency`, `…PluginLatencyLimitMs` | compensação extra 0 | compensação manual mascara P5 |
| M4 (auto) | troca de buffer no JUCE | `jack_set_buffer_size_callback` no `juce_JackAudio.cpp` ou `patches/juce/0001*` | presente | Tracktion fatia blocos (8×32), latência errada |
| M5 (auto) | cache de latência | `latencyGeneration` em `AudioDevices.cpp` | presente | compensação velha após troca |
| M6 (auto c/ medida) | alinhamento | `--measure jack`: medido − informado | ±8 frames | takes fora do tempo |
| M7 (auto c/ medida) | resíduo do orçamento | medido − soma | ≤ 10 % | atraso não explicado (plugins, resampler) |
| M8 (manual) | Preferências › Áudio | MixDaw aberto | buffer e latência = JACK; diagnóstico sem xrun | painel de latência desatualizado |
| M9 (manual) | take alinhado | gravar o clique do cabo contra a base | dentro de ±8 amostras | compensação/latência interna erradas |
| M10 (manual) | monitoração de baixa latência | decisão de arquitetura do DAW: reduzir latência ao monitorar, plugins com PDC | só plugins sem latência no caminho monitorado | eco/atraso no fone |
| M11 (manual) | suíte | `MixDawTests "Audio devices"`, `"Monitoring latency"`, `"Live recording waveform"` | 0 falhas | regressão |

## C · Painel e serviços

| ID | o que medir | comando | esperado | se estiver fora |
|---|---|---|---|---|
| C1 (auto) | tamanhos oferecidos | `BUFFER_SIZES` em `layout.py` × driver | só válidos (48k: 32..2048; 96k: 64..2048) | tamanho inválido abre e falha |
| C2 (auto) | troca reabre a placa | `apply_buffer`/`reopen_tube_nodes` em `layout.py` | presentes | só force-quantum → trava em modo IRQ |
| C3 (auto) | serviços | `systemctl --user is-active firestudio-tube-ctl firestudio-tube-unmute pipewire wireplumber pipewire-pulse` | active | DSP sem controle; Loop mudo |
| C4 (auto) | painel aberto | processo firestudio-control | — | segura o controle ALSA |
| C5 (manual, altera) | todos os tamanhos trocam | `auditoria.py --sizes 32,64,128,256,512 --altera-sistema --modo-troca painel` | todos OK; estado restaurado | linha FALHA mostra a camada |
| C6 (manual) | troca com o MixDaw tocando e gravando | painel 32 ↔ 256 | sem craquelado; Preferências atualizam; take alinhado | ver M4/M5 |

## Como fechar

1. Tabela das FALHA/AVISO com camada e evidência. 2. Orçamento (frames, ms, %; MEDIDO/ESTIMADO; resíduo).
3. "Onde melhorar": item · camada · evidência · ganho (ms/%) · risco · esforço, falhas primeiro.
4. Registrar no vault (Sessões, Erros-e-Soluções, ADR se houver decisão) e guardar o `auditoria.json` com `--history`.
