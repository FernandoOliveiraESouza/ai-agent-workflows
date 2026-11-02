"""Peças comuns do portão de qualidade: os hooks que obrigam o padrão de engenharia (seção 8 do principal,
~/.claude/kernel/KERNEL.md). Tudo aqui falha aberto: um erro inesperado nunca trava o agente.
Contrato conferido no Claude Code 2.1.309; o código passou por duas revisões independentes."""
import glob
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
import unicodedata

EXT_CODIGO = {
    'c', 'cc', 'cpp', 'cxx', 'h', 'hh', 'hpp', 'hxx', 'm', 'mm', 'rs', 'go', 'java', 'kt', 'swift', 'cs', 'zig',
    'py', 'rb', 'php', 'lua', 'pl', 'js', 'jsx', 'mjs', 'cjs', 'ts', 'tsx', 'mts', 'cts', 'vue', 'svelte', 'sh', 'bash',
    'zsh', 'ps1', 'sql', 'html', 'css', 'scss', 'cmake', 'ipynb', 'wgsl', 'glsl',
}
NOMES_CODIGO = {'CMakeLists.txt', 'Makefile', 'Cargo.toml', 'package.json', 'pyproject.toml', 'meson.build'}
EDICAO = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
LIMITE_LINHAS = 300
LIMITE_BLOQUEIOS = 3   # o Claude Code só solta no 9º bloqueio seguido, e qualquer ferramenta zera essa conta
LIMITE_COMANDO = 200_000   # comando maior é cortado: o hook tem 10 s e nunca pode atrasar o agente
PASTAS_TEMPORARIAS = tuple({os.path.realpath(p) for p in (tempfile.gettempdir(), '/tmp', '/var/tmp', '/dev/shm')})
SO_EXECUTAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'so-executar.txt')

# ---------- código e testes ----------
TESTE_CAMINHO = re.compile(
    r'(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*\.py$|_test\.(py|go|rs|cpp|cc|c)$|\.(test|spec)\.[jt]sx?$'
    r'|Tests?\.(java|kt|cs|swift|cpp)$|(^|/)tests?\.rs$')
# Definição de teste: contada antes e depois da edição (sumir uma é tirar teste). O (?<![.\w]) deixa de fora o
# regex.test( do JavaScript e nomes que só terminam em "it" ou "test".
TESTE_DEF = re.compile(
    r'#\[(tokio::)?test\]|\bTEST(_F|_CASE|_P)?\s*\(|\bdef test_\w*|@Test\b|\bfunc Test\w+\s*\(|(?<![.\w])(it|test)\s*\(')
TESTE_TRECHO = re.compile(TESTE_DEF.pattern + r'|#\[cfg\(test\)\]|(?<![.\w])describe\s*\(')
# Só as marcas incondicionais, que desligam, pulam ou isolam teste (.only, fit e fdescribe rodam um e calam os
# outros). Pular com condição (skipif, skipIf, pytest.skip(, t.Skip(, GTEST_SKIP() dentro de um if) é o jeito certo
# de pular onde falta o recurso e não conta.
PULA_TESTE = re.compile(
    r'#\[\s*ignore|@pytest\.mark\.(skip|xfail)\b|@unittest\.(skip|expectedFailure)\b'
    r'|\b(it|test|describe|context)\.(skip|todo|only)\b|(?<![.\w])[xf](it|test|describe)\s*\(|@Disabled\b|@Ignore\b'
    r'|\bDISABLED_\w+')
# Verificação dentro de teste. O .expect( do Rust abre valores no setup e não verifica; expectEquals( e
# expectWithinAbsoluteError( (JUCE) verificam.
VERIFICA_TESTE = re.compile(
    r'\bassert|(?<!\.)\bexpect\w*\s*\(|\b(EXPECT|ASSERT|REQUIRE|CHECK)\w*\s*\(|\bt\.(Error|Fatal|Fail)\w*\(')
COMENTARIO_HASH = {'py', 'rb', 'sh', 'bash', 'zsh', 'pl', 'cmake', 'ps1'}
SENSIVEL_EXATO = {'login', 'logins', 'logon', 'logout', 'token', 'jwt', 'billing', 'security', 'seguranca', 'tls',
                  'ssl', 'cipher', 'keychain', 'keyring', 'credentials', 'credential', 'senha', 'senhas'}
SENSIVEL_PREFIXO = ('auth', 'autentic', 'autoriz', 'credenc', 'credential', 'crypt', 'encrypt', 'decrypt', 'permiss',
                    'passw', 'secret', 'segred', 'pagament', 'payment', 'oauth')


def eh_codigo(caminho):
    if not caminho or not isinstance(caminho, str):
        return False
    nome = os.path.basename(caminho)
    return nome in NOMES_CODIGO or ('.' in nome and nome.rsplit('.', 1)[-1].lower() in EXT_CODIGO)


def eh_temporario(caminho):
    """Arquivo em pasta temporária (rascunho, scratchpad, página de Artifact): não é código de projeto."""
    if not caminho or not isinstance(caminho, str) or not os.path.isabs(os.path.expanduser(caminho)):
        return False
    real = os.path.realpath(os.path.expanduser(caminho))
    return any(real.startswith(p + os.sep) for p in PASTAS_TEMPORARIAS)


def sem_comentarios(texto, caminho):
    """O texto sem comentários, para contar asserts e testes: assert comentado não verifica nada."""
    nome = os.path.basename(caminho or '')
    ext = nome.rsplit('.', 1)[-1].lower() if '.' in nome else ''
    if ext == 'ipynb':
        return texto
    if ext in COMENTARIO_HASH or nome in ('Makefile', 'CMakeLists.txt'):
        return re.sub(r'(?m)(^|\s)#.*$', r'\1', texto)
    if ext in ('sql', 'lua'):
        return re.sub(r'(?m)--.*$', '', texto)
    if ext in ('html', 'vue', 'svelte'):
        texto = re.sub(r'<!--.*?-->', ' ', texto, flags=re.S)
    texto = re.sub(r'/\*.*?\*/', ' ', texto, flags=re.S)
    return re.sub(r'(?m)(?<!:)//.*$', '', texto)   # o (?<!:) poupa http://


def sem_strings(texto):
    """Tira o conteúdo das strings de uma linha: marca de teste ou assert dentro de texto (dado de teste,
    mensagem) não conta."""
    return re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', '""', texto)


def normalizar(texto):
    """Minúsculas, sem acento e sem marcação (* _ `), para achar uma declaração de qualquer jeito que for escrita."""
    t = unicodedata.normalize('NFKD', texto or '')
    return re.sub(r'[*_`]', '', ''.join(c for c in t if not unicodedata.combining(c)).lower())


def declarou(resposta, frase):
    """A resposta traz a declaração ("Sem verificação automática: ..."), com dois-pontos, travessão ou em negrito."""
    return re.search(re.escape(normalizar(frase)) + r'(\W|$)', normalizar(resposta)) is not None


def sensivel(caminho):
    """Código sensível pelo caminho (login, senha, token, pagamento, cripto, segurança), por palavra inteira:
    "desenha", "author", "tokenizer" e "design-tokens" não contam."""
    sem_acento = ''.join(c for c in unicodedata.normalize('NFKD', caminho or '') if not unicodedata.combining(c))
    for p in re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+', sem_acento):
        p = p.lower()
        if p.startswith('author') and not p.startswith('authoriz'):
            continue
        if p in SENSIVEL_EXATO or p.startswith(SENSIVEL_PREFIXO):
            return True
    return False


# ---------- comandos de shell ----------
SHELLS = {'bash', 'sh', 'zsh', 'dash', 'ksh'}
# Prefixos que rodam outro comando: o comando de fato vem depois deles e das opções deles.
PREFIXOS = {'sudo', 'doas', 'env', 'nice', 'ionice', 'time', 'command', 'builtin', 'exec', 'nohup', 'timeout',
            'stdbuf', 'chrt', 'taskset', 'unbuffer', 'xargs', 'systemd-run', 'xvfb-run', 'setsid', 'strace', 'ltrace',
            'flock'}
OPCAO_COM_VALOR = {
    'sudo': {'-u', '-g', '-p', '-C', '-D', '-h', '-r', '-t', '-U', '--user', '--group'}, 'doas': {'-u', '-C'},
    'env': {'-u', '-C', '-S', '--unset', '--chdir'}, 'nice': {'-n', '--adjustment'},
    'ionice': {'-c', '-n', '-p', '-P', '-u'}, 'timeout': {'-s', '-k', '--signal', '--kill-after'},
    'stdbuf': {'-i', '-o', '-e'}, 'xargs': {'-I', '-n', '-P', '-d', '-a', '-E', '-L', '-s', '--max-procs'},
    'systemd-run': {'-p', '--property', '--slice', '--unit', '-u', '-E', '--setenv', '-M', '--machine',
                    '--description', '--working-directory'},
    'xvfb-run': {'-n', '-s', '-e', '-f', '-p', '--server-num', '--server-args', '--error-file', '--auth-file'},
    'flock': {'-w', '-E', '--timeout', '--conflict-exit-code'}, 'strace': {'-o', '-e', '-p', '-s'},
}
POSICIONAL = {'flock': 1, 'chrt': 1, 'taskset': 1}   # flock ARQUIVO comando, chrt PRIORIDADE comando...
PALAVRAS_SHELL = {'if', 'then', 'else', 'elif', 'fi', 'while', 'until', 'do', 'done', '{', '}', '!'}
VALOR_DE_OPCAO = re.compile(r'\d+(\.\d+)?[smhd]?|[A-Z][A-Z0-9_]*|\{\}|[\w.-]+=.*|0x[0-9a-fA-F]+|[\d,-]+')
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z_][\w.-]*))")
SHELL_ANTES = re.compile(r'(^|[;&|(]|\s)(sudo\s+)?(bash|sh|zsh|dash|ksh)(\s+-[a-zA-Z]+)*\s*$')
SHELL_DEPOIS = re.compile(r'\|\s*(sudo\s+)?(bash|sh|zsh|dash|ksh)\b')


def _tira_comentarios(texto):
    """Tira os comentários de shell (# no começo de uma palavra, fora de aspas) e mantém a quebra de linha:
    "git status # confere⏎git add -A" continua sendo dois comandos; $#, ${#x} e a#b não são comentário."""
    if '#' not in texto:
        return texto
    saida, i, simples, duplas, n = [], 0, False, False, len(texto)
    while i < n:
        c = texto[i]
        if c == '\\' and not simples:
            saida.append(texto[i:i + 2])
            i += 2
            continue
        if c == "'" and not duplas:
            simples = not simples
        elif c == '"' and not simples:
            duplas = not duplas
        elif c == '#' and not simples and not duplas and (i == 0 or texto[i - 1] in ' \t\n;&|()'):
            fim = texto.find('\n', i)
            i = n if fim < 0 else fim
            continue
        saida.append(c)
        i += 1
    return ''.join(saida)


def _entre_aspas(trecho):
    """O fim do trecho está dentro de aspas (conta aspas simples e duplas, ignorando as escapadas)."""
    simples = duplas = False
    i = 0
    while i < len(trecho):
        c = trecho[i]
        if c == '\\' and not simples:
            i += 2
            continue
        if c == "'" and not duplas:
            simples = not simples
        elif c == '"' and not simples:
            duplas = not duplas
        i += 1
    return simples or duplas


def _tira_heredocs(texto):
    """(texto sem os corpos de heredoc, corpos que vão para um shell, [(delimitador, corpo de dado)]).
    O corpo é dado (mensagem de commit, arquivo gerado, script de outra linguagem), não comando; só o que alimenta
    bash/sh (bash <<EOF ou cat <<EOF | bash) é analisado como comando. "<<" em comentário, entre aspas ou em
    aritmética (( 1 << x )) não abre heredoc."""
    if '<<' not in texto:
        return texto, [], []
    linhas = texto.split('\n')
    fica, scripts, corpos, i = [], [], [], 0
    while i < len(linhas):
        linha = linhas[i]
        fica.append(linha)
        i += 1
        for m in HEREDOC.finditer(linha):
            antes = linha[:m.start()]
            if _entre_aspas(antes) or _tira_comentarios(antes) != antes or antes.count('((') > antes.count('))'):
                continue
            fim = next(g for g in m.groups()[1:] if g is not None)
            corpo = []
            while i < len(linhas):
                atual = linhas[i]
                i += 1
                if (atual.lstrip('\t') if m.group(1) else atual) == fim:
                    break
                corpo.append(atual)
            if SHELL_ANTES.search(antes) or SHELL_DEPOIS.search(linha[m.end():]):
                scripts.append('\n'.join(corpo))
            else:
                corpos.append((fim, '\n'.join(corpo)))
    return '\n'.join(fica), scripts, corpos


def _tokens(texto):
    lex = shlex.shlex(texto, posix=True, punctuation_chars='();<>|&\n')
    lex.whitespace = ' \t\r'   # a quebra de linha separa comandos, como o ;
    lex.whitespace_split = True
    lex.commenters = ''        # os comentários já saíram em _tira_comentarios, sem engolir a quebra de linha
    return list(lex)


def _tipo_operador(t):
    """'redir' para redirecionamento, 'separa' para o que separa comandos, None para palavra comum."""
    if not t or not all(c in '();<>|&\n' for c in t):
        return None
    if t in ('>|', '>>|'):
        return 'redir'
    if any(c in t for c in '();|\n') or t in ('&', '&&'):
        return 'separa'
    return 'redir' if ('<' in t or '>' in t) else 'separa'


def indice_comando(palavras):
    """Posição da palavra que é o comando de fato, pulando palavras do shell (if, then, do, {, !...), VAR=valor,
    $PREFIXO (ex.: "$L cargo test") e prefixos como sudo -u x, nice -n 10, timeout 30, xargs -I {},
    flock arquivo e systemd-run --user -p X=Y. None se não houver."""
    i, prefixo, posicionais = 0, None, 0
    while i < len(palavras):
        p = palavras[i]
        if p == 'function':   # "function nome { ... }": pula a palavra e o nome
            i += 2
            continue
        if p in PALAVRAS_SHELL or re.match(r'[A-Za-z_][A-Za-z0-9_]*=', p) or re.fullmatch(r'\$\{?\w+\}?', p):
            i += 1
            continue
        if prefixo:
            if p.startswith('-'):
                i += 2 if (p in OPCAO_COM_VALOR.get(prefixo, ()) and '=' not in p) else 1
                continue
            if posicionais > 0:
                posicionais -= 1
                i += 1
                continue
            if VALOR_DE_OPCAO.fullmatch(p):
                i += 1
                continue
        if os.path.basename(p) in PREFIXOS:
            prefixo, posicionais = os.path.basename(p), POSICIONAL.get(os.path.basename(p), 0)
            i += 1
            continue
        return i
    return None


def _substituicoes(palavra):
    """O que roda dentro de $( ) numa palavra, também entre aspas duplas, onde o shell ainda executa."""
    saida, i = [], palavra.find('$(')
    while i >= 0:
        if palavra.startswith('$((', i):   # aritmética, não comando
            i = palavra.find('$(', i + 3)
            continue
        nivel, j = 0, i + 1
        while j < len(palavra):
            if palavra[j] == '(':
                nivel += 1
            elif palavra[j] == ')':
                nivel -= 1
                if nivel == 0:
                    break
            j += 1
        saida.append(palavra[i + 2:j])
        i = palavra.find('$(', j)
    return saida


def _internos(palavras):
    """O que roda dentro de um comando: texto (bash -c '...', eval, watch, foreach, $( ), crases) ou já em palavras
    (find -exec, que passa os argumentos direto, sem shell)."""
    saida = re.findall(r'(?<!\\)`([^`]*)`', ' '.join(palavras))
    for p in palavras:
        if '$(' in p:
            saida += _substituicoes(p)
    for j, p in enumerate(palavras):   # flock arquivo -c 'comando'
        if os.path.basename(p) == 'flock':
            for jj in range(j + 1, len(palavras) - 1):
                if palavras[jj] in ('-c', '--command'):
                    saida.append(palavras[jj + 1])
                    break
            break
    k = indice_comando(palavras)
    if k is None:
        return saida
    cmd, args = os.path.basename(palavras[k]), palavras[k + 1:]
    if cmd in SHELLS or cmd in ('su', 'runuser'):
        for j, a in enumerate(args):
            if re.fullmatch(r'-[a-zA-Z]*c[a-zA-Z]*', a):
                resto = [x for x in args[j + 1:] if x != '--']
                if resto:
                    saida.append(resto[0])
                break
    elif cmd == 'eval':
        saida.append(' '.join(args))
    elif cmd == 'watch':
        saida.append(' '.join(a for a in args if not a.startswith('-') and not VALOR_DE_OPCAO.fullmatch(a)))
    elif cmd == 'git' and 'foreach' in args:
        saida.append(' '.join(a for a in args[args.index('foreach') + 1:] if a not in ('--recursive', '-q', '--quiet')))
    elif cmd == 'find':
        for op in ('-exec', '-execdir', '-ok', '-okdir'):
            if op in args:
                saida.append(args[args.index(op) + 1:])
    return saida


def _expande(palavras, redir, escopo, profundidade, contador):
    """O comando e o que roda dentro dele, cada coisa interna num escopo novo (é outro processo ou subshell)."""
    saida = [(palavras, redir, escopo)]
    for interno in _internos(palavras):
        contador[0] += 1
        novo = escopo + (contador[0],)
        if isinstance(interno, list):
            saida += _expande(interno, [], novo, profundidade + 1, contador) if interno and profundidade < 5 else []
        else:
            saida += comandos_com_escopo(interno, profundidade + 1, novo, contador)
    return saida


def comandos(texto, profundidade=0):
    """Comandos simples de uma linha de shell, na ordem em que rodam: [(palavras, [(redirecionamento, alvo, corpo)])]
    (o corpo é o texto do heredoc ligado ao "<<", ou None). Respeita aspas; tira comentários e corpos de heredoc;
    junta linha quebrada com \\; separa em ; && || | & ( ) e quebra de linha; e abre o que roda dentro de bash -c,
    eval, watch, foreach, find -exec, flock -c, $( ), crases, if/for/while e heredoc que alimenta um shell."""
    return [(p, r) for p, r, _ in comandos_com_escopo(texto, profundidade)]


def comandos_com_escopo(texto, profundidade=0, escopo=(), contador=None):
    """Como comandos(), com o escopo de cada comando simples: () é o nível de cima; cada subshell "( )", "$( )",
    bash -c, eval e afins abre um escopo novo, para que um cd lá dentro não valha fora."""
    contador = contador if contador is not None else [0]
    if not texto or not isinstance(texto, str) or profundidade > 4:
        return []
    texto = texto[:LIMITE_COMANDO]
    texto, scripts, corpos = _tira_heredocs(texto)
    texto = _tira_comentarios(texto).replace('\\\n', ' ')
    try:
        toks = _tokens(texto)
    except ValueError:   # aspas sem fechar: corta do jeito simples, para não deixar passar
        toks = re.findall(r'[();|&\n]+|[<>]+|[^\s();|&<>]+', texto)
    simples, palavras, redir, pilha, i = [], [], [], [escopo], 0
    while i < len(toks):
        t = toks[i]
        tipo = _tipo_operador(t)
        if tipo == 'redir':
            alvo = toks[i + 1] if i + 1 < len(toks) and _tipo_operador(toks[i + 1]) is None else ''
            corpo = None
            if t.startswith('<<') and t != '<<<':
                chave = alvo.lstrip('-')
                for j, (fim, texto_corpo) in enumerate(corpos):
                    if fim == chave:
                        corpo = texto_corpo
                        del corpos[j]
                        break
            redir.append((t, alvo, corpo))
            i += 2 if alvo else 1
            continue
        if tipo == 'separa':
            if palavras or redir:
                simples.append((palavras, redir, pilha[-1]))
            palavras, redir = [], []
            for c in t:   # "(" abre subshell (escopo novo); ")" fecha
                if c == '(':
                    contador[0] += 1
                    pilha.append(pilha[-1] + (contador[0],))
                elif c == ')' and len(pilha) > 1:
                    pilha.pop()
        else:
            palavras.append(t)
        i += 1
    if palavras or redir:
        simples.append((palavras, redir, pilha[-1]))
    saida = []
    for palavras, redir, esc in simples:
        saida += _expande(palavras, redir, esc, profundidade, contador)
    for script in scripts:   # heredoc que alimenta um shell: outro processo, escopo novo
        contador[0] += 1
        saida += comandos_com_escopo(script, profundidade + 1, escopo + (contador[0],), contador)
    return saida


def expandir(palavra, variaveis):
    """A palavra com $VAR e ${VAR} trocados pelos valores conhecidos (e $HOME), ou None se sobrar algo que não dá para
    resolver: o portão não adivinha caminho."""
    def troca(m):
        nome = m.group(1) or m.group(2)
        if nome in variaveis:
            return variaveis[nome]
        if nome == 'HOME':
            return os.path.expanduser('~')
        raise KeyError(nome)
    if not isinstance(palavra, str):
        return None
    try:
        resultado = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)', troca, palavra)
    except KeyError:
        return None
    return None if ('$' in resultado or '`' in resultado) else resultado


def comandos_com_pasta(texto, cwd):
    """(palavras, redirecionamentos, pasta, variáveis) de cada comando simples. Segue cd, pushd e popd dentro do
    escopo (o cd de um subshell, $( ) ou bash -c não vale fora dele) e guarda os VAR=valor e export VAR=valor do
    próprio comando. A pasta é None quando não dá para saber."""
    pastas, variaveis = {(): cwd if isinstance(cwd, str) and cwd else None}, {}
    for palavras, redir, escopo in comandos_com_escopo(texto):
        if escopo not in pastas:   # escopo novo herda a pasta do escopo de fora mais próximo
            pai = escopo[:-1]
            while pai not in pastas:
                pai = pai[:-1]
            pastas[escopo] = pastas[pai]
        pasta = pastas[escopo]
        yield palavras, redir, pasta, variaveis
        k = indice_comando(palavras)
        if k is None:
            atribuicoes = palavras
        elif os.path.basename(palavras[k]) in ('export', 'declare', 'local', 'readonly'):
            atribuicoes = palavras[k + 1:]
        else:
            atribuicoes = []
        for a in atribuicoes:
            m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)=(.*)', a, re.S)
            if m:
                valor = expandir(m.group(2), variaveis)
                if valor is None:
                    variaveis.pop(m.group(1), None)
                else:
                    variaveis[m.group(1)] = valor
        if k is None or os.path.basename(palavras[k]) not in ('cd', 'pushd', 'popd'):
            continue
        cmd = os.path.basename(palavras[k])
        alvos = [a for a in palavras[k + 1:] if not a.startswith('-')]
        if cmd == 'popd' or (alvos and alvos[0] == '-'):
            pastas[escopo] = None
            continue
        if not alvos:
            pastas[escopo] = os.path.expanduser('~') if cmd == 'cd' else None
            continue
        destino = expandir(alvos[0], variaveis)
        destino = os.path.expanduser(destino) if destino is not None else None
        if destino is None or not (os.path.isabs(destino) or pasta):
            pastas[escopo] = None
        else:
            pastas[escopo] = os.path.normpath(destino if os.path.isabs(destino) else os.path.join(pasta, destino))


VERIFICA_DIRETO = {'make', 'gmake', 'ninja', 'ctest', 'pytest', 'py.test', 'tox', 'nox', 'rspec', 'jest', 'vitest',
                   'mocha', 'tsc', 'eslint', 'ruff', 'mypy', 'pyright', 'flake8', 'pylint', 'shellcheck', 'valgrind',
                   'gcc', 'g++', 'clang', 'clang++', 'cc', 'c++', 'mvn', 'gradle', 'gradlew', 'xcodebuild',
                   'fc-testar', 'phpunit', 'bazel', 'scons', 'clang-tidy', 'cppcheck', 'semgrep', 'stylelint', 'just'}
VERIFICA_SUB = {
    'cargo': {'test', 't', 'build', 'b', 'check', 'c', 'clippy', 'nextest', 'xtask', 'run', 'r', 'bench', 'miri'},
    'go': {'test', 'build', 'vet', 'run'}, 'swift': {'build', 'test', 'run'}, 'dotnet': {'build', 'test', 'run'},
    'zig': {'build', 'test', 'run'}, 'deno': {'test', 'check', 'run', 'lint'}, 'meson': {'test', 'compile'},
    'npm': {'test', 't', 'tst', 'run', 'run-script', 'ci', 'build', 'exec', 'start'},
    'flutter': {'test', 'analyze', 'build', 'run'}, 'dart': {'test', 'analyze', 'run'}, 'mix': {'test', 'compile'},
}
CARGO_COM_VALOR = {'--manifest-path', '--target-dir', '--config', '-Z', '--color', '-C'}
NPM_COM_VALOR = {'--prefix', '-C', '--workspace', '-w', '--cwd', '--dir', '--filter', '-F'}
MAKE_COM_VALOR = {'-C', '--directory', '-f', '--file', '--makefile', '-I', '--include-dir', '-o', '--old-file', '-W',
                  '--what-if', '--new-file', '--assume-new', '-l', '--load-average'}
INTERPRETADOR_COM_VALOR = {'python': {'-W', '-X', '-Q'}, 'node': {'-r', '--require', '--import', '--loader', '-C',
                                                                 '--conditions', '--input-type'},
                           'ruby': {'-I', '-r'}, 'perl': {'-I', '-M', '-m'}, 'php': {'-d', '-c'}}
GERENCIADORES = {'pnpm', 'yarn', 'bun'}   # rodam os scripts do package.json pelo nome
SO_INSTALA = {'add', 'remove', 'rm', 'install', 'i', 'upgrade', 'up', 'update', 'info', 'why', 'outdated', 'init',
              'link', 'unlink', 'cache', 'config', 'login', 'logout', 'publish', 'audit', 'list', 'ls', 'version'}
RODA_O_RESTO = {'uv': 'run', 'poetry': 'run', 'pipenv': 'run', 'pdm': 'run', 'hatch': 'run', 'rye': 'run',
                'bundle': 'exec', 'pnpm': 'dlx'}   # "uv run pytest": quem verifica é o que vem depois
MAKE_SEM_VERIFICAR = {'clean', 'distclean', 'mrproper', 'install', 'uninstall', 'help'}
INTERPRETADORES = {'node', 'ruby', 'perl', 'php', 'lua', 'Rscript', 'julia'} | SHELLS
CODIGO_SOLTO = {'python': ('-c', '-'), 'node': ('-e', '--eval', '-p', '--print', '-'), 'ruby': ('-e',),
                'perl': ('-e', '-E'), 'php': ('-r',)}
NAO_VERIFICA = {'git', 'ls', 'cat', 'bat', 'less', 'more', 'head', 'tail', 'grep', 'rg', 'ag', 'echo', 'printf',
                'diff', 'wc', 'sed', 'awk', 'find', 'fd', 'cd', 'cp', 'mv', 'rm', 'rmdir', 'mkdir', 'touch', 'chmod',
                'chown', 'ln', 'sleep', 'true', 'false', 'test', '[', 'stat', 'file', 'du', 'df', 'which', 'type',
                'readlink', 'realpath', 'basename', 'dirname', 'date', 'tee', 'sort', 'uniq', 'cut', 'tr', 'xxd',
                'od', 'md5sum', 'sha256sum', 'tar', 'zip', 'unzip', 'gzip', 'curl', 'wget', 'ssh', 'scp', 'rsync',
                'systemctl', 'journalctl', 'ps', 'kill', 'pkill', 'pgrep', 'jq', 'yq', 'tree', 'xdg-open', 'code',
                'vim', 'nano', 'gh', 'pip', 'pip3', 'apt', 'apt-get', 'dpkg', 'snap', 'flatpak', 'pushd', 'popd'}


def _sub(args, com_valor=()):
    """O subcomando: o primeiro argumento que não é opção (nem valor de opção, nem +toolchain)."""
    j = 0
    while j < len(args):
        a = args[j]
        if a.startswith('+'):
            j += 1
        elif a.startswith('-'):
            j += 2 if (a in com_valor and '=' not in a) else 1
        else:
            return a
    return ''


def _linguagem(cmd):
    if re.fullmatch(r'python[\d.]*|pypy3?', cmd):
        return 'python'
    return cmd if cmd in CODIGO_SOLTO else None


def codigo_solto(palavras, redir, entrada=None):
    """O código passado direto ao interpretador (python -c, python - <<EOF, python - arg <<EOF, node -e...), ou None.
    Quem lê o script da entrada sem heredoc próprio (cat <<EOF | python3 -) usa o corpo de entrada."""
    k = indice_comando(palavras)
    if k is None:
        return None
    lingua = _linguagem(os.path.basename(palavras[k]))
    if not lingua:
        return None
    args, j = palavras[k + 1:], 0
    while j < len(args):
        a = args[j]
        if a == '-':
            break   # o script vem da entrada; o que vem depois são argumentos dele
        if a in CODIGO_SOLTO[lingua]:
            return args[j + 1] if j + 1 < len(args) else ''
        if a in INTERPRETADOR_COM_VALOR.get(lingua, ()):
            j += 2
            continue
        if not a.startswith('-'):
            return None   # roda um arquivo de script
        j += 1
    corpo = next((c for op, _, c in redir if op.startswith('<<') and c is not None), None)
    return corpo if corpo is not None else entrada


ESCREVE_ARQUIVO = re.compile(
    r"""\bopen\s*\([^\n]{0,300}?['"](?:[wax]b?\+?|r\+b?|rb\+)['"]|\bfdopen\s*\([^\n]{0,100}?['"](?:[wax]|r\+)"""
    r"""|\.write_(text|bytes)\s*\(|\bos\.(replace|rename)\s*\(|\bshutil\.(copy\w*|move)\s*\("""
    r"""|\.(write|append)(Text)?File(Sync)?\s*\(|\bFile\.write\s*\(""")
LITERAL_DE_CODIGO = re.compile(r"""['"]([^'"\n]*\.(?:%s))['"]""" % '|'.join(
    sorted((re.escape(e) for e in EXT_CODIGO), key=len, reverse=True)))


def escritas_soltas(codigo):
    """Arquivos de código que um trecho de Python/Node/Ruby grava (o remendo por python - <<EOF): só quando o trecho
    grava arquivo e cita um arquivo de código; editar nota ou dado com Python não é mudança de código."""
    if not codigo or not ESCREVE_ARQUIVO.search(codigo):
        return []
    return sorted({m.group(1) for m in LITERAL_DE_CODIGO.finditer(codigo)})


def eh_verificacao(palavras, nomes=()):
    """O comando simples verifica código: build, teste, lint, checagem de sintaxe, ou roda um programa ou o próprio
    arquivo mudado. Ler, listar, comparar, fazer commit, make clean e código solto (python -c, node -e) não contam."""
    k = indice_comando(palavras)
    if k is None:
        return False
    cmd, args = os.path.basename(palavras[k]), palavras[k + 1:]
    if cmd in nomes:
        return True
    if cmd in NAO_VERIFICA:
        return False
    if cmd in RODA_O_RESTO:
        if _sub(args) == RODA_O_RESTO[cmd]:
            return eh_verificacao(args[args.index(RODA_O_RESTO[cmd]) + 1:], nomes)
        if cmd not in GERENCIADORES:
            return False   # "uv pip install", "poetry add": não verificam nada
    if cmd in ('npx', 'pnpx', 'bunx'):
        resto = [a for a in args if not a.startswith('-')]
        if resto and os.path.basename(resto[0]) == 'prettier':
            return '--check' in args
        return bool(resto)   # roda uma ferramenta do projeto
    if cmd in ('make', 'gmake'):
        alvos = [a for a in _argumentos(args, MAKE_COM_VALOR)[0] if '=' not in a]
        return not alvos or not set(alvos) <= MAKE_SEM_VERIFICAR
    if cmd in VERIFICA_DIRETO:
        return True
    if cmd in VERIFICA_SUB:
        com_valor = CARGO_COM_VALOR if cmd == 'cargo' else NPM_COM_VALOR if cmd == 'npm' else ()
        return _sub(args, com_valor) in VERIFICA_SUB[cmd]
    if cmd in GERENCIADORES:
        sub = _sub(args, NPM_COM_VALOR)
        return bool(sub) and sub not in SO_INSTALA
    if cmd == 'cmake':
        return '--build' in args
    if cmd == 'prettier':
        return '--check' in args
    if cmd in ('gio', 'gtk-launch'):   # abrir o programa pelo atalho da área de trabalho é rodá-lo
        return cmd == 'gtk-launch' or _sub(args) == 'launch'
    lingua = _linguagem(cmd)
    if lingua == 'python':
        j = 0
        while j < len(args):
            a = args[j]
            if a == '-m':
                return j + 1 < len(args) and args[j + 1] not in ('pip', 'venv', 'ensurepip', 'http.server',
                                                                 'json.tool')
            if a in CODIGO_SOLTO['python']:
                return False   # código solto: pode ser remendo, não é verificação
            j += 2 if a in ('-W', '-X', '-Q') else 1
            if not a.startswith('-'):
                return True    # roda um arquivo
        return False
    if cmd in INTERPRETADORES:
        if cmd in SHELLS and any(re.fullmatch(r'-[a-zA-Z]*c[a-zA-Z]*', a) for a in args):
            return False   # bash -c: o que roda dentro é analisado à parte
        if any(a in CODIGO_SOLTO.get(cmd, ()) for a in args):
            return False
        return any(not a.startswith('-') for a in args) or '-n' in args or '--check' in args
    return '/' in palavras[k]   # roda um programa pelo caminho: ./x, build/AppTests, target/debug/app...


def grava_arquivo(op, alvo):
    """O redirecionamento grava num arquivo: 2>&1, >&2 e >&- apontam para um canal de saída, não para um arquivo."""
    return '>' in op and bool(alvo) and not alvo.startswith('/dev/') and not (
        '&' in op and op != '&>' and op != '&>>' and re.fullmatch(r'\d+|-', alvo))


def arquivos_escritos(palavras, redirecoes):
    """Arquivos de código que um comando simples grava: > e >>, tee, sed -i e perl -i, cp/mv/install para código, e
    patch/git apply (arquivo desconhecido, que conta como código)."""
    escritos = [alvo for op, alvo, _ in redirecoes if grava_arquivo(op, alvo) and eh_codigo(alvo)]
    k = indice_comando(palavras)
    if k is None:
        return escritos
    cmd, args = os.path.basename(palavras[k]), palavras[k + 1:]
    soltos = [a for a in args if not a.startswith('-')]
    curtas = ''.join(a[1:] for a in args if re.fullmatch(r'-[A-Za-z]+', a))
    if cmd == 'tee' or (cmd in ('sed', 'perl') and ('i' in curtas or any(a.startswith(('-i', '--in-place'))
                                                                          for a in args))):
        escritos += [a for a in soltos if eh_codigo(a)]
    elif cmd in ('cp', 'mv', 'install') and len(soltos) >= 2:
        destino = soltos[-1]
        if eh_codigo(destino):
            escritos.append(destino)
        elif destino.endswith('/') or destino in ('.', '..'):   # para dentro de uma pasta: os arquivos de código vão
            escritos += [os.path.join(destino, os.path.basename(a)) for a in soltos[:-1] if eh_codigo(a)]
    elif (cmd == 'patch' and '--dry-run' not in args) or (
            cmd == 'git' and soltos[:1] == ['apply'] and not {'--check', '--stat'} & set(args)):
        escritos.append('(patch)')
    return escritos


def eventos_do_bash(comando, nomes=()):
    """[('verifica',) | ('edita', arquivo, linhas)] na ordem em que o comando roda."""
    eventos, corpo_anterior = [], None
    for palavras, redir in comandos(comando):
        solto = codigo_solto(palavras, redir, corpo_anterior)
        if solto is not None:   # remendo por python -c, python - <<EOF, node -e...: muda código, não verifica
            for arq in escritas_soltas(solto):
                eventos.append(('edita', arq, max(1, solto.count('\n') + 1)))
        elif eh_verificacao(palavras, nomes):
            eventos.append(('verifica',))
        tamanho = sum(c.count('\n') + 1 for op, _, c in redir if c is not None)
        for arq in arquivos_escritos(palavras, redir):
            eventos.append(('edita', arq, max(1, tamanho)))
            tamanho = 0   # o tamanho do heredoc conta uma vez só
        corpo_anterior = next((c for op, _, c in redir if c is not None), corpo_anterior)   # cat <<EOF | python3 -
    return eventos


# ---------- pastas que só se executam (repositórios de ferramenta, baixados só para usar) ----------
ESCREVE_NOS_ARGUMENTOS = {'rm', 'rmdir', 'touch', 'mkdir', 'truncate', 'shred', 'unlink', 'tee'}
# Opções que levam valor (o valor não é o arquivo gravado): mkdir -m 700, touch -r REF, cp -t DESTINO...
VALOR_POR_COMANDO = {
    'mkdir': {'-m', '--mode', '-Z', '--context'}, 'touch': {'-r', '--reference', '-d', '--date', '-t'},
    'truncate': {'-s', '--size', '-r', '--reference'}, 'shred': {'-n', '--iterations', '-s', '--size'},
    'cp': {'-t', '--target-directory', '-S', '--suffix'}, 'mv': {'-t', '--target-directory', '-S', '--suffix'},
    'ln': {'-t', '--target-directory', '-S', '--suffix'},
    'install': {'-t', '--target-directory', '-m', '--mode', '-o', '--owner', '-g', '--group', '-S', '--suffix'},
    'rsync': {'-e', '--rsh', '--exclude', '--include', '--filter', '-f', '--exclude-from', '--include-from',
              '--files-from', '--chmod', '--chown', '--log-file', '--partial-dir', '--temp-dir', '-T', '--backup-dir',
              '--suffix', '--rsync-path', '--password-file', '--port', '--timeout', '--bwlimit', '--max-size',
              '--min-size'},
    'sed': {'-e', '--expression', '-f', '--file', '-l', '--line-length'},
    'perl': {'-e', '-E', '-M', '-m', '-I', '-x'},
    'tar': {'-C', '--directory', '-f', '--file', '-T', '--files-from', '-X', '--exclude-from'},
    'unzip': {'-d', '-x'},
    'curl': {'-o', '--output', '-d', '--data', '-H', '--header', '-X', '--request', '-u', '--user', '-A', '-e', '-b',
             '-c', '-T', '--upload-file', '-w', '--write-out'},
    'wget': {'-O', '--output-document', '-P', '--directory-prefix', '-o', '--output-file', '-U', '--user-agent'},
    'npm': NPM_COM_VALOR, 'pnpm': NPM_COM_VALOR, 'yarn': NPM_COM_VALOR, 'bun': NPM_COM_VALOR,
}
INSTALA = {'install', 'i', 'ci', 'add', 'remove', 'rm', 'uninstall', 'update', 'up', 'upgrade', 'link', 'rebuild',
           'prune', 'dedupe'}


def raizes_so_executar():
    """As pastas de ~/.claude/hooks/portao/so-executar.txt (uma por linha; # comenta): repositórios que o usuário
    só quer executar, nunca modificar. Linha que pegaria a casa inteira (/, ~ ou acima) ou
    que não é uma pasta existente é ignorada."""
    try:
        with open(SO_EXECUTAR, encoding='utf-8') as f:
            linhas = [l.split('#', 1)[0].strip() for l in f]
    except OSError:
        return []
    casa = os.path.realpath(os.path.expanduser('~'))
    largas = {'/', casa, os.path.dirname(casa)}
    raizes = (os.path.realpath(os.path.expanduser(l)) for l in linhas if l)
    return [r for r in raizes if r not in largas and os.path.isdir(r)]


def _argumentos(args, com_valor):
    """(argumentos soltos, {opção: valor}), pulando o valor das opções de com_valor e as --opcao=valor."""
    soltos, valores, j = [], {}, 0
    while j < len(args):
        a = args[j]
        if a.startswith('-') and a != '-':
            if a.startswith('--') and '=' in a:
                op, v = a.split('=', 1)
                valores[op] = v
            elif a in com_valor and j + 1 < len(args):
                valores[a] = args[j + 1]
                j += 1
            j += 1
            continue
        soltos.append(a)
        j += 1
    return soltos, valores


def dentro_de(caminho, raizes, base=None):
    """A raiz (de raizes) que contém o caminho, ou None. Caminho relativo é resolvido a partir de base."""
    if not caminho or not raizes or not isinstance(caminho, str):
        return None
    p = os.path.expanduser(caminho)
    if not os.path.isabs(p):
        if not base:
            return None
        p = os.path.join(base, p)
    real = os.path.realpath(p)
    return next((r for r in raizes if real == r or real.startswith(r + os.sep)), None)


def alvos_de_escrita(palavras, redirecoes):
    """Todo caminho que um comando simples grava, de qualquer tipo (para proteger as pastas só de executar).
    "." quer dizer a pasta em que o comando roda (npm install, tar -x sem -C, curl -O...)."""
    alvos = [a for op, a, _ in redirecoes if grava_arquivo(op, a)]
    k = indice_comando(palavras)
    if k is None:
        return alvos
    cmd, args = os.path.basename(palavras[k]), palavras[k + 1:]
    soltos, val = _argumentos(args, VALOR_POR_COMANDO.get(cmd, set()))
    curtas = ''.join(a[1:] for a in args if re.fullmatch(r'-[A-Za-z]+', a))
    no_lugar = 'i' in curtas or any(a.startswith(('-i', '--in-place')) for a in args)
    if cmd in ('chmod', 'chown', 'chgrp'):   # o 1º argumento é o modo ou o dono, não um arquivo
        alvos += soltos if any(a.startswith('--reference') for a in args) else soltos[1:]
    elif cmd in ESCREVE_NOS_ARGUMENTOS:
        alvos += soltos
    elif cmd in ('sed', 'perl') and no_lugar:   # sem -e/-f, o 1º argumento do sed é o script
        script_a_parte = cmd == 'perl' or {'-e', '--expression', '-f', '--file'} & set(val)
        alvos += soltos if script_a_parte else soltos[1:]
    elif cmd in ('cp', 'mv', 'install', 'ln', 'rsync'):
        destino = val.get('-t') or val.get('--target-directory') or (soltos[-1] if soltos else None)
        if destino:
            alvos.append(destino)
        if cmd == 'mv':
            alvos += [a for a in soltos if a != destino]
    elif cmd == 'tar' and ('x' in curtas or '--extract' in args or '--get' in args):
        alvos.append(val.get('-C') or val.get('--directory') or '.')
    elif cmd == 'unzip':
        alvos.append(val.get('-d') or '.')
    elif cmd == 'curl' and (val.get('-o') or val.get('--output') or 'O' in curtas or '--remote-name' in args):
        alvos.append(val.get('-o') or val.get('--output') or '.')
    elif cmd == 'wget':
        alvos.append(val.get('-O') or val.get('--output-document') or val.get('-P') or val.get('--directory-prefix')
                     or '.')
    elif cmd in ('npm', 'pnpm', 'yarn', 'bun') and _sub(args, NPM_COM_VALOR) in INSTALA:
        alvos.append(val.get('--prefix') or val.get('-C') or val.get('--cwd') or '.')
    elif cmd == 'git' and _sub(args) == 'clone':
        resto = soltos[soltos.index('clone') + 1:] if 'clone' in soltos else []
        if len(resto) >= 2:
            alvos.append(resto[1])
        elif resto:
            alvos.append(re.sub(r'\.git$', '', resto[0].rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1]) or '.')
    return alvos


# ---------- a conversa (transcrição) ----------
def _linhas_do_fim(caminho, bloco=1 << 20):
    """Linhas de um arquivo, da última para a primeira, sem carregar tudo (a conversa pode ter centenas de MB)."""
    with open(caminho, 'rb') as f:
        f.seek(0, os.SEEK_END)
        pos, resto = f.tell(), b''
        while pos > 0:
            ler = min(bloco, pos)
            pos -= ler
            f.seek(pos)
            pedaco = f.read(ler) + resto
            linhas = pedaco.split(b'\n')
            resto = linhas.pop(0)
            for linha in reversed(linhas):
                if linha.strip():
                    yield linha.decode('utf-8', 'replace')
        if resto.strip():
            yield resto.decode('utf-8', 'replace')


def _json(linha):
    try:
        e = json.loads(linha)
        return e if isinstance(e, dict) else None
    except ValueError:
        return None


def _blocos(e):
    m = e.get('message')
    c = m.get('content') if isinstance(m, dict) else None
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def _eh_pedido(e):
    """Linha de pedido de verdade (texto do usuário ou aviso), não só resultado de ferramenta."""
    if e.get('type') != 'user':
        return False
    m = e.get('message')
    c = m.get('content') if isinstance(m, dict) else None
    return isinstance(c, str) or any(b.get('type') == 'text' for b in _blocos(e))


def _reduz(nome, ent):
    """Só o que a checagem usa de cada ferramenta (o conteúdo inteiro de um Write ou de uma saída não fica na memória)."""
    if not isinstance(ent, dict):
        ent = {}
    if nome in EDICAO:
        if nome == 'Write':
            linhas = str(ent.get('content') or '').count('\n') + 1
        elif nome == 'Edit':
            linhas = str(ent.get('new_string') or '').count('\n') + 1
        elif nome == 'MultiEdit':
            linhas = sum(str(e.get('new_string') or '').count('\n') + 1 for e in ent.get('edits') or []
                         if isinstance(e, dict))
        else:
            linhas = 1
        return {'arquivo': str(ent.get('file_path') or ent.get('notebook_path') or ''), 'linhas': linhas}
    if nome in ('Bash', 'PowerShell'):
        return {'command': str(ent.get('command') or '')[:50000]}
    if nome == 'Skill':
        return {'skill': str(ent.get('skill') or '')}
    if nome in ('Agent', 'Task'):
        return {'description': str(ent.get('description') or ''), 'prompt': str(ent.get('prompt') or '')[:400],
                'tipo': str(ent.get('subagent_type') or '')}
    return {}


def _usos_e_erros(e, usos, erros):
    for b in _blocos(e):
        if b.get('type') == 'tool_use' and e.get('type') == 'assistant':
            nome = str(b.get('name') or '')
            usos.append((b.get('id'), nome, _reduz(nome, b.get('input'))))
        elif b.get('type') == 'tool_result' and b.get('is_error'):
            erros.add(b.get('tool_use_id'))


def _sem_falhas(usos, erros):
    """Edição negada ou que falhou não mudou nada, e entrega negada não entregou; comando com erro conta (teste que
    falha também foi rodado)."""
    return [(nome, ent) for ident, nome, ent in usos
            if not ((nome in EDICAO or nome == 'SubagentHandback') and ident is not None and ident in erros)]


def acoes_do_pedido(caminho, prompt_id):
    """Ferramentas usadas no pedido atual da sessão principal, em ordem: [(nome, entrada reduzida)].
    Sem prompt_id não faz nada: ler a conversa inteira custou 9 s e 1,7 GB numa conversa real."""
    if not prompt_id or not caminho or not os.path.isfile(caminho):
        return []
    trechos, erros = [], set()   # do fim para o começo: (é do pedido atual?, é do assistente?, é pedido?, usos)
    for linha in _linhas_do_fim(caminho):
        e = _json(linha)
        if not e:
            continue
        pid = e.get('promptId')
        if e.get('type') == 'user' and pid and pid != prompt_id:
            break   # chegou ao pedido anterior
        usos = []
        _usos_e_erros(e, usos, erros)
        trechos.append((pid == prompt_id, e.get('type') == 'assistant', _eh_pedido(e), usos))
    trechos.reverse()
    inicio = next((i for i, t in enumerate(trechos) if t[0]), None)
    if inicio is None:
        return []
    if not trechos[inicio][2]:
        # Mensagem na fila: o id novo chega já no resultado da ferramenta, e a chamada (com a linha do hook no meio)
        # fica antes dele. A leitura já parou na última linha de usuário do pedido anterior, então tudo o que veio
        # depois dela é deste turno.
        inicio = 0
    return _sem_falhas([u for t in trechos[inicio:] for u in t[3]], erros)


def acoes_do_agente(caminho):
    """Ferramentas usadas na conversa inteira de um subagente, em ordem, lendo uma linha por vez."""
    usos, erros = [], set()
    with open(caminho, encoding='utf-8', errors='replace') as f:
        for linha in f:
            e = _json(linha) if linha.strip() else None
            if e:
                _usos_e_erros(e, usos, erros)
    return _sem_falhas(usos, erros)


def transcricao_do_agente(transcript_path, sessao, agente):
    """A conversa de um subagente: <pasta da sessão>/<sessão>/subagents/**/agent-<id>.jsonl."""
    if not transcript_path or not agente:
        return None
    nome = f'agent-{_limpo(agente)}.jsonl'
    if os.path.basename(transcript_path) == nome and os.path.isfile(transcript_path):
        return transcript_path
    base = os.path.dirname(transcript_path)
    for raiz in (os.path.join(base, _limpo(sessao), 'subagents'), os.path.join(base, 'subagents')):
        if os.path.isdir(raiz):
            achados = glob.glob(os.path.join(glob.escape(raiz), '**', nome), recursive=True)
            if achados:
                return achados[0]
    return None


# ---------- a checagem do fim ----------
REVISAO_SKILL = {'revisar', 'code-review', 'security-review'}
REVISAO_AGENTE = re.compile(r'revis|review|audit', re.I)


def _eh_codigo_do_projeto(arq):
    return arq == '(patch)' or (eh_codigo(arq) and not eh_temporario(arq))


def checar_fim(acoes, resposta):
    """O que falta para o agente entregar: ([motivos], [arquivos de código mudados], quantas mudanças)."""
    nomes = set()   # 1ª passada: os arquivos mudados, para reconhecer quem roda o próprio arquivo
    for nome, ent in acoes:
        if nome in EDICAO:
            nomes.add(os.path.basename(ent.get('arquivo') or ''))
        elif nome in ('Bash', 'PowerShell'):
            nomes |= {os.path.basename(e[1]) for e in eventos_do_bash(ent.get('command') or '') if e[0] == 'edita'}
    nomes.discard('')
    eventos = []
    for nome, ent in acoes:
        if nome in EDICAO:
            if _eh_codigo_do_projeto(ent.get('arquivo')):
                eventos.append(('edita', ent['arquivo'], ent.get('linhas', 1)))
        elif nome in ('Bash', 'PowerShell'):
            eventos += [e for e in eventos_do_bash(ent.get('command') or '', nomes)
                        if e[0] != 'edita' or _eh_codigo_do_projeto(e[1])]
        elif nome == 'Skill' and (ent.get('skill') or '').split(':')[-1] in REVISAO_SKILL:
            eventos.append(('revisa',))
        elif nome in ('Agent', 'Task') and ent.get('tipo') != 'fork' \
                and REVISAO_AGENTE.search(f"{ent.get('description')} {ent.get('prompt')}"):
            eventos.append(('revisa',))   # o kernel pede revisor novo: fork não conta
    edicoes = [e for e in eventos if e[0] == 'edita']
    if not edicoes:
        return [], [], 0
    primeira = min(i for i, e in enumerate(eventos) if e[0] == 'edita')
    ultima = max(i for i, e in enumerate(eventos) if e[0] == 'edita')
    verificou = any(e[0] == 'verifica' for e in eventos[ultima + 1:])
    revisou = any(e[0] == 'revisa' for e in eventos[primeira + 1:])   # revisão antes de mudar nada não revisa nada
    total = sum(e[2] for e in edicoes)
    faltas = []
    if not verificou and not declarou(resposta, 'Sem verificação automática'):
        faltas.append(
            'você mudou código e não rodou build, testes, lint nem o próprio programa depois da última mudança. '
            'Rode o gate do projeto (ou execute o que mudou) e mostre o resultado real. Se não houver como '
            'verificar automaticamente (hardware, interface), escreva na resposta final uma linha começando com '
            '"Sem verificação automática:" dizendo como verificou de verdade.')
    if (total > LIMITE_LINHAS or any(sensivel(e[1]) for e in edicoes)) and not revisou \
            and not declarou(resposta, 'Sem revisão independente'):
        faltas.append(
            f'a mudança é de nível 3 ({total} linhas, ou arquivo sensível). Antes de terminar, peça revisão '
            'independente (/revisar ou um agente revisor novo, não fork, que receba o código e não a sua conclusão). '
            'Se não der, escreva na resposta final uma linha começando com "Sem revisão independente:" com o motivo.')
    return faltas, sorted({os.path.basename(e[1]) for e in edicoes}), len(edicoes)


def _limpo(texto):
    return re.sub(r'[^A-Za-z0-9_-]', '_', texto)[:150] if isinstance(texto, str) else ''


def _arquivo_estado(pasta, chave):
    """Nome do arquivo de estado pela chave inteira (hash): chaves longas não colidem e nada vira caminho."""
    return os.path.join(pasta, hashlib.sha256(chave.encode('utf-8', 'replace')).hexdigest()[:40] + '.json')


def _pasta_estado():
    """Pasta privada da contagem de bloqueios, ou None se não der para garantir que é nossa."""
    base = os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir()
    pasta = os.path.join(base, f'claude-portao-{os.getuid()}')
    try:
        os.mkdir(pasta, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(pasta)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return None
    return pasta


def _limpa_velhos(pasta, idade=2 * 86400):
    agora = time.time()
    for e in os.scandir(pasta):
        try:
            if e.is_file(follow_symlinks=False) and agora - e.stat(follow_symlinks=False).st_mtime > idade:
                os.unlink(e.path)
        except OSError:
            pass


def pode_bloquear(chave, edicoes):
    """Número deste bloqueio (1 a LIMITE_BLOQUEIOS), ou 0 quando o agente já levou LIMITE_BLOQUEIOS bloqueios sem
    mudar código desde então: aí o portão solta e avisa o usuário. Sem onde contar, também solta (falha aberta)."""
    pasta = _pasta_estado()
    if pasta is None:
        return 0
    arq = _arquivo_estado(pasta, chave)
    try:
        with open(arq, encoding='utf-8') as f:
            est = json.load(f)
    except (OSError, ValueError):
        est = {}
    if not isinstance(est, dict) or est.get('edicoes') != edicoes or not isinstance(est.get('bloqueios'), int):
        est = {'edicoes': edicoes, 'bloqueios': 0}
    if est['bloqueios'] >= LIMITE_BLOQUEIOS:
        return 0
    est['bloqueios'] += 1
    fd, tmp = tempfile.mkstemp(dir=pasta, prefix='.tmp-')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(est, f)
    os.replace(tmp, arq)
    _limpa_velhos(pasta)
    return est['bloqueios']


def esquecer(chave):
    pasta = _pasta_estado()
    if pasta:
        try:
            os.unlink(_arquivo_estado(pasta, chave))
        except FileNotFoundError:
            pass


def checar_entrega(d, resposta, evento):
    """Checagem do fim (seção 8) para o Stop, o SubagentStop e a entrega do subagente pelo SubagentHandback
    (evento 'PreToolUse'; ali o Claude Code descarta o bloqueio do SubagentStop, então o portão nega a entrega)."""
    sessao = d.get('session_id') or ''
    if evento == 'Stop':
        acoes = acoes_do_pedido(d.get('transcript_path'), d.get('prompt_id'))
        chave, quem = f"{sessao}--{d.get('prompt_id')}", 'a sessão principal'
    else:
        if evento == 'SubagentStop':
            if not d.get('agent_type'):
                return   # agentes internos do Claude Code (sugestões etc.)
            caminho = d.get('agent_transcript_path')
        else:
            caminho = transcricao_do_agente(d.get('transcript_path'), sessao, d.get('agent_id'))
        if not caminho or not os.path.isfile(caminho):
            return
        acoes = acoes_do_agente(caminho)
        if evento == 'SubagentStop':
            entregas = [i for i, (n, _) in enumerate(acoes) if n == 'SubagentHandback']
            if entregas:   # a entrega já passou pela checagem no guarda.py: só conta o que veio depois dela
                acoes = acoes[entregas[-1] + 1:]
        chave = f"{sessao}--{d.get('agent_id') or caminho}"
        quem = f"o subagente {d.get('agent_type') or d.get('agent_id') or ''}".strip()
    faltas, arquivos, edicoes = checar_fim(acoes, resposta)
    if not faltas:
        esquecer(chave)
        return
    n = pode_bloquear(chave, edicoes)
    lista = ', '.join(arquivos[:6]) + (' e outros' if len(arquivos) > 6 else '')
    if not n:
        responder({'systemMessage': f'Portão de qualidade: {quem} terminou sem cumprir o padrão de código '
                                    f'({lista}) depois de {LIMITE_BLOQUEIOS} avisos. Confira esse trabalho antes '
                                    'de confiar nele.'})
    motivo = (f'Portão de qualidade (seção 8 do principal, ~/.claude/kernel/KERNEL.md), aviso {n} de '
              f'{LIMITE_BLOQUEIOS}. Arquivos: {lista}. ' + ' Além disso, '.join(f[0].upper() + f[1:] for f in faltas))
    if evento == 'PreToolUse':
        motivo = motivo.replace('na resposta final', 'no relatório da entrega') + (
            ' A declaração só vale dentro do relatório: faça o que falta (ou ponha a linha no texto) e chame o '
            'SubagentHandback de novo.')
        responder({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                          'permissionDecisionReason': motivo}})
    responder({'decision': 'block', 'reason': motivo})


# ---------- entrada e saída ----------
def ler_entrada():
    d = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace'))   # UTF-8 em qualquer locale
    return d if isinstance(d, dict) else {}


def responder(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))
    sys.stdout.flush()
    sys.exit(0)
