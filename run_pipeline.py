"""
Pipeline principal de ETL, Data Cleaning e Treinamento do DeliveryLens-AI.

Fluxo executado:
1. Coleta das avaliações via Google Play Scraper (opcional com --skip-collect).
2. Armazenamento imutável dos dados brutos em data/raw/apps_reviews.csv.
3. Investigação, limpeza e rastreabilidade via src/data/cleaner.py.
4. Geração dos dados tratados (data/processed/apps_reviews.csv), rejeitados e relatório de qualidade.
5. Adição dos rótulos de sentimento (add_sentiment_column).
6. Treinamento e avaliação do classificador TF-IDF + Regressão Logística (train_model).
"""

import argparse
from pathlib import Path

from src.data.cleaner import run_data_cleaning
from src.data.collector import (
    add_sentiment_column,
    collect_app_reviews,
    reviews_to_dataframe,
    save_reviews_csv,
)
from src.models.train import train_model

DEFAULT_APPS_ID = ["br.com.brainweb.ifood", "com.grability.rappi"]
RAW_PATH = Path("data/raw/apps_reviews.csv")
PROCESSED_PATH = Path("data/processed/apps_reviews.csv")
REJECTED_PATH = Path("data/processed/apps_reviews_rejected.csv")
REPORT_JSON_PATH = Path("data/processed/data_quality_report.json")
REPORT_MD_PATH = Path("data/processed/data_quality_report.md")


def run_pipeline(
    apps_id: list[str] | None = None,
    total_limit: int = 10000,
    page_size: int = 10,
    skip_collect: bool = False,
    raw_path: str | Path = RAW_PATH,
    processed_path: str | Path = PROCESSED_PATH,
    rejected_path: str | Path = REJECTED_PATH,
    report_json_path: str | Path = REPORT_JSON_PATH,
    report_md_path: str | Path = REPORT_MD_PATH,
) -> dict:
    """Executa o pipeline completo mantendo os dados brutos intactos."""
    target_apps = apps_id or DEFAULT_APPS_ID
    raw_file = Path(raw_path)
    processed_file = Path(processed_path)

    # 1 e 2. Coleta e armazenamento dos dados brutos
    if not skip_collect:
        todas_avaliacoes = []
        for app_id in target_apps:
            dados = collect_app_reviews(
                app_id=f"{app_id}",
                total_limit=total_limit,
                page_size=page_size,
            )
            todas_avaliacoes.extend(dados)

        tabela_bruta = reviews_to_dataframe(todas_avaliacoes)
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        save_reviews_csv(tabela=tabela_bruta, caminho=str(raw_file))
        del tabela_bruta, todas_avaliacoes

    # 3 e 4. Análise e limpeza dos dados (nunca altera data/raw/apps_reviews.csv)
    tabela_limpa, _, report = run_data_cleaning(
        raw_path=raw_file,
        processed_path=processed_file,
        rejected_path=rejected_path,
        report_json_path=report_json_path,
        report_md_path=report_md_path,
    )

    summary = report["summary"]
    print(
        f"[Data Cleaning] Brutos: {summary['initial_records']} | "
        f"Válidos: {summary['final_records']} | "
        f"Duplicados removidos: {summary['duplicates_removed']} | "
        f"Rejeitados: {summary['rejected_records']} | "
        f"Registros modificados: {summary['unique_records_modified']} "
        f"({summary['total_correction_operations']} operações)"
    )

    # 5. Adição dos rótulos de sentimento sobre os registros válidos
    tabela_final = add_sentiment_column(tabela=tabela_limpa)
    save_reviews_csv(tabela=tabela_final, caminho=str(processed_file))

    # 6. Treinamento e avaliação do classificador
    Path("artifacts").mkdir(parents=True, exist_ok=True)
    train_model(str(processed_file))

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Executa o pipeline do DeliveryLens-AI.")
    parser.add_argument(
        "--skip-collect",
        action="store_true",
        help="Pula a coleta na Google Play e executa limpeza + treino a partir de data/raw/apps_reviews.csv.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10000,
        help="Quantidade máxima de avaliações coletadas por aplicativo (padrão: 10000).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_pipeline(total_limit=args.limit, skip_collect=args.skip_collect)