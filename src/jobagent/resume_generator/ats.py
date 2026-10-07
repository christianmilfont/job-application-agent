"""Análise ATS determinística: quais palavras-chave da vaga aparecem no currículo gerado.

O percentual é reproduzível (mesma entrada -> mesmo número), por isso não vem do LLM.
Ausências são separadas em:
- "adicionáveis": você possui (está no perfil) mas o currículo não menciona -> adicione;
- "não adicionáveis": você não possui -> NÃO adicione (seria inventar).
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from ..job_parser import ParsedJob
from ..profile import ProfileIndex
from .selector import job_skill_weights

W_CERT, W_ROLE, W_EDU = 2, 2, 1

_STOP = set("""a o e de da do das dos em para com por que um uma os as no na nos nas ao à às se sua seu suas seus
ou mais como ser ter você voce nossa nosso nossos nossas sobre entre também tambem the and of to in for with on
at by an is are be as or from this that will you your our we experiência experiencia conhecimento conhecimentos
vaga time área area trabalho atuar boas práticas praticas desenvolvimento pessoa pessoas empresa equipe
básico basico básica basica básicos básicas noção nocao noções nocoes forte sólido sólidos desejável""".split())
_SENIORITY_WORDS = re.compile(r"\b(j[uú]nior|jr|pleno|pl|s[eê]nior|sr|i{1,3}|iv|trainee|est[aá]gio|especialista|staff|lead)\b", re.I)


def _dedupe_ci(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i.lower() not in seen:
            seen.add(i.lower())
            out.append(i)
    return out


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


@dataclass
class KeywordCheck:
    keyword: str
    kind: str          # tecnologia | certificação | cargo | formação
    weight: int
    present: bool
    owned: bool        # o perfil possui?


@dataclass
class ATSReport:
    score: int
    checks: list[KeywordCheck] = field(default_factory=list)
    add_these: list[str] = field(default_factory=list)
    do_not_add: list[str] = field(default_factory=list)
    frequent_terms_missing: list[str] = field(default_factory=list)
    seniority_note: str = ""
    tips: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze_ats(resume_text: str, job_title: str, parsed: ParsedJob, idx: ProfileIndex,
                profile_seniority: list[str]) -> ATSReport:
    tax = idx.tax
    found_certs = tax.find_certs(resume_text)
    resume_norm = _norm(resume_text)
    checks: list[KeywordCheck] = []

    # ATS compara o TERMO usado na vaga: vaga diz "Kafka", currículo diz "SQS" -> ausente.
    job_literals = tax.find_skill_literals(parsed.description + "\n" + job_title)
    for sid, w in sorted(job_skill_weights(parsed).items(), key=lambda kv: -kv[1]):
        terms = sorted(job_literals.get(sid) or {tax.name(sid)}, key=len)
        for term in _dedupe_ci(terms)[:2]:
            present = bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", resume_text, re.I))
            # "possui" = o perfil cobre a skill E o termo exato é verdadeiro para você
            owned = bool(idx.covers(sid)) and idx.literal_allowed(sid, term)
            checks.append(KeywordCheck(term, "tecnologia", w, present, owned))

    # Alternativas ("Java, Go ou Rust"): se uma aparece, as outras não contam como ausência
    present_terms = {c.keyword.lower() for c in checks if c.present}
    for group in parsed.required_any_of:
        group_terms = {t.lower() for sid in group for t in (job_literals.get(sid) or {tax.name(sid)})}
        if group_terms & present_terms:
            checks = [c for c in checks if c.present or c.keyword.lower() not in group_terms]

    owned_certs = idx.owned_cert_ids()
    for cid in parsed.required_certs + parsed.desired_certs:
        entry = tax.certs[cid]
        checks.append(KeywordCheck(entry.name, "certificação", W_CERT, cid in found_certs,
                                   bool(set(entry.satisfied_by) & owned_certs)))

    # Cargo: termos do título sem senioridade/empresa (ex.: "Engenheiro DevOps" -> engenheiro, devops)
    main = re.split(r"\s*[|–—]\s*|\s+-\s+", re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", job_title))[0]
    role_terms = [t for t in re.findall(r"[\wÀ-ú#+./]+", _SENIORITY_WORDS.sub(" ", main))
                  if len(t) > 2 and _norm(t) not in _STOP]
    for t in role_terms:
        checks.append(KeywordCheck(t, "cargo", W_ROLE, _norm(t) in resume_norm, True))

    if parsed.education_required:
        has_edu = any(e.level == "graduacao" for e in idx.profile.education)
        checks.append(KeywordCheck("Formação superior", "formação", W_EDU,
                                   bool(re.search(r"gradua|forma[cç][aã]o|education|tecn[oó]logo|an[aá]lise e desenvolvimento", resume_text, re.I)),
                                   has_edu))

    total = sum(c.weight for c in checks) or 1
    score = round(100 * sum(c.weight for c in checks if c.present) / total)

    # Termos do cargo têm dica própria: não são algo que você "possui"
    add = [c.keyword for c in checks if not c.present and c.owned and c.kind != "cargo"]
    no = [c.keyword for c in checks if not c.present and not c.owned and c.kind != "cargo"]

    # Termos frequentes na vaga (informativo, fora do %)
    words = [w for w in re.findall(r"[a-zà-ú]{4,}", (parsed.requirements_text or "").lower()) if _norm(w) not in _STOP]
    freq = [w for w, n in Counter(words).most_common(15) if n >= 2 and _norm(w) not in resume_norm]

    sen = parsed.seniority
    if sen in ("senior", "lead"):
        sen_note = "Vaga acima do nível do perfil; o currículo não deve (e não vai) declarar senioridade."
    elif sen in profile_seniority or sen == "unspecified":
        sen_note = "Senioridade compatível com o perfil." if sen != "unspecified" else "Senioridade não informada na vaga."
    else:
        sen_note = f"Senioridade da vaga: {sen}."

    tips = []
    if add:
        tips.append("Você possui, mas o currículo não menciona: " + ", ".join(add)
                    + ". Inclua no resumo, nas competências ou nos bullets do projeto correspondente.")
    if no:
        tips.append("Ausentes porque você não possui: " + ", ".join(no)
                    + ". Não adicione; considere estudar/criar um projeto real com elas.")
    missing_roles = [c.keyword for c in checks if c.kind == "cargo" and not c.present]
    if missing_roles:
        tips.append("Use os termos do cargo no título/resumo quando forem verdadeiros: " + ", ".join(missing_roles) + ".")
    if freq:
        tips.append("Termos frequentes nos requisitos que não aparecem no currículo: " + ", ".join(freq[:8]) + ".")
    if not tips:
        tips.append("Boa cobertura de palavras-chave. Revise a redação e a ordem das seções.")

    return ATSReport(score, checks, add, no, freq[:8], sen_note, tips)
