from src.data.collector import collect_app_reviews, reviews_to_dataframe, add_sentiment_column, save_reviews_csv
from src.models.train import train_model

apps_id = ['br.com.brainweb.ifood', 'com.grability.rappi']

todas_avaliacoes = []

for ids in apps_id:

    dados = collect_app_reviews(app_id=f'{ids}', total_limit=100, page_size=10)
    todas_avaliacoes.extend(dados)

tabela = reviews_to_dataframe(todas_avaliacoes)
save_reviews_csv(tabela=tabela, caminho='data/raw/apps_reviews.csv')
tabela = add_sentiment_column(tabela=tabela)
save_reviews_csv(tabela=tabela, caminho='data/processed/apps_reviews.csv')

train_model('data/processed/apps_reviews.csv')