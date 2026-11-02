"""Bateria do portão de qualidade: roda os hooks como o Claude Code roda (JSON no stdin) e confere a decisão.
Roda numa cópia temporária de hooks/portao (com um so-executar.txt de teste), sem tocar no ~/.claude real:
    python3 -I tests/test_portao.py
PORTAO_DIR=<pasta> testa outra cópia (essa precisa listar a pasta de $PORTAO_FERRAMENTA no so-executar.txt)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAIXA = tempfile.mkdtemp()   # tudo o que o teste cria fica aqui e é apagado no fim
REA = os.environ.get('PORTAO_FERRAMENTA') or os.path.join(CAIXA, 'ferramenta')   # "pasta que só se executa" de teste
os.makedirs(os.path.join(REA, 'scripts'), exist_ok=True)
open(os.path.join(REA, 'README.md'), 'w').close()
P = os.environ.get('PORTAO_DIR')
if not P:
    P = os.path.join(CAIXA, 'portao')
    shutil.copytree(os.path.join(RAIZ, 'hooks', 'portao'), P, ignore=shutil.ignore_patterns('__pycache__'))
    with open(os.path.join(P, 'so-executar.txt'), 'w') as f:
        f.write('# lista de teste\n' + REA + '\n')
falhas = []
ESTADO = tempfile.mkdtemp()   # a contagem de bloqueios dos testes não vai para a pasta real
os.environ['XDG_RUNTIME_DIR'] = ESTADO


def rodar(script, payload):
    r = subprocess.run([sys.executable, '-I', os.path.join(P, script)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        falhas.append(f'{script}: saiu com {r.returncode}: {r.stderr[-300:]}')
    return json.loads(r.stdout) if r.stdout.strip() else None


def confere(rotulo, obtido, esperado):
    ok = obtido == esperado
    print(('ok    ' if ok else 'FALHA ') + rotulo + ('' if ok else f'  (esperado {esperado}, veio {obtido})'))
    if not ok:
        falhas.append(rotulo)


# ---------- guarda.py: git ----------
def git(cmd):
    out = rodar('guarda.py', {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': cmd}})
    return (out or {}).get('hookSpecificOutput', {}).get('permissionDecision', 'segue')

perigosos = ['git add -A', 'git add .', 'git add -u', 'git -C ~/projeto add --all', 'git commit -am "x"',
             'git commit -a -m x', 'git commit --no-verify -m x', 'git reset --hard HEAD~1', 'git checkout -- .',
             'git checkout -- src/a.rs', 'git checkout -f main', 'git restore src/a.rs', 'git restore -W -S a',
             'git clean -fd', 'git clean -fdx', 'git stash', 'git stash pop', 'git push --force', 'git push -f origin x',
             'git push origin +main', 'git push origin :velha', 'git push --force-with-lease', 'git rebase main',
             'git branch -D feat', 'git filter-branch --all', 'git reflog expire --all', 'git worktree remove --force ../x',
             'cd ~/projeto && git add -A && git commit -m x', 'sudo git clean -fd', 'GIT_DIR=x git reset --hard',
             'git -c core.x=y stash', 'git switch --discard-changes main', 'git push --no-verify']
seguros = ['git status', 'git diff', 'git add src/a.rs tests/b.rs', 'git commit -m "msg"', 'git log --oneline',
           'git checkout -b nova', 'git checkout main', 'git restore --staged a.rs', 'git clean -n', 'git clean -fdn',
           'git stash list', 'git stash show', 'git push origin main', 'git push -u origin feat', 'git rebase --continue',
           'git branch -d feat', 'git commit --amend -m x', 'echo "git add -A"', 'grep -r "git reset --hard" .',
           'cd ~/git && ls', 'git -C ~/projeto status', 'git fetch --all', 'git pull', 'git worktree list',
           'ls -la', 'cargo test', 'meu-backup-snapshot']
for c in perigosos:
    confere(f'git perigoso pede ok: {c}', git(c), 'ask')
for c in seguros:
    confere(f'git seguro segue:      {c}', git(c), 'segue')


# ---------- guarda.py: testes ----------
def edicao(nome, entrada):
    out = rodar('guarda.py', {'hook_event_name': 'PreToolUse', 'tool_name': nome, 'tool_input': entrada})
    return (out or {}).get('hookSpecificOutput', {}).get('permissionDecision', 'segue')

confere('Edit que põe #[ignore] num teste Rust pede ok',
        edicao('Edit', {'file_path': '/p/src/lib.rs', 'old_string': '#[test]\nfn a() {', 'new_string': '#[test]\n#[ignore]\nfn a() {'}), 'ask')
confere('Edit que tira assert de tests/ pede ok',
        edicao('Edit', {'file_path': '/p/tests/t.py', 'old_string': 'x = f()\n    assert x == 1\n    assert y', 'new_string': 'x = f()\n    assert y'}), 'ask')
confere('Edit que põe it.skip pede ok',
        edicao('Edit', {'file_path': '/p/a.test.ts', 'old_string': "it('a', () => {", 'new_string': "it.skip('a', () => {"}), 'ask')
confere('Edit que troca assert por outro assert segue',
        edicao('Edit', {'file_path': '/p/tests/t.py', 'old_string': 'assert x == 1', 'new_string': 'assert x == 2'}), 'segue')
confere('Edit que acrescenta asserts segue',
        edicao('Edit', {'file_path': '/p/tests/t.py', 'old_string': 'assert a', 'new_string': 'assert a\n    assert b'}), 'segue')
confere('Edit em código normal (não teste) que tira assert segue',
        edicao('Edit', {'file_path': '/p/src/app.py', 'old_string': 'assert x\nrun()', 'new_string': 'run()'}), 'segue')
confere('Edit que troca .expect( por ? no Rust de teste segue',
        edicao('Edit', {'file_path': '/p/tests/t.rs', 'old_string': 'let v = f().expect("x");\nassert!(v);', 'new_string': 'let v = f()?;\nassert!(v);'}), 'segue')
confere('Edit em .md segue',
        edicao('Edit', {'file_path': '/p/README.md', 'old_string': 'assert', 'new_string': ''}), 'segue')
confere('MultiEdit que tira EXPECT_ de teste C++ pede ok',
        edicao('MultiEdit', {'file_path': '/p/tests/a_test.cpp', 'edits': [{'old_string': 'EXPECT_EQ(a, 1);\nEXPECT_TRUE(b);', 'new_string': 'EXPECT_TRUE(b);'}]}), 'ask')
with tempfile.TemporaryDirectory() as d:
    t = os.path.join(d, 'test_x.py')
    open(t, 'w').write('def test_a():\n    assert f() == 1\n    assert g() == 2\n')
    confere('Write que reescreve teste com menos asserts pede ok',
            edicao('Write', {'file_path': t, 'content': 'def test_a():\n    assert g() == 2\n'}), 'ask')
    confere('Write de teste novo segue',
            edicao('Write', {'file_path': os.path.join(d, 'test_novo.py'), 'content': 'def test_b():\n    assert 1\n'}), 'segue')
    confere('Write de teste novo já pulado pede ok',
            edicao('Write', {'file_path': os.path.join(d, 'test_novo2.py'), 'content': 'import pytest\n@pytest.mark.skip\ndef test_b():\n    assert 1\n'}), 'ask')


# ---------- final.py ----------
def conversa(d, linhas):
    caminho = os.path.join(d, f'c{len(os.listdir(d))}.jsonl')
    with open(caminho, 'w') as f:
        for e in linhas:
            f.write(json.dumps(e) + '\n')
    return caminho

def pedido(pid, texto='faz'):
    return {'type': 'user', 'promptId': pid, 'message': {'role': 'user', 'content': texto}}

def usa(nome, entrada):
    return {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': nome, 'input': entrada}]}}

def resultado(pid):
    return {'type': 'user', 'promptId': pid, 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'ok'}]}}

def parar(caminho, pid, final='Pronto.', sub=False, tipo='general-purpose'):
    if sub:
        p = {'hook_event_name': 'SubagentStop', 'agent_type': tipo, 'agent_transcript_path': caminho,
             'transcript_path': '/nao/existe', 'prompt_id': pid, 'last_assistant_message': final, 'stop_hook_active': False}
    else:
        p = {'hook_event_name': 'Stop', 'transcript_path': caminho, 'prompt_id': pid, 'last_assistant_message': final,
             'stop_hook_active': False}
    out = rodar('final.py', p)
    return (out or {}).get('decision', 'termina')

E = lambda arq, n=3: usa('Edit', {'file_path': arq, 'old_string': 'a', 'new_string': '\n'.join(['x'] * n)})
B = lambda cmd: usa('Bash', {'command': cmd})
with tempfile.TemporaryDirectory() as d:
    c = conversa(d, [pedido('P1'), E('/p/a.py'), resultado('P1')])
    confere('mudou código e não testou: barra', parar(c, 'P1'), 'block')
    c = conversa(d, [pedido('P1'), E('/p/a.py'), resultado('P1'), B('pytest -q'), resultado('P1')])
    confere('mudou e rodou pytest depois: termina', parar(c, 'P1'), 'termina')
    c = conversa(d, [pedido('P1'), B('pytest -q'), resultado('P1'), E('/p/a.py'), resultado('P1')])
    confere('testou ANTES da última mudança: barra', parar(c, 'P1'), 'block')
    c = conversa(d, [pedido('P1'), E('/p/x/script.py'), resultado('P1'), B('python3 x/script.py --ajuda'), resultado('P1')])
    confere('rodou o próprio arquivo mudado: termina', parar(c, 'P1'), 'termina')
    c = conversa(d, [pedido('P1'), E('/p/a.py'), resultado('P1')])
    confere('declarou "Sem verificação automática:": termina',
            parar(c, 'P1', final='Feito.\nSem verificação automática: conferi a tela no app aberto.'), 'termina')
    confere('declaração sem acento também vale',
            parar(c, 'P1', final='sem verificacao automatica: hardware'), 'termina')
    c = conversa(d, [pedido('P0'), E('/p/a.py'), resultado('P0'), {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'fim'}]}},
                     pedido('P1', 'agora explica'), {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}]}}])
    confere('código mudado num pedido ANTERIOR não conta: termina', parar(c, 'P1'), 'termina')
    c = conversa(d, [pedido('P1'), E('/p/notas.md'), resultado('P1')])
    confere('só mudou .md: termina', parar(c, 'P1'), 'termina')
    c = conversa(d, [pedido('P1'), E('/p/a.rs', 400), resultado('P1'), B('cargo test'), resultado('P1')])
    confere('mudança de 400 linhas testada mas sem revisão: barra', parar(c, 'P1'), 'block')
    c = conversa(d, [pedido('P1'), E('/p/a.rs', 400), resultado('P1'), B('cargo test'), resultado('P1'),
                     usa('Skill', {'skill': 'revisar'}), resultado('P1')])
    confere('mudança grande testada e com /revisar: termina', parar(c, 'P1'), 'termina')
    c = conversa(d, [pedido('P1'), E('/p/src/auth/login.ts'), resultado('P1'), B('npm test'), resultado('P1')])
    confere('arquivo sensível sem revisão: barra', parar(c, 'P1'), 'block')
    confere('arquivo sensível com "Sem revisão independente:": termina',
            parar(c, 'P1', final='Sem revisão independente: mudança só de texto de log.'), 'termina')
    c = conversa(d, [pedido('P1'), E('/p/src/auth/login.ts'), resultado('P1'), B('npm test'), resultado('P1'),
                     usa('Agent', {'description': 'Revisão independente do login', 'prompt': 'revise'}), resultado('P1')])
    confere('arquivo sensível com agente revisor: termina', parar(c, 'P1'), 'termina')
    # continuação depois de um bloqueio (mesmo pedido): a mudança antiga ainda conta
    c = conversa(d, [pedido('P1'), E('/p/a.py'), resultado('P1'), {'type': 'user', 'promptId': 'P1', 'message': {'role': 'user', 'content': 'Stop hook feedback: ...'}}])
    confere('depois de um bloqueio, sem testar ainda: barra de novo', parar(c, 'P1'), 'block')
    # subagente
    c = conversa(d, [pedido('P9'), E('/p/b.go'), resultado('P9')])
    confere('subagente mudou código e não testou: barra', parar(c, 'P9', sub=True), 'block')
    c = conversa(d, [pedido('P9'), E('/p/b.go'), resultado('P9'), B('go test ./...'), resultado('P9')])
    confere('subagente testou: termina', parar(c, 'P9', sub=True), 'termina')
    confere('agente interno sem tipo: termina', parar(c, 'P9', sub=True, tipo=''), 'termina')
    # entradas quebradas: falha aberta
    confere('conversa inexistente: termina', parar('/nao/existe.jsonl', 'P1'), 'termina')
    with open(os.path.join(d, 'lixo.jsonl'), 'w') as f:
        f.write('{quebrado\n\n[1,2]\n')
    confere('conversa com lixo: termina', parar(os.path.join(d, 'lixo.jsonl'), 'P1'), 'termina')

r = subprocess.run([sys.executable, '-I', os.path.join(P, 'final.py')], input='não é json', capture_output=True, text=True)
confere('entrada inválida no final.py: sai 0 sem decisão', (r.returncode, r.stdout.strip()), (0, ''))
r = subprocess.run([sys.executable, '-I', os.path.join(P, 'guarda.py')], input='', capture_output=True, text=True)
confere('entrada vazia no guarda.py: sai 0 sem decisão', (r.returncode, r.stdout.strip()), (0, ''))

# ================= revisão independente de 2026-10-23: cada achado vira teste =================
# --- guarda.py: git escondido ou disfarçado (pede ok) e git só citado em texto (segue) ---
perigosos2 = ['bash -c "git add -A"', "sh -c 'cd x && git reset --hard'", 'eval "git stash"', '(git add -A)',
              'x=$(git stash)', 'a=`git stash` b', 'watch -n 5 "git stash"', 'git add \\\n  -A',
              'git submodule foreach git reset --hard', "git submodule foreach --recursive 'git clean -fd'",
              'find . -name x -exec git checkout -- {} \\;', 'git add ./', 'git add ..', 'git branch -df feat',
              'git branch -d -f feat', 'git -c core.hooksPath=/dev/null commit -m x', 'git rm -rf .',
              'git rm -r --cached .', 'git checkout -B main', 'nice -n 10 git reset --hard', 'timeout 30 git clean -fd',
              'xargs -I {} git checkout -- {}', "bash <<'EOF'\ncd x\ngit reset --hard\nEOF", 'git switch -C main',
              'systemd-run --user --scope -p MemoryMax=1G git stash', 'git status; git stash',
              'git log --oneline | head && git reset --hard HEAD~1', 'git status\ngit add -A']
seguros2 = ["git commit -F - <<'EOF'\nmensagem que cita git add -A e git stash\nEOF",
            'git commit -m "$(cat <<\'EOF\'\nCorrige: nunca usar git reset --hard; git add -A\nEOF\n)"',
            'echo "x ; git add . y"', "cat > s.sh <<'EOF'\ngit reset --hard\nEOF",
            'gh pr create --title t --body "rode git stash antes; depois git add -A"', 'git log --grep "git add -A"',
            'git commit -m "uso: git stash pop"', "python3 - <<'EOF'\nprint('git reset --hard')\nEOF",
            'git checkout -b nova-branch', 'git rm src/velho.rs', 'git add src/ tests/a.rs',
            'grep -rn "git stash" . | head', 'git status # depois: git reset --hard', 'git pull --rebase']
for c in perigosos2:
    confere(f'git escondido pede ok: {c!r}', git(c), 'ask')
for c in seguros2:
    confere(f'git só citado segue:   {c[:60]!r}', git(c), 'segue')

with tempfile.TemporaryDirectory() as repo:   # checkout de arquivo sem "--" também descarta
    os.makedirs(os.path.join(repo, 'src'))
    open(os.path.join(repo, 'src', 'a.rs'), 'w').close()

    def git_em(cmd, cwd=repo):
        out = rodar('guarda.py', {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'cwd': cwd,
                                  'tool_input': {'command': cmd}})
        return (out or {}).get('hookSpecificOutput', {}).get('permissionDecision', 'segue')
    confere('checkout de arquivo sem "--": pede ok', git_em('git checkout src/a.rs'), 'ask')
    confere('checkout HEAD arquivo: pede ok', git_em('git checkout HEAD src/a.rs'), 'ask')
    confere('checkout de branch (não é arquivo): segue', git_em('git checkout main'), 'segue')
    confere('checkout de arquivo com -C de outra pasta: pede ok', git_em(f'git -C {repo} checkout src/a.rs', '/'), 'ask')

# --- guarda.py: teste enfraquecido ---
casos_ask = [
    ('assert comentado (Python)', 'Edit', {'file_path': '/p/tests/t.py', 'old_string': 'assert x == 1\n    assert y',
                                           'new_string': '# assert x == 1\n    assert y'}),
    ('assert_eq! comentado (Rust)', 'Edit', {'file_path': '/p/tests/t.rs', 'old_string': 'assert_eq!(a, 1);\n    assert!(b);',
                                             'new_string': '// assert_eq!(a, 1);\n    assert!(b);'}),
    ('EXPECT_EQ dentro de /* */ (C++)', 'Edit', {'file_path': '/p/tests/a_test.cpp', 'old_string': 'EXPECT_EQ(a, 1);',
                                                 'new_string': '/* EXPECT_EQ(a, 1); */'}),
    ('it.only isola um teste', 'Edit', {'file_path': '/p/a.test.ts', 'old_string': "it('a', () => {",
                                        'new_string': "it.only('a', () => {"}),
    ('fit isola um teste', 'Edit', {'file_path': '/p/a.spec.js', 'old_string': "it('a', () => {",
                                    'new_string': "fit('a', () => {"}),
    ('fdescribe isola um bloco', 'Edit', {'file_path': '/p/a.spec.js', 'old_string': "describe('a', () => {",
                                          'new_string': "fdescribe('a', () => {"}),
    ('tirar #[test] some com o teste', 'Edit', {'file_path': '/p/src/lib.rs',
                                                'old_string': '#[test]\nfn a() {}\n#[test]\nfn b() {}',
                                                'new_string': '#[test]\nfn a() {}\nfn b() {}'}),
    ('renomear def test_ desliga o teste', 'Edit', {'file_path': '/p/tests/test_x.py', 'old_string': 'def test_soma():',
                                                    'new_string': 'def soma_antiga():'}),
    ('tirar expectEquals (JUCE)', 'Edit', {'file_path': '/p/Source/MixerTests.cpp',
                                                    'old_string': 'expectEquals (a, 1);\n        expect (b);',
                                                    'new_string': 'expect (b);'}),
    ('tirar expectWithinAbsoluteError', 'Edit', {'file_path': '/p/Source/DspTests.cpp',
                                                'old_string': 'expectWithinAbsoluteError (x, 0.0f, 1e-6f);',
                                                'new_string': ''}),
]
casos_segue = [
    ('regex.test( em código comum não é teste', 'Edit', {'file_path': '/p/src/util.js',
                                                        'old_string': 'if (re.test(s)) { assert(x) }',
                                                        'new_string': 'if (re.test(s)) {}'}),
    ('trocar expect por expectEquals', 'Edit', {'file_path': '/p/Source/MixerTests.cpp', 'old_string': 'expect (a == 1);',
                                               'new_string': 'expectEquals (a, 1);'}),
    ('comentário novo sem mexer em assert', 'Edit', {'file_path': '/p/tests/t.py', 'old_string': 'assert x',
                                                    'new_string': '# confere o x\n    assert x'}),
    ('URL com // não apaga o resto da linha', 'Edit', {'file_path': '/p/tests/t.js',
                                                      'old_string': "it('a', () => { expect(get('http://x')).toBe(1) })",
                                                      'new_string': "it('a', () => { expect(get('https://x')).toBe(1) })"}),
]
for rotulo, nome, ent in casos_ask:
    confere(f'teste enfraquecido pede ok: {rotulo}', edicao(nome, ent), 'ask')
for rotulo, nome, ent in casos_segue:
    confere(f'teste mantido segue: {rotulo}', edicao(nome, ent), 'segue')
with tempfile.TemporaryDirectory() as d:
    confere('teste enfraquecido segue: skipif com condição em teste novo',
            edicao('Write', {'file_path': os.path.join(d, 'test_c.py'), 'content':
                             'import sys, pytest\n@pytest.mark.skipif(sys.platform == "win32", reason="sem ALSA")\n'
                             'def test_c():\n    assert 1\n'}), 'segue')


# --- final.py: declarações, Bash, verificação de verdade, erros, caminhos sensíveis, limite ---
def parar2(linhas, pid='P1', final='Pronto.', sessao=None, sub=False, completo=False):
    with tempfile.TemporaryDirectory() as d:
        caminho = conversa(d, linhas)
        p = {'hook_event_name': 'SubagentStop' if sub else 'Stop', 'session_id': sessao or os.urandom(6).hex(),
             'transcript_path': caminho, 'prompt_id': pid, 'last_assistant_message': final, 'stop_hook_active': False}
        if sub:
            p.update(agent_type='general-purpose', agent_id='ag1', agent_transcript_path=caminho)
        if pid is None:
            del p['prompt_id']
        out = rodar('final.py', p) or {}
    return out if completo else out.get('decision', 'termina')

so_edita = [pedido('P1'), E('/p/a.py'), resultado('P1')]
confere('declaração em negrito vale', parar2(so_edita, final='**Sem verificação automática**: vi na tela.'), 'termina')
confere('declaração com travessão vale', parar2(so_edita, final='Sem verificação automática — testei à mão.'), 'termina')
confere('declaração de revisão entre crases vale',
        parar2([pedido('P1'), E('/p/src/auth/x.ts'), resultado('P1'), B('npm test'), resultado('P1')],
               final='`Sem revisão independente`: só texto de log.'), 'termina')
confere('sem prompt_id não faz nada', parar2(so_edita, pid=None), 'termina')

for rotulo, cmd in [('sed -i', "sed -i 's/a/b/' src/a.py"), ('cat > heredoc', "cat > x.py <<'EOF'\nprint(1)\nEOF"),
                    ('tee', 'echo x | tee src/a.rs'), ('cp para código', 'cp /tmp/novo.py src/a.py'),
                    ('git apply', 'git apply correcao.diff'), ('perl -pi', "perl -pi -e 's/a/b/' a.c")]:
    confere(f'mudou código pelo Bash ({rotulo}) e não testou: barra',
            parar2([pedido('P1'), B(cmd), resultado('P1')]), 'block')
confere('mudou pelo Bash e rodou pytest depois: termina',
        parar2([pedido('P1'), B("sed -i 's/a/b/' src/a.py"), resultado('P1'), B('pytest -q'), resultado('P1')]), 'termina')
confere('criou x.py por heredoc e rodou: termina',
        parar2([pedido('P1'), B("cat > x.py <<'EOF'\nprint(1)\nEOF\npython3 x.py"), resultado('P1')]), 'termina')
confere('sed -i e cargo test no mesmo comando, nessa ordem: termina',
        parar2([pedido('P1'), B("sed -i 's/a/b/' src/a.rs && cargo test"), resultado('P1')]), 'termina')
confere('cargo test e depois sed -i no mesmo comando: barra',
        parar2([pedido('P1'), B("cargo test && sed -i 's/a/b/' src/a.rs"), resultado('P1')]), 'block')
confere('heredoc grande conta as linhas (400) e pede revisão',
        parar2([pedido('P1'), B("cat > big.py <<'EOF'\n" + 'x = 1\n' * 400 + "EOF\npython3 big.py"), resultado('P1')]),
        'block')

for cmd in ['cat a.py', 'git diff a.py', 'git add a.py && git commit -m "make sure tests pass"', 'ls tests/',
            'echo "make sure"', 'grep contest a.py', 'head -50 a.py', 'git log -p a.py']:
    confere(f'"verificação" que não verifica ({cmd}): barra',
            parar2([pedido('P1'), E('/p/a.py'), resultado('P1'), B(cmd), resultado('P1')]), 'block')
for cmd in ['systemd-run --user --scope -p MemoryMax=26G nice -n 10 cargo test', 'xvfb-run -a npm test',
            './run_tests.sh', 'python3 -m py_compile a.py', 'bash -n deploy.sh',
            'cd /p && make -j4', 'timeout 600 ctest --output-on-failure', 'npx vitest run', 'tox -e py312',
            'flake8 a.py', 'pnpm lint', 'go vet ./...', '(setsid gio launch ~/Desktop/App.desktop >/dev/null 2>&1 &)']:
    confere(f'verificação de verdade ({cmd}): termina',
            parar2([pedido('P1'), E('/p/a.py'), resultado('P1'), B(cmd), resultado('P1')]), 'termina')

negada = [pedido('P1'), {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
    {'type': 'tool_use', 'id': 'tu1', 'name': 'Edit', 'input': {'file_path': '/p/a.py', 'old_string': 'a', 'new_string': 'b'}}]}},
          {'type': 'user', 'promptId': 'P1', 'message': {'role': 'user', 'content': [
              {'type': 'tool_result', 'tool_use_id': 'tu1', 'is_error': True, 'content': 'The user denied this'}]}}]
confere('edição negada não conta como mudança: termina', parar2(negada), 'termina')

for arq in ['/p/src/desenha.py', '/p/crates/speech/src/whisper/tokenizer.rs', '/p/src/author.rs',
            '/p/ui/design-tokens.css']:
    confere(f'caminho que só parece sensível ({os.path.basename(arq)}): termina',
            parar2([pedido('P1'), E(arq), resultado('P1'), B('make test'), resultado('P1')]), 'termina')
for arq in ['/p/src/authorization.rs', '/p/src/UserLoginView.swift', '/p/src/PasswordReset.ts']:
    confere(f'caminho sensível ({os.path.basename(arq)}) sem revisão: barra',
            parar2([pedido('P1'), E(arq), resultado('P1'), B('make test'), resultado('P1')]), 'block')
confere('Agent com prompt nulo não derruba a checagem',
        parar2([pedido('P1'), E('/p/src/auth.rs'), resultado('P1'), B('cargo test'), resultado('P1'),
                usa('Agent', {'description': 'Revisão do login', 'prompt': None}), resultado('P1')]), 'termina')

# limite próprio: 3 bloqueios seguidos sem mudança nova; no 4º solta e avisa; mudança nova volta a contar
seq = [parar2(so_edita, sessao='S-limite', completo=True) for _ in range(4)]
confere('limite: 3 bloqueios e depois solta',
        [s.get('decision', 'termina') for s in seq], ['block', 'block', 'block', 'termina'])
confere('limite: ao soltar, avisa o usuário (systemMessage)', 'Portão de qualidade' in seq[3].get('systemMessage', ''), True)
confere('limite: o aviso conta (aviso 2 de 3)', 'aviso 2 de 3' in seq[1].get('reason', ''), True)
mais = so_edita + [E('/p/b.py'), resultado('P1')]
confere('limite: mudança nova no código volta a bloquear', parar2(mais, sessao='S-limite'), 'block')

# desempenho: conversa enorme com pedidos antigos (só o pedido atual é lido)
with tempfile.TemporaryDirectory() as d:
    grande = os.path.join(d, 'grande.jsonl')
    with open(grande, 'w') as f:
        velho = json.dumps(usa('Bash', {'command': 'x' * 2000}))
        for i in range(60000):
            f.write(json.dumps(pedido(f'V{i // 100}')) + '\n' if i % 100 == 0 else velho + '\n')
        for e in [pedido('P1'), E('/p/a.py'), resultado('P1'), B('pytest'), resultado('P1')]:
            f.write(json.dumps(e) + '\n')
    t0 = time.monotonic()
    out = rodar('final.py', {'hook_event_name': 'Stop', 'session_id': 's', 'transcript_path': grande, 'prompt_id': 'P1',
                             'last_assistant_message': 'ok'})
    confere(f'conversa de {os.path.getsize(grande) // 1_000_000} MB: decide certo e rápido',
            ((out or {}).get('decision', 'termina'), time.monotonic() - t0 < 2), ('termina', True))


# --- guarda.py: entrega do subagente pelo SubagentHandback ---
with tempfile.TemporaryDirectory() as proj:
    sessao = 'sess-entrega'
    principal = os.path.join(proj, f'{sessao}.jsonl')
    open(principal, 'w').close()
    pasta_sub = os.path.join(proj, sessao, 'subagents')
    os.makedirs(pasta_sub)

    def agente(aid, linhas):
        with open(os.path.join(pasta_sub, f'agent-{aid}.jsonl'), 'w') as f:
            for e in linhas:
                f.write(json.dumps(e) + '\n')

    def entrega(aid, msg='Pronto.'):
        out = rodar('guarda.py', {'hook_event_name': 'PreToolUse', 'tool_name': 'SubagentHandback',
                                  'tool_input': {'message': msg}, 'session_id': sessao, 'agent_id': aid,
                                  'agent_type': 'general-purpose', 'transcript_path': principal}) or {}
        return out.get('hookSpecificOutput', {}).get('permissionDecision', 'segue'), out.get('systemMessage', '')

    agente('a1', [pedido('X'), E('/p/a.rs'), resultado('X')])
    agente('a2', [pedido('X'), E('/p/a.rs'), resultado('X'), B('cargo test'), resultado('X')])
    agente('a3', [pedido('X'), B('cat README.md'), resultado('X')])
    confere('entrega: subagente mudou código e não testou: nega', entrega('a1')[0], 'deny')
    confere('entrega: subagente testou: segue', entrega('a2')[0], 'segue')
    confere('entrega: subagente declarou como verificou: segue',
            entrega('a1', 'Feito.\n**Sem verificação automática**: só mudei um texto.')[0], 'segue')
    confere('entrega: subagente que não mexeu em código: segue', entrega('a3')[0], 'segue')
    confere('entrega: subagente sem conversa gravada: segue', entrega('nao-existe')[0], 'segue')
    agente('a4', [pedido('X'), E('/p/a.rs'), resultado('X')])   # agente só deste teste: passar zera a contagem
    voltas = [entrega('a4') for _ in range(4)]
    confere('entrega: limite de 3 negativas e na 4ª solta avisando',
            [v[0] for v in voltas] + [bool(voltas[-1][1])], ['deny', 'deny', 'deny', 'segue', True])

r = subprocess.run(['sh', '-c', 'python3 -I "$HOME/.claude/hooks/portao/nao-existe.py" || true'],
                   capture_output=True, text=True, env=dict(os.environ))
confere('arquivo de hook sumido com "|| true": sai 0 e não bloqueia', (r.returncode, r.stdout), (0, ''))
r = subprocess.run([sys.executable, '-I', os.path.join(P, 'guarda.py')], input='{"tool_name": "Bash", "tool_input": 7}',
                   capture_output=True, text=True)
confere('entrada estranha no guarda.py: sai 0 sem decisão', (r.returncode, r.stdout.strip()), (0, ''))

# ================= re-revisão de 2026-10-23: cada achado vira teste =================
def decisao(cmd, cwd=None):
    p = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': cmd}}
    if cwd:
        p['cwd'] = cwd
    return ((rodar('guarda.py', p) or {}).get('hookSpecificOutput') or {}).get('permissionDecision', 'segue')


def fim(*cmds, edita=None):   # pedido com um Edit opcional e comandos Bash, até o Stop
    linhas = [pedido('P1')] + ([E(edita), resultado('P1')] if edita else [])
    for c in cmds:
        linhas += [B(c), resultado('P1')]
    return parar2(linhas)


# A1: remendo por código solto (python - <<EOF, -c, node -e) é mudança e não é verificação
remendo = "python3 - <<'EOF'\np = 'src/a.rs'\nopen(p, 'w').write('x')\nEOF"
confere('A1: remendo por python - <<EOF sem testar: barra', fim(remendo), 'block')
confere('A1: Edit, teste e depois remendo sem testar: barra', fim('cargo test', remendo, edita='/p/a.py'), 'block')
confere('A1: remendo e depois cargo test: termina', fim(remendo, 'cargo test'), 'termina')
confere('A1: python que só grava nota (.md) não é mudança de código',
        fim("python3 - <<'EOF'\nopen('notas.md', 'w').write('x')\nEOF"), 'termina')
confere('A1: python -c que grava .py: barra', fim("python3 -c \"open('x.py', 'w').write('1')\""), 'block')
confere('A1: node -e que grava .js: barra', fim("node -e \"require('fs').writeFileSync('a.js', 'x')\""), 'block')
confere('A1: gravação atômica (fdopen + os.replace) num .py: barra',
        fim("python3 -I - <<'EOF'\nimport os, tempfile\nfd, tmp = tempfile.mkstemp()\nwith os.fdopen(fd, 'w') as f:\n"
            "    f.write(t)\nos.replace(tmp, '/p/jogo.py')\nEOF"), 'block')
for cmd in ['python3 -c "print(1)"', 'node -e "1"', "python3 - <<'EOF'\nprint(1)\nEOF"]:
    confere(f'A1: código solto não é verificação ({cmd[:24]!r}): barra', fim(cmd, edita='/p/a.py'), 'block')

# M1, M2, M3: comando escondido em $( ) entre aspas, em palavra do shell e depois de "#"
for cmd in ['x="$(git stash)"', 'echo "$(cd x && git reset --hard)"',
            'for f in a b; do git checkout -- "$f"; done', 'if true; then git stash; fi', '{ git stash; }',
            '! git stash', 'while true; do git clean -fd; done',
            'git status # confere\ngit add -A', 'echo a#b; git stash', 'echo ${#arr[@]}; git stash', 'echo $#; git stash']:
    confere(f'M1-M3: comando escondido pede ok: {cmd!r}', decisao(cmd), 'ask')
confere('M1: aritmética $(( )) não é comando', decisao('echo "$((1 + 2))"'), 'segue')
confere('M3: comentário de verdade continua ignorado', decisao('git status  # depois: git reset --hard'), 'segue')
confere('M2: sed -i dentro de laço conta como mudança: barra', fim("for f in 1 2; do sed -i 's/a/b/' src/a.py; done"),
        'block')
confere('M2: cargo test dentro de laço conta como verificação', fim('for i in 1 2; do cargo test; done', edita='/p/a.rs'),
        'termina')

# M4: checkout de arquivo depois de um cd
with tempfile.TemporaryDirectory() as repo:
    os.makedirs(os.path.join(repo, 'src'))
    open(os.path.join(repo, 'src', 'a.rs'), 'w').close()
    confere('M4: cd e checkout de arquivo: pede ok', decisao(f'cd {repo} && git checkout src/a.rs', '/'), 'ask')
    confere('M4: cd relativo e checkout: pede ok', decisao('cd src && git checkout a.rs', repo), 'ask')
    confere('M4: cd e checkout de branch: segue', decisao(f'cd {repo} && git checkout main', '/'), 'segue')

# M5: verificação de verdade que não era reconhecida, e o que parecia verificação e não é
for cmd in ['timeout 300 build/AppTests_artefacts/Release/AppTests', '$L cargo test -p app',
            'target/debug/app --version', 'tools/validate.sh', 'uv run pytest', 'poetry run pytest -q',
            'bundle exec rspec', 'just test', 'cargo +nightly test', 'cargo test --manifest-path x/Cargo.toml',
            'npx remotion render', 'pnpm dlx vitest run']:
    confere(f'M5: verificação reconhecida ({cmd}): termina', fim(cmd, edita='/p/a.cpp'), 'termina')
for cmd in ['make clean', 'npx prettier --write .', 'cargo add serde']:
    confere(f'M5: não verifica ({cmd}): barra', fim(cmd, edita='/p/a.cpp'), 'block')

# M6: pedido de ok à toa em teste
for rotulo, ent in [
        ('marca de teste dentro de string', {'file_path': '/p/tests/t.js', 'old_string': 'const casos = []',
                                             'new_string': "const casos = ['it.only(', 'fit(', 'xit(']"}),
        ('pytest.skip com condição', {'file_path': '/p/tests/test_hw.py', 'old_string': 'def test_placa():\n    x = abrir()',
                                      'new_string': 'def test_placa():\n    if not tem_placa():\n        pytest.skip("sem placa")\n'
                                                    '    x = abrir()'}),
        ('GTEST_SKIP dentro de if', {'file_path': '/p/tests/a_test.cpp', 'old_string': 'TEST(A, b) {\n  run();',
                                     'new_string': 'TEST(A, b) {\n  if (!gpu()) GTEST_SKIP();\n  run();'})]:
    confere(f'M6: não pede ok à toa: {rotulo}', edicao('Edit', ent), 'segue')

# M7: mensagem na fila (o id novo chega no resultado da ferramenta) não esconde a edição feita antes
fila = [pedido('P1'),
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'q1', 'name': 'Edit', 'input': {'file_path': '/p/a.py', 'old_string': 'a',
                                                                       'new_string': 'b'}}]}},
        {'type': 'user', 'promptId': 'P2', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'q1', 'content': 'ok'}]}},
        pedido('P2', 'e mais isto')]
confere('M7: mensagem na fila não esconde a edição anterior: barra', parar2(fila, pid='P2'), 'block')

# M8: revisão por fork ou antes de qualquer mudança não conta
grande = [pedido('P1'), E('/p/a.rs', 400), resultado('P1'), B('cargo test'), resultado('P1')]
rev = lambda tipo='general-purpose': usa('Agent', {'description': 'Revisão independente', 'prompt': 'revise',
                                                   'subagent_type': tipo})
confere('M8: revisão por fork não conta: barra', parar2(grande + [rev('fork'), resultado('P1')]), 'block')
confere('M8: revisão antes de qualquer mudança não conta: barra',
        parar2([pedido('P1'), rev(), resultado('P1')] + grande[1:]), 'block')
confere('M8: revisor novo depois da mudança: termina', parar2(grande + [rev(), resultado('P1')]), 'termina')

# B1, B2: subagente que entrega pelo SubagentHandback
with tempfile.TemporaryDirectory() as proj:
    sessao = 'sess-b'
    principal = os.path.join(proj, f'{sessao}.jsonl')
    open(principal, 'w').close()
    pasta_sub = os.path.join(proj, sessao, 'subagents')
    os.makedirs(pasta_sub)
    for aid, linhas in [('b1', [pedido('X'), E('/p/a.rs'), resultado('X'), usa('SubagentHandback', {'message': 'Pronto'})]),
                        ('b2', [pedido('X'), E('/p/a.rs'), resultado('X')])]:
        with open(os.path.join(pasta_sub, f'agent-{aid}.jsonl'), 'w') as f:
            f.write(''.join(json.dumps(e) + '\n' for e in linhas))
    out = rodar('final.py', {'hook_event_name': 'SubagentStop', 'session_id': sessao, 'agent_id': 'b1',
                             'agent_type': 'general-purpose', 'agent_transcript_path': os.path.join(pasta_sub, 'agent-b1.jsonl'),
                             'last_assistant_message': ''})
    confere('B1: SubagentStop de quem entregou pelo handback não repete o aviso', out, None)
    out = rodar('guarda.py', {'hook_event_name': 'PreToolUse', 'tool_name': 'SubagentHandback',
                              'tool_input': {'message': 'Pronto'}, 'session_id': sessao, 'agent_id': 'b2',
                              'agent_type': 'general-purpose', 'transcript_path': principal}) or {}
    razao = out.get('hookSpecificOutput', {}).get('permissionDecisionReason', '')
    confere('B2: a negação manda pôr a declaração no relatório e entregar de novo',
            ('relatório da entrega' in razao, 'SubagentHandback de novo' in razao), (True, True))

# B4: edição pelo Bash que não era, e a que faltava
confere('B4: cp a.py a.py.bak não muda a.py', fim('cp src/a.py src/a.py.bak'), 'termina')
confere('B4: patch --dry-run não muda nada', fim('patch --dry-run -p1 < x.diff'), 'termina')
confere('B4: cp de código para dentro de uma pasta conta', fim('cp novo.py src/'), 'block')

# B5, B6: git que escapava, e o que perguntava sem precisar
for cmd in ['sudo -u outro git stash', "bash -c -- 'git stash'", "cat <<'EOF' | bash\ngit stash\nEOF",
            "find . -name x -exec sh -c 'git stash' \\;", 'flock /tmp/trava git stash',
            'git config core.hooksPath /dev/null', 'git push --prune origin', 'echo x # <<EOF\ngit stash',
            '(( x = 1 << y ))\ngit stash']:
    confere(f'B5: escapava e agora pede ok: {cmd!r}', decisao(cmd), 'ask')
confere('B5: git config --get core.hooksPath só consulta', decisao('git config --get core.hooksPath'), 'segue')
confere('B6: worktree remove sem --force segue (o git recusa se houver mudança)', decisao('git worktree remove ../x'),
        'segue')

# B7, B8: o guarda não trava nem passa do tempo
with tempfile.TemporaryDirectory() as d:
    fifo = os.path.join(d, 'x.py')
    os.mkfifo(fifo)
    t0 = time.monotonic()
    confere('B7: Write num FIFO chamado .py não trava o guarda',
            (edicao('Write', {'file_path': fifo, 'content': 'x'}), time.monotonic() - t0 < 5), ('segue', True))
t0 = time.monotonic()
confere('B8: comando de 1 MB não passa do tempo do hook',
        (decisao('python3 -c "' + 'x = 1; ' * 150000 + '"'), time.monotonic() - t0 < 5), ('segue', True))

# B9: religar com o matcher mudado não duplica o guarda
LIGAR = os.path.join(RAIZ, 'install', 'ligar-hooks.py')
with tempfile.TemporaryDirectory() as casa:
    os.makedirs(os.path.join(casa, '.claude/hooks/portao'))
    for s in ('padrao-subagente.py', 'checklist-codigo.py', 'portao/guarda.py', 'portao/final.py'):
        open(os.path.join(casa, '.claude/hooks', s), 'w').close()
    with open(os.path.join(casa, '.claude/settings.json'), 'w') as f:
        json.dump({'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': 'python3 -I "$HOME/.claude/hooks/portao/guarda.py" || true', 'timeout': 10}]}]}}, f)
    subprocess.run([sys.executable, '-I', LIGAR, '--portao'], env=dict(os.environ, HOME=casa), capture_output=True)
    ligado = json.load(open(os.path.join(casa, '.claude/settings.json')))
    confere('B9: religar com matcher novo não duplica o guarda',
            [g['matcher'] for g in ligado['hooks']['PreToolUse'] for h in g['hooks'] if 'guarda.py' in h['command']],
            ['Bash|Edit|Write|MultiEdit|NotebookEdit|SubagentHandback'])

# Pastas só de executar (REA = a ferramenta de teste): nada se escreve nelas; usar e ler continua livre
if os.path.isfile(os.path.join(P, 'so-executar.txt')):
    for nome, ent in [('Write', {'file_path': f'{REA}/novo.js', 'content': 'x'}),
                      ('Edit', {'file_path': f'{REA}/scripts/rea.mjs', 'old_string': 'a', 'new_string': 'b'}),
                      ('NotebookEdit', {'notebook_path': f'{REA}/x.ipynb', 'new_source': 'x'})]:
        confere(f'REA: {nome} dentro do REA é negado', edicao(nome, ent), 'deny')
    for cmd in [f'echo x > {REA}/a.txt', f'cd {REA} && git pull', f'rm -rf {REA}/dist', f'cp /tmp/x {REA}/',
                f'git -C {REA} checkout -- .', f"sed -i 's/a/b/' {REA}/README.md", f'cd {REA} && touch novo',
                f'cd {REA} && git fetch', f'cd {REA} && node scripts/rea.mjs x > saida.json 2>&1',
                f'echo x &> {REA}/log.txt']:
        confere(f'REA: escrita pelo Bash é negada: {cmd}', decisao(cmd), 'deny')
    for cmd in [f'git -C {REA} status', f'git -C {REA} log -1', f'git -C {REA} branch -a',
                '~/tools/outra/rea.sh analyze x', f'cd {REA} && node scripts/rea.mjs --help',
                f'cp {REA}/README.md /tmp/leitura.md', f'cat {REA}/README.md', f'cd {REA}-nao-e-a-mesma && touch x',
                f'cd {REA} && node scripts/rea.mjs doctor --json 2>&1 | head', f'cd {REA} && ls >&2']:
        confere(f'REA: usar sem escrever segue: {cmd}', decisao(cmd), 'segue')

# ================= conferência final de 2026-10-23: cada achado vira teste =================
# Falsos positivos da proteção do REA (ela nega sem perguntar, então não pode errar para o lado de negar)
if os.path.isfile(os.path.join(P, 'so-executar.txt')):
    for cmd, cwd in [('chmod +x /home/user/tools/outra/rea.sh', REA),
                     ('chmod 755 /tmp/x.sh', REA), ('chown user:user /tmp/x', REA),
                     ('mkdir -m 700 /tmp/x', REA), (f'touch -r {REA}/README.md /tmp/x', None),
                     (f'cp -t /tmp/dest {REA}/README.md', None),
                     ('D=/home/user/tools/outra/installer; mkdir -p $D', REA),
                     ('mkdir -p "$S/saida"', REA), (f'cd {REA} && node scripts/rea.mjs x > "$S/out.json"', None),
                     (f'(cd {REA} && git log -1); echo x > notas.txt', '/tmp'),
                     (f'x=$(cd {REA} && pwd); echo x > y.txt', '/tmp'),
                     (f"bash -c 'cd {REA} && ls'; echo x > z.txt", '/tmp')]:
        confere(f'REA: não nega à toa: {cmd!r}', decisao(cmd, cwd), 'segue')
    for cmd, cwd in [(f'chmod +x {REA}/scripts/novo.sh', None), (f'cp -t {REA}/ x.js', None),
                     ('touch novo', REA), (f'(cd {REA} && touch novo)', '/tmp'),
                     (f'D={REA}/lib; mkdir -p $D', '/tmp'), (f'cd {REA} && npm install', None),
                     (f'tar -xf x.tgz -C {REA}', None), (f'git clone https://x/y.git {REA}/y', None),
                     (f'curl -o {REA}/x.js https://x', None), ('wget https://x/a.js', REA)]:
        confere(f'REA: escrita de verdade continua negada: {cmd!r}', decisao(cmd, cwd), 'deny')
    sys.path.append(P)
    import comum as _comum   # validação da lista: linha larga demais (~, /) não pode virar proteção de tudo
    with tempfile.TemporaryDirectory() as d:
        lista = os.path.join(d, 'so-executar.txt')
        with open(lista, 'w') as f:
            f.write('~\n/\n# comentário\n/pasta/que/nao/existe\n' + REA + '\n')
        original, _comum.SO_EXECUTAR = _comum.SO_EXECUTAR, lista
        confere('REA: a lista ignora ~, / e pasta que não existe', _comum.raizes_so_executar(), [os.path.realpath(REA)])
        _comum.SO_EXECUTAR = original

# M7: a linha do hook fica entre a chamada e o resultado (formato real do Claude Code 2.1.309)
fila_hook = [pedido('P1'),
             {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'h1', 'name': 'Edit', 'input': {'file_path': '/p/a.py', 'old_string': 'a',
                                                                            'new_string': 'b'}}]}},
             {'type': 'attachment', 'attachment': {'type': 'hook_success', 'hookEvent': 'PreToolUse'}},
             {'type': 'user', 'promptId': 'P2', 'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': 'h1', 'content': 'ok'}]}},
             pedido('P2', 'e mais isto')]
confere('M7: mensagem na fila com a linha do hook no meio não esconde a edição: barra', parar2(fila_hook, pid='P2'),
        'block')

# B1: subagente que entregou, foi continuado, mudou código sem testar e terminou em texto
with tempfile.TemporaryDirectory() as d:
    caminho = os.path.join(d, 'agent-c1.jsonl')
    with open(caminho, 'w') as f:
        f.write(''.join(json.dumps(e) + '\n' for e in [
            pedido('X'), E('/p/a.rs'), resultado('X'), B('cargo test'), resultado('X'),
            usa('SubagentHandback', {'message': 'Pronto'}), resultado('X'), pedido('Y', 'continua'),
            E('/p/b.rs'), resultado('Y')]))
    out = rodar('final.py', {'hook_event_name': 'SubagentStop', 'session_id': 's-b1', 'agent_id': 'c1',
                             'agent_type': 'general-purpose', 'agent_transcript_path': caminho,
                             'last_assistant_message': 'feito'}) or {}
    confere('B1: mudança depois da entrega é checada no SubagentStop: barra', out.get('decision', 'termina'), 'block')

# A1: resíduos do remendo por código solto
for rotulo, cmd in [('argumento depois do -', "python3 - \"$S/x.sh\" <<'EOF'\nopen('a.py', 'w').write('x')\nEOF"),
                    ('opção com valor antes do -', "python3 -W ignore - <<'EOF'\nopen('a.py', 'w').write('x')\nEOF"),
                    ('cat <<EOF | python3 -', "cat <<'EOF' | python3 -\nopen('a.py', 'w').write('x')\nEOF"),
                    ('open(os.path.join(...), "w")', "python3 -c \"open(os.path.join(r, 'a.cpp'), 'w').write('x')\""),
                    ('open(p, "r+")', "python3 - <<'EOF'\np = 'x.py'\nf = open(p, 'r+')\nf.write('y')\nEOF")]:
    confere(f'A1: remendo visto ({rotulo}): barra', fim(cmd), 'block')

# M2, M5, B5: resíduos
confere('M2: function f { git stash; } pede ok', decisao('function f { git stash; }'), 'ask')
confere('B5: flock arquivo -c "git stash" pede ok', decisao("flock /tmp/trava -c 'git stash'"), 'ask')
for cmd in ['~/.local/bin/uv pip install x', 'make -C build clean']:
    confere(f'M5: não verifica ({cmd}): barra', fim(cmd, edita='/p/a.py'), 'block')
confere('M5: npm --prefix web test verifica', fim('npm --prefix web test', edita='/p/a.js'), 'termina')

# Checagem rápida final: resíduos da proteção do REA
if os.path.isfile(os.path.join(P, 'so-executar.txt')):
    for cmd, cwd in [(f'npm ci --prefix {REA}', '/tmp'), (f'cd {REA} && git commit -m "$MSG"', None),
                     (f'git -C {REA} pull origin "$B"', None), (f'cd {REA} && git checkout -- "$f"', None)]:
        confere(f'REA: variável na mensagem ou --prefix não escondem a escrita: {cmd!r}', decisao(cmd, cwd), 'deny')
    confere('REA: pasta do repositório em variável que não dá para resolver não é negada',
            decisao('git -C "$X" pull', '/tmp'), 'segue')

shutil.rmtree(ESTADO, ignore_errors=True)
shutil.rmtree(CAIXA, ignore_errors=True)
print(f'\n{"TUDO OK" if not falhas else str(len(falhas)) + " FALHA(S)"}')
sys.exit(1 if falhas else 0)
