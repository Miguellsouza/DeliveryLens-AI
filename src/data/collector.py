from google_play_scraper import reviews, Sort
import pandas as pd

def fetch_review_page(app_id, quant, continuation_token=None):
    avaliacoes, tk = reviews(app_id,
                        lang="pt-BR",
                        country="br",
                        sort=Sort.NEWEST,
                        count=quant,
                        continuation_token=continuation_token)

    lista = []

    for avaliacao in avaliacoes:
        lista.append({"app_id": app_id,
                    "reviewId": avaliacao["reviewId"],
                    "content": avaliacao["content"],
                    "score": avaliacao["score"],
                    "at": avaliacao["at"],
                    "appVersion": avaliacao["appVersion"],
                    "reviewCreatedVersion": avaliacao["reviewCreatedVersion"],
                    "thumbsUpCount": avaliacao["thumbsUpCount"],
                    "replyContent": avaliacao["replyContent"],
                    "repliedAt": avaliacao["repliedAt"]
                    })

    return lista, tk


def collect_app_reviews(app_id:str, total_limit:int, page_size:int = 10):
    avaliacoes_coletadas = []
    token = None

    if not isinstance(app_id, str):
        raise TypeError('o tipo recebido de app_id está errado')
    else:

        if app_id.strip():

            if isinstance(total_limit,bool):
                raise TypeError('o tipo recebido de total_limit está incorreto')
            else:
                if isinstance(total_limit, int):
                    if total_limit <= 0:
                        raise ValueError('o tipo está correto, mas o valor de total_limit não é permitido')
                    else:
                        if isinstance(page_size, bool):
                            raise TypeError('o tipo recebido de page_size está incorreto')
                        else:
                            if isinstance(page_size, int):
                                #é inteiro
                                if page_size <= 0:
                                    raise ValueError('o tipo está correto, mas o valor de page_size não é permitido')
                                else:
                                    while len(avaliacoes_coletadas) < total_limit:
                                        falta = total_limit - len(avaliacoes_coletadas)
                                        quantidade_pagina = min(page_size, falta)
                                        pagina, token = fetch_review_page(app_id=app_id, quant=quantidade_pagina, continuation_token=token)
                                        avaliacoes_coletadas.extend(pagina)

                                        if not pagina:
                                           break
                                        elif token.token is None:
                                            break
                                    """
                                    O continuation_token guarda também a quantidade (count) da primeira solicitação.
                                    Por isso, quando começamos com páginas de 10, as próximas continuam retornando 10,
                                    mesmo que solicitemos 5, por isso temos que fazer um slicing com o total_limit.
                                    """
                                    return avaliacoes_coletadas[:total_limit]
                            else:
                                raise TypeError('o tipo recebido de page_size está incorreto')
                else:
                    raise TypeError('o tipo recebido de total_limit está incorreto')
        else:
            raise ValueError('o tipo está correto, mas o valor de app_id não é permitido')

def reviews_to_dataframe(avaliacoes_filtradas):
    tabela = pd.DataFrame(avaliacoes_filtradas)
    tabela['at'] = pd.to_datetime(tabela['at'])

    return tabela

def save_reviews_csv(tabela, caminho):
    tabela.to_csv(caminho, index=False)

def add_sentiment_column(tabela):
    sentiment = {
        1: 'Negativo',
        2: 'Negativo',
        3: 'Neutro',
        4: 'Positivo',
        5: 'Positivo'
    }

    tabela['sentiment'] = tabela['score'].map(sentiment)

    return tabela