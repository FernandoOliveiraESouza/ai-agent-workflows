#!/usr/bin/env python3
"""Lista os projetos do registro ($PROJETOS_REGISTRO, padrão ~/vault/Projetos/00-PROJETOS.md), mais recentes primeiro e concluídos no fim.
Sem argumento: lista numerada pronta para mostrar. --tsv: nome<TAB>pasta<TAB>status<TAB>data (para o menu do terminal)."""
import os
import sys

REGISTRO = os.path.expanduser(os.environ.get('PROJETOS_REGISTRO', '~/vault/Projetos/00-PROJETOS.md'))


def projetos():
    linhas = []
    for l in open(REGISTRO, encoding='utf-8'):
        if not l.startswith('| ') or l.startswith('|---'):
            continue
        c = [x.strip() for x in l.strip().strip('|').split('|')]
        if c[:2] == ['Projeto', 'Pasta']:   # cabeçalho, pelas colunas: um projeto pode se chamar "Projeto ..."
            continue
        if len(c) >= 5:
            nome, pasta, _, status, data = c[:5]
            linhas.append((nome, pasta.strip('`'), status, data))
    # Estável: dentro da mesma data, mantém a ordem do registro.
    linhas.sort(key=lambda p: p[3], reverse=True)
    linhas.sort(key=lambda p: p[2] == 'concluido')
    return linhas


if __name__ == '__main__':
    lista = projetos()
    if '--tsv' in sys.argv:
        for p in lista:
            print('\t'.join(p))
    else:
        for i, (nome, _, status, data) in enumerate(lista, 1):
            print(f'{i}. {nome} ({status}, {data})')
        print(f'{len(lista) + 1}. Novo projeto')
