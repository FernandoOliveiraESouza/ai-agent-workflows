#!/bin/bash
# Reabre uma conversa do Claude Code numa janela nova do GNOME Terminal.
# uso: abrir.sh <session-id> [mensagem inicial]
#
# Limpa as variáveis CLAUDE* herdadas de quem chamou: aberta de dentro de outra sessão, a conversa
# herda CLAUDE_CODE_CHILD_SESSION e passa a rodar com "Transcript saving is off" (não grava o histórico).
set -u
if [ "${1:-}" = "--dentro" ]; then
  shift
  for v in $(compgen -e | grep -E '^CLAUDE'); do unset "$v"; done
  cd "${REABRIR_CWD:-$HOME}" || exit 1
  if [ -n "${2:-}" ]; then
    "$HOME/.local/bin/claude" --resume "$1" --permission-mode auto "$2"
  else
    "$HOME/.local/bin/claude" --resume "$1" --permission-mode auto
  fi
  exec bash
fi

[ -n "${1:-}" ] || { echo "uso: abrir.sh <session-id> [mensagem inicial]" >&2; exit 2; }
limpar=()
for v in $(compgen -e | grep -E '^CLAUDE'); do limpar+=(-u "$v"); done
env "${limpar[@]}" REABRIR_CWD="${REABRIR_CWD:-$HOME}" \
  gnome-terminal --window -- "$(readlink -f "$0")" --dentro "$@"
