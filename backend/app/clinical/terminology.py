from __future__ import annotations

import re


_DRUGS = {
    "METFORMIN": ("METFORMIN", "二甲双胍"),
    "METFORMIN HYDROCHLORIDE": ("METFORMIN", "二甲双胍"),
    "SILDENAFIL": ("SILDENAFIL", "西地那非"),
    "SILDENAFIL CITRATE": ("SILDENAFIL", "西地那非"),
    "ASPIRIN": ("ASPIRIN", "阿司匹林"),
}

_REACTIONS_ZH = {
    "ABORTION": "流产",
    "DEATH": "死亡",
    "DIARRHOEA": "腹泻",
    "DRY EYE": "眼干",
    "DYSPNOEA": "呼吸困难",
    "FALL": "跌倒",
    "FATIGUE": "疲劳",
    "HEADACHE": "头痛",
    "MATERNAL EXPOSURE DURING PREGNANCY": "妊娠期母体暴露",
    "OFF LABEL USE": "超说明书使用",
}


def _key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().rstrip(".").upper())


def normalize_drug_term(value: object) -> tuple[str | None, str | None]:
    """Return a governed canonical code and Chinese display label for a known drug term."""

    item = _DRUGS.get(_key(value))
    return item if item else (None, None)


def reaction_label_zh(value: object) -> str | None:
    """Translate a known MedDRA preferred term while leaving unknown source values untouched."""

    return _REACTIONS_ZH.get(_key(value))

