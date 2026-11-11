import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
ARQUIVO_AVALIACOES = BASE_DIR / 'data' / 'processed' / 'apps_reviews.csv'

class Api():
    def obter_indicadores(self, app_id):
        df = pd.read_csv(ARQUIVO_AVALIACOES)

        df_app = df[df['app_id'] == app_id]
        total_avaliacoes = len(df_app)
        total_positivas = len(df_app[df_app['sentiment'] == 'Positivo'])
        total_negativas = len(df_app[df_app['sentiment'] == 'Negativo'])
        total_neutras = len(df_app[df_app['sentiment'] == 'Neutro'])
        nota_media = df_app['score'].mean()

        indicadores = {
            'total_avaliacoes': total_avaliacoes,
            'total_positivas':  total_positivas,
            'total_negativas': total_negativas,
            'total_neutras':  total_neutras,
            'nota_media':  float(nota_media)
        }

        return indicadores
    
    def obter_avaliacoes_por_dia(self, app_id):
        df_day = pd.read_csv('data/processed/apps_reviews.csv')

        df_app_day = df_day[df_day['app_id'] == app_id].copy()
        df_app_day['at'] = pd.to_datetime(df_app_day['at'], errors='coerce')
        df_app_day = df_app_day.dropna(subset=['at'])
        df_app_day['data'] = df_app_day['at'].dt.strftime('%Y-%m-%d')
        avaliacoes_dia = df_app_day.groupby(['data']).size().sort_index()

        df_app_data ={
            'data': avaliacoes_dia.index.tolist(),
            'quantidade':avaliacoes_dia.tolist()
        }

        return df_app_data