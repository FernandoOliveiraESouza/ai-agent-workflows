# ai-agent-workflows

My personal system for orchestrating AI coding agents with [Claude Code](https://docs.claude.com/en/docs/claude-code):
a **kernel** of working rules that every session and sub-agent follows, **long-term memory** in an Obsidian vault,
**reusable skills** for jobs I repeat, and a **quality gate** enforced by hooks, so agents cannot call work "done"
without proof.

Everything here was built by orchestrating AI agents (Claude Code): I defined the rules, the problems and the
acceptance criteria, ran independent reviewer agents against the result, and approved each change. The agents wrote
the code.

> **Language note:** the prompts, rules, code comments and messages are in **Brazilian Portuguese**, because that is
> how I work day to day. The structure and the code are language-agnostic; translate the Markdown if you adopt it.

## What's inside

```
kernel/
  KERNEL.template.md      the master prompt (generic version of mine)
  codigo-checklist.md     engineering checklist injected when an agent opens code
hooks/
  padrao-subagente.py     SubagentStart hook
  checklist-codigo.py     PostToolUse / SessionStart hook
  portao/                 the quality gate (guarda.py, final.py, comum.py, so-executar.txt)
install/ligar-hooks.py    turns the hooks on/off in ~/.claude/settings.json
tests/test_portao.py      344-check test battery for the quality gate
skills/                   six Claude Code skills (see below)
```

### Kernel (`kernel/KERNEL.template.md`)
A single master prompt, imported from `~/.claude/CLAUDE.md`, that applies to every session and every sub-agent, in
any folder and for any kind of work (code, video, research, system config). Projects and skills only add specifics
and never override it. Sections:

1. **Interpreting requests**: find the real goal behind a simple request and cover what it implies (edge cases, side
   effects, risks); anything beyond the goal is a suggestion, not a change.
2. **How to work**: get it right the first time, look before assuming, **prove before delivering** (tests for code,
   automated QA for video, dated sources for research), autonomy with limits (ask before anything irreversible,
   anything that leaves the machine or spends money), the user's resources are finite, repeated processes become
   skills.
3. **Security first**: protect the user and their users from intrusion and data leaks; privacy law second; warn
   before risk; independent review of sensitive code; check open ports.
4. **Memory in an Obsidian vault**: what to read at session start, when to grep the vault before guessing, and a
   table of where each kind of information is written (diary, ideas, plans, decisions as ADRs, projects).
5. **Sub-agents and model choice**: split work when parts are independent, and pick the model per sub-task by the
   capability it needs (Opus for hard reasoning and final review, Sonnet for regular implementation and research,
   Haiku for mechanical tasks).
6. **Modules per kind of work** (code checklist, video QA, sound effects).
7. **Projects**: one registry of all projects, one folder per agent, "tool-only" repositories that agents may run
   but never modify.
8. **Engineering standard**: task levels by risk (level 3 = security, user data, file formats, concurrency,
   real-time, low-level or >300 lines → mandatory independent review), check APIs against the installed version,
   reproduce bugs before fixing, atomic writes, never weaken a test to get green, git rules for many agents working in
   parallel, and the quality gate below.

### Hooks

| Hook | Event | What it does |
|---|---|---|
| `padrao-subagente.py` | `SubagentStart` (Explore, Plan) | These built-in agents don't receive `CLAUDE.md`; the hook injects section 8 of the kernel (the engineering standard), read live from the file. |
| `checklist-codigo.py` | `PostToolUse` (Read/Edit/Write…), `SessionStart` (compact/clear) | When an agent opens or edits a code file **outside** the session's folder (where Claude Code's path-scoped rules don't apply), injects the engineering checklist once per context, and again after compaction. File patterns come from the checklist's own `paths:` front matter. |
| `portao/guarda.py` | `PreToolUse` | **Asks the user** before git commands that can destroy work or sweep in other agents' files (`add -A`, `commit -a`, `reset --hard`, discarding `checkout`/`restore`, `clean`, `stash`, `push --force`, `rebase`, `branch -D`, `--no-verify`, `core.hooksPath`…), including when hidden inside `bash -c`, `eval`, `$( )`, backticks, `if`/`for`, heredocs or after a `cd`. Asks before edits that skip/isolate tests or remove assertions. **Denies** any write into folders listed in `so-executar.txt`, and denies a sub-agent's hand-back if it changed code without verifying. |
| `portao/final.py` | `Stop`, `SubagentStop` | Blocks an agent from finishing if it changed code (also via `sed -i`, `cat >`, `tee`, `cp`, `patch`, inline Python) and did not run a build, tests, lint or the program **after** the last change, or if a large (>300 lines) or sensitive change (auth, passwords, tokens, payments, crypto) had no independent review. The agent can instead state `Sem verificação automática:` / `Sem revisão independente:` with how it verified or why. After 3 consecutive blocks with no new code change it lets go and warns the user. |
| `portao/comum.py` | (library) | Shell parsing (quotes, substitutions, `cd` tracking, variables), transcript reading, test and sensitive-file detection, block counting. |

All hooks **fail open**: an unexpected error never blocks the agent (and the installer appends `|| true` to every
command, because a missing hook file would otherwise make Python exit with code 2 and block every tool). Hooks only
use the Python standard library.

Known limits: the gate checks that proof was run, not that it was meaningful (an agent can run any test just to
pass), which is why the kernel also requires independent review and an honest final report. It does not detect a
test weakened by changing the expected value, or edits made by a generated script.

### Skills

| Skill | What it does | Needs |
|---|---|---|
| `video-qa` | One-command delivery check for a rendered video: format/fps/duration against a spec, integrated loudness and true peak (target −14 LUFS, ≤ −1 dBTP), black frames, frozen frames, "rewind" detection (a moving frame that reappears almost identical 0.3–5 s later, which looks like a glitch) and a contact sheet with an optional caption guide line. Exits 1 on failure. | `ffmpeg`/`ffprobe`, Python + `numpy` |
| `sfx-magnific` | Workflow for sound effects synced to on-screen action: find the exact action frame, reuse a local library or download free sounds first (Kenney CC0, Mixkit), and only generate with paid AI credits (Magnific MCP) after explicit approval. `sfx_peak.py` finds each file's energy peak and attack, counts peaks and clicks, trims around the peak and normalises to −1 dBFS, printing the offset to place the cue. | `ffmpeg`, Python + `numpy`; Magnific MCP optional |
| `revisar` | Local code reviewer (nothing leaves the machine). `revisar.py` collects the diff (uncommitted, `--base`, `--commit`, `--range`, `--pr N`, or whole paths), runs analyzers only on changed lines (clippy, ruff, shellcheck, semgrep with `--metrics off`, secret patterns), the project's rules and past lessons. The skill then runs specialist reviewer agents in parallel (correctness, tests, security, project rules), an adversarial verifier that tries to refute each finding, re-review of previous findings, and stores per-repository lessons. | `git`; optional `gh`, `cargo clippy`, `ruff`, `shellcheck`, `semgrep` |
| `reabrir-sessoes` | After a crash (desktop restart, terminal closed), lists the Claude Code conversations that stopped in the last hours and are not open, cross-checked with the journal to tell a crash from a normal close, and reopens each one in its own GNOME Terminal window, clearing inherited `CLAUDE*` variables so the transcript keeps saving. | Linux, GNOME Terminal, `journalctl`, Claude Code CLI |
| `projetos` | `/projetos` menu: lists all projects from the vault registry (most recent first, finished last), resumes one (reads its notes and the project's `CLAUDE.md`/`AGENTS.md`, summarises where it stopped) or creates a new one with its folder, README and registry entries. | Python (stdlib) |
| `latencia-audio` | Example of a hardware-specific diagnostic skill: audits the whole audio chain of my own setup (PreSonus FireStudio Tube → my Linux driver → ALSA → PipeWire/WirePlumber → JACK → MixDaw, my own DAW) in one read-only command: stalled PCM, buffer size consistent across layers, isochronous queue recomputed from the driver code, declared vs. real latency, budget in ms and %, kernel/RT priorities, optional loopback measurement and buffer-switch test (only with explicit flags, restored in `finally`), and a prioritised list of improvements. Paths are configurable (`LATENCIA_DRIVER_REPO`, `LATENCIA_PANEL_REPO`, `LATENCIA_DAW_REPO`, `LATENCIA_HISTORY`). 27 unit tests for the pure functions. | Linux, PipeWire tools, `journalctl`, `gcc` + JACK headers (for the `jack_rt` helper), the hardware |

## Install

```bash
git clone https://github.com/FernandoOliveiraESouza/ai-agent-workflows
cd ai-agent-workflows

# 1. Kernel and checklist
mkdir -p ~/.claude/kernel
cp kernel/codigo-checklist.md ~/.claude/kernel/
cp kernel/KERNEL.template.md ~/.claude/kernel/KERNEL.md      # then edit the <...> placeholders
echo '@~/.claude/kernel/KERNEL.md' >> ~/.claude/CLAUDE.md
# optional: also inject the checklist inside the session folder through a path-scoped rule
mkdir -p ~/.claude/rules && ln -s ~/.claude/kernel/codigo-checklist.md ~/.claude/rules/engenharia-checklist.md

# 2. Hooks
mkdir -p ~/.claude/hooks
cp -r hooks/* ~/.claude/hooks/
python3 -I install/ligar-hooks.py            # checklist + Explore/Plan hook
python3 -I install/ligar-hooks.py --portao   # also the quality gate (guarda.py + final.py)

# 3. Skills (copy the ones you want)
mkdir -p ~/.claude/skills
cp -r skills/video-qa skills/revisar ~/.claude/skills/
```

`ligar-hooks.py` needs an existing `~/.claude/settings.json` (if you have none: `echo '{}' > ~/.claude/settings.json`). It backs up `settings.json` to `~/.claude/backups/`, never duplicates entries, leaves other hooks alone
and writes atomically. Use `--desligar` (and `--desligar --portao`) to turn them off. If you prefer to edit
`~/.claude/settings.json` by hand, this is what it adds:

```json
{
  "hooks": {
    "SubagentStart": [{"matcher": "Explore|Plan", "hooks": [{"type": "command", "command": "python3 -I \"$HOME/.claude/hooks/padrao-subagente.py\" || true", "timeout": 10}]}],
    "PostToolUse": [{"matcher": "Read|Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": "python3 -I \"$HOME/.claude/hooks/checklist-codigo.py\" || true", "timeout": 10}]}],
    "SessionStart": [{"matcher": "compact|clear", "hooks": [{"type": "command", "command": "python3 -I \"$HOME/.claude/hooks/checklist-codigo.py\" || true", "timeout": 10}]}],
    "PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit|SubagentHandback", "hooks": [{"type": "command", "command": "python3 -I \"$HOME/.claude/hooks/portao/guarda.py\" || true", "timeout": 10}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "python3 -I \"$HOME/.claude/hooks/portao/final.py\" || true", "timeout": 30}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "python3 -I \"$HOME/.claude/hooks/portao/final.py\" || true", "timeout": 30}]}]
  }
}
```

Configuration through environment variables (all optional):

| Variable | Default | Used by |
|---|---|---|
| `AGENT_KERNEL` | `~/.claude/kernel/KERNEL.md` | `padrao-subagente.py`, `checklist-codigo.py` |
| `AGENT_CHECKLIST` | `~/.claude/kernel/codigo-checklist.md` | `checklist-codigo.py`, `padrao-subagente.py` |
| `PROJETOS_REGISTRO` | `~/vault/Projetos/00-PROJETOS.md` | `projetos` skill |
| `REABRIR_CWD` | `$HOME` | `reabrir-sessoes` skill |

Tool-only folders: list them, one per line, in `~/.claude/hooks/portao/so-executar.txt`; the gate then denies any
write inside them.

## Tests

```bash
python3 -I tests/test_portao.py                                   # 344 checks, runs on a temporary copy of the hooks
python3 -I skills/latencia-audio/scripts/test_auditoria.py        # 27 unit tests, no hardware needed
```

The gate battery feeds the hooks the same JSON Claude Code sends and checks every decision (ask / deny / allow /
block). Each finding from the two independent reviews became a test case.

Requirements: Python 3 (tested with 3.12.3) and Claude Code (hook contract checked on Claude Code 2.1.309). Linux for the
`reabrir-sessoes` and `latencia-audio` skills.

## License

MIT, see [LICENSE](LICENSE).

---

### Em português

Este é o sistema que uso para orquestrar agentes de IA no Claude Code: um kernel de regras que vale para toda sessão e
todo subagente, memória de longo prazo num vault do Obsidian, skills reaproveitáveis e um portão de qualidade feito
com hooks, que não deixa o agente dizer "pronto" sem prova (build, testes ou revisão independente). Todo o código foi
escrito por agentes de IA (Claude Code) orquestrados por mim: eu defini as regras, os problemas e os critérios de
aceite, mandei revisores independentes conferirem e aprovei cada mudança. Os prompts e comentários estão em português.
