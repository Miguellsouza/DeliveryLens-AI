"""
Módulo de Data Cleaning e Auditoria de Qualidade de Dados do DeliveryLens-AI.

Responsável por:
- Investigar e perfilar a base bruta (schema, colunas extras, tipos, ausentes, duplicatas, anomalias).
- Tratar duplicatas compostas por (app_id, reviewId) comparando cada registro descartado
  diretamente com o registro vencedor escolhido para permanecer.
- Normalizar textos em português (Unicode NFC, espaços) preservando acentos, pontuação,
  emojis simples ou compostos via U+200D (ZWJ) e caixa original.
- Separar registros inválidos ou sem texto para quarentena rastreável sem sobrescrever data/raw.
- Validar notas (score inteiro 1..5), datas (at, repliedAt) e thumbsUpCount (preservando valores
  ausentes como desconhecidos <NA> e enviando valores negativos ou não numéricos para quarentena).
- Diferenciar registros únicos modificados de operações de correção (incluindo normalização de
  formato da data 'at' e metadados opcionais).
- Gerar relatórios detalhados de qualidade (JSON e Markdown) antes e depois da limpeza.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any
import warnings

import numpy as np
import pandas as pd

EXPECTED_COLUMNS: list[str] = [
    "app_id",
    "reviewId",
    "content",
    "score",
    "at",
    "appVersion",
    "reviewCreatedVersion",
    "thumbsUpCount",
    "replyContent",
    "repliedAt",
]

OPTIONAL_METADATA_COLUMNS: list[str] = [
    "appVersion",
    "reviewCreatedVersion",
    "replyContent",
    "repliedAt",
]

REJECTION_COLUMNS: list[str] = [
    "raw_row_index",
    "rejection_reason",
    "rejection_details",
]

MIN_VALID_DATE = pd.Timestamp("2008-01-01 00:00:00")
# Preserva U+200D (Zero Width Joiner) necessário para emojis compostos (ex.: 👨‍👩‍👧‍👦, 👩‍💻)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200c\ufeff]")
_MULTI_SPACE_RE = re.compile(r"\s+")


def compute_file_sha256(file_path: str | Path) -> str:
    """Calcula o hash SHA-256 de um arquivo em blocos para baixo consumo de memória."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def validate_schema(df: pd.DataFrame, required_columns: list[str] | None = None) -> list[str]:
    """
    Valida se todas as colunas obrigatórias existem no DataFrame.
    Retorna a lista de colunas adicionais não previstas no esquema padrão.
    """
    expected = required_columns or EXPECTED_COLUMNS
    missing = [col for col in expected if col not in df.columns]
    if missing:
        raise ValueError(f"Esquema inválido: colunas obrigatórias ausentes: {missing}")
    return [col for col in df.columns if col not in expected]


def _normalize_single_text(value: Any) -> Any:
    """
    Normaliza uma string preservando acentos do português, pontuação, emojis (incluindo
    sequências compostas com U+200D) e caixa original.

    Operações aplicadas:
    - Normalização Unicode NFC (evita caracteres acentuados decompostos).
    - Remoção de caracteres de controle invisíveis, zero-width space (U+200B),
      zero-width non-joiner (U+200C) e BOM (U+FEFF), mantendo U+200D intacto.
    - Colapso de múltiplos espaços/quebras de linha consecutivos em um único espaço.
    - Remoção de espaços extras no início e no final.
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return np.nan
    if pd.isna(value):
        return np.nan

    text = str(value)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text).strip()
    return text


def _parse_datetime_series(series: pd.Series) -> pd.Series:
    """Converte uma Series de datas heterogêneas para datetime64[ns] sem fuso horário."""
    try:
        parsed = pd.to_datetime(series, format="mixed", errors="coerce")
    except (TypeError, ValueError):
        parsed = pd.to_datetime(series, errors="coerce")

    if parsed.dtype == "object":
        normalized_items = [
            pd.Timestamp(v).tz_convert("UTC").tz_localize(None)
            if getattr(v, "tzinfo", None) is not None
            else v
            for v in parsed
        ]
        parsed = pd.Series(pd.to_datetime(normalized_items, errors="coerce"), index=series.index)
    elif isinstance(parsed.dtype, pd.DatetimeTZDtype):
        parsed = parsed.dt.tz_convert("UTC").dt.tz_localize(None)

    return parsed


def _parse_valid_int_score(value: Any) -> int | None:
    """
    Converte o score para inteiro entre 1 e 5 sem inventar ou arredondar valores inválidos.
    Retorna None se o valor for ausente, booleano, não numérico, decimal fracionário ou fora de [1, 5].
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None

    if isinstance(value, (int, np.integer)):
        int_val = int(value)
        return int_val if 1 <= int_val <= 5 else None

    if isinstance(value, (float, np.floating)):
        if np.isnan(value) or np.isinf(value) or not float(value).is_integer():
            return None
        int_val = int(value)
        return int_val if 1 <= int_val <= 5 else None

    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned.lower() in {"true", "false", "nan", "none", "null"}:
            return None
        try:
            num = float(cleaned)
        except ValueError:
            return None
        if np.isnan(num) or np.isinf(num) or not num.is_integer():
            return None
        int_val = int(num)
        return int_val if 1 <= int_val <= 5 else None

    return None


def _classify_thumbs_up_value(value: Any) -> tuple[str, Any]:
    """
    Classifica um valor de 'thumbsUpCount' diferenciando ausente de inválido.

    Retornos possíveis:
    - ("missing", pd.NA): valor desconhecido/ausente (None, NaN, string vazia).
    - ("valid", int_val): inteiro válido >= 0.
    - ("invalid", raw_value): valor negativo, booleano, fracionário ou não numérico.
    """
    if value is None:
        return "missing", pd.NA
    if isinstance(value, bool):
        return "invalid", value
    try:
        if pd.isna(value):
            return "missing", pd.NA
    except (TypeError, ValueError):
        return "invalid", value

    if isinstance(value, (int, np.integer)):
        int_val = int(value)
        return ("valid", int_val) if int_val >= 0 else ("invalid", value)

    if isinstance(value, (float, np.floating)):
        if np.isnan(value):
            return "missing", pd.NA
        if np.isinf(value) or not float(value).is_integer():
            return "invalid", value
        int_val = int(value)
        return ("valid", int_val) if int_val >= 0 else ("invalid", value)

    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return "missing", pd.NA
        if cleaned.lower() in {"true", "false", "nan", "none", "null"}:
            return "invalid", value
        try:
            num = float(cleaned)
        except ValueError:
            return "invalid", value
        if np.isnan(num) or np.isinf(num) or not num.is_integer():
            return "invalid", value
        int_val = int(num)
        return ("valid", int_val) if int_val >= 0 else ("invalid", value)

    return "invalid", value


def inspect_raw_data(df: pd.DataFrame) -> dict[str, Any]:
    """
    Realiza investigação inicial não destrutiva da base de dados e retorna métricas de qualidade.
    Diferencia explicitamente valores ausentes de valores inválidos e identifica colunas extras.
    """
    extra_columns = validate_schema(df)

    total_records = int(len(df))
    total_columns = int(len(df.columns))
    dtypes = {col: str(dtype) for col, dtype in df.dtypes.items()}

    missing_by_column: dict[str, int] = {}
    missing_pct_by_column: dict[str, float] = {}
    for col in df.columns:
        miss_count = int(df[col].isna().sum())
        missing_by_column[col] = miss_count
        missing_pct_by_column[col] = round((miss_count / total_records * 100.0), 2) if total_records > 0 else 0.0

    # Duplicatas baseadas em (app_id, reviewId) quando reviewId está presente
    valid_id_mask = df["reviewId"].notna() & (df["reviewId"].astype(str).str.strip() != "")
    dup_mask = valid_id_mask & df.duplicated(subset=["app_id", "reviewId"], keep="first")
    duplicate_count = int(dup_mask.sum())

    # Distribuições
    app_series = df["app_id"].fillna("<AUSENTE>").astype(str)
    app_distribution = {str(k): int(v) for k, v in app_series.value_counts().items()}

    score_series = df["score"].fillna("<AUSENTE>").astype(str)
    score_distribution = {str(k): int(v) for k, v in score_series.value_counts().items()}

    # Textos vazios ou compostos apenas por espaços
    empty_content_mask = df["content"].isna() | (df["content"].astype(str).str.strip() == "")
    empty_content_count = int(empty_content_mask.sum())

    # Datas inválidas
    parsed_at = _parse_datetime_series(df["at"])
    invalid_at_mask = parsed_at.isna() | (parsed_at < MIN_VALID_DATE)
    invalid_at_count = int(invalid_at_mask.sum())

    replied_non_null = df["repliedAt"].notna() & (df["repliedAt"].astype(str).str.strip() != "")
    parsed_replied_at = _parse_datetime_series(df.loc[replied_non_null, "repliedAt"])
    invalid_replied_at_count = int(parsed_replied_at.isna().sum())

    # Notas inválidas ou fora do intervalo [1, 5]
    parsed_scores = df["score"].map(_parse_valid_int_score)
    out_of_range_score_count = int(parsed_scores.isna().sum())

    # thumbsUpCount: diferencia ausentes de valores inválidos (negativos / não numéricos)
    thumbs_statuses = [status for status, _ in df["thumbsUpCount"].map(_classify_thumbs_up_value)]
    missing_thumbs_up_count = int(sum(1 for st in thumbs_statuses if st == "missing"))
    invalid_thumbs_up_count = int(sum(1 for st in thumbs_statuses if st == "invalid"))

    return {
        "total_records": total_records,
        "total_columns": total_columns,
        "columns": list(df.columns),
        "extra_columns": extra_columns,
        "dtypes": dtypes,
        "missing_by_column": missing_by_column,
        "missing_pct_by_column": missing_pct_by_column,
        "duplicate_count": duplicate_count,
        "app_distribution": app_distribution,
        "score_distribution": score_distribution,
        "empty_content_count": empty_content_count,
        "invalid_at_count": invalid_at_count,
        "invalid_replied_at_count": invalid_replied_at_count,
        "out_of_range_score_count": out_of_range_score_count,
        "missing_thumbs_up_count": missing_thumbs_up_count,
        "invalid_thumbs_up_count": invalid_thumbs_up_count,
    }


def _build_rejected_rows(
    df_subset: pd.DataFrame,
    reason: str,
    details: str | pd.Series,
    data_columns: list[str] | None = None,
) -> pd.DataFrame:
    """Monta DataFrame padronizado de registros rejeitados preservando rastreabilidade."""
    cols = data_columns or [c for c in df_subset.columns if c not in REJECTION_COLUMNS]
    if df_subset.empty:
        return pd.DataFrame(columns=cols + REJECTION_COLUMNS)

    rejected = df_subset[cols + ["raw_row_index"]].copy()
    rejected["rejection_reason"] = reason
    rejected["rejection_details"] = details.values if isinstance(details, pd.Series) else details
    return rejected[cols + REJECTION_COLUMNS]


def validate_identifiers(
    df: pd.DataFrame,
    data_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separa registros sem app_id ou reviewId válidos."""
    cols = data_columns or [c for c in df.columns if c not in REJECTION_COLUMNS]
    app_missing = df["app_id"].isna() | (df["app_id"].astype(str).str.strip() == "")
    rev_missing = df["reviewId"].isna() | (df["reviewId"].astype(str).str.strip() == "")
    invalid_mask = app_missing | rev_missing

    if not invalid_mask.any():
        return df, pd.DataFrame(columns=cols + REJECTION_COLUMNS)

    details = np.where(
        app_missing[invalid_mask] & rev_missing[invalid_mask],
        "app_id e reviewId ausentes",
        np.where(app_missing[invalid_mask], "app_id ausente", "reviewId ausente"),
    )
    rejected = _build_rejected_rows(
        df.loc[invalid_mask],
        reason="missing_required_identifier",
        details=pd.Series(details, index=df.index[invalid_mask]),
        data_columns=cols,
    )
    valid_df = df.loc[~invalid_mask].copy()
    valid_df["app_id"] = valid_df["app_id"].astype(str).str.strip()
    valid_df["reviewId"] = valid_df["reviewId"].astype(str).str.strip()
    return valid_df, rejected


def _normalize_cell_for_comparison(value: Any) -> str:
    """Normaliza valor escalar para comparação fiel entre registro descartado e registro vencedor."""
    if value is None or isinstance(value, bool):
        return f"BOOL:{value}" if isinstance(value, bool) else "<NA>"
    try:
        if pd.isna(value):
            return "<NA>"
    except (TypeError, ValueError):
        pass

    if isinstance(value, (int, np.integer)):
        return f"NUM:{int(value)}"
    if isinstance(value, (float, np.floating)):
        if np.isnan(value):
            return "<NA>"
        return f"NUM:{int(value)}" if float(value).is_integer() else f"NUM:{float(value)}"

    text = unicodedata.normalize("NFC", str(value)).strip()
    if text == "":
        return "<NA>"
    try:
        num = float(text)
        if not np.isnan(num) and not np.isinf(num):
            return f"NUM:{int(num)}" if num.is_integer() else f"NUM:{num}"
    except ValueError:
        pass
    return f"STR:{text}"


def deduplicate_reviews(
    df: pd.DataFrame,
    data_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """
    Identifica e trata duplicatas considerando a chave composta (app_id, reviewId).

    Estratégia quando há registros com mesmo (app_id, reviewId):
    1. Seleciona o registro vencedor priorizando: score válido + texto não vazio,
       data 'at' mais recente, presença de 'replyContent' e maior completude de campos.
    2. Compara CADA registro descartado diretamente com o registro vencedor do seu grupo
       (evitando classificações incorretas em grupos com 3 ou mais ocorrências).
    """
    cols = data_columns or [c for c in df.columns if c not in REJECTION_COLUMNS]
    stats = {
        "duplicates_detected": 0,
        "exact_duplicates_removed": 0,
        "conflicting_duplicates_resolved": 0,
    }

    if df.empty:
        return df, pd.DataFrame(columns=cols + REJECTION_COLUMNS), stats

    dup_group_mask = df.duplicated(subset=["app_id", "reviewId"], keep=False)
    if not dup_group_mask.any():
        return df, pd.DataFrame(columns=cols + REJECTION_COLUMNS), stats

    dup_subset = df.loc[dup_group_mask].copy()

    has_valid_score = dup_subset["score"].map(_parse_valid_int_score).notna().astype(np.int8)
    has_non_empty_text = (
        dup_subset["content"].notna() & (dup_subset["content"].astype(str).str.strip() != "")
    ).astype(np.int8)
    parsed_at = _parse_datetime_series(dup_subset["at"])
    at_epoch = parsed_at.fillna(pd.Timestamp("1970-01-01")).astype("int64")
    has_reply = (
        dup_subset["replyContent"].notna() & (dup_subset["replyContent"].astype(str).str.strip() != "")
    ).astype(np.int8)
    completeness = dup_subset[cols].notna().sum(axis=1).astype(np.int8)

    dup_subset["_p_valid"] = has_valid_score + has_non_empty_text
    dup_subset["_p_at"] = at_epoch
    dup_subset["_p_reply"] = has_reply
    dup_subset["_p_comp"] = completeness

    dup_sorted = dup_subset.sort_values(
        by=["app_id", "reviewId", "_p_valid", "_p_at", "_p_reply", "_p_comp", "raw_row_index"],
        ascending=[True, True, False, False, False, False, True],
    )

    winners_df = dup_sorted.drop_duplicates(subset=["app_id", "reviewId"], keep="first")
    best_indices = set(winners_df.index)
    discarded_indices = [idx for idx in dup_subset.index if idx not in best_indices]

    compare_cols = [c for c in cols if c not in ("app_id", "reviewId")]

    winner_signatures: dict[tuple[str, str], dict[str, str]] = {}
    for _, w_row in winners_df.iterrows():
        key = (str(w_row["app_id"]), str(w_row["reviewId"]))
        winner_signatures[key] = {
            col: _normalize_cell_for_comparison(w_row[col]) for col in compare_cols
        }

    discarded_df = df.loc[discarded_indices].copy()
    exact_flags: list[bool] = []
    detail_messages: list[str] = []

    for _, d_row in discarded_df.iterrows():
        key = (str(d_row["app_id"]), str(d_row["reviewId"]))
        winner_sig = winner_signatures[key]
        diff_cols = [
            col
            for col in compare_cols
            if _normalize_cell_for_comparison(d_row[col]) != winner_sig[col]
        ]
        if not diff_cols:
            exact_flags.append(True)
            detail_messages.append(
                "Registro duplicado exato em relação ao registro preservado para (app_id, reviewId)"
            )
        else:
            exact_flags.append(False)
            detail_messages.append(
                "Registro duplicado conflitante com o registro preservado para (app_id, reviewId); "
                f"campos divergentes: {', '.join(diff_cols)}"
            )

    exact_count = int(sum(1 for flag in exact_flags if flag))
    conflict_count = int(sum(1 for flag in exact_flags if not flag))

    stats["duplicates_detected"] = len(discarded_indices)
    stats["exact_duplicates_removed"] = exact_count
    stats["conflicting_duplicates_resolved"] = conflict_count

    rejected = _build_rejected_rows(
        discarded_df,
        reason="duplicate_review",
        details=pd.Series(detail_messages, index=discarded_df.index),
        data_columns=cols,
    )

    keep_mask = ~df.index.isin(discarded_indices)
    dedup_df = df.loc[keep_mask].copy()
    return dedup_df, rejected, stats


def clean_text_series(df: pd.DataFrame) -> tuple[pd.DataFrame, set[int], int]:
    """
    Normaliza o campo 'content' sem destruir acentos, emojis (inclusive com U+200D),
    pontuação ou maiúsculas/minúsculas.
    Retorna (df_atualizado, conjunto_raw_row_index_modificados, total_operacoes).
    """
    if df.empty:
        return df, set(), 0

    df = df.copy()
    original_non_null = df["content"].notna()
    original_values = df.loc[original_non_null, "content"].astype(str)
    cleaned_values = original_values.map(_normalize_single_text)

    changed_mask = original_values != cleaned_values
    modified_indices = set(df.loc[original_non_null].loc[changed_mask, "raw_row_index"].astype(int).tolist())
    operations_count = int(changed_mask.sum())

    df.loc[original_non_null, "content"] = cleaned_values
    return df, modified_indices, operations_count


def separate_empty_texts(
    df: pd.DataFrame,
    data_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Identifica avaliações sem conteúdo textual (NaN ou string vazia após normalização)
    e as separa do conjunto destinado à classificação de sentimentos.
    """
    cols = data_columns or [c for c in df.columns if c not in REJECTION_COLUMNS]
    if df.empty:
        return df, pd.DataFrame(columns=cols + REJECTION_COLUMNS)

    empty_mask = df["content"].isna() | (df["content"].astype(str).str.strip() == "")
    if not empty_mask.any():
        return df, pd.DataFrame(columns=cols + REJECTION_COLUMNS)

    rejected = _build_rejected_rows(
        df.loc[empty_mask],
        reason="missing_or_empty_content",
        details="Avaliação sem conteúdo textual válido após normalização de espaços",
        data_columns=cols,
    )
    return df.loc[~empty_mask].copy(), rejected


def validate_and_clean_scores(
    df: pd.DataFrame,
    data_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, set[int], int]:
    """
    Valida que 'score' seja inteiro entre 1 e 5 sem inventar notas.
    Registros com notas ausentes, inválidas ou fora de [1, 5] são separados.
    Retorna (df_valido, df_rejeitado, conjunto_raw_row_index_convertidos, total_operacoes).
    """
    cols = data_columns or [c for c in df.columns if c not in REJECTION_COLUMNS]
    if df.empty:
        return df, pd.DataFrame(columns=cols + REJECTION_COLUMNS), set(), 0

    df = df.copy()
    parsed = df["score"].map(_parse_valid_int_score)
    invalid_mask = parsed.isna()

    rejected = pd.DataFrame(columns=cols + REJECTION_COLUMNS)
    if invalid_mask.any():
        raw_bad = df.loc[invalid_mask, "score"].fillna("<AUSENTE>").astype(str)
        details = "Nota inválida ou fora do intervalo permitido [1..5]: " + raw_bad
        rejected = _build_rejected_rows(
            df.loc[invalid_mask],
            reason="invalid_score",
            details=details,
            data_columns=cols,
        )

    valid_df = df.loc[~invalid_mask].copy()
    valid_parsed = parsed.loc[~invalid_mask].astype("int64")

    cast_indices: set[int] = set()
    for row_idx, orig, clean in zip(valid_df["raw_row_index"], valid_df["score"], valid_parsed):
        if isinstance(orig, (str, float, np.floating)) or str(orig) != str(clean):
            cast_indices.add(int(row_idx))

    valid_df["score"] = valid_parsed
    return valid_df, rejected, cast_indices, len(cast_indices)


def clean_dates(
    df: pd.DataFrame,
    data_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """
    Valida e converte os campos de data 'at' e 'repliedAt'.
    - Registros com 'at' ausente ou inválido são separados.
    - Registros com 'at' válido cujo formato textual difere de '%Y-%m-%d %H:%M:%S' são
      contabilizados em 'at_normalized' e 'at_normalized_indices'.
    - Registros com 'repliedAt' ausente são preservados; valores inválidos em 'repliedAt' são anulados
      sem descartar a avaliação.
    """
    cols = data_columns or [c for c in df.columns if c not in REJECTION_COLUMNS]
    stats: dict[str, Any] = {
        "invalid_at_rejected": 0,
        "at_normalized": 0,
        "at_normalized_indices": set(),
        "replied_at_sanitized": 0,
        "replied_at_sanitized_indices": set(),
    }
    if df.empty:
        return df, pd.DataFrame(columns=cols + REJECTION_COLUMNS), stats

    df = df.copy()
    parsed_at = _parse_datetime_series(df["at"])
    invalid_at_mask = parsed_at.isna() | (parsed_at < MIN_VALID_DATE)

    rejected = pd.DataFrame(columns=cols + REJECTION_COLUMNS)
    if invalid_at_mask.any():
        stats["invalid_at_rejected"] = int(invalid_at_mask.sum())
        raw_bad_at = df.loc[invalid_at_mask, "at"].fillna("<AUSENTE>").astype(str)
        details = "Data de avaliação ('at') ausente ou inválida: " + raw_bad_at
        rejected = _build_rejected_rows(
            df.loc[invalid_at_mask],
            reason="invalid_at_date",
            details=details,
            data_columns=cols,
        )

    valid_df = df.loc[~invalid_at_mask].copy()
    valid_parsed_at = parsed_at.loc[~invalid_at_mask]
    formatted_at = valid_parsed_at.dt.strftime("%Y-%m-%d %H:%M:%S")

    orig_at_str = valid_df["at"].astype(str)
    at_changed_mask = orig_at_str != formatted_at
    stats["at_normalized"] = int(at_changed_mask.sum())
    stats["at_normalized_indices"] = set(
        valid_df.loc[at_changed_mask, "raw_row_index"].astype(int).tolist()
    )

    valid_df["at"] = formatted_at

    replied_present = valid_df["repliedAt"].notna() & (valid_df["repliedAt"].astype(str).str.strip() != "")
    if replied_present.any():
        parsed_replied = _parse_datetime_series(valid_df.loc[replied_present, "repliedAt"])
        corresponding_at = valid_parsed_at.loc[replied_present]
        bad_replied = parsed_replied.isna() | (parsed_replied < MIN_VALID_DATE) | (parsed_replied < corresponding_at)
        stats["replied_at_sanitized"] = int(bad_replied.sum())
        stats["replied_at_sanitized_indices"] = set(
            valid_df.loc[replied_present].loc[bad_replied, "raw_row_index"].astype(int).tolist()
        )

        formatted_replied = parsed_replied.dt.strftime("%Y-%m-%d %H:%M:%S")
        formatted_replied = formatted_replied.where(~bad_replied, other=np.nan)
        valid_df["repliedAt"] = pd.Series(np.nan, index=valid_df.index, dtype="object")
        valid_df.loc[replied_present, "repliedAt"] = formatted_replied.astype("object").values
    else:
        valid_df["repliedAt"] = pd.Series(np.nan, index=valid_df.index, dtype="object")

    return valid_df, rejected, stats


def validate_and_clean_thumbs_up(
    df: pd.DataFrame,
    data_columns: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """
    Valida 'thumbsUpCount' diferenciando valores desconhecidos/ausentes de valores inválidos:
    - Valores válidos (inteiros >= 0): convertidos para tipo nullable integer ('Int64').
    - Valores ausentes (None, NaN, string vazia): preservados como desconhecidos (<NA>),
      nunca transformados automaticamente em zero.
    - Valores inválidos (negativos, não numéricos, fracionários, booleanos): enviados para
      quarentena com justificativa explícita.
    """
    cols = data_columns or [c for c in df.columns if c not in REJECTION_COLUMNS]
    stats: dict[str, Any] = {
        "missing_preserved": 0,
        "invalid_rejected": 0,
        "type_cast_indices": set(),
        "type_cast_operations": 0,
    }
    if df.empty:
        return df, pd.DataFrame(columns=cols + REJECTION_COLUMNS), stats

    df = df.copy()
    classified = df["thumbsUpCount"].map(_classify_thumbs_up_value)
    statuses = [st for st, _ in classified]
    values = [val for _, val in classified]

    invalid_mask = pd.Series([st == "invalid" for st in statuses], index=df.index)
    missing_mask = pd.Series([st == "missing" for st in statuses], index=df.index)
    stats["missing_preserved"] = int(missing_mask.sum())

    rejected = pd.DataFrame(columns=cols + REJECTION_COLUMNS)
    if invalid_mask.any():
        stats["invalid_rejected"] = int(invalid_mask.sum())
        raw_bad = df.loc[invalid_mask, "thumbsUpCount"].astype(str)
        details = "Valor inválido em 'thumbsUpCount' (esperado inteiro >= 0 ou ausente): " + raw_bad
        rejected = _build_rejected_rows(
            df.loc[invalid_mask],
            reason="invalid_thumbs_up_count",
            details=details,
            data_columns=cols,
        )

    valid_mask = ~invalid_mask
    valid_df = df.loc[valid_mask].copy()
    valid_values = [val for val, keep in zip(values, valid_mask) if keep]

    cast_indices: set[int] = set()
    for row_idx, orig, clean in zip(valid_df["raw_row_index"], valid_df["thumbsUpCount"], valid_values):
        if clean is not pd.NA and isinstance(orig, (str, float, np.floating)):
            cast_indices.add(int(row_idx))

    stats["type_cast_indices"] = cast_indices
    stats["type_cast_operations"] = len(cast_indices)

    valid_df["thumbsUpCount"] = pd.Series(valid_values, index=valid_df.index, dtype="Int64")
    return valid_df, rejected, stats


def clean_optional_metadata(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[int, int]]:
    """
    Normaliza metadados textuais opcionais (appVersion, reviewCreatedVersion, replyContent)
    sem inventar informações para campos naturalmente ausentes.
    Retorna (df_atualizado, mapa_raw_row_index_para_qtd_celulas_modificadas).
    """
    if df.empty:
        return df, {}

    df = df.copy()
    ops_by_row: dict[int, int] = {}

    for col in ("appVersion", "reviewCreatedVersion", "replyContent"):
        df[col] = df[col].astype("object")
        non_null = df[col].notna()
        if not non_null.any():
            continue
        orig = df.loc[non_null, col].astype(str)
        cleaned = orig.map(_normalize_single_text)
        cleaned = cleaned.replace({"": np.nan})

        changed_mask = orig.fillna("<NA>") != cleaned.fillna("<NA>")
        row_ids = df.loc[non_null].loc[changed_mask, "raw_row_index"].astype(int).tolist()
        for r_id in row_ids:
            ops_by_row[r_id] = ops_by_row.get(r_id, 0) + 1

        df.loc[non_null, col] = cleaned

    return df, ops_by_row


def generate_markdown_report(report: dict[str, Any]) -> str:
    """Gera relatório legível em Markdown com métricas antes/depois e rastreabilidade."""
    before = report["before_cleaning"]
    after = report["after_cleaning"]
    summary = report["summary"]
    corr = summary["corrections_breakdown"]

    lines = [
        "# Relatório de Qualidade de Dados — DeliveryLens-AI",
        "",
        f"- **Data/Hora de Execução (UTC):** `{report['generated_at_utc']}`",
        f"- **Arquivo Bruto (Preservado):** `{report['paths']['raw_path']}`",
        f"- **SHA-256 Arquivo Bruto:** `{report['raw_file_sha256']}`",
        f"- **Arquivo Tratado:** `{report['paths']['processed_path']}`",
        f"- **Arquivo de Rejeitados:** `{report['paths']['rejected_path']}`",
        "",
        "## 1. Resumo Executivo da Limpeza",
        "",
        "| Métrica | Quantidade |",
        "| :--- | ---: |",
        f"| Registros iniciais (brutos) | {summary['initial_records']} |",
        f"| Registros válidos finais | {summary['final_records']} |",
        f"| Registros duplicados removidos | {summary['duplicates_removed']} |",
        f"| — Duplicatas exatas | {summary['exact_duplicates_removed']} |",
        f"| — Duplicatas conflitantes resolvidas | {summary['duplicate_conflicts_resolved']} |",
        f"| Total de registros rejeitados/separados | {summary['rejected_records']} |",
        f"| **Registros únicos modificados/corrigidos** | **{summary['unique_records_modified']}** |",
        f"| **Total de operações de correção aplicadas** | **{summary['total_correction_operations']}** |",
        "",
        "### Detalhamento das Operações de Correção (nos Registros Válidos)",
        "",
        "| Tipo de Operação | Operações (Células) | Registros Únicos Afetados |",
        "| :--- | ---: | ---: |",
        f"| Normalização de texto (`content`) | {corr['texts_normalized_operations']} | {corr['texts_normalized_records']} |",
        f"| Conversão de tipo em `score` | {corr['scores_type_cast_operations']} | {corr['scores_type_cast_records']} |",
        f"| Normalização de formato de data (`at`) | {corr['at_normalized_operations']} | {corr['at_normalized_records']} |",
        f"| Saneamento de `repliedAt` inválido | {corr['replied_at_sanitized_operations']} | {corr['replied_at_sanitized_records']} |",
        f"| Conversão de tipo em `thumbsUpCount` | {corr['thumbs_up_type_cast_operations']} | {corr['thumbs_up_type_cast_records']} |",
        f"| Normalização de metadados opcionais (`appVersion`, `reviewCreatedVersion`, `replyContent`) | {corr['metadata_normalized_operations']} | {corr['metadata_normalized_records']} |",
        "",
        "## 2. Detalhamento dos Registros Rejeitados",
        "",
        "| Motivo da Rejeição | Quantidade |",
        "| :--- | ---: |",
    ]

    if summary["rejection_breakdown"]:
        for reason, count in summary["rejection_breakdown"].items():
            lines.append(f"| `{reason}` | {count} |")
    else:
        lines.append("| Nenhum registro rejeitado | 0 |")

    lines.extend([
        "",
        "## 3. Valores Ausentes por Coluna (Antes vs. Depois)",
        "",
        "| Coluna | Tipo Original | Ausentes (Antes) | % (Antes) | Ausentes (Depois) | % (Depois) |",
        "| :--- | :--- | ---: | ---: | ---: | ---: |",
    ])

    for col in before["columns"]:
        b_cnt = before["missing_by_column"].get(col, 0)
        b_pct = before["missing_pct_by_column"].get(col, 0.0)
        a_cnt = after["missing_by_column"].get(col, 0)
        a_pct = after["missing_pct_by_column"].get(col, 0.0)
        dtype = before["dtypes"].get(col, "-")
        lines.append(f"| `{col}` | `{dtype}` | {b_cnt} | {b_pct:.2f}% | {a_cnt} | {a_pct:.2f}% |")

    lines.extend([
        "",
        "## 4. Distribuição por Aplicativo (Antes vs. Depois)",
        "",
        "| Aplicativo (`app_id`) | Antes | Depois |",
        "| :--- | ---: | ---: |",
    ])
    all_apps = sorted(set(before["app_distribution"].keys()) | set(after["app_distribution"].keys()))
    for app in all_apps:
        lines.append(
            f"| `{app}` | {before['app_distribution'].get(app, 0)} | {after['app_distribution'].get(app, 0)} |"
        )

    lines.extend([
        "",
        "## 5. Distribuição das Notas (`score`) (Antes vs. Depois)",
        "",
        "| Nota (`score`) | Antes | Depois |",
        "| :--- | ---: | ---: |",
    ])
    all_scores = sorted(set(before["score_distribution"].keys()) | set(after["score_distribution"].keys()))
    for sc in all_scores:
        lines.append(
            f"| `{sc}` | {before['score_distribution'].get(sc, 0)} | {after['score_distribution'].get(sc, 0)} |"
        )

    lines.extend([
        "",
        "## 6. Problemas Identificados e Ações Realizadas",
        "",
        "### Problemas Identificados",
    ])
    for issue in report["issues_identified"]:
        lines.append(f"- {issue}")

    lines.extend([
        "",
        "### Ações Realizadas",
    ])
    for action in report["actions_taken"]:
        lines.append(f"- {action}")

    return "\n".join(lines) + "\n"


def run_data_cleaning(
    raw_path: str | Path = "data/raw/apps_reviews.csv",
    processed_path: str | Path = "data/processed/apps_reviews.csv",
    rejected_path: str | Path = "data/processed/apps_reviews_rejected.csv",
    report_json_path: str | Path = "data/processed/data_quality_report.json",
    report_md_path: str | Path = "data/processed/data_quality_report.md",
    preserve_extra_columns: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """
    Executa o pipeline completo de Data Cleaning sobre data/raw/apps_reviews.csv
    sem nunca modificar ou sobrescrever o arquivo bruto original.

    Parâmetros:
    - preserve_extra_columns: quando True (padrão), preserva colunas adicionais além do
      esquema padrão e registra aviso explícito; quando False, remove-as com registro no relatório.
    """
    raw_file = Path(raw_path)
    processed_file = Path(processed_path)
    rejected_file = Path(rejected_path)
    report_json_file = Path(report_json_path)
    report_md_file = Path(report_md_path)

    if not raw_file.exists():
        raise FileNotFoundError(f"Arquivo bruto não encontrado: {raw_file}")

    if raw_file.resolve() == processed_file.resolve():
        raise ValueError("Caminho de saída não pode ser igual ao arquivo bruto original.")

    sha_before = compute_file_sha256(raw_file)

    df_raw = pd.read_csv(raw_file, dtype={"app_id": "object", "reviewId": "object", "content": "object"})
    before_profile = inspect_raw_data(df_raw)
    extra_columns = before_profile["extra_columns"]

    if extra_columns:
        if preserve_extra_columns:
            active_columns = EXPECTED_COLUMNS + extra_columns
            warnings.warn(
                f"Colunas adicionais não previstas no esquema padrão detectadas e preservadas: {extra_columns}",
                UserWarning,
                stacklevel=2,
            )
        else:
            active_columns = EXPECTED_COLUMNS
            warnings.warn(
                f"Colunas adicionais não previstas no esquema padrão removidas explicitamente: {extra_columns}",
                UserWarning,
                stacklevel=2,
            )
    else:
        active_columns = EXPECTED_COLUMNS

    df_work = df_raw[active_columns].copy()
    df_work["raw_row_index"] = np.arange(len(df_work), dtype=np.int64)
    del df_raw

    rejected_parts: list[pd.DataFrame] = []

    # 1. Validação de identificadores obrigatórios
    df_work, rej_ids = validate_identifiers(df_work, data_columns=active_columns)
    if not rej_ids.empty:
        rejected_parts.append(rej_ids)

    # 2. Deduplicação composta (app_id, reviewId) comparando cada descarte ao vencedor
    df_work, rej_dups, dup_stats = deduplicate_reviews(df_work, data_columns=active_columns)
    if not rej_dups.empty:
        rejected_parts.append(rej_dups)

    # 3. Normalização de textos (preservando acentos, pontuação, emojis compostos com U+200D e caixa)
    df_work, text_mod_indices, _ = clean_text_series(df_work)

    # 4. Separação de avaliações sem texto válido
    df_work, rej_empty = separate_empty_texts(df_work, data_columns=active_columns)
    if not rej_empty.empty:
        rejected_parts.append(rej_empty)

    # 5. Validação de notas (score inteiro 1..5)
    df_work, rej_scores, score_cast_indices, _ = validate_and_clean_scores(df_work, data_columns=active_columns)
    if not rej_scores.empty:
        rejected_parts.append(rej_scores)

    # 6. Validação e conversão de datas (at e repliedAt)
    df_work, rej_dates, date_stats = clean_dates(df_work, data_columns=active_columns)
    if not rej_dates.empty:
        rejected_parts.append(rej_dates)

    # 7. Validação de thumbsUpCount (preserva ausentes como <NA>; envia inválidos para quarentena)
    df_work, rej_thumbs, thumbs_stats = validate_and_clean_thumbs_up(df_work, data_columns=active_columns)
    if not rej_thumbs.empty:
        rejected_parts.append(rej_thumbs)

    # 8. Normalização não destrutiva de metadados opcionais
    df_work, meta_ops_by_row = clean_optional_metadata(df_work)

    # Consolidação final
    valid_raw_indices = set(df_work["raw_row_index"].astype(int).tolist())
    df_clean = df_work[active_columns].reset_index(drop=True)

    if rejected_parts:
        df_rejected = pd.concat(rejected_parts, ignore_index=True).sort_values("raw_row_index").reset_index(drop=True)
    else:
        df_rejected = pd.DataFrame(columns=active_columns + REJECTION_COLUMNS)

    after_profile = inspect_raw_data(df_clean)

    # Contabiliza correções exclusivamente nos registros válidos finais
    valid_text_mod = text_mod_indices & valid_raw_indices
    valid_score_cast = score_cast_indices & valid_raw_indices
    valid_at_normalized = date_stats["at_normalized_indices"] & valid_raw_indices
    valid_replied_sanitized = date_stats["replied_at_sanitized_indices"] & valid_raw_indices
    valid_thumbs_cast = thumbs_stats["type_cast_indices"] & valid_raw_indices
    valid_meta_mod = {r_idx for r_idx in valid_raw_indices if meta_ops_by_row.get(r_idx, 0) > 0}
    meta_ops_valid = sum(meta_ops_by_row.get(r_idx, 0) for r_idx in valid_raw_indices)

    unique_modified_records = len(
        valid_text_mod
        | valid_score_cast
        | valid_at_normalized
        | valid_replied_sanitized
        | valid_thumbs_cast
        | valid_meta_mod
    )

    total_correction_operations = (
        len(valid_text_mod)
        + len(valid_score_cast)
        + len(valid_at_normalized)
        + len(valid_replied_sanitized)
        + len(valid_thumbs_cast)
        + meta_ops_valid
    )

    rejection_breakdown = (
        {str(k): int(v) for k, v in df_rejected["rejection_reason"].value_counts().items()}
        if not df_rejected.empty
        else {}
    )

    issues_identified: list[str] = []
    if extra_columns:
        issues_identified.append(
            f"Colunas adicionais não previstas no esquema padrão detectadas: {extra_columns}."
        )
    if dup_stats["duplicates_detected"] > 0:
        issues_identified.append(
            f"{dup_stats['duplicates_detected']} registros duplicados em (app_id, reviewId) "
            f"({dup_stats['exact_duplicates_removed']} exatos e {dup_stats['conflicting_duplicates_resolved']} conflitantes)."
        )
    if before_profile["empty_content_count"] > 0:
        issues_identified.append(
            f"{before_profile['empty_content_count']} avaliações com texto ausente, vazio ou composto apenas por espaços."
        )
    if before_profile["out_of_range_score_count"] > 0:
        issues_identified.append(
            f"{before_profile['out_of_range_score_count']} registros com nota ('score') ausente, inválida ou fora de [1, 5]."
        )
    if before_profile["invalid_at_count"] > 0:
        issues_identified.append(
            f"{before_profile['invalid_at_count']} registros com data ('at') ausente ou inválida."
        )
    if before_profile["invalid_replied_at_count"] > 0:
        issues_identified.append(
            f"{before_profile['invalid_replied_at_count']} datas de resposta ('repliedAt') em formato inválido."
        )
    if before_profile["missing_thumbs_up_count"] > 0:
        issues_identified.append(
            f"{before_profile['missing_thumbs_up_count']} registros com 'thumbsUpCount' ausente/desconhecido (preservados como <NA>)."
        )
    if before_profile["invalid_thumbs_up_count"] > 0:
        issues_identified.append(
            f"{before_profile['invalid_thumbs_up_count']} registros com 'thumbsUpCount' inválido (negativo ou não numérico)."
        )
    for meta_col in OPTIONAL_METADATA_COLUMNS:
        miss_cnt = before_profile["missing_by_column"].get(meta_col, 0)
        if miss_cnt > 0:
            miss_pct = before_profile["missing_pct_by_column"].get(meta_col, 0.0)
            issues_identified.append(
                f"Metadado opcional '{meta_col}' naturalmente ausente em {miss_cnt} registros ({miss_pct:.2f}%)."
            )
    if not issues_identified:
        issues_identified.append("Nenhuma anomalia crítica encontrada na base analisada.")

    actions_taken: list[str] = [
        "Preservação integral de data/raw/apps_reviews.csv verificada via hash SHA-256.",
    ]
    if extra_columns:
        if preserve_extra_columns:
            actions_taken.append(
                f"Colunas adicionais não previstas preservadas explicitamente no dataset tratado: {extra_columns}."
            )
        else:
            actions_taken.append(
                f"Colunas adicionais não previstas removidas explicitamente conforme configuração: {extra_columns}."
            )
    actions_taken.extend([
        f"Deduplicação por chave composta (app_id, reviewId): {dup_stats['duplicates_detected']} duplicatas separadas "
        f"({dup_stats['exact_duplicates_removed']} exatas e {dup_stats['conflicting_duplicates_resolved']} conflitantes comparadas diretamente ao vencedor).",
        f"Normalização Unicode NFC e limpeza de espaços em 'content' ({len(valid_text_mod)} registros) preservando acentos, emojis compostos (U+200D), pontuação e caixa.",
        f"Separação de {len(rej_empty)} avaliações sem conteúdo textual válido para quarentena rastreável.",
        f"Validação estrita de 'score' inteiro entre 1 e 5 ({len(rej_scores)} inválidos separados; {len(valid_score_cast)} conversões de tipo aplicadas).",
        f"Padronização de datas 'at' ({date_stats['invalid_at_rejected']} inválidas separadas; {len(valid_at_normalized)} normalizadas) e saneamento não destrutivo de 'repliedAt' ({len(valid_replied_sanitized)} corrigidas).",
        f"Validação de 'thumbsUpCount' como inteiro >= 0 nullable ('Int64'): {thumbs_stats['missing_preserved']} ausentes preservados como <NA> e {len(rej_thumbs)} inválidos enviados para quarentena.",
        f"Normalização não destrutiva de metadados opcionais ({meta_ops_valid} células ajustadas em {len(valid_meta_mod)} registros únicos) sem preenchimento artificial.",
    ])

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "raw_file_sha256": sha_before,
        "paths": {
            "raw_path": str(raw_file),
            "processed_path": str(processed_file),
            "rejected_path": str(rejected_file),
            "report_json_path": str(report_json_file),
            "report_md_path": str(report_md_file),
        },
        "schema_check": {
            "expected_columns": EXPECTED_COLUMNS,
            "extra_columns_detected": extra_columns,
            "extra_columns_preserved": bool(preserve_extra_columns),
        },
        "summary": {
            "initial_records": before_profile["total_records"],
            "valid_records": after_profile["total_records"],
            "final_records": after_profile["total_records"],
            "duplicates_removed": dup_stats["duplicates_detected"],
            "exact_duplicates_removed": dup_stats["exact_duplicates_removed"],
            "duplicate_conflicts_resolved": dup_stats["conflicting_duplicates_resolved"],
            "rejected_records": int(len(df_rejected)),
            "unique_records_modified": int(unique_modified_records),
            "total_correction_operations": int(total_correction_operations),
            "corrections_breakdown": {
                "texts_normalized_operations": len(valid_text_mod),
                "texts_normalized_records": len(valid_text_mod),
                "scores_type_cast_operations": len(valid_score_cast),
                "scores_type_cast_records": len(valid_score_cast),
                "at_normalized_operations": len(valid_at_normalized),
                "at_normalized_records": len(valid_at_normalized),
                "replied_at_sanitized_operations": len(valid_replied_sanitized),
                "replied_at_sanitized_records": len(valid_replied_sanitized),
                "thumbs_up_type_cast_operations": len(valid_thumbs_cast),
                "thumbs_up_type_cast_records": len(valid_thumbs_cast),
                "metadata_normalized_operations": meta_ops_valid,
                "metadata_normalized_records": len(valid_meta_mod),
            },
            "rejection_breakdown": rejection_breakdown,
        },
        "before_cleaning": before_profile,
        "after_cleaning": after_profile,
        "issues_identified": issues_identified,
        "actions_taken": actions_taken,
    }

    processed_file.parent.mkdir(parents=True, exist_ok=True)
    rejected_file.parent.mkdir(parents=True, exist_ok=True)
    report_json_file.parent.mkdir(parents=True, exist_ok=True)
    report_md_file.parent.mkdir(parents=True, exist_ok=True)

    df_clean.to_csv(processed_file, index=False)
    df_rejected.to_csv(rejected_file, index=False)

    with open(report_json_file, "w", encoding="utf-8") as f_json:
        json.dump(report, f_json, ensure_ascii=False, indent=2)

    markdown_text = generate_markdown_report(report)
    with open(report_md_file, "w", encoding="utf-8") as f_md:
        f_md.write(markdown_text)

    sha_after = compute_file_sha256(raw_file)
    if sha_before != sha_after:
        raise RuntimeError("Violação de integridade: o arquivo bruto original foi alterado!")

    return df_clean, df_rejected, report