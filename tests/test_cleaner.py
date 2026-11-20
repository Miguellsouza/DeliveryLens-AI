"""
Testes automatizados para o módulo src/data/cleaner.py e integração com o pipeline e API desktop.
Utiliza conjuntos de dados sintéticos em diretórios temporários (tmp_path),
sem modificar a base real do projeto.
"""

from pathlib import Path
import pandas as pd
import pytest

from src.data.cleaner import (
    EXPECTED_COLUMNS,
    clean_dates,
    clean_optional_metadata,
    clean_text_series,
    compute_file_sha256,
    deduplicate_reviews,
    inspect_raw_data,
    run_data_cleaning,
    separate_empty_texts,
    validate_and_clean_scores,
    validate_and_clean_thumbs_up,
)
from src.data.collector import add_sentiment_column, save_reviews_csv
import src.desktop.api as desktop_api_module
from src.desktop.api import Api
from src.models.train import train_model


def _make_sample_df(rows: list[dict], extra_cols: dict | None = None) -> pd.DataFrame:
    """Cria DataFrame sintético aderente ao esquema real do DeliveryLens-AI."""
    defaults = {
        "app_id": "br.com.brainweb.ifood",
        "reviewId": "rev-001",
        "content": "Entrega rápida e comida excelente!",
        "score": 5,
        "at": "2026-11-18 16:00:00",
        "appVersion": "10.148.0",
        "reviewCreatedVersion": "10.148.0",
        "thumbsUpCount": 0,
        "replyContent": None,
        "repliedAt": None,
    }
    cols = list(EXPECTED_COLUMNS)
    if extra_cols:
        for k, v in extra_cols.items():
            defaults[k] = v
            if k not in cols:
                cols.append(k)

    full_rows = []
    for idx, row in enumerate(rows):
        item = defaults.copy()
        item["reviewId"] = f"rev-{idx:03d}"
        item.update(row)
        full_rows.append(item)
    df = pd.DataFrame(full_rows, columns=cols)
    df["raw_row_index"] = range(len(df))
    return df


def test_deduplicate_reviews_exact_and_conflicting_with_three_or_more_records():
    """
    - Compara cada registro descartado diretamente com o vencedor escolhido.
    - Em grupos com 3 ocorrências (2 antigas idênticas entre si + 1 vencedora mais recente),
      ambas as descartadas devem ser classificadas como conflitantes em relação à vencedora.
    """
    df = _make_sample_df([
        {"app_id": "br.com.brainweb.ifood", "reviewId": "rev-exact", "content": "Muito bom", "score": 5, "at": "2026-11-18 10:00:00"},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "rev-exact", "content": "Muito bom", "score": 5, "at": "2026-11-18 10:00:00"},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "rev-triple", "content": "Demorou muito", "score": 2, "at": "2026-11-18 10:00:00", "replyContent": None},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "rev-triple", "content": "Demorou muito", "score": 2, "at": "2026-11-18 10:00:00", "replyContent": None},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "rev-triple", "content": "Resolvido pelo suporte!", "score": 4, "at": "2026-11-18 12:00:00", "replyContent": "Obrigado pelo retorno!"},
        {"app_id": "com.grability.rappi", "reviewId": "rev-exact", "content": "Rappi ok", "score": 4, "at": "2026-11-18 11:00:00"},
    ])

    dedup_df, rejected_df, stats = deduplicate_reviews(df)

    assert len(dedup_df) == 3
    assert len(rejected_df) == 3
    assert stats["duplicates_detected"] == 3
    assert stats["exact_duplicates_removed"] == 1
    assert stats["conflicting_duplicates_resolved"] == 2

    triple_rejects = rejected_df[rejected_df["reviewId"] == "rev-triple"]
    assert len(triple_rejects) == 2
    assert all("conflitante com o registro preservado" in d for d in triple_rejects["rejection_details"])


def test_clean_text_preserves_portuguese_accents_emojis_and_casing():
    """Verifica normalização de espaços e Unicode preservando acentos, pontuação, emojis e caixa."""
    df = _make_sample_df([
        {"content": "   Oferta   de   R$0,90!!   Muito   BOA 😍🍕  "},
        {"content": "Cafe\u0301 e pão de queijo, não veio!"},
        {"content": "   \t\n   "},
        {"content": None},
    ])

    cleaned_df, mod_indices, ops_count = clean_text_series(df)
    assert ops_count == 3
    assert mod_indices == {0, 1, 2}
    assert cleaned_df.loc[0, "content"] == "Oferta de R$0,90!! Muito BOA 😍🍕"
    assert cleaned_df.loc[1, "content"] == "Café e pão de queijo, não veio!"
    assert cleaned_df.loc[2, "content"] == ""

    valid_df, rejected_df = separate_empty_texts(cleaned_df)
    assert len(valid_df) == 2
    assert len(rejected_df) == 2
    assert set(rejected_df["rejection_reason"]) == {"missing_or_empty_content"}


def test_clean_text_preserves_zwj_compound_emojis():
    """
    Garante que o caractere Unicode U+200D (Zero Width Joiner) NÃO seja removido,
    preservando emojis compostos como 👨‍👩‍👧‍👦 e 👩‍💻 intactos após a normalização,
    enquanto caracteres de controle reais (\u200b, \u200c, \ufeff) são removidos.
    """
    family_emoji = "👨‍👩‍👧‍👦"
    dev_emoji = "👩‍💻"
    assert "\u200d" in family_emoji
    assert "\u200d" in dev_emoji

    df = _make_sample_df([
        {"content": f"  Pedido   para toda a família {family_emoji}!!  "},
        {"content": f"Suporte técnico excelente {dev_emoji}\u200b\u200c\ufeff"},
        {"content": f"Jantar em família {family_emoji} e trabalho {dev_emoji}"},
    ])

    cleaned_df, mod_indices, ops_count = clean_text_series(df)
    assert ops_count == 2
    assert mod_indices == {0, 1}
    assert cleaned_df.loc[0, "content"] == f"Pedido para toda a família {family_emoji}!!"
    assert cleaned_df.loc[1, "content"] == f"Suporte técnico excelente {dev_emoji}"
    assert cleaned_df.loc[2, "content"] == f"Jantar em família {family_emoji} e trabalho {dev_emoji}"
    assert "\u200d" in cleaned_df.loc[0, "content"]
    assert "\u200d" in cleaned_df.loc[1, "content"]
    assert "\u200b" not in cleaned_df.loc[1, "content"]
    assert "\u200c" not in cleaned_df.loc[1, "content"]
    assert "\ufeff" not in cleaned_df.loc[1, "content"]


def test_validate_and_clean_scores():
    """Garante separação de notas inválidas/fora de [1..5] sem inventar valores."""
    df = _make_sample_df([
        {"score": 1},
        {"score": "5"},
        {"score": 4.0},
        {"score": 0},
        {"score": 6},
        {"score": 3.5},
        {"score": "ótimo"},
        {"score": None},
        {"score": True},
    ])

    valid_df, rejected_df, cast_indices, cast_ops = validate_and_clean_scores(df)
    assert list(valid_df["score"]) == [1, 5, 4]
    assert str(valid_df["score"].dtype) == "int64"
    assert cast_ops == 2
    assert cast_indices == {1, 2}
    assert len(rejected_df) == 6
    assert set(rejected_df["rejection_reason"]) == {"invalid_score"}


def test_clean_dates_preserves_missing_replied_at_and_tracks_at_normalization():
    """
    Valida datas 'at' e 'repliedAt' sem descartar avaliações sem resposta,
    e rastreia registros cujo formato de 'at' foi efetivamente alterado.
    """
    df = _make_sample_df([
        {"at": "2026-11-18 16:49:36", "repliedAt": None},
        {"at": "2026-11-18T15:00:00", "repliedAt": "2026-11-18 18:30:00"},
        {"at": "2026-11-18", "repliedAt": "data-invalida"},
        {"at": "data-invalida", "repliedAt": None},
        {"at": "1999-01-01 00:00:00", "repliedAt": None},
    ])

    valid_df, rejected_df, stats = clean_dates(df)
    assert len(valid_df) == 3
    assert len(rejected_df) == 2
    assert stats["invalid_at_rejected"] == 2
    assert stats["at_normalized"] == 2
    assert stats["at_normalized_indices"] == {1, 2}
    assert stats["replied_at_sanitized"] == 1
    assert stats["replied_at_sanitized_indices"] == {2}
    assert valid_df.iloc[0]["at"] == "2026-11-18 16:49:36"
    assert valid_df.iloc[1]["at"] == "2026-11-18 15:00:00"
    assert valid_df.iloc[2]["at"] == "2026-11-18 00:00:00"
    assert pd.isna(valid_df.iloc[0]["repliedAt"])
    assert valid_df.iloc[1]["repliedAt"] == "2026-11-18 18:30:00"
    assert pd.isna(valid_df.iloc[2]["repliedAt"])


def test_thumbs_up_preserves_missing_and_quarantines_invalid():
    """
    - Valores ausentes em thumbsUpCount permanecem desconhecidos (<NA>), nunca 0.
    - Valores negativos ou não numéricos vão para quarentena com justificativa.
    - Inspeção inicial diferencia ausentes de inválidos.
    """
    df = _make_sample_df([
        {"thumbsUpCount": 12},
        {"thumbsUpCount": None},
        {"thumbsUpCount": ""},
        {"thumbsUpCount": "7"},
        {"thumbsUpCount": -3},
        {"thumbsUpCount": "invalido"},
        {"thumbsUpCount": 2.5},
    ])

    profile = inspect_raw_data(df.drop(columns=["raw_row_index"]))
    assert profile["missing_thumbs_up_count"] == 2
    assert profile["invalid_thumbs_up_count"] == 3

    valid_df, rejected_df, stats = validate_and_clean_thumbs_up(df)
    assert len(valid_df) == 4
    assert len(rejected_df) == 3
    assert stats["missing_preserved"] == 2
    assert stats["invalid_rejected"] == 3
    assert valid_df.iloc[0]["thumbsUpCount"] == 12
    assert pd.isna(valid_df.iloc[1]["thumbsUpCount"])
    assert pd.isna(valid_df.iloc[2]["thumbsUpCount"])
    assert valid_df.iloc[3]["thumbsUpCount"] == 7
    assert set(rejected_df["rejection_reason"]) == {"invalid_thumbs_up_count"}


def test_correction_counts_include_at_normalization_and_metadata(tmp_path: Path):
    """
    - Diferencia registros únicos modificados de total de operações de correção.
    - Inclui alterações de formato da data 'at' e nos metadados opcionais
      nos relatórios JSON e Markdown.
    """
    raw_path = tmp_path / "raw.csv"
    report_json_path = tmp_path / "rep.json"
    report_md_path = tmp_path / "rep.md"

    df = _make_sample_df([
        # Linha 0: 4 correções na MESMA linha (content + at + appVersion + replyContent) -> 1 registro único, 4 operações
        {
            "reviewId": "r0",
            "content": "  Ótimo   app!  ",
            "score": 5,
            "at": "2026-11-18T16:00:00",
            "appVersion": " 10.148.0 ",
            "reviewCreatedVersion": "10.148.0",
            "replyContent": "  Obrigado!  ",
        },
        # Linha 1: 2 correções (at sem hora + reviewCreatedVersion) -> 1 registro único, 2 operações
        {
            "reviewId": "r1",
            "content": "Entrega rápida.",
            "score": 4,
            "at": "2026-11-18",
            "appVersion": "10.148.0",
            "reviewCreatedVersion": " 10.147.0 ",
            "replyContent": None,
        },
        # Linha 2: nenhuma alteração -> 0 operações
        {
            "reviewId": "r2",
            "content": "Sem problemas.",
            "score": 5,
            "at": "2026-11-18 16:00:00",
            "appVersion": "10.148.0",
            "reviewCreatedVersion": "10.148.0",
            "replyContent": None,
        },
    ]).drop(columns=["raw_row_index"])
    df.to_csv(raw_path, index=False)

    _, _, report = run_data_cleaning(
        raw_path=raw_path,
        processed_path=tmp_path / "proc.csv",
        rejected_path=tmp_path / "rej.csv",
        report_json_path=report_json_path,
        report_md_path=report_md_path,
    )

    summary = report["summary"]
    breakdown = summary["corrections_breakdown"]
    assert summary["unique_records_modified"] == 2
    assert summary["total_correction_operations"] == 6
    assert breakdown["texts_normalized_operations"] == 1
    assert breakdown["at_normalized_operations"] == 2
    assert breakdown["at_normalized_records"] == 2
    assert breakdown["metadata_normalized_operations"] == 3
    assert breakdown["metadata_normalized_records"] == 2

    md_content = report_md_path.read_text(encoding="utf-8")
    assert "| Normalização de formato de data (`at`) | 2 | 2 |" in md_content
    assert "| **Registros únicos modificados/corrigidos** | **2** |" in md_content
    assert "| **Total de operações de correção aplicadas** | **6** |" in md_content


def test_extra_columns_are_preserved_and_signaled_explicitly(tmp_path: Path):
    """
    - Colunas adicionais não previstas são detectadas, sinalizadas no relatório/aviso
      e preservadas por padrão (ou removidas explicitamente quando preserve_extra_columns=False).
    """
    raw_path = tmp_path / "raw_extra.csv"
    df = _make_sample_df(
        [{"reviewId": "r1", "content": "Excelente!", "score": 5, "userName": "Maria", "sourceTag": "android"}],
        extra_cols={"userName": "Maria", "sourceTag": "android"},
    ).drop(columns=["raw_row_index"])
    df.to_csv(raw_path, index=False)

    with pytest.warns(UserWarning, match="Colunas adicionais não previstas"):
        clean_preserved, _, rep_preserved = run_data_cleaning(
            raw_path=raw_path,
            processed_path=tmp_path / "proc1.csv",
            rejected_path=tmp_path / "rej1.csv",
            report_json_path=tmp_path / "rep1.json",
            report_md_path=tmp_path / "rep1.md",
            preserve_extra_columns=True,
        )

    assert "userName" in clean_preserved.columns
    assert "sourceTag" in clean_preserved.columns
    assert rep_preserved["schema_check"]["extra_columns_detected"] == ["userName", "sourceTag"]
    assert rep_preserved["schema_check"]["extra_columns_preserved"] is True

    with pytest.warns(UserWarning, match="removidas explicitamente"):
        clean_dropped, _, rep_dropped = run_data_cleaning(
            raw_path=raw_path,
            processed_path=tmp_path / "proc2.csv",
            rejected_path=tmp_path / "rej2.csv",
            report_json_path=tmp_path / "rep2.json",
            report_md_path=tmp_path / "rep2.md",
            preserve_extra_columns=False,
        )

    assert list(clean_dropped.columns) == EXPECTED_COLUMNS
    assert rep_dropped["schema_check"]["extra_columns_preserved"] is False
    assert any("removidas explicitamente" in a for a in rep_dropped["actions_taken"])


def test_desktop_api_compatibility_with_processed_csv(tmp_path: Path, monkeypatch):
    """
    - Verifica compatibilidade direta do CSV gerado pelo Data Cleaning + add_sentiment_column
      com os métodos obter_indicadores() e obter_avaliacoes_por_dia() de src/desktop/api.py.
    """
    monkeypatch.chdir(tmp_path)
    raw_path = tmp_path / "data" / "raw" / "apps_reviews.csv"
    processed_path = tmp_path / "data" / "processed" / "apps_reviews.csv"
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(desktop_api_module, "ARQUIVO_AVALIACOES", processed_path)

    df = _make_sample_df([
        {"app_id": "br.com.brainweb.ifood", "reviewId": "i1", "content": "Ótimo!", "score": 5, "at": "2026-11-18 10:00:00", "thumbsUpCount": None},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "i2", "content": "Bom.", "score": 4, "at": "2026-11-18 14:00:00", "thumbsUpCount": 2},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "i3", "content": "Mais ou menos.", "score": 3, "at": "2026-11-19 09:00:00", "thumbsUpCount": 0},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "i4", "content": "Péssimo!", "score": 1, "at": "2026-11-19 11:00:00", "thumbsUpCount": None},
        {"app_id": "com.grability.rappi", "reviewId": "r1", "content": "Rappi rápido.", "score": 5, "at": "2026-11-19 12:00:00", "thumbsUpCount": 1},
    ]).drop(columns=["raw_row_index"])
    df.to_csv(raw_path, index=False)

    df_clean, _, _ = run_data_cleaning(
        raw_path=raw_path,
        processed_path=processed_path,
        rejected_path=tmp_path / "data" / "processed" / "apps_reviews_rejected.csv",
        report_json_path=tmp_path / "data" / "processed" / "data_quality_report.json",
        report_md_path=tmp_path / "data" / "processed" / "data_quality_report.md",
    )
    df_final = add_sentiment_column(df_clean)
    save_reviews_csv(df_final, str(processed_path))

    api = Api()
    indicadores = api.obter_indicadores("br.com.brainweb.ifood")
    assert indicadores["total_avaliacoes"] == 4
    assert indicadores["total_positivas"] == 2
    assert indicadores["total_negativas"] == 1
    assert indicadores["total_neutras"] == 1
    assert indicadores["nota_media"] == pytest.approx(3.25)

    por_dia = api.obter_avaliacoes_por_dia("br.com.brainweb.ifood")
    assert por_dia == {
        "data": ["2026-11-18", "2026-11-19"],
        "quantidade": [2, 2],
    }


def test_end_to_end_cleaning_preserves_raw_file_and_trains_model(tmp_path: Path, monkeypatch):
    """
    Testa o fluxo ponta a ponta:
    - Integridade (SHA-256) de data/raw/apps_reviews.csv preservada.
    - Geração de processed, rejected e relatórios de qualidade.
    - Compatibilidade com add_sentiment_column() e train_model().
    """
    monkeypatch.chdir(tmp_path)
    raw_path = tmp_path / "data" / "raw" / "apps_reviews.csv"
    processed_path = tmp_path / "data" / "processed" / "apps_reviews.csv"
    rejected_path = tmp_path / "data" / "processed" / "apps_reviews_rejected.csv"
    report_json = tmp_path / "data" / "processed" / "data_quality_report.json"
    report_md = tmp_path / "data" / "processed" / "data_quality_report.md"
    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    raw_df = _make_sample_df([
        {"app_id": "br.com.brainweb.ifood", "reviewId": "r1", "content": "Excelente aplicativo 👨‍👩‍👧‍👦, entrega muito rápida!", "score": 5, "thumbsUpCount": None},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "r2", "content": "Pedido veio errado e não tive suporte.", "score": 1, "thumbsUpCount": 3},
        {"app_id": "com.grability.rappi", "reviewId": "r3", "content": "Adorei o cupom de desconto, chegou quentinho!", "score": 5, "thumbsUpCount": 0},
        {"app_id": "com.grability.rappi", "reviewId": "r4", "content": "App travando muito, péssima experiência.", "score": 1, "thumbsUpCount": 1},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "r5", "content": "Ótimo serviço e entregador educado.", "score": 4, "thumbsUpCount": None},
        {"app_id": "com.grability.rappi", "reviewId": "r6", "content": "Cobrança indevida no cartão, horrível.", "score": 2, "thumbsUpCount": 0},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "r7", "content": "   ", "score": 5, "thumbsUpCount": 0},
        {"app_id": "br.com.brainweb.ifood", "reviewId": "r1", "content": "Excelente aplicativo 👨‍👩‍👧‍👦, entrega muito rápida!", "score": 5, "thumbsUpCount": None},
        {"app_id": "com.grability.rappi", "reviewId": "r8", "content": "Nota inválida", "score": 99, "thumbsUpCount": 0},
        {"app_id": "com.grability.rappi", "reviewId": "r9", "content": "Likes negativos", "score": 3, "thumbsUpCount": -5},
    ]).drop(columns=["raw_row_index"])

    raw_df.to_csv(raw_path, index=False)
    sha_before = compute_file_sha256(raw_path)

    df_clean, df_rejected, report = run_data_cleaning(
        raw_path=raw_path,
        processed_path=processed_path,
        rejected_path=rejected_path,
        report_json_path=report_json,
        report_md_path=report_md,
    )

    sha_after = compute_file_sha256(raw_path)
    assert sha_before == sha_after, "O arquivo raw original não deve ser modificado!"
    assert len(df_clean) == 6
    assert "👨‍👩‍👧‍👦" in df_clean.loc[0, "content"]
    assert len(df_rejected) == 4
    assert report["summary"]["initial_records"] == 10
    assert report["summary"]["final_records"] == 6
    assert report["summary"]["rejection_breakdown"]["invalid_thumbs_up_count"] == 1
    assert report_json.exists()
    assert report_md.exists()

    df_with_sentiment = add_sentiment_column(df_clean)
    save_reviews_csv(df_with_sentiment, str(processed_path))
    train_model(str(processed_path))

    assert (tmp_path / "artifacts" / "sentiment_model.pkl").exists()
    assert (tmp_path / "artifacts" / "tfidf_vectorizer.pkl").exists()


def test_guardrails_missing_schema_and_same_path_overwrite(tmp_path: Path):
    """Garante que esquemas inválidos e tentativa de sobrescrever raw lancem ValueError."""
    bad_raw = tmp_path / "bad_raw.csv"
    pd.DataFrame({"app_id": ["a"], "content": ["ok"]}).to_csv(bad_raw, index=False)

    with pytest.raises(ValueError, match="Esquema inválido"):
        run_data_cleaning(
            raw_path=bad_raw,
            processed_path=tmp_path / "proc.csv",
            rejected_path=tmp_path / "rej.csv",
            report_json_path=tmp_path / "rep.json",
            report_md_path=tmp_path / "rep.md",
        )

    with pytest.raises(ValueError, match="não pode ser igual ao arquivo bruto"):
        run_data_cleaning(
            raw_path=bad_raw,
            processed_path=bad_raw,
            rejected_path=tmp_path / "rej.csv",
            report_json_path=tmp_path / "rep.json",
            report_md_path=tmp_path / "rep.md",
        )